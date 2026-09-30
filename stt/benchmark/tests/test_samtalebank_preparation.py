import io
import json
from contextlib import redirect_stderr
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from stt.benchmark.datasets import DatasetAdapter, manifest, prepare, samtalebank


def _write_wav(path: Path, duration_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * duration_s))


class SamtaleBankPreparationTests(unittest.TestCase):
    def test_sam3_adapter_implements_shared_dataset_contract(self):
        self.assertTrue(callable(getattr(samtalebank, "SamtaleBankSam3Adapter", None)))
        adapter = samtalebank.SamtaleBankSam3Adapter()
        self.assertIsInstance(adapter, DatasetAdapter)
        self.assertEqual(adapter.spec.dataset, "samtalebank-sam3")
        self.assertEqual(adapter.spec.dataset_class, "PRIMARY-INDEPENDENTISH")
    def test_cut_wav_window_keeps_requested_sample_interval(self):
        self.assertTrue(callable(getattr(samtalebank, "cut_wav_window", None)))
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.wav"
            output = Path(tmp) / "cut.wav"
            _write_wav(source, 4.0)
            samtalebank.cut_wav_window(source, output, 1.25, 2.5)
            with wave.open(str(output), "rb") as handle:
                self.assertEqual(handle.getframerate(), 16000)
                self.assertEqual(handle.getnchannels(), 1)
                self.assertEqual(handle.getsampwidth(), 2)
                self.assertEqual(handle.getnframes(), 20000)

    def test_video_extraction_requests_mono_16khz_pcm_without_normalization(self):
        self.assertTrue(callable(getattr(samtalebank, "extract_audio_to_wav", None)))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "audio.wav"

            def fake_run(command, **kwargs):
                _write_wav(output, 1.0)
                return mock.Mock(returncode=0, stderr="")

            with mock.patch("stt.benchmark.datasets.samtalebank.shutil.which", return_value="ffmpeg"):
                with mock.patch("stt.benchmark.datasets.samtalebank.subprocess.run", side_effect=fake_run) as run:
                    samtalebank.extract_audio_to_wav(Path(tmp) / "input.mp4", output)
            command = run.call_args.args[0]
            self.assertIn("-vn", command)
            self.assertEqual(command[command.index("-ac") + 1], "1")
            self.assertEqual(command[command.index("-ar") + 1], "16000")
            self.assertEqual(command[command.index("-c:a") + 1], "pcm_s16le")
            self.assertNotIn("loudnorm", command)

    def test_missing_source_reports_legitimate_talkbank_access_steps(self):
        self.assertTrue(callable(getattr(samtalebank, "prepare_samtalebank", None)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "(?i)register or log in to TalkBank"):
                samtalebank.prepare_samtalebank(
                    root / "missing",
                    root / "prepared",
                    lock_path=root / "dataset-lock.json",
                )

    def test_prepare_cli_reports_access_instructions_without_network_access(self):
        self.assertTrue(callable(getattr(prepare, "main", None)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = prepare.main([
                    "--suite", "samtalebank-sam3",
                    "--source-path", str(root / "missing"),
                    "--output-dir", str(root / "prepared"),
                    "--lock-path", str(root / "dataset-lock.json"),
                ])
            self.assertEqual(result, 2)
            self.assertIn("register or log in to talkbank", stderr.getvalue().lower())
    def test_prepare_cli_without_source_reports_access_steps(self):
        self.assertTrue(callable(getattr(prepare, "main", None)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stderr = io.StringIO()
            try:
                with redirect_stderr(stderr):
                    result = prepare.main([
                        "--suite", "samtalebank-sam3",
                        "--output-dir", str(root / "prepared"),
                        "--lock-path", str(root / "dataset-lock.json"),
                    ])
            except TypeError as exc:
                self.fail(f"missing source must produce access guidance, not TypeError: {exc}")
            self.assertEqual(result, 2)
            self.assertIn("register or log in to talkbank", stderr.getvalue().lower())
    def test_local_chat_and_audio_prepare_a_valid_public_manifest_and_lock(self):
        self.assertTrue(callable(getattr(samtalebank, "prepare_samtalebank", None)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "raw" / "samtalebank-sam3"
            source_root.mkdir(parents=True)
            _write_wav(source_root / "sample.wav", 90.0)
            time = chr(0x15)
            turns = []
            speakers = ("KIR", "LOU", "MIK")
            for start in range(0, 90, 3):
                speaker = speakers[(start // 3) % 3]
                turns.append(f"*{speaker}:\tord {time}{start * 1000}_{(start + 2) * 1000}{time}")
            transcript = (
                "@Begin\n"
                "@Participants: KIR Kirsten Adult, LOU Louise Adult, MIK Mikael Adult\n"
                "@Media: sample, audio\n"
                "@Options: multiple\n"
                + "\n".join(turns)
                + "\n@End\n"
            )
            (source_root / "sample.cha").write_text(transcript, encoding="utf-8")
            output_root = root / "prepared" / "samtalebank-sam3"
            lock_path = root / "dataset-lock.json"
            manifest_path = samtalebank.prepare_samtalebank(
                source_root,
                output_root,
                lock_path=lock_path,
            )
            rows = manifest.load_manifest(manifest_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["dataset_class"], "PRIMARY-INDEPENDENTISH")
            self.assertEqual(rows[0]["speaker_count"], 3)
            self.assertEqual(len(rows[0]["segments"]), 30)
            self.assertEqual(rows[0]["metadata"]["reference_transform"], "talkbank-ca-v1")
            self.assertEqual(rows[0]["metadata"]["bootstrap_group_type"], "source_recording")
            self.assertEqual(rows[0]["metadata"]["bootstrap_group"], rows[0]["source_recording"])
            self.assertEqual(rows[0]["metadata"]["reference_semantics"], "chronological_single_stream")
            report = json.loads((output_root / "dataset-validation.json").read_text(encoding="utf-8"))
            self.assertEqual(report["parsed_speaker_tiers"], 30)
            self.assertEqual(report["lexical_utterances_without_timing"], 0)
            self.assertGreater(report["total_speaker_time_s"], 0)
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            locked_files = lock["datasets"]["samtalebank-sam3"]["files"]
            self.assertEqual(
                lock["datasets"]["samtalebank-sam3"]["source_files_relative_to"],
                "SourcePath",
            )
            self.assertTrue(all(not Path(item["path"]).is_absolute() for item in locked_files))
            self.assertTrue(all(".." not in Path(item["path"]).parts for item in locked_files))
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertIn("samtalebank-sam3", lock["datasets"])


if __name__ == "__main__":
    unittest.main()
