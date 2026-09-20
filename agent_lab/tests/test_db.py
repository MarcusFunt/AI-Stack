import tempfile
import unittest
from pathlib import Path

from agent_lab.db import RunStore
from agent_lab.schemas import RunStatus, TaskSpec


class RunStoreTests(unittest.TestCase):
    def test_round_trip_run_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            task = TaskSpec(objective="Fix the failing example")
            created = store.create_run("a" * 32, task)
            self.assertEqual(created.status, RunStatus.PREPARING)

            updated = store.update_run(
                created.id,
                status=RunStatus.READY,
                workspace="/tmp/work",
                base_commit="abc123",
            )
            self.assertEqual(updated.workspace, "/tmp/work")
            self.assertEqual(updated.base_commit, "abc123")

            store.add_event(created.id, "example", {"ok": True})
            events = store.list_events(created.id)
            self.assertEqual(events[-1].kind, "example")
            self.assertTrue(events[-1].payload["ok"])


if __name__ == "__main__":
    unittest.main()
