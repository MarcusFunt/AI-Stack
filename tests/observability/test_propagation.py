import unittest

from core.context import TraceContext
from observability.propagation import extract_trace_context, inject_trace_context


class PropagationTests(unittest.TestCase):
    def test_extracts_w3c_parent_request_id_and_baggage(self):
        context = extract_trace_context({
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00",
            "x-request-id": "request-42",
            "baggage": "tenant=acme,debug=true",
        })

        self.assertEqual(context.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(context.span_id, "00f067aa0ba902b7")
        self.assertEqual(context.trace_flags, "00")
        self.assertEqual(context.request_id, "request-42")
        self.assertEqual(context.baggage, {"tenant": "acme", "debug": "true"})

    def test_invalid_traceparent_falls_back_to_a_fresh_valid_context(self):
        context = extract_trace_context({"traceparent": "not-a-traceparent"})

        self.assertRegex(context.trace_id, r"^[0-9a-f]{32}$")
        self.assertEqual(context.trace_flags, "01")

    def test_injection_round_trips_trace_context_without_losing_sampling_flags(self):
        context = TraceContext(
            trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
            span_id="00f067aa0ba902b7",
            request_id="request-42",
            trace_flags="00",
            baggage={"tenant": "acme"},
        )
        headers = {}

        inject_trace_context(headers, context)
        restored = extract_trace_context(headers)

        self.assertEqual(headers["traceparent"], "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00")
        self.assertEqual(restored.trace_id, context.trace_id)
        self.assertEqual(restored.span_id, context.span_id)
        self.assertEqual(restored.request_id, context.request_id)
        self.assertEqual(restored.baggage, context.baggage)


    def test_injection_replaces_stale_headers_and_drops_empty_baggage(self):
        headers = {"traceparent": "stale", "x-request-id": "stale", "baggage": "bad=value"}
        context = TraceContext(trace_id="4bf92f3577b34da6a3ce929d0e0e4736")

        inject_trace_context(headers, context)

        self.assertEqual(headers["traceparent"], f"00-{context.trace_id}-{context.span_id}-01")
        self.assertEqual(headers["X-Request-ID"], context.request_id)
        self.assertNotIn("x-request-id", headers)
        self.assertNotIn("baggage", headers)

if __name__ == "__main__":
    unittest.main()
