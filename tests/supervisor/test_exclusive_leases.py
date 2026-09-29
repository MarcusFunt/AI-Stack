from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_DIR = ROOT / "supervisor"


class ExclusiveLeaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        config = root / "models.json"
        config.write_text(
            json.dumps(
                {
                    "services": {
                        "stt": {"gpu": True},
                        "llm": {"gpu": True},
                    },
                    "models": [],
                }
            ),
            encoding="utf-8",
        )
        cls.environment = patch.dict(
            os.environ,
            {
                "CONFIG_PATH": str(config),
                "SUPERVISOR_TOKEN": "test-supervisor-token",
                "LLAMA_API_KEY": "test-llama-key",
                "DOCKER_CONTROL_TOKEN": "test-docker-control-token",
                "LEASE_STATE_PATH": str(root / "leases.json"),
                "RUNTIME_SETTINGS_PATH": str(root / "settings.json"),
                "SERVICE_METRICS_PATH": str(root / "metrics.json"),
            },
        )
        cls.environment.start()
        sys.path.insert(0, str(SUPERVISOR_DIR))
        spec = importlib.util.spec_from_file_location(
            "supervisor_service_app", SUPERVISOR_DIR / "app.py"
        )
        cls.service = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.service)

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()
        if str(SUPERVISOR_DIR) in sys.path:
            sys.path.remove(str(SUPERVISOR_DIR))
        sys.modules.pop("supervisor_service_app", None)
        cls.tmp.cleanup()

    def setUp(self):
        self.service.active_leases = defaultdict(set)
        self.service.active_jobs = defaultdict(int)
        self.service.active_lease_profiles = defaultdict(dict)
        self.service.active_exclusive_leases = defaultdict(set)
        self.service.pending_requests = {}

    def test_exclusive_request_waits_for_existing_same_service_lease(self):
        self.service.active_leases["stt"].add("live-request")
        self.service.active_jobs["stt"] = 1
        self.service.active_lease_profiles["stt"]["live-request"] = "interactive"

        self.assertTrue(
            self.service.has_blocking_active_jobs(
                "stt", "benchmark", exclusive=True, pending_id="benchmark"
            )
        )

    def test_regular_request_waits_for_active_exclusive_lease(self):
        self.service.active_leases["stt"].add("benchmark")
        self.service.active_jobs["stt"] = 1
        self.service.active_exclusive_leases["stt"].add("benchmark")

        self.assertTrue(
            self.service.has_blocking_active_jobs(
                "stt", "interactive", exclusive=False, pending_id="regular"
            )
        )

    def test_pending_exclusive_request_blocks_new_regular_same_service_work(self):
        self.service.pending_requests["benchmark"] = {
            "service": "stt",
            "profile": "benchmark",
            "exclusive": True,
            "queued_at": 0.0,
        }

        self.assertTrue(
            self.service.has_blocking_active_jobs(
                "stt", "interactive", exclusive=False, pending_id="regular"
            )
        )
        self.assertFalse(
            self.service.has_blocking_active_jobs(
                "stt", "benchmark", exclusive=True, pending_id="benchmark"
            )
        )


if __name__ == "__main__":
    unittest.main()
