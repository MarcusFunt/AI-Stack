import asyncio
import unittest

from core.cancellation import CancellationToken, InvocationCancelled
from core.invocation import Principal
from core.sessions import SessionManager, SessionType


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_parent_cancellation_propagates_to_child_and_waiter(self):
        parent = CancellationToken()
        child = CancellationToken(parent=parent)
        waiter = asyncio.create_task(child.wait())

        self.assertTrue(parent.cancel("voice-barge-in"))
        self.assertFalse(parent.cancel("late-cancel"))
        self.assertTrue(child.cancelled)
        self.assertEqual(await asyncio.wait_for(waiter, timeout=1), "voice-barge-in")
        with self.assertRaises(InvocationCancelled):
            child.raise_if_cancelled()


class SessionTests(unittest.TestCase):
    def test_manager_does_not_expose_nested_mutable_session_state(self):
        manager = SessionManager()
        session = manager.create(
            SessionType.CHAT,
            principal=Principal(id="user-1", kind="user", scopes={"chat"}),
            transport_metadata={"client": {"name": "browser"}},
        )
        snapshot = manager.get(session.id)
        snapshot.transport_metadata["client"]["name"] = "mutated"

        self.assertEqual(manager.get(session.id).transport_metadata["client"]["name"], "browser")
    def test_manager_tracks_session_identity_permissions_and_active_invocations(self):
        manager = SessionManager()
        session = manager.create(
            SessionType.REALTIME_VOICE,
            principal=Principal(id="user-1", kind="user", scopes={"voice"}),
            model_profile="voice-fast",
            tool_permissions={"calendar.read"},
            transport_metadata={"transport": "websocket"},
        )
        before = session.last_activity
        manager.start_invocation(session.id, "invocation-1")
        active = manager.get(session.id)
        manager.finish_invocation(session.id, "invocation-1")
        finished = manager.get(session.id)

        self.assertEqual(active.session_type, SessionType.REALTIME_VOICE)
        self.assertEqual(active.principal.id, "user-1")
        self.assertEqual(active.active_invocations, {"invocation-1"})
        self.assertEqual(active.model_profile, "voice-fast")
        self.assertEqual(active.tool_permissions, {"calendar.read"})
        self.assertEqual(finished.active_invocations, set())
        self.assertGreaterEqual(finished.last_activity, before)
        manager.delete(session.id)
        self.assertIsNone(manager.get(session.id))


if __name__ == "__main__":
    unittest.main()
