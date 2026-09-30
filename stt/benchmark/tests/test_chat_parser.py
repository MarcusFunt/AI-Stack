import tempfile
import unittest
from pathlib import Path

from stt.benchmark.datasets import chat


FIXTURE = Path(__file__).parent / "fixtures" / "talkbank" / "sam3-mini.cha"


def _fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8-sig").replace("\\x15", "\x15")


class ChatParserTests(unittest.TestCase):
    def test_parse_headers_speakers_continuations_and_multiple_bullets(self):
        self.assertTrue(callable(getattr(chat, "parse_chat_text", None)))
        transcript = chat.parse_chat_text(_fixture_text())
        self.assertEqual(transcript.media_name, "sam3-mini")
        self.assertEqual(transcript.media_type, "video")
        self.assertEqual(set(transcript.participants), {"KIR", "LOU", "MIK"})
        self.assertEqual(len(transcript.utterances), 6)
        first = transcript.utterances[0]
        self.assertEqual(first.speaker, "KIR")
        self.assertIn("og det var blåt", first.raw_text)
        self.assertEqual(len(first.timed_segments), 2)
        self.assertEqual(first.timed_segments[0].start_ms, 1000)
        self.assertEqual(first.timed_segments[1].end_ms, 3700)
        multiple = transcript.utterances[3]
        self.assertEqual(len(multiple.timed_segments), 2)
        self.assertEqual(multiple.timed_segments[0].scoring_text, "jeg tænker på første del")
        self.assertEqual(multiple.timed_segments[1].scoring_text, "og så den sidste del")
        self.assertEqual(transcript.diagnostics["multiple_bullet_utterances"], 2)
        self.assertEqual(transcript.diagnostics["skipped_non_speech_tiers"], 1)

    def test_raw_text_is_preserved_while_scoring_removes_markup_and_events(self):
        self.assertTrue(callable(getattr(chat, "normalize_chat_text", None)))
        transcript = chat.parse_chat_text(_fixture_text())
        utterance = transcript.utterances[0]
        self.assertIn("[<]", utterance.raw_text)
        self.assertIn("&=laughs", utterance.raw_text)
        self.assertIn("øh", utterance.scoring_text)
        self.assertIn("vej-", utterance.scoring_text)
        self.assertNotIn("[<]", utterance.scoring_text)
        self.assertNotIn("laughs", utterance.scoring_text)
        self.assertEqual(chat.normalize_chat_text("⌈jeg⌉ sagde ⌊ja⌋"), "jeg sagde ja")
        self.assertEqual(
            chat.normalize_chat_text("øh <jeg ved> det [<] nu [>] vej- &=laughs [=! latter] (.)"),
            "øh jeg ved det nu vej-",
        )

    def test_malformed_and_missing_timing_are_counted_without_guessing(self):
        self.assertTrue(callable(getattr(chat, "parse_chat_text", None)))
        body = (
            "@Participants: KIR Kirsten Adult\n"
            "@Media: sample, video\n"
            "*KIR:\thej \\x15not_a_time\\x15\n"
            "*KIR:\tfarvel.\n"
        ).replace("\\x15", "\x15")
        transcript = chat.parse_chat_text(body)
        self.assertEqual(transcript.diagnostics["malformed_bullet_count"], 1)
        self.assertEqual(transcript.diagnostics["lexical_utterances_without_timing"], 2)
        self.assertEqual(transcript.utterances[0].timed_segments, ())

    def test_parse_file_reads_utf8_danish_chat(self):
        self.assertTrue(callable(getattr(chat, "parse_chat_file", None)))
        transcript = chat.parse_chat_file(FIXTURE)
        self.assertIn("øh", transcript.utterances[0].scoring_text)


if __name__ == "__main__":
    unittest.main()
