from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from scripts import host_agent


class MaintenanceOperationTests(unittest.TestCase):
    def test_voice_smoke_uses_the_ai_stack_cli_action(self):
        args, snapshot = host_agent._operation_args("voice-smoke", {})

        self.assertEqual(args[-1], "voice-smoke")
        self.assertEqual(Path(args[args.index("-File") + 1]).name, "ai.ps1")
        self.assertIsNone(snapshot)


if __name__ == "__main__":
    unittest.main()
