import json
import unittest

from agent_lab.worker.model import ModelClient, build_patch_messages, parse_patch_response


class ModelContractTests(unittest.TestCase):
    def test_fenced_json_patch_is_accepted(self):
        raw = """```json
{"summary":"fix","edits":[{"op":"create","path":"x.py","content":"x = 1\\n"}]}
```"""
        parsed = parse_patch_response(raw)
        self.assertEqual(parsed["summary"], "fix")
        self.assertEqual(parsed["edits"][0]["path"], "x.py")

    def test_thinking_and_leading_text_are_removed_before_json_parse(self):
        raw = (
            "<think>private reasoning</think>\nResult:\n"
            '{"summary":"fix","edits":[]}'
        )
        parsed = parse_patch_response(raw)
        self.assertEqual(parsed, {"summary": "fix", "edits": []})

    def test_non_object_and_non_list_edits_are_rejected(self):
        for raw in ('[]', '{"edits":{}}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_patch_response(raw)

    def test_invalid_json_is_not_silently_repaired(self):
        with self.assertRaisesRegex(ValueError, "invalid patch JSON"):
            parse_patch_response('{"edits": [}')

    def test_prompt_preserves_security_contract_and_feedback(self):
        prior = {"status": "failed", "checks": [{"id": "unit", "message": "boom"}]}
        messages = build_patch_messages("Fix it", "===== app.py =====\nx = 1", prior)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        system = messages[0]["content"]
        user = messages[1]["content"]
        for phrase in (
            "Never modify tests",
            "hidden evaluation files",
            "secrets",
            "Do not weaken checks",
        ):
            self.assertIn(phrase, system)
        self.assertIn('"status": "failed"', user)
        self.assertIn("===== app.py =====", user)
        self.assertIn("at most 5 file operations", user)

    def test_patch_output_budget_is_bounded_for_reasoning_model(self):
        reasoning = ModelClient("http://gateway", "key", "local-reasoning")
        fast = ModelClient("http://gateway", "key", "local-fast")
        self.assertEqual(reasoning.patch_output_token_budget(), 512)
        self.assertEqual(reasoning.patch_output_token_budget(60), 120)
        self.assertEqual(reasoning.patch_output_token_budget(25), 96)
        self.assertEqual(fast.patch_output_token_budget(25), 1024)

    def test_reasoning_patch_request_caps_internal_thinking(self):
        client = ModelClient("http://gateway", "key", "local-reasoning")
        payload = client._request_payload([], 512)
        self.assertEqual(payload["reasoning_format"], "deepseek")
        self.assertEqual(payload["thinking_budget_tokens"], 0)
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})

    def test_no_feedback_is_explicit(self):
        messages = build_patch_messages("Fix it", "context", None)
        self.assertIn("Previous harness result:\nnone", messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
