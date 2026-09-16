import unittest
from unittest.mock import patch

from playback import PLAYBACK_ACTIVE_GRACE_SECONDS, PlaybackRegistry, safe_metadata


class PlaybackRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlaybackRegistry()
        self.hid = "a" * 16
        self.token = "session-secret"
        self.playback_id = self.registry.register(self.hid, self.token, {
            "media_key": "movie:test:0:0",
            "language": "ita",
            "segs": ["https://cdn.example.test/audio.ts?signature=secret"],
            "durs": [5.0],
            "headers": {"Authorization": "secret", "User-Agent": "ToastFlix"},
        })

    def test_request_query_is_used_before_a_control_is_bound(self):
        offset, rate, control = self.registry.resolve(
            self.hid, self.token, 1.25, 1.0001, "playlist"
        )

        self.assertEqual(offset, 1.25)
        self.assertEqual(rate, 1.0001)
        self.assertEqual(control["source"], "request")

    def test_manual_change_is_applied_on_the_next_player_request(self):
        self.registry.bind(self.hid, self.token, {"cache_key": "cache-key"}, {
            "status": "ok",
            "offset": 0.5,
            "rate": 1.0,
            "cached": True,
            "cache_source": "remote",
        })
        changed = self.registry.set_override(self.playback_id, -0.125, 1.0)
        self.assertTrue(changed["pending_player_request"])

        offset, rate, control = self.registry.resolve(
            self.hid, self.token, 0.5, 1.0, "segment"
        )

        self.assertEqual(offset, -0.125)
        self.assertEqual(rate, 1.0)
        self.assertEqual(control["source"], "manual")
        self.assertFalse(self.registry.get(self.playback_id)["pending_player_request"])

        restored = self.registry.restore_automatic(self.playback_id)
        self.assertEqual(restored["current_offset"], 0.5)
        self.assertEqual(restored["control_source"], "cached")

    def test_dashboard_can_attach_a_generated_cache_key(self):
        attached = self.registry.attach_cache_key(self.playback_id, "generated-key", {
            "cache_key": "generated-key",
            "media_key": "movie:test:0:0",
            "resolution": 1080,
            "video_fingerprint": "video-fp",
            "audio_fingerprint": "audio-fp",
        })

        self.assertEqual(attached["cache_key"], "generated-key")
        self.assertEqual(attached["resolution"], 1080)
        self.assertEqual(attached["sync_metadata"]["video_fingerprint"], "video-fp")
        self.assertEqual(
            self.registry.context_for_cache("generated-key")["provider"], ""
        )

    def test_debug_metadata_preserves_complete_values(self):
        captured = safe_metadata({
            "video_url": "https://cdn.example.test/video.m3u8?token=secret",
            "vpsAccess": "private",
            "video_headers": {"Authorization": "Bearer private"},
        })

        self.assertEqual(captured["video_url"], "https://cdn.example.test/video.m3u8?token=secret")
        self.assertEqual(captured["vpsAccess"], "private")
        self.assertEqual(captured["video_headers"]["Authorization"], "Bearer private")

    def test_specific_cached_or_calculated_candidate_can_be_restored(self):
        self.registry.bind(self.hid, self.token, {"cache_key": "cache-key"}, {
            "status": "ok", "offset": 0.5, "rate": 1.0,
            "cached": True, "cache_source": "remote",
        })
        self.registry.bind(self.hid, self.token, {"cache_key": "cache-key"}, {
            "status": "ok", "offset": 0.25, "rate": 1.0, "cached": False,
        })
        self.registry.set_override(self.playback_id, -0.125, 1.0)

        cached = self.registry.restore_automatic(self.playback_id, "cached")
        self.assertEqual(cached["current_offset"], 0.5)
        self.assertEqual(cached["control_source"], "cached")

        calculated = self.registry.restore_automatic(self.playback_id, "calculated")
        self.assertEqual(calculated["current_offset"], 0.25)
        self.assertEqual(calculated["control_source"], "calculated")

    def test_upload_context_can_come_from_prepare_request(self):
        registry = PlaybackRegistry()
        playback_id = registry.register(
            "b" * 16,
            self.token,
            {"media_key": "series:tt13210838:2:8", "language": "eng"},
            request_metadata={
                "vpsHost": "https://offsets.example.test",
                "vpsAccess": "complete-access-value",
            },
        )
        registry.bind("b" * 16, self.token, {"cache_key": "prepared-context"}, {})

        self.assertEqual(registry.get(playback_id)["cache_key"], "prepared-context")
        self.assertEqual(registry.context_for_cache("prepared-context"), {
            "vpsHost": "https://offsets.example.test",
            "vpsAccess": "complete-access-value",
            "provider": "",
            "server": "",
            "title": "",
        })

    def test_upload_context_falls_back_to_a_previous_track(self):
        registry = PlaybackRegistry()
        registry.register(
            "c" * 16,
            self.token,
            {"media_key": "movie:first", "language": "eng"},
            request_metadata={
                "vpsHost": "https://previous.example.test",
                "vpsAccess": "previous-access",
            },
        )
        registry.register(
            "d" * 16,
            self.token,
            {"media_key": "movie:second", "language": "ita"},
            request_metadata={"vpsHost": "https://incomplete.example.test"},
        )
        registry.bind("d" * 16, self.token, {"cache_key": "new-cache"}, {})

        context = registry.context_for_cache("new-cache")

        self.assertEqual(context["vpsHost"], "https://previous.example.test")
        self.assertEqual(context["vpsAccess"], "previous-access")
        self.assertEqual(
            registry.context_for_playback(registry._playback_id("d" * 16, self.token))["vpsHost"],
            "https://previous.example.test",
        )

    def test_upload_context_never_combines_credentials_from_different_sources(self):
        registry = PlaybackRegistry()
        playback_id = registry.register(
            "e" * 16, self.token, {"media_key": "movie:test"},
            request_metadata={
                "vpsHost": "https://prepare.example.test", "vpsAccess": "prepare-access",
            },
        )
        registry.bind("e" * 16, self.token, {
            "vpsHost": "https://sync.example.test",
        }, {})
        context = registry.context_for_playback(playback_id)
        self.assertEqual(context["vpsHost"], "https://prepare.example.test")
        self.assertEqual(context["vpsAccess"], "prepare-access")

        partials = PlaybackRegistry()
        partials.register("f" * 16, self.token, {"media_key": "movie:first"},
                          request_metadata={"vpsHost": "https://first.example.test"})
        partial_id = partials.register("0" * 16, self.token, {"media_key": "movie:second"},
                                       request_metadata={"vpsAccess": "unpaired-access"})
        context = partials.context_for_playback(partial_id)
        self.assertEqual(context["vpsHost"], "https://first.example.test")
        self.assertFalse(context["vpsAccess"])

    def test_playback_remains_active_during_pause_grace_period(self):
        player = self.registry._players[self.playback_id]
        player["last_request_at"] = 1_000.0

        with patch("playback.time.time", return_value=1_000.0 + PLAYBACK_ACTIVE_GRACE_SECONDS - 1):
            self.assertTrue(self.registry.get(self.playback_id)["active"])

        with patch("playback.time.time", return_value=1_000.0 + PLAYBACK_ACTIVE_GRACE_SECONDS):
            self.assertFalse(self.registry.get(self.playback_id)["active"])


if __name__ == "__main__":
    unittest.main()
