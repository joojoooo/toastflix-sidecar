import unittest
import base64
import json
import sys
from pathlib import Path

# Add repo root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audio import AudioStore, parse_cuts_param


class TestCutsTimeline(unittest.TestCase):

    def setUp(self):
        # Sample metadata: 20 segments of 4.0s each (0.0 to 80.0s)
        self.starts = [float(i * 4.0) for i in range(20)]
        self.durs = [4.0] * 20
        self.metadata_ita = {
            "starts": self.starts,
            "durs": self.durs,
            "language": "ita",
        }
        # English track: same duration
        self.metadata_eng = {
            "starts": self.starts,
            "durs": self.durs,
            "language": "eng",
        }

    def test_parse_cuts_param(self):
        sample = [{"start_sec": 10.0, "end_sec": 20.0, "action": "audio_cut"}]
        # Raw JSON
        res1 = parse_cuts_param(json.dumps(sample))
        self.assertEqual(len(res1), 1)
        self.assertEqual(res1[0]["action"], "audio_cut")

        # Base64 URL-safe
        b64 = base64.urlsafe_b64encode(json.dumps(sample).encode()).decode()
        res2 = parse_cuts_param(b64)
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2[0]["start_sec"], 10.0)

        # Empty / None / invalid
        self.assertEqual(parse_cuts_param(None), [])
        self.assertEqual(parse_cuts_param(""), [])
        self.assertEqual(parse_cuts_param("invalid-not-json"), [])

    def test_expanded_rate_limits(self):
        # Standard PAL 25fps -> Cinema 23.976fps
        pal_to_ntsc = 0.959040
        timeline_pal = AudioStore.timeline(self.metadata_ita, offset=0.0, rate=pal_to_ntsc)
        self.assertTrue(len(timeline_pal) > 0)

        # Cinema 24fps -> PAL 25fps
        cinema_to_pal = 1.041667
        timeline_cinema = AudioStore.timeline(self.metadata_ita, offset=0.0, rate=cinema_to_pal)
        self.assertTrue(len(timeline_cinema) > 0)

        # Extreme values out of bounds (0.85 .. 1.15)
        with self.assertRaises(ValueError):
            AudioStore.timeline(self.metadata_ita, rate=0.80)
        with self.assertRaises(ValueError):
            AudioStore.timeline(self.metadata_ita, rate=1.20)

    def test_audio_cut(self):
        # Cut 12s of extra Italian audio between 12.0s and 24.0s (segments 3, 4, 5)
        cuts = [
            {"start_sec": 12.0, "end_sec": 24.0, "duration_sec": 12.0, "action": "audio_cut"}
        ]
        timeline = AudioStore.timeline(self.metadata_ita, offset=0.0, rate=1.0, cuts=cuts, hid="ita123")

        indices = [item["idx"] for item in timeline]
        # Segments 3, 4, 5 should be skipped
        self.assertNotIn(3, indices)
        self.assertNotIn(4, indices)
        self.assertNotIn(5, indices)
        self.assertIn(2, indices)
        self.assertIn(6, indices)

        # Segment 6 (orig start 24.0s) should now start at 12.0s
        seg6 = next(item for item in timeline if item["idx"] == 6)
        self.assertAlmostEqual(seg6["start"], 12.0, places=2)
        # Discontinuity flag must be True on resumed segment
        self.assertTrue(seg6["discontinuity"])

    def test_english_bridge(self):
        # Video has 10s extra scene from 40.0s to 50.0s, bridge with English
        cuts = [
            {"start_sec": 40.0, "end_sec": 50.0, "duration_sec": 10.0, "action": "english_bridge"}
        ]
        timeline = AudioStore.timeline(
            self.metadata_ita,
            offset=0.0,
            rate=1.0,
            cuts=cuts,
            bridge_metadata=self.metadata_eng,
            hid="ita123",
            bridge_hid="eng456",
        )

        hids = [item["hid"] for item in timeline]
        # Must contain both Italian and English tracks
        self.assertIn("ita123", hids)
        self.assertIn("eng456", hids)

        # Find English bridge segments
        bridge_segs = [item for item in timeline if item["hid"] == "eng456"]
        self.assertTrue(len(bridge_segs) > 0)
        # First bridge segment must have discontinuity
        self.assertTrue(bridge_segs[0]["discontinuity"])

        # Italian segment after the gap should be shifted by 10s
        ita_after = next(item for item in timeline if item["hid"] == "ita123" and item["start"] >= 50.0)
        self.assertTrue(ita_after["discontinuity"])

    def test_mute_video_gap(self):
        # Video has 15s extra scene from 30.0s to 45.0s, action mute
        cuts = [
            {"start_sec": 30.0, "end_sec": 45.0, "duration_sec": 15.0, "action": "mute"}
        ]
        timeline = AudioStore.timeline(self.metadata_ita, offset=0.0, rate=1.0, cuts=cuts, hid="ita123")

        # Segment starting at/after 30s should be shifted forward by 15s
        resumed = next(item for item in timeline if item["start"] >= 45.0)
        self.assertTrue(resumed["discontinuity"])

    def test_backward_compatibility(self):
        # Normal call without cuts
        timeline = AudioStore.timeline(self.metadata_ita, offset=1.5, rate=1.0)
        self.assertEqual(len(timeline), 20)
        self.assertAlmostEqual(timeline[0]["start"], 1.5, places=2)
        self.assertFalse(timeline[0]["discontinuity"])


if __name__ == "__main__":
    unittest.main()
