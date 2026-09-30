"""Command-line entry point for preparing local public STT datasets."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .samtalebank import SamtaleBankSam3Adapter, TalkBankAccessError
from .synthetic import DiarizationK3Adapter
from .speech_recognition import (
    CoRalConversationTestAdapter,
    FleursDanishTestAdapter,
    NstDanishTestAdapter,
)
from .danpass import write_pending_access_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare Danish public STT benchmark datasets."
    )
    parser.add_argument(
        "--suite",
        choices=(
            "samtalebank-sam3",
            "diarization-k3",
            "coral-conversation-test",
            "nst-da-test",
            "fleurs-da-dk-test",
            "danpass-dialogue",
        ),
        required=True,
    )
    parser.add_argument("--source-path", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--lock-path", type=Path)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument("--revision")
    args = parser.parse_args(argv)

    repository_root = Path(__file__).resolve().parents[3]
    output_dir = args.output_dir or (
        repository_root / "data" / "stt-benchmark" / "prepared" / args.suite
    )
    lock_path = args.lock_path or repository_root / "data" / "stt-benchmark" / "dataset-lock.json"
    try:
        if args.suite == "samtalebank-sam3":
            manifest = SamtaleBankSam3Adapter().prepare(
                args.source_path,
                output_dir,
                lock_path=lock_path,
                seed=args.seed,
            )
        elif args.suite == "diarization-k3":
            manifest = DiarizationK3Adapter().prepare(
                args.source_path,
                output_dir,
                lock_path=lock_path,
                revision=args.revision,
            )
        elif args.suite == "danpass-dialogue":
            status_path = write_pending_access_status(output_dir)
            print(f"Dataset status: {status_path} (PENDING_ACCESS)")
            return 0
        else:
            adapters = {
                "coral-conversation-test": CoRalConversationTestAdapter,
                "nst-da-test": NstDanishTestAdapter,
                "fleurs-da-dk-test": FleursDanishTestAdapter,
            }
            manifest = adapters[args.suite]().prepare(
                args.source_path,
                output_dir,
                lock_path=lock_path,
                revision=args.revision,
            )
    except (TalkBankAccessError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Prepared manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
