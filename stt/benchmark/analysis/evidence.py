"""Model-specific relationships between candidates and benchmark datasets."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


MODEL_METADATA = {
    "edda": {
        "model_id": "danish-foundation-models/edda-v0.1",
        "model_card": "https://huggingface.co/danish-foundation-models/edda-v0.1",
    },
    "saga2": {
        "model_id": "capacit-ai/saga-2-m",
        "model_card": "https://huggingface.co/capacit-ai/saga-2-m",
    },
    "hviske": {
        "model_id": "syvai/hviske-v6",
        "model_card": "https://huggingface.co/syvai/hviske-v6",
    },
}


MODEL_DATASET_EVIDENCE = {
    "edda": {
        "fleurs-da-dk-test": {
            "status": "declared_train_split_only",
            "decisive": True,
            "source": "model_card",
            "note": "Training used declared training splits; the released checkpoint was not selected from evaluation performance.",
        },
        "coral-conversation-test": {
            "status": "training_family_overlap_test_overlap_unknown",
            "decisive": False,
            "source": "model_card",
            "note": "CoRal is a declared training family; test-specific overlap has not been established.",
        },
        "nst-da-test": {
            "status": "training_family_overlap_test_overlap_unknown",
            "decisive": False,
            "source": "model_card",
            "note": "NST Danish is a declared training family; utterance holdout does not establish speaker independence.",
        },
        "samtalebank-sam3": {
            "status": "possible_pretraining_overlap",
            "decisive": False,
            "source": "model_card",
            "note": "No declared Sam3 fine-tuning overlap; base-model pretraining exposure is unknown.",
        },
        "diarization-k3": {
            "status": "controlled_synthetic_overlap",
            "decisive": False,
            "source": "benchmark_design",
            "note": "Synthetic source families overlap candidate training data; ASR scores are diagnostic only.",
        },
    },
    "saga2": {
        "fleurs-da-dk-test": {
            "status": "checkpoint_selection_overlap",
            "decisive": False,
            "source": "model_card",
            "note": "Part of the da_dk test split was used during checkpoint selection.",
        },
        "coral-conversation-test": {
            "status": "training_family_overlap_test_overlap_unknown",
            "decisive": False,
            "source": "model_card",
            "note": "CoRal is a declared training family; test-specific overlap has not been established.",
        },
        "nst-da-test": {
            "status": "unknown",
            "decisive": False,
            "source": "model_card",
            "note": "The public declaration does not establish test-utterance and speaker separation.",
        },
        "samtalebank-sam3": {
            "status": "possible_pretraining_overlap",
            "decisive": False,
            "source": "model_card",
            "note": "No declared Sam3 fine-tuning overlap; base-model pretraining exposure is unknown.",
        },
        "diarization-k3": {
            "status": "controlled_synthetic_overlap",
            "decisive": False,
            "source": "benchmark_design",
            "note": "Synthetic source families overlap candidate training data; ASR scores are diagnostic only.",
        },
    },
    "hviske": {
        "fleurs-da-dk-test": {
            "status": "declared_hash_excluded_test",
            "decisive": True,
            "source": "model_card",
            "note": "The benchmark test sets were excluded from training by transcript hash.",
        },
        "coral-conversation-test": {
            "status": "training_family_overlap_test_overlap_unknown",
            "decisive": False,
            "source": "model_card",
            "note": "CoRal is a declared training family; test-specific overlap has not been established.",
        },
        "nst-da-test": {
            "status": "unknown",
            "decisive": False,
            "source": "model_card",
            "note": "The public declaration does not establish test-utterance and speaker separation.",
        },
        "samtalebank-sam3": {
            "status": "possible_pretraining_overlap",
            "decisive": False,
            "source": "model_card",
            "note": "No declared Sam3 fine-tuning overlap; base-model pretraining exposure is unknown.",
        },
        "diarization-k3": {
            "status": "controlled_synthetic_overlap",
            "decisive": False,
            "source": "benchmark_design",
            "note": "Synthetic source families overlap candidate training data; ASR scores are diagnostic only.",
        },
    },
}


_FROZEN_EVIDENCE_FIELDS = {
    "dataset", "status", "decisive", "source", "note", "model_revision",
}


def build_model_dataset_evidence(result: dict[str, Any], alias: str) -> dict[str, Any]:
    """Build evidence for a newly generated run or a pre-v3 legacy result."""
    dataset = result.get("dataset") if isinstance(result.get("dataset"), dict) else {}
    dataset_name = str(dataset.get("name", "unknown"))
    model = result.get("models", {}).get(alias, {})
    model_meta = MODEL_METADATA.get(alias, {})
    evidence = MODEL_DATASET_EVIDENCE.get(alias, {}).get(dataset_name)
    if evidence is None:
        evidence = {
            "status": "unknown",
            "decisive": False,
            "source": "unresolved",
            "note": "No candidate-specific evidence is recorded for this dataset.",
        }
    return {
        "model_id": model_meta.get("model_id", model.get("repo")),
        "model_card": model_meta.get("model_card"),
        "dataset": dataset_name,
        **evidence,
        "source_url": model_meta.get("model_card"),
        "model_revision": model.get("revision"),
    }


def _schema_version(result: dict[str, Any]) -> int:
    try:
        return int(result.get("schema_version", 0))
    except (TypeError, ValueError):
        return 0


def _stored_model_evidence(result: dict[str, Any], alias: str) -> dict[str, Any]:
    models = result.get("models", {})
    model = models.get(alias) if isinstance(models, dict) else None
    if not isinstance(model, dict):
        raise ValueError(f"schema-v3 result is missing a valid model result for {alias}")
    evidence = model.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError(f"schema-v3 result is missing valid model evidence for {alias}")
    missing = sorted(_FROZEN_EVIDENCE_FIELDS - evidence.keys())
    if missing:
        raise ValueError(
            f"schema-v3 result has incomplete model evidence for {alias}; "
            f"missing: {', '.join(missing)}"
        )
    if not isinstance(evidence.get("dataset"), str) or not evidence["dataset"].strip():
        raise ValueError(f"schema-v3 result has invalid evidence dataset for {alias}")
    dataset = result.get("dataset") if isinstance(result.get("dataset"), dict) else {}
    dataset_name = dataset.get("name")
    if dataset_name and evidence["dataset"] != dataset_name:
        raise ValueError(
            f"schema-v3 result evidence dataset for {alias} does not match the result dataset"
        )
    if not isinstance(evidence.get("status"), str) or not evidence["status"].strip():
        raise ValueError(f"schema-v3 result has invalid evidence status for {alias}")
    if not isinstance(evidence.get("decisive"), bool):
        raise ValueError(f"schema-v3 result has invalid evidence decisiveness for {alias}")
    if not isinstance(evidence.get("source"), str) or not isinstance(evidence.get("note"), str):
        raise ValueError(f"schema-v3 result has invalid evidence source or note for {alias}")
    return deepcopy(evidence)


def model_dataset_evidence(result: dict[str, Any], alias: str) -> dict[str, Any]:
    """Read frozen evidence, using current declarations only for legacy results."""
    models = result.get("models", {})
    model = models.get(alias) if isinstance(models, dict) else None
    if not isinstance(model, dict):
        if _schema_version(result) >= 3:
            raise ValueError(f"schema-v3 result is missing a valid model result for {alias}")
        model = {}
    if "evidence" in model:
        return _stored_model_evidence(result, alias)
    if _schema_version(result) >= 3:
        raise ValueError(f"schema-v3 result is missing model evidence for {alias}")
    return build_model_dataset_evidence(result, alias)
