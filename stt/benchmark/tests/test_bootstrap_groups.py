import unittest

from stt.benchmark.analysis.comparison import _paired_units, pairwise_comparison_rows
from stt.benchmark.analysis.report import _pair_for_candidate


def _result(dataset, recordings):
    ids = [row["id"] for row in recordings]
    per_recording = {
        record_id: {"content_errors": 0, "content_reference_words": 10}
        for record_id in ids
    }
    return {
        "dataset": {"name": dataset, "class": "HELD-OUT-IN-DOMAIN"},
        "recordings": recordings,
        "models": {
            "edda": {"status": "success", "content_wer": 0.0,
                     "per_recording": per_recording},
            "hviske": {"status": "success", "content_wer": 0.1,
                       "per_recording": per_recording},
        },
    }


class BootstrapGroupTests(unittest.TestCase):
    def test_repeated_single_speaker_clips_share_speaker_group(self):
        run = _result("fleurs-da-dk-test", [
            {"id": "clip-a", "source_recording": "clip-a", "metadata": {
                "bootstrap_group": "speaker-1", "bootstrap_group_type": "speaker"}},
            {"id": "clip-b", "source_recording": "clip-b", "metadata": {
                "bootstrap_group": "speaker-1", "bootstrap_group_type": "speaker"}},
            {"id": "clip-c", "source_recording": "clip-c", "metadata": {
                "bootstrap_group": "speaker-2", "bootstrap_group_type": "speaker"}},
        ])
        units = _paired_units(run, "edda", "hviske")
        self.assertEqual(units[0]["bootstrap_group"], units[1]["bootstrap_group"])
        self.assertEqual(units[0]["bootstrap_group_type"], "speaker")
        pair = pairwise_comparison_rows(run, samples=5000, seed=17)[0]
        self.assertEqual(pair["group_unit"], "speaker")
        self.assertEqual(pair["group_count"], 2)
        self.assertIsNotNone(pair["group_ci95_low"])

    def test_missing_speaker_ids_fall_back_to_independent_recordings(self):
        run = _result("nst-da-test", [
            {"id": "clip-a", "source_recording": "clip-a", "metadata": {}},
            {"id": "clip-b", "source_recording": "clip-b", "metadata": {}},
        ])
        units = _paired_units(run, "edda", "hviske")
        self.assertEqual([unit["bootstrap_group"] for unit in units], ["clip-a", "clip-b"])
        self.assertEqual(
            [unit["bootstrap_group_type"] for unit in units],
            ["recording", "recording"],
        )

    def test_sam3_windows_keep_source_recording_group(self):
        run = _result("samtalebank-sam3", [
            {"id": "window-1", "source_recording": "source-a", "metadata": {
                "bootstrap_group": "source-a", "bootstrap_group_type": "source_recording"}},
            {"id": "window-2", "source_recording": "source-a", "metadata": {
                "bootstrap_group": "source-a", "bootstrap_group_type": "source_recording"}},
            {"id": "window-3", "source_recording": "source-b", "metadata": {
                "bootstrap_group": "source-b", "bootstrap_group_type": "source_recording"}},
        ])
        units = _paired_units(run, "edda", "hviske")
        self.assertEqual(units[0]["bootstrap_group"], units[1]["bootstrap_group"])
        pair = pairwise_comparison_rows(run, samples=5000, seed=19)[0]
        self.assertEqual(pair["group_unit"], "source_recording")
        self.assertIsNotNone(pair["group_ci95_low"])

    def test_one_speaker_cluster_does_not_fall_back_to_clip_level_decision_ci(self):
        run = _result("fleurs-da-dk-test", [
            {"id": "clip-a", "source_recording": "clip-a", "metadata": {
                "bootstrap_group": "speaker-1", "bootstrap_group_type": "speaker"}},
            {"id": "clip-b", "source_recording": "clip-b", "metadata": {
                "bootstrap_group": "speaker-1", "bootstrap_group_type": "speaker"}},
        ])
        run["models"]["edda"]["speaker_attributed_wer"] = 0.1
        run["models"]["hviske"]["speaker_attributed_wer"] = 0.2
        pair = pairwise_comparison_rows(run, samples=5000, seed=23)[0]
        self.assertEqual(pair["group_status"], "insufficient_groups")
        self.assertEqual(pair["group_count"], 1)
        self.assertIsNone(pair["group_ci95_low"])
        self.assertEqual(pair["ci95_low"], pair["ci95_high"])
        self.assertIsNone(_pair_for_candidate([pair], "edda", "hviske"))


if __name__ == "__main__":
    unittest.main()
