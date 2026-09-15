import tempfile
import unittest
from pathlib import Path

from activity import ActivityLog, body_record


class ActivityLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_transaction_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            log = ActivityLog(str(Path(directory) / "activity.db"))
            summary = await log.record(
                direction="outbound",
                category="offset-db",
                method="POST",
                url="https://toastflix.example/dual/offset/report",
                status=200,
                duration_ms=12.5,
                request_headers={"authorization": "Bearer complete-secret"},
                request_body=body_record(
                    b'{"vpsAccess":"complete-value"}', {"content-type": "application/json"}
                ),
                response_headers={"content-type": "application/json"},
                response_body=body_record(b'{"ok":true}', {"content-type": "application/json"}),
                metadata={"test": True},
            )

            detail = await log.get(summary["id"])
            recent = await log.recent()

            self.assertEqual(detail["request_headers"]["authorization"], "Bearer complete-secret")
            self.assertIn("complete-value", detail["request_body"]["content"])
            self.assertEqual(recent[0]["url"], "https://toastflix.example/dual/offset/report")

    def test_binary_request_can_be_retained_in_base64(self):
        captured = body_record(b"\x00\xff", {"content-type": "application/octet-stream"}, True)
        self.assertEqual(captured["encoding"], "base64")
        self.assertEqual(captured["content"], "AP8=")


if __name__ == "__main__":
    unittest.main()
