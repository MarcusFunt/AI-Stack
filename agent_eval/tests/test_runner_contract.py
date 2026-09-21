import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_eval.runner_runtime import QueueModelClient


class QueueModelClientContractTests(unittest.TestCase):
    def _capture_feedback(self, root, name, prior_result):
        request_dir = root / name / "requests"
        result_dir = root / name / "results"
        request_dir.mkdir(parents=True)
        result_dir.mkdir()
        client = QueueModelClient(request_dir, result_dir, "cap", timeout_s=5)
        captured = []

        def responder():
            deadline = time.time() + 5
            while time.time() < deadline:
                files = list(request_dir.glob("*.json"))
                if files:
                    request = json.loads(files[0].read_text(encoding="utf-8"))
                    captured.append(request)
                    result = result_dir / f"{request['request_id']}.json"
                    result.write_text(
                        json.dumps({
                            "content": json.dumps({"summary": "ok", "edits": []})
                        }),
                        encoding="utf-8",
                    )
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=responder, daemon=True)
        thread.start()
        client.propose_patch("fix timeout parsing", "stable repository context", prior_result)
        thread.join(timeout=5)
        if not captured:
            self.fail("model request was not captured")
        return captured[0]["messages"][1]["content"]

    def test_propose_patch_accepts_agent_runner_timeout_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requests = root / "requests"
            results = root / "results"
            requests.mkdir()
            results.mkdir()
            client = QueueModelClient(requests, results, "cap", timeout_s=5)

            def responder():
                deadline = time.time() + 2
                while time.time() < deadline:
                    files = list(requests.glob("*.json"))
                    if files:
                        payload = json.loads(files[0].read_text(encoding="utf-8"))
                        result = results / f"{payload['request_id']}.json"
                        result.write_text(
                            json.dumps({
                                "content": json.dumps({
                                    "summary": "ok",
                                    "edits": [],
                                })
                            }),
                            encoding="utf-8",
                        )
                        return
                    time.sleep(0.01)

            thread = threading.Thread(target=responder, daemon=True)
            thread.start()
            proposal = client.propose_patch(
                "objective",
                "context",
                None,
                timeout_seconds=1.5,
            )
            thread.join(timeout=1)
            self.assertEqual(proposal["summary"], "ok")
            self.assertEqual(proposal["edits"], [])

    def test_retry_feedback_ignores_run_paths_and_timing_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def failure(evaluation_id, run_id, duration_s, test_duration_s):
                message = (
                    "FAIL: test_string_and_cap (test_regression.Tests.test_string_and_cap)\n"
                    "Traceback (most recent call last):\n"
                    f'  File "/tmp/agent-eval/{evaluation_id}/parse-timeout/runs/{run_id}/workspace/test_regression.py", line 6, in test_string_and_cap\n'
                    "    self.assertEqual(parse_timeout(90), 60.0)\n"
                    "AssertionError: 90.0 != 60.0\n\n"
                    f"Ran 1 test in {test_duration_s}s\n\nFAILED (failures=1)"
                )
                return {
                    "harness_id": "python-unit",
                    "status": "failed",
                    "checks": [{
                        "id": "unittest",
                        "status": "failed",
                        "duration_s": duration_s,
                        "message": message,
                    }],
                    "metrics": {"returncode": 1, "duration_s": duration_s},
                }

            first = failure("a" * 32, "b" * 32, 0.147, 0.002)
            second = failure("c" * 32, "d" * 32, 0.391, 0.009)
            first_feedback = self._capture_feedback(root, "first", first)
            second_feedback = self._capture_feedback(root, "second", second)

            self.assertEqual(first_feedback, second_feedback)
            self.assertIn("AssertionError: 90.0 != 60.0", first_feedback)
            self.assertIn('"status": "failed"', first_feedback)
            self.assertNotIn("a" * 32, first_feedback)
            self.assertNotIn("b" * 32, first_feedback)
            self.assertNotIn("0.147", first_feedback)
            self.assertNotIn("0.002", first_feedback)


if __name__ == "__main__":
    unittest.main()
