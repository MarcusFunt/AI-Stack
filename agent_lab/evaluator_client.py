from __future__ import annotations

from typing import Any

import httpx


class EvaluatorClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def health(self) -> dict[str, Any]:
        with httpx.Client(timeout=5) as client:
            response = client.get(f"{self.base_url}/health")
            response.raise_for_status()
            return response.json()

    def create(
        self,
        *,
        run_id: str,
        candidate_commit: str,
        base_commit: str,
        token_budget: int,
        wall_time_seconds: int = 900,
    ) -> dict[str, Any]:
        payload = {
            "run_id": run_id,
            "candidate_commit": candidate_commit,
            "base_commit": base_commit,
            "suite": "agent-lab-selfmod-v1",
            "model": "local-fast",
            "token_budget": token_budget,
            "wall_time_seconds": wall_time_seconds,
        }
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{self.base_url}/evaluations", json=payload
            )
            response.raise_for_status()
            return response.json()

    def get(self, evaluation_id: str) -> dict[str, Any]:
        with httpx.Client(timeout=10) as client:
            response = client.get(
                f"{self.base_url}/evaluations/{evaluation_id}"
            )
            response.raise_for_status()
            return response.json()

    def by_run(self, run_id: str) -> list[dict[str, Any]]:
        with httpx.Client(timeout=10) as client:
            response = client.get(
                f"{self.base_url}/runs/{run_id}/evaluations"
            )
            response.raise_for_status()
            return response.json()

    def cancel(self, evaluation_id: str) -> dict[str, Any]:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                f"{self.base_url}/evaluations/{evaluation_id}/cancel"
            )
            response.raise_for_status()
            return response.json()

    def latest_passing(self, run_id: str) -> dict[str, Any] | None:
        with httpx.Client(timeout=10) as client:
            response = client.get(
                f"{self.base_url}/runs/{run_id}/latest-passing"
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
