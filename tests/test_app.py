import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx


_cache = tempfile.TemporaryDirectory()
os.environ["SIDECAR_CACHE_DIR"] = _cache.name
os.environ["SIDECAR_ADMIN_TOKEN"] = "admin-test-token"
os.environ["SIDECAR_FIXED_TOKEN"] = "player-test-token"

from fastapi.testclient import TestClient  # noqa: E402

import app as sidecar  # noqa: E402


class DashboardApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(sidecar.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        _cache.cleanup()

    def setUp(self):
        sidecar.playbacks = sidecar.PlaybackRegistry()

    def test_dashboard_is_served_but_state_requires_admin_token(self):
        page = self.client.get("/dashboard")
        denied = self.client.get("/api/dashboard/state")
        allowed = self.client.get(
            "/api/dashboard/state",
            headers={"Authorization": "Bearer admin-test-token"},
        )

        self.assertEqual(page.status_code, 200)
        self.assertIn("ToastFlix Sidecar Console", page.text)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("players", allowed.json())

    def test_dashboard_metadata_edit_supplies_missing_alignment_video_url(self):
        hid = "e" * 16
        playback_id = sidecar.playbacks.register(hid, "player-test-token", {
            "media_key": "movie:missing-video", "language": "ita",
        })
        headers = {"Authorization": "Bearer admin-test-token"}
        path = f"/api/dashboard/players/{playback_id}"
        denied = self.client.patch(f"{path}/metadata", json={
            "field": "video_url", "value": "https://video.example.test/movie.m3u8",
        })
        self.assertEqual(denied.status_code, 401)

        edited = self.client.patch(f"{path}/metadata", headers=headers, json={
            "field": "video_url", "value": "https://video.example.test/movie.m3u8",
        })
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(
            edited.json()["player"]["sync_metadata"]["video_url"],
            "https://video.example.test/movie.m3u8",
        )
        sidecar.playbacks.bind(hid, "player-test-token", {"cache_key": "later-key"}, {})
        self.assertEqual(
            sidecar.playbacks.get(playback_id)["sync_metadata"]["video_url"],
            "https://video.example.test/movie.m3u8",
        )
        audio_headers = {"Authorization": "Bearer sample"}
        header_edit = self.client.patch(f"{path}/metadata", headers=headers, json={
            "field": "audio_headers", "value": audio_headers,
        })
        self.assertEqual(header_edit.status_code, 200)

        preview = Path(_cache.name) / "edited-video-preview.wav"
        preview.write_bytes(b"RIFF" + b"\0" * 44)
        with patch.object(sidecar.sync_engine, "manual_preview", new_callable=AsyncMock) as manual:
            manual.return_value = preview
            response = self.client.get(f"{path}/alignment/reference.wav", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(manual.await_args.args[0]["video_url"],
                         "https://video.example.test/movie.m3u8")
        self.assertEqual(manual.await_args.args[0]["audio_headers"], audio_headers)

        rejected = self.client.patch(f"{path}/metadata", headers=headers, json={
            "field": "video_headers", "value": "not JSON headers",
        })
        self.assertEqual(rejected.status_code, 400)

    def test_rejected_request_body_is_still_captured_in_full(self):
        response = self.client.patch(
            "/api/dashboard/players/missing/offset",
            json={"offset": 1.25, "marker": "complete-rejected-body"},
        )

        self.assertEqual(response.status_code, 401)
        summary = next(
            item for item in sidecar.activity._recent(50)
            if "/api/dashboard/players/missing/offset" in item["url"]
        )
        detail = sidecar.activity._get(summary["id"])
        self.assertIn("complete-rejected-body", detail["request_body"]["content"])

    def test_automatic_upload_toggle_requires_admin_and_updates_dashboard_state(self):
        denied = self.client.patch(
            "/api/dashboard/settings/automatic-upload", json={"enabled": False}
        )
        self.assertEqual(denied.status_code, 401)

        try:
            changed = self.client.patch(
                "/api/dashboard/settings/automatic-upload",
                headers={"Authorization": "Bearer admin-test-token"},
                json={"enabled": False},
            )
            state = self.client.get(
                "/api/dashboard/state",
                headers={"Authorization": "Bearer admin-test-token"},
            )

            self.assertEqual(changed.status_code, 200)
            self.assertFalse(changed.json()["enabled"])
            self.assertFalse(state.json()["server"]["automatic_remote_upload_enabled"])
        finally:
            self.client.patch(
                "/api/dashboard/settings/automatic-upload",
                headers={"Authorization": "Bearer admin-test-token"},
                json={"enabled": True},
            )

    def test_manual_offset_overrides_the_next_hls_playlist_request(self):
        hid = "a" * 16
        metadata = {
            "media_key": "movie:test:0:0",
            "language": "ita",
            "source_fingerprint": "audio-fp",
            "starts": [0.0, 5.0],
            "durs": [5.0, 5.0],
            "segs": ["https://cdn.example.test/0.ts", "https://cdn.example.test/1.ts"],
            "headers": {},
        }
        playback_id = sidecar.playbacks.register(
            hid, "player-test-token", metadata, cached_audio=False
        )
        sidecar.playbacks.set_override(playback_id, -0.125, 1.0)
        original_metadata = sidecar.audio.metadata
        sidecar.audio.metadata = lambda audio_hid: metadata
        try:
            response = self.client.get(
                f"/dual/aud/{hid}/audio.m3u8",
                params={"o": 500, "r": 1_000_000_000, "t": "player-test-token"},
            )
        finally:
            sidecar.audio.metadata = original_metadata

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-sidecar-offset"], "-0.125000")
        self.assertEqual(response.headers["x-sidecar-offset-source"], "manual")
        self.assertIn("o=-125", response.text)
        self.assertFalse(sidecar.playbacks.get(playback_id)["pending_player_request"])

    def test_manual_upload_uses_the_current_calculated_or_custom_selection(self):
        hid = "b" * 16
        cache_key = "selected-upload-cache-key"
        playback_id = sidecar.playbacks.register(
            hid,
            "player-test-token",
            {"media_key": "movie:test:0:0", "language": "ita"},
        )
        sidecar.playbacks.bind(
            hid,
            "player-test-token",
            {
                "cache_key": cache_key,
                "media_key": "movie:test:0:0",
                "resolution": 1080,
                "video_fingerprint": "video-fp",
                "audio_fingerprint": "audio-fp",
            },
            {
                "status": "ok",
                "offset": 0.375,
                "rate": 1.0002,
                "confidence": 0.91,
                "cached": False,
            },
        )
        uploaded = {
            "local_saved": True,
            "remote_configured": True,
            "remote_uploaded": True,
            "remote_status": 200,
        }

        with patch.object(sidecar.offsets, "upload", new_callable=AsyncMock) as upload:
            upload.return_value = uploaded
            calculated = self.client.post(
                f"/api/dashboard/offsets/{cache_key}/upload",
                params={"playback_id": playback_id},
                headers={"Authorization": "Bearer admin-test-token"},
            )
            calculated_selection = upload.await_args.args[2]

            sidecar.playbacks.set_override(playback_id, -0.125, 0.9998)
            custom = self.client.post(
                f"/api/dashboard/offsets/{cache_key}/upload",
                params={"playback_id": playback_id},
                headers={"Authorization": "Bearer admin-test-token"},
            )
            custom_selection = upload.await_args.args[2]

        self.assertEqual(calculated.status_code, 200)
        self.assertEqual(calculated_selection["selected_for_upload"], "calculated")
        self.assertEqual(calculated_selection["offset"], 0.375)
        self.assertEqual(calculated_selection["rate"], 1.0002)
        self.assertEqual(custom.status_code, 200)
        self.assertEqual(custom_selection["selected_for_upload"], "manual")
        self.assertEqual(custom_selection["offset"], -0.125)
        self.assertEqual(custom_selection["rate"], 0.9998)

    def test_manual_upload_generates_identity_for_an_unbound_playback(self):
        hid = "e" * 16
        playback_id = sidecar.playbacks.register(
            hid,
            "player-test-token",
            {
                "media_key": "movie:generated:0:0",
                "language": "ita",
                "source_fingerprint": "audio-fp",
            },
            request_metadata={
                "resolution": 1080,
                "videoFingerprint": "video-fp",
                "vpsHost": "https://offsets.example.test",
                "vpsAccess": "access-value",
            },
        )
        sidecar.playbacks.set_override(playback_id, -0.225, 1.0)
        uploaded = {
            "local_saved": True,
            "remote_configured": True,
            "remote_uploaded": True,
            "remote_status": 200,
        }

        with patch.object(sidecar.offsets, "upload_player_value", new_callable=AsyncMock) as upload:
            upload.return_value = uploaded
            response = self.client.post(
                f"/api/dashboard/players/{playback_id}/offset/upload",
                headers={"Authorization": "Bearer admin-test-token"},
            )

        expected_key = sidecar.offsets.key(
            "movie:generated:0:0", 1080, "video-fp", "audio-fp"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cache_key"], expected_key)
        self.assertEqual(sidecar.playbacks.get(playback_id)["cache_key"], expected_key)
        self.assertEqual(upload.await_args.args[0]["cache_key"], expected_key)
        self.assertEqual(upload.await_args.args[1]["selected_for_upload"], "manual")
        self.assertEqual(upload.await_args.args[1]["offset"], -0.225)

    def test_manual_upload_trusts_an_existing_record_key(self):
        cache_key = "legacy-record-key"
        playback_id = sidecar.playbacks.register(
            "f" * 16, "player-test-token", {"media_key": "movie:legacy"},
            request_metadata={"vpsHost": "https://vps.example", "vpsAccess": "access"},
        )
        sidecar.playbacks.bind("f" * 16, "player-test-token", {"cache_key": cache_key}, {
            "status": "ok", "offset": 0.25, "rate": 1.0,
        })
        record = {
            "cache_key": cache_key, "media_key": "movie:legacy", "resolution": 1080,
            "video_fingerprint": "legacy-video", "audio_fingerprint": "legacy-audio",
            "details": {"offset": 0.25},
        }
        with (patch.object(sidecar.offsets, "get", new_callable=AsyncMock) as get,
              patch.object(sidecar.offsets, "upload_player_value", new_callable=AsyncMock) as upload):
            get.return_value = record
            upload.return_value = {
                "local_saved": True, "remote_configured": True,
                "remote_uploaded": True, "remote_status": 200,
            }
            response = self.client.post(
                f"/api/dashboard/players/{playback_id}/offset/upload",
                headers={"Authorization": "Bearer admin-test-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upload.await_args.args[0]["cache_key"], cache_key)

    def test_manual_upload_without_playback_identity_explains_why_it_cannot_continue(self):
        playback_id = sidecar.playbacks.register(
            "f" * 16,
            "player-test-token",
            {"media_key": "movie:missing-identity", "language": "ita"},
        )
        sidecar.playbacks.set_override(playback_id, 0.25, 1.0)

        response = self.client.post(
            f"/api/dashboard/players/{playback_id}/offset/upload",
            headers={"Authorization": "Bearer admin-test-token"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "UPLOAD_IDENTITY_REQUIRED")
        self.assertIn("resolution", response.json()["detail"]["missing_fields"])
        self.assertIn("video_fingerprint", response.json()["detail"]["missing_fields"])
        self.assertIn("audio_fingerprint", response.json()["detail"]["missing_fields"])

    def test_unbound_playback_upload_requests_missing_vps_details(self):
        playback_id = sidecar.playbacks.register(
            "1" * 16,
            "player-test-token",
            {"media_key": "movie:needs-vps", "source_fingerprint": "audio-fp"},
            request_metadata={"resolution": 720, "videoFingerprint": "video-fp"},
        )
        sidecar.playbacks.set_override(playback_id, 0.125, 1.0)
        missing = {
            "local_saved": True,
            "remote_configured": False,
            "remote_uploaded": False,
            "missing_fields": ["vpsHost", "vpsAccess"],
            "remote_error": "Manual upload needs vpsHost and vpsAccess",
        }

        with patch.object(sidecar.offsets, "upload_player_value", new_callable=AsyncMock) as upload:
            upload.return_value = missing
            response = self.client.post(
                f"/api/dashboard/players/{playback_id}/offset/upload",
                headers={"Authorization": "Bearer admin-test-token"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "UPLOAD_CONTEXT_REQUIRED")
        self.assertEqual(
            response.json()["detail"]["missing_fields"], ["vpsHost", "vpsAccess"]
        )
        upload.assert_not_awaited()
        self.assertIsNone(sidecar.playbacks.get(playback_id)["cache_key"])

    def test_player_upload_uses_a_known_key_even_without_a_local_record(self):
        playback_id = sidecar.playbacks.register(
            "2" * 16, "player-test-token",
            {"media_key": "movie:remote-only", "source_fingerprint": "audio-fp"},
            request_metadata={"vpsHost": "https://vps.example", "vpsAccess": "access"},
        )
        sidecar.playbacks.bind(
            "2" * 16, "player-test-token", {"cache_key": "remote-only-key"},
            {"status": "lookup-miss", "cached": False},
        )
        sidecar.playbacks.set_override(playback_id, -0.125, 1.0)
        headers = {"Authorization": "Bearer admin-test-token"}
        info = self.client.get(
            f"/api/dashboard/players/{playback_id}/offset/upload-info", headers=headers
        )
        self.assertEqual(info.status_code, 200)
        self.assertEqual(info.json()["cache_key"], "remote-only-key")
        self.assertFalse(info.json()["local_record_found"])
        with patch.object(sidecar.offsets, "upload_player_value", new_callable=AsyncMock) as upload:
            upload.return_value = {
                "local_saved": False, "remote_configured": True,
                "remote_uploaded": True, "remote_status": 200,
            }
            response = self.client.post(
                f"/api/dashboard/players/{playback_id}/offset/upload", headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["storage"]["local_saved"])
        self.assertEqual(upload.await_args.args[0]["cache_key"], "remote-only-key")
        self.assertEqual(upload.await_args.args[0]["vpsHost"], "https://vps.example")
        self.assertEqual(upload.await_args.args[1]["offset"], -0.125)

    def test_hls_carried_offset_uploads_with_key_only_and_no_video_metadata(self):
        hid = "5" * 16
        key = "hls-carried-remote-key"
        playback_id = sidecar.playbacks.register(
            hid, "player-test-token",
            {"media_key": "movie:hls-only", "source_fingerprint": "audio-fp"},
            request_metadata={"vpsHost": "https://vps.example", "vpsAccess": "access"},
        )
        sidecar.playbacks.bind(
            hid, "player-test-token", {"cache_key": key},
            {"status": "lookup-miss", "cached": False},
        )
        sidecar.playbacks.resolve(hid, "player-test-token", 0.375, 1.0, "playlist")
        response = httpx.Response(
            200, json={"ok": True},
            request=httpx.Request("POST", "https://vps.example/dual/offset/report"),
        )
        with patch("offsets.logged_http_request", new_callable=AsyncMock) as send:
            send.return_value = response
            uploaded = self.client.post(
                f"/api/dashboard/players/{playback_id}/offset/upload",
                headers={"Authorization": "Bearer admin-test-token"},
            )

        self.assertEqual(uploaded.status_code, 200)
        self.assertFalse(uploaded.json()["storage"]["local_saved"])
        sent = send.await_args.kwargs["json"]
        self.assertEqual(sent["cache_key"], key)
        self.assertEqual(sent["offset"]["offset"], 0.375)
        self.assertNotIn("video_fingerprint", sent)
        self.assertIsNone(sidecar.offsets._local_get(key))

    def test_player_upload_can_use_operator_supplied_identity(self):
        playback_id = sidecar.playbacks.register(
            "3" * 16, "player-test-token",
            {"media_key": "movie:manual-identity", "source_fingerprint": "audio-fp"},
        )
        sidecar.playbacks.set_override(playback_id, 0.375, 1.0)
        headers = {"Authorization": "Bearer admin-test-token"}
        with patch.object(sidecar.offsets, "upload_player_value", new_callable=AsyncMock) as upload:
            upload.return_value = {
                "local_saved": True, "remote_configured": True,
                "remote_uploaded": True, "remote_status": 200,
            }
            response = self.client.post(
                f"/api/dashboard/players/{playback_id}/offset/upload",
                headers=headers,
                json={
                    "resolution": "1080", "video_fingerprint": "exact-toastflix-fp",
                    "vpsHost": "https://vps.example", "vpsAccess": "entered-access",
                },
            )
        expected = sidecar.offsets.key(
            "movie:manual-identity", 1080, "exact-toastflix-fp", "audio-fp"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cache_key"], expected)
        self.assertEqual(upload.await_args.args[0]["cache_key"], expected)
        self.assertEqual(upload.await_args.args[0]["vpsAccess"], "entered-access")

    def test_upload_info_suggests_identity_from_a_similar_playback(self):
        sidecar.playbacks.register(
            "6" * 16, "player-test-token",
            {"media_key": "movie:same-title", "source_fingerprint": "audio-fp"},
            request_metadata={"resolution": 1080, "videoFingerprint": "earlier-video-fp"},
        )
        current = sidecar.playbacks.register(
            "7" * 16, "player-test-token",
            {"media_key": "movie:same-title", "source_fingerprint": "audio-fp"},
        )

        response = self.client.get(
            f"/api/dashboard/players/{current}/offset/upload-info",
            headers={"Authorization": "Bearer admin-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["identity"]["resolution"])
        self.assertEqual(response.json()["identity"]["audio_fingerprint"], "audio-fp")
        self.assertEqual(response.json()["suggestions"][0]["resolution"], 1080)
        self.assertEqual(
            response.json()["suggestions"][0]["video_fingerprint"], "earlier-video-fp"
        )

    def test_upload_info_recovers_identity_from_previous_playback_with_same_key(self):
        cache_key = "remote-cache-key-without-local-record"
        sidecar.playbacks.register(
            "8" * 16, "player-test-token",
            {"media_key": "movie:same-edition", "source_fingerprint": "audio-fp"},
        )
        sidecar.playbacks.bind("8" * 16, "player-test-token", {
            "cache_key": cache_key, "media_key": "movie:same-edition",
            "resolution": 1080, "video_fingerprint": "exact-video-fp",
            "audio_fingerprint": "audio-fp",
        }, {})
        current = sidecar.playbacks.register(
            "9" * 16, "player-test-token",
            {"media_key": "movie:same-edition", "source_fingerprint": "audio-fp"},
        )
        sidecar.playbacks.bind("9" * 16, "player-test-token", {
            "cache_key": cache_key,
        }, {})

        response = self.client.get(
            f"/api/dashboard/players/{current}/offset/upload-info",
            headers={"Authorization": "Bearer admin-test-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["suggestions"][0]["cache_key"], cache_key)
        self.assertEqual(response.json()["suggestions"][0]["video_fingerprint"], "exact-video-fp")

        sidecar.playbacks.set_override(current, 0.375, 1.0)
        with patch.object(sidecar.offsets, "upload_player_value", new_callable=AsyncMock) as upload:
            upload.return_value = {
                "local_saved": True, "remote_configured": True,
                "remote_uploaded": True, "remote_status": 200,
            }
            uploaded = self.client.post(
                f"/api/dashboard/players/{current}/offset/upload",
                headers={"Authorization": "Bearer admin-test-token"},
                json={
                    "cache_key": cache_key, "resolution": 1080,
                    "video_fingerprint": "exact-video-fp",
                    "vpsHost": "https://vps.example", "vpsAccess": "access",
                },
            )
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(upload.await_args.args[0]["cache_key"], cache_key)

    def test_changing_vps_host_requires_its_matching_access(self):
        playback_id = sidecar.playbacks.register(
            "4" * 16, "player-test-token",
            {"media_key": "movie:paired", "source_fingerprint": "audio-fp"},
            request_metadata={
                "resolution": 1080, "videoFingerprint": "video-fp",
                "vpsHost": "https://old.example", "vpsAccess": "old-access",
            },
        )
        sidecar.playbacks.set_override(playback_id, 0.125, 1.0)
        response = self.client.post(
            f"/api/dashboard/players/{playback_id}/offset/upload",
            headers={"Authorization": "Bearer admin-test-token"},
            json={"vpsHost": "https://new.example"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["missing_fields"], ["vpsAccess"])
        self.assertIsNone(sidecar.playbacks.get(playback_id)["cache_key"])

    def test_manual_upload_accepts_vps_details_entered_in_the_dashboard(self):
        sidecar.playbacks.register(
            "c" * 16,
            "player-test-token",
            {"media_key": "movie:previous", "language": "eng"},
            request_metadata={"vpsHost": "https://previous.example.test"},
        )
        playback_id = sidecar.playbacks.register(
            "d" * 16,
            "player-test-token",
            {"media_key": "movie:current", "language": "ita"},
        )
        lookup_payload = {
            "token": "player-test-token",
            "audio_hid": "d" * 16,
            "cache_key": "prompted-cache-key",
            "media_key": "movie:current",
            "resolution": 1080,
            "video_fingerprint": "video-fp",
            "audio_fingerprint": "audio-fp",
        }
        with patch.object(sidecar.offsets, "lookup", new_callable=AsyncMock) as lookup:
            lookup.return_value = None
            lookup_response = self.client.post("/offset/lookup", json=lookup_payload)
        self.assertEqual(lookup_response.status_code, 200)
        self.assertFalse(lookup_response.json()["found"])
        self.assertEqual(sidecar.playbacks.get(playback_id)["cache_key"], "prompted-cache-key")
        sidecar.playbacks.set_override(playback_id, -0.225, 1.0)
        uploaded = {
            "local_saved": True,
            "remote_configured": True,
            "remote_uploaded": True,
            "remote_status": 200,
        }
        with patch.object(sidecar.offsets, "upload", new_callable=AsyncMock) as upload:
            upload.return_value = uploaded
            response = self.client.post(
                "/api/dashboard/offsets/prompted-cache-key/upload",
                params={"playback_id": playback_id},
                headers={"Authorization": "Bearer admin-test-token"},
                json={"vpsAccess": "prompted-access"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upload.await_args.args[1]["vpsHost"], "https://previous.example.test")
        self.assertEqual(upload.await_args.args[1]["vpsAccess"], "prompted-access")
        self.assertEqual(upload.await_args.args[2]["selected_for_upload"], "manual")
        self.assertEqual(upload.await_args.args[2]["offset"], -0.225)

    def test_manual_upload_identifies_details_needed_by_the_popup(self):
        missing = {
            "local_saved": True,
            "remote_configured": False,
            "remote_uploaded": False,
            "missing_fields": ["vpsAccess"],
            "remote_error": "Manual upload needs vpsAccess when OFFSET_API_URL is not configured",
        }
        with patch.object(sidecar.offsets, "upload", new_callable=AsyncMock) as upload:
            upload.return_value = missing
            response = self.client.post(
                "/api/dashboard/offsets/missing-access-cache-key/upload",
                headers={"Authorization": "Bearer admin-test-token"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "UPLOAD_CONTEXT_REQUIRED")
        self.assertEqual(response.json()["detail"]["missing_fields"], ["vpsAccess"])


if __name__ == "__main__":
    unittest.main()
