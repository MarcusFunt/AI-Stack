import unittest

from core.capabilities import Capability, CapabilityRegistry
from core.providers import ProviderHealth, ProviderHealthState


class CapabilityTests(unittest.TestCase):
    def test_registry_finds_providers_by_capability_with_constraints(self):
        registry = CapabilityRegistry()
        registry.register(
            "llama.cpp",
            {Capability.CHAT, Capability.STREAMING_TEXT},
            constraints={Capability.STREAMING_TEXT: {"max_context": 32768}},
        )
        registry.register("faster-whisper", {Capability.TRANSCRIPTION})

        self.assertEqual(registry.providers_for(Capability.CHAT), ("llama.cpp",))
        self.assertEqual(registry.providers_for("transcription"), ("faster-whisper",))
        self.assertEqual(
            registry.constraints_for("llama.cpp", Capability.STREAMING_TEXT),
            {"max_context": 32768},
        )

    def test_provider_health_has_stable_status_and_detail(self):
        health = ProviderHealth(ProviderHealthState.DEGRADED, details={"reason": "warmup"})

        self.assertFalse(health.ready)
        self.assertEqual(health.details["reason"], "warmup")


if __name__ == "__main__":
    unittest.main()
