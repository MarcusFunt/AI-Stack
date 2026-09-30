from __future__ import annotations

import unittest

from voice.session import (
    SentenceChunker,
    VoiceSessionRegistry,
    visible_assistant_text,
)


class VoiceSessionRegistryTests(unittest.TestCase):
    def test_session_token_is_short_lived_and_single_use(self):
        registry = VoiceSessionRegistry(token_ttl_seconds=30, max_sessions=2)

        session, token = registry.create(
            principal="client-1",
            traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            now=100,
        )

        consumed = registry.consume(session.id, token, now=101)
        self.assertIs(consumed, session)
        self.assertIsNone(registry.consume(session.id, token, now=102))
        self.assertIsNone(registry.consume(session.id, "wrong", now=102))

    def test_response_generations_are_monotonic_and_invalidated_on_cancel(self):
        registry = VoiceSessionRegistry()
        session, _ = registry.create(principal="client-1", traceparent=None, now=100)

        first = session.begin_generation("resp_first")
        self.assertTrue(session.generation_is_current(first, "resp_first"))
        self.assertEqual(session.invalidate_generation(), first + 1)
        self.assertFalse(session.generation_is_current(first, "resp_first"))

        second = session.begin_generation("resp_second")
        self.assertGreater(second, first)
        self.assertTrue(session.generation_is_current(second, "resp_second"))
        self.assertTrue(session.finish_generation(second))
        self.assertFalse(session.generation_is_current(second, "resp_second"))

    def test_expired_session_is_rejected_and_capacity_is_bounded(self):
        registry = VoiceSessionRegistry(token_ttl_seconds=30, max_sessions=1)
        session, token = registry.create(principal="client-1", traceparent=None, now=100)

        with self.assertRaises(OverflowError):
            registry.create(principal="client-2", traceparent=None, now=100)

        self.assertIsNone(registry.consume(session.id, token, now=131))
        next_session, _ = registry.create(principal="client-2", traceparent=None, now=132)
        self.assertNotEqual(session.id, next_session.id)


class VoiceStreamingHelpersTests(unittest.TestCase):
    def test_sentence_chunker_holds_fragments_until_a_natural_boundary(self):
        chunker = SentenceChunker(max_chars=40)

        self.assertEqual(chunker.feed("Hello there. How are"), ["Hello there."])
        self.assertEqual(chunker.feed(" you?"), ["How are you?"])
        self.assertEqual(chunker.feed(" One last thought", final=True), ["One last thought"])

    def test_sentence_chunker_flushes_long_text_without_tiny_fragments(self):
        chunker = SentenceChunker(max_chars=20)
        chunks = chunker.feed("This is a longer response without punctuation.")

        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 20 for chunk in chunks))
        self.assertEqual("".join(chunks).replace(" ", ""), "Thisisalongerresponsewithoutpunctuation.")

    def test_interrupted_history_only_keeps_text_that_was_emitted_as_audio(self):
        self.assertEqual(
            visible_assistant_text("First sentence. Second sentence.", "First sentence.", interrupted=True),
            "First sentence.",
        )
        self.assertEqual(
            visible_assistant_text("First sentence.", "First sentence.", interrupted=False),
            "First sentence.",
        )


if __name__ == "__main__":
    unittest.main()
