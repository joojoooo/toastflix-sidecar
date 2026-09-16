import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


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
