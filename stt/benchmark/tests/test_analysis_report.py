import csv
import json
import tempfile
import unittest
from pathlib import Path

from stt.benchmark.analysis.report import _recommend_for_suite, generate_report


def _sam3_result():
    ids = ["win-1", "win-2", "win-3"]
    metadata = [
        {"overlap_ratio": 0.01, "speech_ratio": 0.4, "median_turn_duration_s": 0.2, "dominant_speaker_fraction": 0.72},
        {"overlap_ratio": 0.07, "speech_ratio": 0.6, "median_turn_duration_s": 0.5, "dominant_speaker_fraction": 0.6},
        {"overlap_ratio": 0.15, "speech_ratio": 0.9, "median_turn_duration_s": 0.8, "dominant_speaker_fraction": 0.5},
    ]
    recordings = [
        {"id": item, "duration_s": 10, "speaker_count": 3, "source_recording": "rec-a" if index < 2 else "rec-b",
         "reference_speakers": ["A", "B", "C"], "reference_text": "reference words", "metadata": metadata[index]}
        for index, item in enumerate(ids)
    ]
    model_rows = {}
    for alias, error in (("edda", 0), ("saga2", 3)):
        model_rows[alias] = {
            "content_wer": error / 10,
            "content_reference_words": 30,
            "verbatim_wer": error / 10,
            "cer": error / 20,
            "speaker_attributed_wer": error / 10,
            "rtf": 0.1,
            "revision": "1" * 40,
            "per_recording": {
                item: {"content_errors": error, "content_reference_words": 10,
                       "content_wer": error / 10, "hypothesis": "some hypothesis"}
                for item in ids
            },
        }
    return {
        "schema_version": 2,
        "dataset": {"name": "samtalebank-sam3", "class": "PRIMARY-INDEPENDENTISH",
                    "source_url": "https://talkbank.org/samtale/access/Sam3.html",
                    "source_license": "CC-BY-NC-SA-3.0", "source_revisions": []},
        "manifest": "manifest.jsonl",
        "total_audio_s": 30,
        "recordings": recordings,
        "models": model_rows,
        "diarization": {
            "global_der_collar_025": 0.2,
            "macro_der_collar_025": 0.2,
            "global_der_collar_0": 0.3,
            "speaker_count_accuracy": 0.8,
            "overlap_region_der": 0.4,
            "non_overlap_der": 0.1,
            "collar_025_per_recording": {
                item: {"miss": 0.1, "false_alarm": 0.05, "confusion": 0.05, "reference_speaker_time": 1.0}
                for item in ids
            },
        },
    }


class AnalysisReportTests(unittest.TestCase):
    def test_recommendation_reports_when_interval_favors_runner_up(self):
        result = {
            "models": {
                "edda": {"content_wer": 0.1},
                "saga2": {"content_wer": 0.2},
            }
        }
        pair_rows = [{
            "model_a": "edda",
            "model_b": "saga2",
            "ci95_low": 0.01,
            "ci95_high": 0.03,
        }]

        recommendation = _recommend_for_suite(result, pair_rows, label="Suite")

        self.assertIn("95% CI for the edda minus saga2 WER difference", recommendation)
        self.assertIn("entirely above zero", recommendation)
        self.assertIn("favoring saga2", recommendation)
        self.assertNotIn("includes zero", recommendation)

    def test_report_keeps_classes_separate_and_writes_requested_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sam3_path = root / "sam3-results.json"
            sam3_path.write_text(json.dumps(_sam3_result()), encoding="utf-8")
            coral_path = root / "coral-results.json"
            coral = {
                "dataset": {"name": "coral-conversation-test", "class": "HELD-OUT-IN-DOMAIN"},
                "manifest": "coral.jsonl", "total_audio_s": 1,
                "recordings": [{"id": "clip", "duration_s": 1, "speaker_count": 1,
                                "reference_speakers": [], "source_recording": "clip", "metadata": {}}],
                "models": {"hviske": {"content_wer": 0.2, "content_reference_words": 5,
                                      "verbatim_wer": 0.2, "cer": 0.1, "rtf": 0.1,
                                      "revision": "2" * 40, "per_recording": {
                                          "clip": {"content_errors": 1, "content_reference_words": 5,
                                                   "content_wer": 0.2, "hypothesis": "some"}}}},
                "diarization": {},
            }
            coral_path.write_text(json.dumps(coral), encoding="utf-8")

            outputs = generate_report([sam3_path, coral_path], root / "report")
            self.assertTrue(outputs["report"].is_file())
            self.assertTrue(outputs["provenance"].is_file())
            self.assertTrue((root / "report" / "errors" / "samtalebank-sam3" / "saga2.jsonl").is_file())
            with outputs["dataset_summary"].open(encoding="utf-8", newline="") as handle:
                summary = list(csv.DictReader(handle))
            self.assertEqual({row["dataset_class"] for row in summary}, {
                "PRIMARY-INDEPENDENTISH", "HELD-OUT-IN-DOMAIN"
            })
            with outputs["pairwise_comparison"].open(encoding="utf-8", newline="") as handle:
                pairs = list(csv.DictReader(handle))
            self.assertEqual(len(pairs), 1)
            self.assertLess(float(pairs[0]["ci95_high"]), 0)
            self.assertEqual(int(pairs[0]["group_count"]), 2)
            report_text = outputs["report"].read_text(encoding="utf-8")
            self.assertIn("No overall average combines", report_text)
            self.assertIn("Best model for natural three-speaker Danish conversation: edda", report_text)
            self.assertIn("Source-group 95% CI", report_text)
            with outputs["strata_summary"].open(encoding="utf-8", newline="") as handle:
                strata = list(csv.DictReader(handle))
            self.assertIn(("overlap", ">10%"), {(row["dimension"], row["bucket"]) for row in strata})
            self.assertIn(("speaker_balance", ">70% dominant"), {(row["dimension"], row["bucket"]) for row in strata})


if __name__ == "__main__":
    unittest.main()
