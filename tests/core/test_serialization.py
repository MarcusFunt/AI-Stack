import unittest

from core.events import InvocationEventSequencer, InvocationEventType
from core.invocation import (
    BinaryReference,
    Invocation,
    InvocationInput,
    InvocationOperation,
    InvocationSource,
    ModelPolicy,
    Modality,
    Principal,
)
from core.serialization import (
    SerializationError,
    dumps_event,
    dumps_invocation,
    loads_event,
    loads_invocation,
)


class SerializationTests(unittest.TestCase):
    def setUp(self):
        self.invocation = Invocation(
            operation=InvocationOperation.TRANSCRIBE,
            modality={Modality.AUDIO, Modality.TEXT},
            source=InvocationSource.OPENAI_AUDIO,
            principal=Principal(id="caller", kind="user", scopes={"audio"}),
            model_policy=ModelPolicy(capability="transcription"),
            input=InvocationInput(
                attachments=[BinaryReference(
                    uri="buffer://audio/123", mime_type="audio/wav", size_bytes=4096
                )],
                data={"vendor_extension": {"kept": [1, 2, 3]}},
            ),
            metadata={"unknown_field": {"value": "kept"}},
        )

    def test_invocation_json_round_trip_preserves_unknown_metadata_and_binary_reference(self):
        restored = loads_invocation(dumps_invocation(self.invocation))

        self.assertEqual(restored, self.invocation)
        self.assertEqual(restored.metadata["unknown_field"]["value"], "kept")
        self.assertEqual(restored.input.attachments[0].uri, "buffer://audio/123")
        self.assertEqual(restored.input.data["vendor_extension"]["kept"], [1, 2, 3])

    def test_event_json_round_trip_preserves_timestamp_and_data(self):
        event = InvocationEventSequencer(self.invocation).emit(
            InvocationEventType.INPUT_AUDIO_COMMITTED,
            {"reference": "buffer://audio/123", "unknown": 9},
        )

        self.assertEqual(loads_event(dumps_event(event)), event)

    def test_raw_binary_payloads_are_rejected_from_json(self):
        invocation = Invocation(input=InvocationInput(data={"audio": b"raw-audio"}))
        with self.assertRaises(SerializationError):
            dumps_invocation(invocation)

        event = InvocationEventSequencer(invocation).emit(
            InvocationEventType.INPUT_AUDIO_DELTA, {"bytes": b"raw-audio"}
        )
        with self.assertRaises(SerializationError):
            dumps_event(event)


if __name__ == "__main__":
    unittest.main()
