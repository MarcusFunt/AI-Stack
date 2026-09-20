import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_eval.runner_runtime import QueueModelClient


class QueueModelClientContractTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
