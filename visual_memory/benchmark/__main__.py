from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .fixtures import build_cases
from .runner import (
    DeterministicFixtureEmbedder,
    GatewayEmbedder,
    Qwen3VLEmbeddingChallenger,
    run_qwen_vlm_comparison,
    run_gateway_retrieval_benchmark,
    run_retrieval_benchmark,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark labelled AI-Stack visual-memory retrieval fixtures.")
    parser.add_argument("--embedder", choices=("fake", "gateway", "qwen"), default="fake")
    parser.add_argument("--gateway-url", default=os.getenv("AI_GATEWAY_URL", "http://127.0.0.1:8090"))
    parser.add_argument("--qwen-model-path", help="Existing local Qwen3-VL-Embedding-2B model directory")
    parser.add_argument("--qwen-revision", help="Immutable 40-character local model commit SHA")
    parser.add_argument("--token-budgets", nargs="+", type=int, default=None, metavar="TOKENS")
    parser.add_argument("--output", type=Path, help="JSON report path; default is a timestamped file under data/benchmarks")
    parser.add_argument("--include-vlm", action="store_true", help="Run expensive Qwen-only vs retrieval-assisted comparison")
    parser.add_argument("--vlm-output", type=Path, help="JSONL raw output path for manual VLM scoring")
    args = parser.parse_args(argv)

    cases = build_cases()
    api_key = os.getenv("AI_API_KEY")
    client = None
    if args.embedder == "fake":
        embedder = DeterministicFixtureEmbedder(cases)
        default_budgets = [280, 560, 1120]
    elif args.embedder == "gateway":
        if not api_key:
            parser.error("gateway mode requires AI_API_KEY in the process environment")
        client = GatewayEmbedder(args.gateway_url, api_key)
        embedder = client
        default_budgets = [280, 560, 1120]
    else:
        if not args.qwen_model_path or not args.qwen_revision:
            parser.error("qwen mode requires --qwen-model-path and --qwen-revision")
        embedder = Qwen3VLEmbeddingChallenger(args.qwen_model_path, args.qwen_revision)
        default_budgets = [560]
    budgets = args.token_budgets or default_budgets
    if args.embedder == "qwen" and budgets != [560]:
        parser.error("the Qwen challenger uses native image processing; run it with --token-budgets 560")
    if args.include_vlm:
        if client is None:
            parser.error("--include-vlm requires --embedder gateway and AI_API_KEY")

    if args.embedder == "gateway":
        report = run_gateway_retrieval_benchmark(client, cases, token_budgets=budgets)
    else:
        report = run_retrieval_benchmark(embedder, cases, token_budgets=budgets)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    if args.embedder == "qwen":
        report["challenger"] = {"model_path": embedder.model_path, "revision": embedder.revision, "device": "cpu"}
    output = args.output or Path("data/benchmarks") / f"visual-memory-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "retrieval": report["retrieval"], "performance": report["performance"]}, indent=2))

    if args.include_vlm:
        vlm_path = args.vlm_output or output.with_suffix(".vlm.jsonl")
        vlm_report = run_qwen_vlm_comparison(client, cases, vlm_path)
        print(json.dumps({"vlm_comparison": vlm_report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
