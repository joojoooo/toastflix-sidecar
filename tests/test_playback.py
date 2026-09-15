import unittest

from playback import PlaybackRegistry, safe_metadata


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

    def test_debug_metadata_redacts_secrets_and_signed_queries(self):
        safe = safe_metadata({
            "video_url": "https://cdn.example.test/video.m3u8?token=secret",
            "vpsAccess": "private",
            "video_headers": {"Authorization": "Bearer private"},
        })

        self.assertEqual(safe["video_url"], "https://cdn.example.test/video.m3u8")
        self.assertEqual(safe["vpsAccess"], "[redacted]")
        self.assertEqual(safe["video_headers"]["Authorization"], "[redacted]")


if __name__ == "__main__":
    unittest.main()
