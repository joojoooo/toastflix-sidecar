import tempfile
import unittest
from pathlib import Path

from offsets import OffsetStore


class OffsetStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = OffsetStore(str(Path(self.temporary.name) / "offsets.db"))
        self.payload = {
            "cache_key": "cache-key",
            "media_key": "movie:test:0:0",
            "resolution": 2160,
            "video_fingerprint": "video-fp",
            "audio_fingerprint": "audio-fp",
        }

    async def test_custom_edit_is_persisted_and_identified_as_local_custom(self):
        await self.store.report(self.payload, {
            "status": "ok", "offset": 0.25, "rate": 1.0, "confidence": 0.9,
        })

        edited = await self.store.update_custom("cache-key", -0.125, 1.0001, "lip sync")
        lookup = await self.store.lookup(self.payload)

        self.assertEqual(edited["offset_seconds"], -0.125)
        self.assertTrue(edited["details"]["custom"])
        self.assertEqual(edited["details"]["custom_note"], "lip sync")
        self.assertEqual(lookup["_cache_source"], "local-custom")

    async def test_report_describes_local_only_storage(self):
        status = await self.store.report(self.payload, {
            "status": "ok", "offset": 0.5, "rate": 1.0, "confidence": 0.8,
        })

        self.assertEqual(status, {
            "local_saved": True,
            "remote_configured": False,
            "remote_uploaded": False,
        })

    def test_remote_payload_excludes_media_credentials(self):
        payload = OffsetStore._remote_payload({
            **self.payload,
            "video_url": "https://signed.example.test/video?token=private",
            "video_headers": {"Authorization": "Bearer private"},
            "vpsAccess": "allowed-access-value",
        })

        self.assertNotIn("video_url", payload)
        self.assertNotIn("video_headers", payload)
        self.assertEqual(payload["access"], "allowed-access-value")


if __name__ == "__main__":
    unittest.main()
