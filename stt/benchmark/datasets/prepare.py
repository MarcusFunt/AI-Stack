"""Command-line entry point for preparing local public STT datasets."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .samtalebank import SamtaleBankSam3Adapter, TalkBankAccessError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare Danish public STT benchmark datasets from local source files."
    )
    parser.add_argument("--suite", choices=("samtalebank-sam3",), required=True)
    parser.add_argument("--source-path", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--lock-path", type=Path)
    parser.add_argument("--seed", type=int, default=20260930)
    args = parser.parse_args(argv)

    repository_root = Path(__file__).resolve().parents[3]
    output_dir = args.output_dir or (
        repository_root / "data" / "stt-benchmark" / "prepared" / args.suite
    )
    lock_path = args.lock_path or repository_root / "data" / "stt-benchmark" / "dataset-lock.json"
    try:
        manifest = SamtaleBankSam3Adapter().prepare(
            args.source_path,
            output_dir,
            lock_path=lock_path,
            seed=args.seed,
        )
    except (TalkBankAccessError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Prepared manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
