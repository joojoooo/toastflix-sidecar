import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

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
        self.assertEqual(edited["details"]["automatic_result"]["offset"], 0.25)
        self.assertEqual(lookup["_cache_source"], "local-custom")

        restored = await self.store.restore_automatic("cache-key")
        self.assertEqual(restored["offset_seconds"], 0.25)
        self.assertFalse(restored["details"]["custom"])

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

    async def test_manual_upload_uses_dynamic_vps_host_without_configured_api(self):
        await self.store.report(self.payload, {
            "status": "ok", "offset": 0.5, "rate": 1.0, "confidence": 0.8,
        })
        await self.store.update_custom("cache-key", 0.75, 1.0, "operator edit")
        response = httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("POST", "https://vps.example/dual/offset/report"),
        )

        with patch("offsets.logged_http_request", new_callable=AsyncMock) as send:
            send.return_value = response
            status = await self.store.upload("cache-key", {
                "vpsHost": "https://vps.example",
                "vpsAccess": "complete-access-value",
            })

        self.assertTrue(status["remote_uploaded"])
        self.assertEqual(send.await_args.args[3], "https://vps.example/dual/offset/report")
        self.assertEqual(send.await_args.kwargs["json"]["access"], "complete-access-value")
        self.assertNotIn("remote_context", send.await_args.kwargs["json"]["offset"])
        stored = await self.store.get("cache-key")
        self.assertEqual(stored["details"]["remote_context"]["vpsAccess"], "complete-access-value")

    async def test_manual_upload_reports_missing_dynamic_vps_access(self):
        await self.store.report(self.payload, {
            "status": "ok", "offset": 0.5, "rate": 1.0, "confidence": 0.8,
        })

        with patch("offsets.logged_http_request", new_callable=AsyncMock) as send:
            status = await self.store.upload("cache-key", {
                "vpsHost": "https://vps.example",
            })

        send.assert_not_awaited()
        self.assertFalse(status["remote_uploaded"])
        self.assertEqual(status["missing_fields"], ["vpsAccess"])

    async def test_manual_upload_reports_all_missing_dynamic_vps_details(self):
        await self.store.report(self.payload, {
            "status": "ok", "offset": 0.5, "rate": 1.0, "confidence": 0.8,
        })

        status = await self.store.upload("cache-key")

        self.assertEqual(status["missing_fields"], ["vpsHost", "vpsAccess"])

    async def test_player_upload_accepts_key_only_without_creating_a_local_record(self):
        response = httpx.Response(
            200, json={"ok": True},
            request=httpx.Request("POST", "https://vps.example/dual/offset/report"),
        )
        with patch("offsets.logged_http_request", new_callable=AsyncMock) as send:
            send.return_value = response
            status = await self.store.upload_player_value(
                {"cache_key": "remote-key", "vpsHost": "https://vps.example", "vpsAccess": "access"},
                {"status": "ok", "offset": -0.125, "rate": 1.0, "selected_for_upload": "manual"},
            )

        self.assertTrue(status["remote_uploaded"])
        self.assertFalse(status["local_saved"])
        self.assertIsNone(await self.store.get("remote-key"))
        self.assertEqual(send.await_args.kwargs["json"]["cache_key"], "remote-key")
        self.assertEqual(send.await_args.kwargs["json"]["offset"]["offset"], -0.125)
        self.assertNotIn("remote_context", send.await_args.kwargs["json"]["offset"])

    async def test_player_upload_does_not_save_or_mutate_on_remote_failure(self):
        response = httpx.Response(
            422, json={"error": "identity needed"},
            request=httpx.Request("POST", "https://vps.example/dual/offset/report"),
        )
        with patch("offsets.logged_http_request", new_callable=AsyncMock) as send:
            send.return_value = response
            status = await self.store.upload_player_value(
                {**self.payload, "vpsHost": "https://vps.example", "vpsAccess": "access"},
                {"status": "ok", "offset": 0.25, "rate": 1.0, "custom": True},
            )

        self.assertFalse(status["remote_uploaded"])
        self.assertEqual(status["remote_status"], 422)
        self.assertIsNone(await self.store.get("cache-key"))

    async def test_player_upload_saves_complete_identity_only_after_success(self):
        response = httpx.Response(
            200, json={"ok": True},
            request=httpx.Request("POST", "https://vps.example/dual/offset/report"),
        )
        with patch("offsets.logged_http_request", new_callable=AsyncMock) as send:
            send.return_value = response
            status = await self.store.upload_player_value(
                {**self.payload, "vpsHost": "https://vps.example", "vpsAccess": "access"},
                {"status": "ok", "offset": 0.25, "rate": 1.0, "custom": True},
            )

        self.assertTrue(status["remote_uploaded"])
        self.assertTrue(status["local_saved"])
        record = await self.store.get("cache-key")
        self.assertEqual(record["offset_seconds"], 0.25)
        self.assertEqual(record["details"]["remote_context"]["vpsAccess"], "access")

    async def test_disabled_automatic_upload_is_persisted_and_manual_upload_still_works(self):
        await self.store.set_automatic_upload_enabled(False)
        reloaded = OffsetStore(str(self.store.path))
        self.assertFalse(reloaded.automatic_upload_enabled)
        payload = {
            **self.payload,
            "vpsHost": "https://vps.example",
            "vpsAccess": "saved-access",
        }

        with patch("offsets.logged_http_request", new_callable=AsyncMock) as send:
            status = await reloaded.report(
                payload,
                {"status": "ok", "offset": 0.5, "rate": 1.0, "confidence": 0.8},
                upload_remote=reloaded.automatic_upload_enabled,
            )

        send.assert_not_awaited()
        self.assertTrue(status["local_saved"])
        self.assertTrue(status["remote_skipped"])
        self.assertEqual((await reloaded.get("cache-key"))["offset_seconds"], 0.5)

        response = httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("POST", "https://vps.example/dual/offset/report"),
        )
        with patch("offsets.logged_http_request", new_callable=AsyncMock) as send:
            send.return_value = response
            manual = await reloaded.upload("cache-key", selected_result={
                "status": "ok", "offset": 1.125, "rate": 1.0,
                "custom": True, "selected_for_upload": "manual",
            })

        self.assertTrue(manual["remote_uploaded"])
        sent_offset = send.await_args.kwargs["json"]["offset"]
        self.assertEqual(sent_offset["offset"], 1.125)
        self.assertEqual(sent_offset["selected_for_upload"], "manual")


if __name__ == "__main__":
    unittest.main()
