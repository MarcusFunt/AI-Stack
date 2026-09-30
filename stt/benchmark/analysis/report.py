"""Write suite-separated benchmark summaries and the evidence-aware report."""

from __future__ import annotations

import argparse
import csv
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .bootstrap import DEFAULT_BOOTSTRAP_SAMPLES, DEFAULT_BOOTSTRAP_SEED
from .comparison import (
    dataset_info,
    dataset_summary_rows,
    is_successful_model,
    pairwise_comparison_rows,
)
from .evidence import model_dataset_evidence
from .strata import metadata_stratified_metrics, sam3_stratified_metrics
from ..datasets.provenance import canonical_json_sha256, file_sha256

def _safe_component(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return result or "unknown"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(field for row in rows for field in row)) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if fields:
            writer.writeheader()
            writer.writerows(rows)


def _dataset_signature(result: dict[str, Any]) -> str:
    dataset = dataset_info(result)
    lock_entry_hash = _dataset_lock_entry_hash(dataset)
    stable_dataset_fields = {
        key: dataset.get(key)
        for key in (
            "name", "class", "source_url", "source_license",
            "source_revisions", "reference_transforms", "manifest_sha256",
        )
    }
    stable_dataset_fields["dataset_lock_entry_sha256"] = lock_entry_hash
    recordings = [
        {
            "id": row.get("id"),
            "duration_s": row.get("duration_s"),
            "source_recording": row.get("source_recording"),
            "reference_speakers": row.get("reference_speakers"),
            "reference_text": row.get("reference_text"),
            "metadata": row.get("metadata", {}),
        }
        for row in sorted(result.get("recordings", []), key=lambda item: str(item.get("id", "")))
    ]
    return json.dumps(
        {
            "dataset": stable_dataset_fields,
            "evaluation_protocol_sha256": result.get("evaluation_protocol_sha256"),
            "recordings": recordings,
        },
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )


def _dataset_lock_entry_hash(dataset: dict[str, Any]) -> str | None:
    entry = dataset.get("dataset_lock_entry")
    stored_hash = dataset.get("dataset_lock_entry_sha256")
    calculated_hash = canonical_json_sha256(entry) if isinstance(entry, dict) else None
    if stored_hash and calculated_hash and stored_hash != calculated_hash:
        raise ValueError("dataset lock entry SHA-256 does not match the embedded lock entry")
    return stored_hash or calculated_hash


def _provenance_evidence(run: dict[str, Any], alias: str) -> dict[str, Any]:
    try:
        schema_version = int(run.get("schema_version", 0) or 0)
    except (TypeError, ValueError):
        schema_version = 0
    model = run.get("models", {}).get(alias, {})
    if schema_version >= 3:
        evidence = model.get("evidence") if isinstance(model, dict) else None
        if not isinstance(evidence, dict):
            raise ValueError(f"schema-v3 result is missing model evidence for {alias}")
        return deepcopy(evidence)
    return model_dataset_evidence(run, alias)


def _merge_runs(results_paths: list[Path]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for path in results_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        dataset = dataset_info(payload)
        try:
            schema_version = int(payload.get("schema_version", 0) or 0)
        except (TypeError, ValueError):
            schema_version = 0
        if schema_version >= 3:
            for alias in payload.get("models", {}):
                model_dataset_evidence(payload, alias)
        key = str(dataset.get("name", "private-manifest"))
        if key not in grouped:
            merged = dict(payload)
            merged["models"] = dict(payload.get("models", {}))
            merged["_input_paths"] = [str(path.resolve())]
            grouped[key] = merged
            continue

        current = grouped[key]
        current_protocol = current.get("evaluation_protocol_sha256")
        next_protocol = payload.get("evaluation_protocol_sha256")
        if not current_protocol or not next_protocol:
            raise ValueError(
                f"result files for {key} are missing an evaluation protocol SHA-256"
            )
        if current_protocol != next_protocol:
            raise ValueError(
                f"result files for {key} have an evaluation protocol mismatch "
                f"({current_protocol} != {next_protocol})"
            )
        if _dataset_signature(current) != _dataset_signature(payload):
            raise ValueError(
                f"result files for {key} have incompatible dataset provenance or references"
            )
        current_ids = {row["id"] for row in current.get("recordings", [])}
        next_ids = {row["id"] for row in payload.get("recordings", [])}
        if current_ids != next_ids:
            raise ValueError(f"result files for {key} do not have identical recording IDs")
        overlap = set(current["models"]) & set(payload.get("models", {}))
        if overlap:
            raise ValueError(f"duplicate model results for {key}: {', '.join(sorted(overlap))}")
        current["models"].update(payload.get("models", {}))
        current["_input_paths"].append(str(path.resolve()))
        if not current.get("diarization", {}).get("collar_025_per_recording"):
            current["diarization"] = payload.get("diarization", current.get("diarization", {}))
    return [grouped[key] for key in sorted(grouped)]


def _pair_for_candidate(
    pair_rows: list[dict[str, Any]], model: str, other: str
) -> tuple[float, float, str] | None:
    for row in pair_rows:
        if {row["model_a"], row["model_b"]} != {model, other}:
            continue
        group_low, group_high = row.get("group_ci95_low"), row.get("group_ci95_high")
        if group_low is not None and group_high is not None:
            low, high = float(group_low), float(group_high)
            group_unit = row.get("group_unit") or "source_recording"
            interval_kind = "speaker-group" if group_unit == "speaker" else "recording-group"
        else:
            group_count = row.get("group_count")
            if (
                row.get("group_unit")
                and group_count is not None
                and int(group_count) < int(row.get("unit_count", 0))
            ):
                return None
            low, high = float(row["ci95_low"]), float(row["ci95_high"])
            interval_kind = "paired"
        if row["model_a"] == model:
            return low, high, interval_kind
        return -high, -low, interval_kind
    return None


def _recommend_for_suite(
    result: dict[str, Any], pair_rows: list[dict[str, Any]], *, label: str
) -> str:
    successful = {
        alias: model for alias, model in result.get("models", {}).items()
        if is_successful_model(model)
    }
    if not successful:
        return f"{label}: no successful model results are available."
    models = {
        alias: model for alias, model in successful.items()
        if model_dataset_evidence(result, alias)["decisive"]
    }
    non_decisive = sorted(set(successful) - set(models))
    if len(non_decisive) == 1:
        excluded_note = (
            f" {non_decisive[0]} is reported but excluded from model selection because "
            "its evidence is non-decisive."
        )
    elif non_decisive:
        excluded_note = (
            f" {', '.join(non_decisive)} are reported but excluded from model selection "
            "because their evidence is non-decisive."
        )
    else:
        excluded_note = ""
    if not models:
        candidates = ", ".join(non_decisive)
        return (
            f"{label}: no decisive candidate evidence is available; scores remain visible, "
            f"but no model is selected ({candidates})."
        )
    ranking = sorted(
        models,
        key=lambda model: float(models[model].get("content_wer", float("inf"))),
    )
    best = ranking[0]
    if len(ranking) == 1:
        return (
            f"{label}: {best} is the only evaluated model with decisive evidence; "
            f"this is not a comparison.{excluded_note}"
        )

    runner_up = ranking[1]
    interval = _pair_for_candidate(pair_rows, best, runner_up)
    if interval and interval[1] < 0:
        return (
            f"{label}: {best} has lower content WER than {runner_up}; "
            f"{interval[2]} 95% CI for WER difference is "
            f"[{interval[0]:.3%}, {interval[1]:.3%}].{excluded_note}"
        )
    if interval and interval[0] > 0:
        return (
            f"{label}: the {interval[2]} 95% CI for the {best} minus {runner_up} "
            f"WER difference is entirely above zero "
            f"[{interval[0]:.3%}, {interval[1]:.3%}], favoring {runner_up} "
            f"despite {best} having the lower observed content WER.{excluded_note}"
        )

    best_speaker = models[best].get("speaker_attributed_wer")
    other_speaker = models[runner_up].get("speaker_attributed_wer")
    speaker_note = (
        f" Speaker-attributed WER is {best_speaker:.3%} vs {other_speaker:.3%} "
        "and is descriptive only."
        if best_speaker is not None and other_speaker is not None else ""
    )
    if interval:
        return (
            f"{label}: no unique winner between {best} and {runner_up}; "
            f"the {interval[2]} 95% CI [{interval[0]:.3%}, {interval[1]:.3%}] includes zero."
            f"{speaker_note}{excluded_note}"
        )
    return (
        f"{label}: no unique winner between {best} and {runner_up}; paired uncertainty is unavailable."
        f"{speaker_note}{excluded_note}"
    )


def _decision_lines(runs: list[dict[str, Any]], pair_rows: list[dict[str, Any]]) -> list[str]:
    by_name = {dataset_info(run).get("name"): run for run in runs}
    lines = []
    sam3 = by_name.get("samtalebank-sam3")
    if sam3:
        sam3_pairs = [row for row in pair_rows if row["dataset"] == "samtalebank-sam3"]
        lines.append(_recommend_for_suite(
            sam3, sam3_pairs, label="Natural three-speaker Danish conversation evidence"
        ))
        lines.append("No declared candidate fine-tuning overlap was found for Sam3; base-model pretraining overlap cannot be ruled out.")
    else:
        lines.append("Natural three-speaker Danish conversation evidence: pending Sam3 results.")

    conventional_name = next(
        (name for name in ("coral-conversation-test", "nst-da-test", "fleurs-da-dk-test") if name in by_name),
        None,
    )
    if conventional_name:
        conventional = by_name[conventional_name]
        lines.append(_recommend_for_suite(
            conventional,
            [row for row in pair_rows if row["dataset"] == conventional_name],
            label=f"Conventional single-speaker Danish ASR evidence ({conventional_name})",
        ))
        lines.append("Other single-speaker suites are supporting evidence; their scores are not pooled.")
    else:
        lines.append("Conventional single-speaker Danish ASR evidence: pending held-out single-speaker results.")
    if "coral-conversation-test" in by_name:
        lines.append("CoRal is a held-out in-domain robustness check. No automatic Sam3-winner rejection threshold is configured; any claimed large regression needs a predeclared threshold and reproducible evidence.")
    if "diarization-k3" in by_name:
        lines.append("The controlled synthetic K=3 suite contributes diarization evidence only; its ASR WER does not decide among candidate models.")
    return lines


def _write_errors(run: dict[str, Any], output_dir: Path) -> None:
    dataset = str(dataset_info(run).get("name", "unknown"))
    directory = output_dir / "errors" / _safe_component(dataset)
    directory.mkdir(parents=True, exist_ok=True)
    recordings = {str(row["id"]): row for row in run.get("recordings", [])}
    for model, model_result in run.get("models", {}).items():
        path = directory / f"{_safe_component(model)}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for record_id, scores in model_result.get("per_recording", {}).items():
                errors = int(scores.get("content_errors", 0))
                hypothesis = str(scores.get("hypothesis", ""))
                if errors <= 0 and hypothesis.strip():
                    continue
                recording = recordings.get(record_id, {})
                item = {
                    "id": record_id,
                    "source_recording": recording.get("source_recording"),
                    "reference": recording.get("reference_text"),
                    "hypothesis": hypothesis,
                    "content_errors": errors,
                    "content_reference_words": scores.get("content_reference_words"),
                    "content_wer": scores.get("content_wer"),
                    "empty_output": not hypothesis.strip(),
                    "failure_class": scores.get("failure_class"),
                }
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def generate_report(
    result_paths: list[Path],
    output_dir: Path,
    *,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Path]:
    if not result_paths:
        raise ValueError("provide at least one results.json path")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    runs = _merge_runs([Path(path) for path in result_paths])
    summary_rows = [row for run in runs for row in dataset_summary_rows(run)]
    pair_rows = [
        row
        for run in runs
        for row in pairwise_comparison_rows(
            run, samples=bootstrap_samples, seed=seed
        )
    ]
    strata_rows = [
        row for run in runs
        for row in (sam3_stratified_metrics(run) + metadata_stratified_metrics(run))
    ]

    summary_path = output_dir / "dataset-summary.csv"
    pairwise_path = output_dir / "pairwise-comparison.csv"
    strata_path = output_dir / "strata-summary.csv"
    _write_csv(summary_path, summary_rows)
    _write_csv(pairwise_path, pair_rows)
    _write_csv(strata_path, strata_rows)

    provenance = {
        "schema_version": 1,
        "bootstrap": {"samples": bootstrap_samples, "seed": seed, "confidence": 0.95},
        "evidence_relationships": {
            dataset_info(run).get("name"): {
                alias: _provenance_evidence(run, alias)
                for alias in run.get("models", {})
            }
            for run in runs
        },
        "evaluation_protocols": [
            {
                "dataset": dataset_info(run).get("name"),
                "protocol": run.get("evaluation_protocol"),
                "sha256": run.get("evaluation_protocol_sha256"),
            }
            for run in runs
        ],
        "inputs": [
            {
                "logical_name": Path(path).name,
                "source_path": str(Path(path).resolve()),
                "sha256": file_sha256(Path(path)),
            }
            for path in result_paths
        ],
        "datasets": [
            {
                "name": dataset_info(run).get("name"),
                "class": dataset_info(run).get("class"),
                "source_url": dataset_info(run).get("source_url"),
                "source_license": dataset_info(run).get("source_license"),
                "source_revisions": dataset_info(run).get("source_revisions", []),
                "dataset_lock": dataset_info(run).get("dataset_lock"),
                "dataset_lock_sha256": dataset_info(run).get("dataset_lock_sha256"),
                "dataset_lock_entry": dataset_info(run).get("dataset_lock_entry"),
                "dataset_lock_entry_sha256": _dataset_lock_entry_hash(dataset_info(run)),
                "manifest": run.get("manifest"),
                "manifest_sha256": dataset_info(run).get("manifest_sha256"),
                "reference_semantics": dataset_info(run).get("reference_semantics"),
                "test_split_status": dataset_info(run).get("test_split_status"),
                "speaker_split_relation": dataset_info(run).get("speaker_split_relation"),
                "model_revisions": {
                    alias: model.get("revision") for alias, model in run.get("models", {}).items()
                },
            }
            for run in runs
        ],
    }
    provenance_path = output_dir / "provenance.json"
    provenance_path.write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for run in runs:
        _write_errors(run, output_dir)

    def percent(value: Any) -> str:
        return f"{float(value):.2%}" if value is not None else "n/a"

    lines = [
        "# Danish STT benchmark report",
        "",
        "Scores are reported by dataset and evidence class. No overall average combines independent-ish, held-out in-domain, and controlled synthetic data.",
        "",
        "WER deltas are model A minus model B. A paired 95% interval that includes zero does not establish a difference.",
        "",
        "## Dataset and model results",
        "",
        "| Dataset | Evidence class | Model | Relationship | Decisive | WER interpretation | Clips | Speakers | Ref words | Content WER | Verbatim WER | CER | Speaker WER | RTF | DER .25 | Strict DER |",
        "|---|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary_rows:
        lines.append(
            f"| {row['dataset']} | {row['dataset_class']} | {row['model']} | "
            f"{row['evidence_status']} | {str(row['evidence_decisive']).lower()} | "
            f"{row['reference_semantics']} | "
            f"{row['recording_count']} | {row['speaker_count']} | "
            f"{row['reference_word_count'] if row['reference_word_count'] is not None else 'n/a'} | "
            f"{percent(row['content_wer'])} | {percent(row['verbatim_wer'])} | "
            f"{percent(row['cer'])} | {percent(row['speaker_attributed_wer'])} | "
            f"{row['rtf'] if row['rtf'] is not None else 'n/a'} | "
            f"{percent(row['global_der'])} | {percent(row['strict_der'])} |"
        )
    failed_models = [
        (dataset_info(run).get("name", "unknown"), alias, model)
        for run in runs
        for alias, model in run.get("models", {}).items()
        if not is_successful_model(model)
    ]
    if failed_models:
        lines.extend([
            "",
            "## Failed candidates",
            "",
            "These runs did not produce scored ASR predictions and are excluded from rankings and paired comparisons.",
            "",
            "| Dataset | Model | Failure |",
            "|---|---|---|",
        ])
        for dataset, alias, model in failed_models:
            failure = model.get("failure") or {}
            detail = ": ".join(
                value for value in (failure.get("class"), failure.get("message")) if value
            ) or "unspecified failure"
            lines.append(f"| {dataset} | {alias} | {detail} |")
    lines.extend([
        "",
        "`dataset-summary.csv` also records total audio, RTF, failures, OOMs, empty outputs, silence-hallucination counts when available, peak VRAM, and DER miss/false-alarm/confusion totals.",
        "",
    ])
    training_rows = [
        "",
        "## Model training data declarations and dataset evidence",
        "",
        "Relationships are model-by-dataset declarations. Recommendation logic uses only successful candidates marked decisive; all scores remain visible. Model revisions identify the exact evaluated checkpoint.",
        "",
        "| Model | Dataset | Status | Decisive | Evidence note |",
        "|---|---|---|---:|---|",
    ]
    for run in runs:
        dataset_name = dataset_info(run).get("name", "unknown")
        for alias in run.get("models", {}):
            relationship = model_dataset_evidence(run, alias)
            model_card = relationship.get("model_card") or relationship.get("source_url") or ""
            model_label = f"[{alias}]({model_card})" if model_card else alias
            training_rows.append(
                f"| {model_label} | {dataset_name} | "
                f"{relationship['status']} | {str(relationship['decisive']).lower()} | "
                f"{relationship['note']} |"
            )
    training_rows.extend([
        "",
        "No declared candidate fine-tuning overlap was found for Sam3; base-model pretraining exposure is unknown. The synthetic K=3 suite reuses audio-source families that overlap candidate training data, so its WER is not decisive.",
        "",
    ])
    lines.extend(training_rows)
    lines.extend(["", "## Pairwise uncertainty", ""])
    if pair_rows:
        lines.extend([
            "| Dataset | Model A | Model B | Δ WER | Paired 95% CI | Grouped by | Grouped 95% CI |",
            "|---|---|---|---:|---:|---|---:|",
        ])
        for row in pair_rows:
            group_ci = (
                f"[{row['group_ci95_low']:.3%}, {row['group_ci95_high']:.3%}]"
                if row.get("group_ci95_low") is not None else "n/a"
            )
            lines.append(
                f"| {row['dataset']} | {row['model_a']} | {row['model_b']} | "
                f"{row['delta_wer_a_minus_b']:.3%} | "
                f"[{row['ci95_low']:.3%}, {row['ci95_high']:.3%}] | "
                f"{row.get('group_unit') or 'n/a'} | {group_ci} |"
            )
    else:
        lines.append("No within-suite model pairs were available.")

    lines.extend(["", "## SamtaleBank Sam3 diagnostics", ""])
    if strata_rows:
        lines.append("`strata-summary.csv` reports content WER and DER by overlap, speech density, turn rate, and dominant-speaker balance. Turn rate is high for windows at or below the corpus median reference turn duration. Windows with a dominant speaker fraction above 70% are flagged.")
    else:
        lines.append("Sam3 results are not available yet.")

    lines.extend(["", "## Model decisions", ""])
    lines.extend(_decision_lines(runs, pair_rows))
    lines.extend([
        "",
        "Diarization is evaluated separately from ASR. Standard DER uses a 0.25 s collar, strict DER uses a 0 s collar, and overlap/non-overlap DER uses the same speaker mapping selected over each full recording.",
        "",
        "Dataset-specific contamination and provenance labels are in `provenance.json`. Sam3 content WER is chronological single-stream WER, so concurrent speech is not scored with an overlap-aware ordering metric.",
        "",
    ])
    if any(dataset_info(run).get("name") == "nst-da-test" for run in runs):
        lines.extend([
            "NST references are utterance-held-out; speakers overlap the broader training corpus, so the suite does not establish speaker-independent generalization.",
            "",
        ])
    report_path = output_dir / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "dataset_summary": summary_path,
        "pairwise_comparison": pairwise_path,
        "strata_summary": strata_path,
        "provenance": provenance_path,
        "report": report_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build dataset-aware Danish STT benchmark comparisons.")
    parser.add_argument("--results", nargs="+", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP_SAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    args = parser.parse_args(argv)
    try:
        outputs = generate_report(
            args.results, args.output_dir,
            bootstrap_samples=args.bootstrap_samples,
            seed=args.seed,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    for name, path in outputs.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
