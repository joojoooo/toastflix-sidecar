import os
import tempfile
import unittest


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
        self.assertIn("ToastFlix Audio Control", page.text)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("players", allowed.json())

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


if __name__ == "__main__":
    unittest.main()
