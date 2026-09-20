import tempfile
import threading
import unittest
from pathlib import Path

from agent_lab.controller import AgentLabController
from agent_lab.db import RunStore
from agent_lab.schemas import RunStatus, TaskSpec


class ControllerLifecycleTests(unittest.TestCase):
    def test_active_cancel_sets_signal_and_cancelling_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            task = TaskSpec(objective="test")
            run_id = "a" * 32
            store.create_run(run_id, task)
            store.update_run(run_id, status=RunStatus.RUNNING)

            controller = AgentLabController.__new__(AgentLabController)
            controller.store = store
            controller._active = {run_id}
            signal = threading.Event()
            controller._cancel_events = {run_id: signal}
            controller._lock = threading.Lock()

            result = controller.cancel_run(run_id)
            self.assertEqual(result.status, RunStatus.CANCELLING)
            self.assertTrue(signal.is_set())
            self.assertEqual(
                store.list_events(run_id)[-1].kind,
                "run_cancel_requested",
            )

    def test_restart_recovery_marks_inflight_run_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs.sqlite3")
            task = TaskSpec(objective="test")
            run_id = "b" * 32
            store.create_run(run_id, task)
            store.update_run(run_id, status=RunStatus.RUNNING)

            controller = AgentLabController.__new__(AgentLabController)
            controller.store = store
            controller._recover_interrupted_runs()

            recovered = store.get_run(run_id)
            self.assertEqual(recovered.status, RunStatus.ERROR)
            self.assertIn("controller restarted", recovered.error)
            self.assertEqual(
                store.list_events(run_id)[-1].kind,
                "run_recovered_as_error",
            )


if __name__ == "__main__":
    unittest.main()
