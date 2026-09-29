import unittest

from supervisor.resource_scheduler import (
    ResourceAllocation,
    ResourceScheduler,
    validate_config,
)


def config_with(*, total_vram=16000, headroom=1000):
    return {
        "resource_capacity": {
            "gpu_total_mb": total_vram,
            "gpu_reserved_headroom_mb": headroom,
        },
        "services": {
            "llm": {
                "gpu": True,
                "resources": {
                    "gpu_vram_mb": 6000,
                    "system_ram_mb": 2048,
                    "exclusive_gpu": False,
                },
                "compatibility_groups": ["voice"],
            },
            "stt": {
                "gpu": True,
                "resources": {
                    "gpu_vram_mb": 3000,
                    "system_ram_mb": 1024,
                    "exclusive_gpu": False,
                },
                "compatibility_groups": ["voice"],
            },
            "image": {
                "gpu": True,
                "resources": {"gpu_vram_mb": 4000, "exclusive_gpu": True},
            },
            "cpu": {"gpu": False},
        },
        "workload_profiles": {
            "voice": {"priority": 100, "prefer_resident": ["llm", "stt"]},
            "interactive": {"priority": 80, "prefer_resident": []},
            "reasoning": {"priority": 70, "prefer_resident": []},
            "vision": {"priority": 60, "prefer_resident": []},
            "image": {"priority": 50, "prefer_resident": ["image"]},
            "video": {"priority": 40, "prefer_resident": ["image"]},
            "benchmark": {"priority": 30, "prefer_resident": []},
        },
    }


class ResourceSchedulerTests(unittest.TestCase):
    def test_legacy_gpu_services_are_exclusive_by_default(self):
        config = {"services": {"llm": {"gpu": True}, "stt": {"gpu": True}}}
        scheduler = ResourceScheduler(config)
        active = [ResourceAllocation("llm-lease", "llm")]

        decision = scheduler.admit("stt", active)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "exclusive_gpu_conflict")

    def test_canonical_resource_gpu_flag_is_also_exclusive_by_default(self):
        scheduler = ResourceScheduler({
            "services": {
                "llm": {"resources": {"gpu": True}},
                "stt": {"resources": {"gpu": True}},
            }
        })

        decision = scheduler.admit("stt", [ResourceAllocation("llm-lease", "llm")])

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "exclusive_gpu_conflict")

    def test_compatible_services_admit_only_with_measured_fit(self):
        scheduler = ResourceScheduler(config_with(), mode="resource")
        active = [scheduler.allocation("llm-lease", "llm", profile="voice")]

        decision = scheduler.admit("stt", active, profile="voice")

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "compatible_capacity_fit")

    def test_unknown_capacity_fails_closed_for_co_residency(self):
        scheduler = ResourceScheduler(config_with(total_vram=None, headroom=None), mode="resource")
        active = [scheduler.allocation("llm-lease", "llm")]

        decision = scheduler.admit("stt", active, profile="voice")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "gpu_capacity_unknown")

    def test_capacity_headroom_prevents_oversubscription(self):
        scheduler = ResourceScheduler(config_with(total_vram=9000, headroom=1000), mode="resource")
        active = [scheduler.allocation("llm-lease", "llm")]

        decision = scheduler.admit("stt", active, profile="voice")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "gpu_capacity_exceeded")

    def test_exclusive_service_cannot_join_compatible_group(self):
        scheduler = ResourceScheduler(config_with(), mode="resource")
        active = [scheduler.allocation("llm-lease", "llm")]

        decision = scheduler.admit("image", active, profile="image")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "exclusive_gpu_conflict")

    def test_same_loaded_service_can_hold_multiple_leases(self):
        scheduler = ResourceScheduler(config_with())
        active = [ResourceAllocation("llm-lease-1", "llm")]

        decision = scheduler.admit("llm", active, profile="interactive")

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "service_already_allocated")

    def test_workload_profile_residency_order_is_declarative(self):
        scheduler = ResourceScheduler(config_with())

        self.assertEqual(
            scheduler.preferred_resident("voice"), ["llm", "stt"]
        )

    def test_voice_resident_plan_keeps_measured_compatible_workers_together(self):
        scheduler = ResourceScheduler(config_with(), mode="resource")

        self.assertEqual(scheduler.resident_plan("voice"), ["llm", "stt"])

    def test_unknown_worker_memory_requirement_prevents_co_residency(self):
        config = config_with()
        config["services"]["stt"]["resources"]["gpu_vram_mb"] = None
        scheduler = ResourceScheduler(config, mode="resource")
        active = [scheduler.allocation("llm-lease", "llm", profile="voice")]

        decision = scheduler.admit("stt", active, profile="voice")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "gpu_requirement_unknown")

    def test_config_validation_rejects_invalid_registry_and_resources(self):
        invalid_cases = [
            {"services": {"llm": {"gpu": True}}, "models": [
                {"id": "same", "service": "llm", "capabilities": ["chat"]},
                {"id": "same", "service": "llm", "capabilities": ["chat"]},
            ]},
            {"services": {"llm": {"gpu": True, "resources": {"gpu_vram_mb": -1}}}},
            {"services": {"llm": {"gpu": True}}, "models": [
                {"id": "m", "service": "missing", "capabilities": ["chat"]}
            ]},
            {"services": {"llm": {"gpu": True}}, "models": [
                {"id": "m", "service": "llm", "capabilities": "chat"}
            ]},
            {"services": {"llm": {"gpu": True}}, "models": [
                {"id": "m", "service": "llm", "profiles": ["typo"]}
            ]},
            {"services": {"llm": {"gpu": True}}, "models": [
                {"id": "m", "service": "llm", "capabilities": ["chat"]}
            ], "aliases": {"fast": "missing"}},
            {"services": {"llm": {"gpu": True}}, "workload_profiles": {
                "voice": {"priority": 1, "prefer_resident": ["missing"]}
            }},
        ]
        for config in invalid_cases:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    validate_config(config)

    def test_state_reports_unknown_capacity_and_live_allocations(self):
        scheduler = ResourceScheduler(config_with(total_vram=None, headroom=None))
        state = scheduler.state([ResourceAllocation("lease", "llm")], ["llm"])

        self.assertIsNone(state["gpu_total_mb"])
        self.assertEqual(state["loaded_services"], ["llm"])
        self.assertEqual(state["allocations"][0]["lease_id"], "lease")
        self.assertEqual(state["workload_profiles"]["voice"]["eligible_resident"], ["llm"])


if __name__ == "__main__":
    unittest.main()
