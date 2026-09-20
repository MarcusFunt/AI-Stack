from __future__ import annotations

import json
import re
from typing import Any

import httpx


_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class ModelClient:
    def __init__(self, base_url: str, api_key: str, model: str = "local-fast"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _request(self, messages: list[dict[str, str]], max_tokens: int = 2048) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        with httpx.Client(timeout=600) as client:
            response = client.post(
                f"{self.base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        return str(body["choices"][0]["message"]["content"])

    def propose_patch(
        self,
        objective: str,
        repo_context: str,
        prior_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        feedback = json.dumps(prior_result, ensure_ascii=False) if prior_result else "none"
        system = (
            "You are a coding agent in an isolated Git worktree. "
            "Return ONLY valid JSON. You may make exact text replacements in files "
            "shown in the repository context. Never modify secrets, .git, generated "
            "state, tests solely to make a failure disappear, or files not shown."
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
    {{"path": "relative/path.py", "old": "exact existing text", "new": "replacement"}}
  ]
}}
Use at most 5 edits. If no safe edit is justified, return an empty edits list."""
        raw = self._request(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        cleaned = _JSON_FENCE.sub("", raw.strip()).strip()
        result = json.loads(cleaned)
        if not isinstance(result, dict) or not isinstance(result.get("edits", []), list):
            raise ValueError("model returned invalid patch object")
        return result
