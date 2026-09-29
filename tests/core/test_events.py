import unittest
from uuid import UUID

from core.events import InvocationEventSequencer, InvocationEventType
from core.invocation import Invocation


class InvocationEventTests(unittest.TestCase):
    def test_event_sequence_is_monotonic_and_ids_are_unique(self):
        invocation = Invocation()
        sequence = InvocationEventSequencer(invocation)
        first = sequence.emit(InvocationEventType.STARTED, {"source": "test"})
        second = sequence.emit(InvocationEventType.COMPLETED, {"ok": True})
        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertIsInstance(UUID(first.event_id), UUID)
        self.assertEqual(first.invocation_id, invocation.id)
        self.assertEqual(first.trace_id, invocation.trace_context.trace_id)
        self.assertLessEqual(first.timestamp, second.timestamp)

    def test_event_sequencer_requires_an_invocation(self):
        with self.assertRaises(TypeError):
            InvocationEventSequencer(None)


if __name__ == "__main__":
    unittest.main()
