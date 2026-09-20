from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx


_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def build_patch_messages(
    objective: str,
    repo_context: str,
    prior_result: dict[str, Any] | None,
) -> list[dict[str, str]]:
    feedback = (
        json.dumps(prior_result, ensure_ascii=False)
        if prior_result
        else "none"
    )
    system = (
        "You are a coding agent in an isolated Git worktree. "
        "Return ONLY valid JSON. You may replace exact text in existing files "
        "shown in the repository context, or create a small new source/config/"
        "documentation file when the objective requires it. Never modify tests, "
        "hidden evaluation files, secrets, .git, or generated state. Do not "
        "weaken checks to make a failure disappear."
    )
    user = f"""Objective:
{objective}

Previous harness result:
{feedback}

Repository context:
{repo_context}

Return this JSON shape:
{{
  "summary": "short reasoning summary",
  "edits": [
    {{
      "op": "replace",
      "path": "relative/path.py",
      "old": "exact existing text",
      "new": "replacement"
    }},
    {{
      "op": "create",
      "path": "relative/new_file.py",
      "content": "complete new file contents"
    }}
  ]
}}
Use only the operations you actually need and at most 5 file operations total.
Prefer a general implementation that satisfies the full objective semantics rather
than narrowly special-casing the visible failing example.
Do not edit test files. If no safe edit is justified, return an empty edits list."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_patch_response(raw: str) -> dict[str, Any]:
    cleaned = _THINK_RE.sub("", raw).strip()
    cleaned = _JSON_FENCE.sub("", cleaned).strip()
    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        extracted = cleaned[start : end + 1]
        if extracted != cleaned:
            candidates.append(extracted)

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            result = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(result, dict) or not isinstance(result.get("edits", []), list):
            raise ValueError("model returned invalid patch object")
        return result

    preview = cleaned[:500].replace("\n", "\\n")
    raise ValueError(
        f"model returned invalid patch JSON: {last_error}; response={preview!r}"
    )


class ModelClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "local-fast",
        request_timeout_seconds: float = 150.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.request_timeout_seconds = max(1.0, float(request_timeout_seconds))

    def patch_output_token_budget(self, timeout_seconds: float | None = None) -> int:
        cap = 512 if self.model == "local-reasoning" else 1024
        if timeout_seconds is None or self.model != "local-reasoning":
            return cap
        # The local reasoning backend is about 4-5 tokens/s. Keep enough
        # margin for prompt evaluation so one proposal cannot consume the run.
        generation_seconds = max(20.0, float(timeout_seconds) - 20.0)
        return min(cap, max(96, int(generation_seconds * 3.0)))

    def _request_payload(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if self.model == "local-fast":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        elif self.model == "local-reasoning":
            # Structured patch synthesis needs a short machine-readable answer.
            # Current llama.cpp/Qwen reasoning templates can otherwise spend the
            # entire completion budget thinking and never emit the JSON patch.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload["reasoning_format"] = "deepseek"
            payload["thinking_budget_tokens"] = 0
            payload["reasoning_effort"] = "none"
        return payload

    def _request(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 2048,
        timeout_seconds: float | None = None,
    ) -> str:
        effective_timeout = self.request_timeout_seconds
        if timeout_seconds is not None:
            effective_timeout = min(effective_timeout, max(1.0, timeout_seconds))
        payload = self._request_payload(messages, max_tokens)
        deadline = time.monotonic() + effective_timeout
        retryable = {500, 502, 503, 504}
        last_error: Exception | None = None

        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                with httpx.Client(timeout=httpx.Timeout(max(1.0, remaining))) as client:
                    response = client.post(
                        f"{self.base_url}/v1/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                if response.status_code in retryable and attempt < 2:
                    last_error = httpx.HTTPStatusError(
                        f"retryable upstream status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                    delay = min(2.0 ** attempt, max(0.0, deadline - time.monotonic() - 1.0))
                    if delay > 0:
                        time.sleep(delay)
                    continue
                response.raise_for_status()
                body = response.json()
                choice = body["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError("model response hit the output-token limit before completing")
                return str(choice["message"]["content"])
            except httpx.TimeoutException as exc:
                last_error = exc
                break
            except httpx.RequestError as exc:
                last_error = exc
                if attempt < 2:
                    delay = min(2.0 ** attempt, max(0.0, deadline - time.monotonic() - 1.0))
                    if delay > 0:
                        time.sleep(delay)
                    continue
                break

        if isinstance(last_error, httpx.TimeoutException) or time.monotonic() >= deadline:
            raise TimeoutError(f"model request timed out after {effective_timeout:.1f}s") from last_error
        if last_error is not None:
            raise last_error
        raise TimeoutError(f"model request timed out after {effective_timeout:.1f}s")

    def propose_patch(
        self,
        objective: str,
        repo_context: str,
        prior_result: dict[str, Any] | None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        messages = build_patch_messages(
            objective,
            repo_context,
            prior_result,
        )
        raw = self._request(
            messages,
            max_tokens=self.patch_output_token_budget(timeout_seconds),
            timeout_seconds=timeout_seconds,
        )
        return parse_patch_response(raw)
