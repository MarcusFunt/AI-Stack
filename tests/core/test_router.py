from __future__ import annotations

import unittest

from core.invocation import Invocation, InvocationOperation, InvocationSource, ModelPolicy
from core.router import InvocationRouter, ModelNotFoundError, UnsupportedCapabilityError
from core.events import InvocationEventSequencer, InvocationEventType


MODELS = [
    {"id": "local-fast", "service": "llm", "capabilities": ["chat", "streaming_text"]},
    {"id": "local-stt", "service": "stt", "capabilities": ["transcription"]},
]


class FakeProvider:
    async def health(self):
        return None

    async def capabilities(self):
        return frozenset({"chat"})

    async def invoke(self, invocation):
        yield InvocationEventSequencer(invocation).emit(InvocationEventType.INVOCATION_COMPLETED)

    async def cancel(self, invocation_id):
        return None


class InvocationRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.router = InvocationRouter(MODELS, aliases={"fast": "local-fast"})

    def test_resolves_model_alias_and_checks_capability(self):
        invocation = Invocation(
            source=InvocationSource.OPENAI_CHAT,
            requested_model="FAST",
            model_policy=ModelPolicy(capability="chat"),
        )

        route = self.router.resolve(invocation)

        self.assertEqual(route.requested_model, "FAST")
        self.assertEqual(route.model_id, "local-fast")
        self.assertEqual(route.provider_id, "llm")

    def test_embed_operation_uses_existing_embedding_capability(self):
        router = InvocationRouter(
            [
                {"id": "local-visual-embedding", "service": "visual-memory", "capabilities": ["embedding"]}
            ],
            aliases={"visual-embedding": "local-visual-embedding"},
        )

        route = router.resolve(
            Invocation(operation=InvocationOperation.EMBED, requested_model="visual-embedding")
        )

        self.assertEqual(route.model_id, "local-visual-embedding")
        self.assertEqual(route.provider_id, "visual-memory")
        self.assertIn("embedding", route.capabilities)

    def test_rejects_unknown_models_and_unsupported_capabilities(self):
        with self.assertRaises(ModelNotFoundError):
            self.router.resolve(Invocation(requested_model="missing"))

        with self.assertRaises(UnsupportedCapabilityError):
            self.router.resolve(Invocation(requested_model="local-stt", model_policy=ModelPolicy(capability="chat")))

    async def test_dispatches_provider_events_through_canonical_router(self):
        self.router.register_provider("llm", FakeProvider())
        invocation = Invocation(
            operation=InvocationOperation.GENERATE,
            requested_model="fast",
            model_policy=ModelPolicy(capability="chat"),
        )

        events = [event async for event in self.router.invoke(invocation)]

        self.assertEqual([event.type for event in events], [InvocationEventType.INVOCATION_COMPLETED.value])
        self.assertEqual(events[0].invocation_id, invocation.id)


if __name__ == "__main__":
    unittest.main()
