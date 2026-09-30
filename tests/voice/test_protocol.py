from __future__ import annotations

import unittest

from pydantic import ValidationError

from voice.protocol import RealtimeClientCommand, RealtimeServerEvent


class RealtimeProtocolTests(unittest.TestCase):
    def test_v2_response_event_requires_race_identity(self) -> None:
        with self.assertRaises(ValidationError):
            RealtimeServerEvent(
                protocol_version="v2",
                type="response.audio.delta",
                session_id="session-1",
                data={"delta": "pcm"},
            )

        event = RealtimeServerEvent(
            protocol_version="v2",
            type="response.audio.delta",
            session_id="session-1",
            turn_id="turn-1",
            response_id="response-1",
            generation_id=4,
            data={"delta": "pcm"},
        )
        self.assertEqual(event.generation_id, 4)

    def test_v2_cancel_command_requires_response_and_generation_identity(self) -> None:
        with self.assertRaises(ValidationError):
            RealtimeClientCommand(
                protocol_version="v2",
                type="response.cancel",
                session_id="session-1",
            )

        command = RealtimeClientCommand(
            protocol_version="v2",
            type="response.cancel",
            session_id="session-1",
            response_id="response-1",
            generation_id=4,
        )
        self.assertEqual(command.type, "response.cancel")


if __name__ == "__main__":
    unittest.main()
