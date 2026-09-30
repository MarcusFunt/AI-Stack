import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from stt.benchmark import runner
from stt.benchmark.analysis.comparison import pairwise_comparison_rows
from stt.benchmark.analysis.report import generate_report


class OutOfMemoryError(RuntimeError):
    pass


class SuccessfulAdapter:
    revision = "a" * 40

    def transcribe(self, paths, batch_size):
        return ["hej med dig" for _ in paths]

    def unload(self):
        pass


class TranscriptionFailureAdapter(SuccessfulAdapter):
    def transcribe(self, paths, batch_size):
        raise RuntimeError("decoder failed")


class FailedModelResultTests(unittest.TestCase):
    def test_transcription_exception_is_failed_and_preserves_partial_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.jsonl"
            manifest.write_text("{}\n", encoding="utf-8")
            output = root / "run"
            records = [{
                "id": "clip-1", "audio_path": str(root / "clip.wav"),
                "reference_text": "hej med dig", "segments": [],
                "has_speaker_transcripts": False, "speaker_count": 1, "metadata": {},
            }]
            no_diarization = {
                "revision": None, "predicted_by_id": {}, "der_by_id": {},
                "der_025_by_id": {}, "der_0_by_id": {}, "regions_by_id": {},
            }
            arguments = [
                "--manifest", str(manifest), "--output", str(output),
                "--models", "edda", "--no-speaker-attributed",
            ]
            with (
                patch.object(runner, "load_manifest", return_value=records),
                patch.object(runner, "audio_duration", return_value=1.0),
                patch.object(runner, "_run_reference_diarization", return_value=no_diarization),
                patch.object(runner, "load_adapter", return_value=TranscriptionFailureAdapter()),
                patch.object(sys, "argv", ["runner", *arguments]),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = runner.main()
            payload = json.loads((output / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["models"]["edda"]["status"], "failed")
        self.assertEqual(payload["models"]["edda"]["failure"], {
            "class": "RuntimeError", "message": "decoder failed",
        })
        self.assertIsNone(payload["models"]["edda"]["content_wer"])

    def test_oom_is_unscored_and_excluded_from_ranking_and_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.jsonl"
            manifest.write_text("{}\n", encoding="utf-8")
            output = root / "run"
            records = [{
                "id": "clip-1",
                "audio_path": str(root / "clip.wav"),
                "reference_text": "hej med dig",
                "segments": [],
                "has_speaker_transcripts": False,
                "speaker_count": 1,
                "metadata": {},
            }]
            no_diarization = {
                "revision": None,
                "predicted_by_id": {},
                "der_by_id": {},
                "der_025_by_id": {},
                "der_0_by_id": {},
                "regions_by_id": {},
            }

            def load(alias, **_kwargs):
                if alias == "edda":
                    raise OutOfMemoryError("CUDA out of memory")
                return SuccessfulAdapter()

            arguments = [
                "--manifest", str(manifest), "--output", str(output),
                "--models", "edda", "saga2", "--no-speaker-attributed",
            ]
            with (
                patch.object(runner, "load_manifest", return_value=records),
                patch.object(runner, "audio_duration", return_value=1.0),
                patch.object(runner, "_run_reference_diarization", return_value=no_diarization),
                patch.object(runner, "load_adapter", side_effect=load),
                patch.object(sys, "argv", ["runner", *arguments]),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = runner.main()

            self.assertEqual(exit_code, 1)
            payload = json.loads((output / "results.json").read_text(encoding="utf-8"))
            failed = payload["models"]["edda"]
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["failure"]["class"], "OutOfMemoryError")
            self.assertIn("CUDA out of memory", failed["failure"]["message"])
            for metric in (
                "content_wer", "verbatim_wer", "cer", "speaker_attributed_wer", "rtf"
            ):
                self.assertIsNone(failed[metric])
            self.assertEqual(payload["ranking_by_content_wer"], ["saga2"])
            self.assertNotIn("content_errors", failed["per_recording"]["clip-1"])
            self.assertEqual(failed["per_recording"]["clip-1"]["hypothesis"], "")
            self.assertEqual(len(pairwise_comparison_rows(payload)), 0)

            with (output / "summary.csv").open(encoding="utf-8", newline="") as handle:
                summary = list(csv.DictReader(handle))
            self.assertEqual([(row["model"], row["status"]) for row in summary], [
                ("saga2", "success"), ("edda", "failed")
            ])
            self.assertIn("Failed candidates", (output / "summary.md").read_text(encoding="utf-8"))

            report_dir = root / "report"
            report_outputs = generate_report([output / "results.json"], report_dir)
            report_text = report_outputs["report"].read_text(encoding="utf-8")
            self.assertIn("edda", report_text)
            self.assertIn("CUDA out of memory", report_text)
            self.assertIn("Failed candidates", report_text)


if __name__ == "__main__":
    unittest.main()
