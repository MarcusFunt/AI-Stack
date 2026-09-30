"""Public STT dataset adapters and manifest utilities."""

from .base import DATASET_CLASSES, DatasetAdapter, DatasetSpec
from .chat import ChatTimedSegment, ChatTranscript, ChatUtterance, normalize_chat_text, parse_chat_file, parse_chat_text
from .manifest import load_manifest, validate_manifest, write_manifest
from .provenance import build_lock_entry, file_sha256, load_lock, update_lock
from .samtalebank import (
    SamtaleBankSam3Adapter,
    build_windows,
    cut_wav_window,
    extract_audio_to_wav,
    prepare_samtalebank,
    window_metrics,
)
from .synthetic import DiarizationK3Adapter, prepare_diarization_k3

__all__ = [
    "DATASET_CLASSES",
    "ChatTimedSegment",
    "ChatTranscript",
    "ChatUtterance",
    "DatasetAdapter",
    "DatasetSpec",
    "SamtaleBankSam3Adapter",
    "DiarizationK3Adapter",
    "build_lock_entry",
    "build_windows",
    "cut_wav_window",
    "extract_audio_to_wav",
    "file_sha256",
    "load_lock",
    "load_manifest",
    "normalize_chat_text",
    "parse_chat_file",
    "parse_chat_text",
    "prepare_samtalebank",
    "prepare_diarization_k3",
    "update_lock",
    "validate_manifest",
    "window_metrics",
    "write_manifest",
]
