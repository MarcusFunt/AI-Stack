from __future__ import annotations

import math
import threading
from collections import Counter


COUNTERS = (
    "embedding_requests_total",
    "embedding_failures_total",
    "index_records_total",
    "index_vectors_total",
    "duplicate_exact_total",
    "duplicate_perceptual_total",
    "duplicate_embedding_total",
    "context_pack_images",
    "context_pack_text_chunks",
)
BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
HISTOGRAMS = (
    "embedding_latency_seconds",
    "image_preprocessing_seconds",
    "visual_search_latency_seconds",
)


class VisualMemoryMetrics:
    """Thread-safe, low-cardinality process metrics. No caller values become labels."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters = Counter({name: 0 for name in COUNTERS})
        self._histograms = {name: [] for name in HISTOGRAMS}

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in COUNTERS or isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("unknown counter or invalid increment")
        with self._lock:
            self._counters[name] += amount

    def observe(self, name: str, value: float) -> None:
        if name not in HISTOGRAMS or not math.isfinite(float(value)) or value < 0:
            raise ValueError("unknown histogram or invalid observation")
        with self._lock:
            self._histograms[name].append(float(value))

    def counter(self, name: str) -> int:
        if name not in COUNTERS:
            raise ValueError("unknown counter")
        with self._lock:
            return int(self._counters[name])

    def percentiles(self, name: str) -> dict[str, float | None]:
        if name not in HISTOGRAMS:
            raise ValueError("unknown histogram")
        with self._lock:
            values = sorted(self._histograms[name])
        if not values:
            return {"p50": None, "p95": None}
        def quantile(fraction: float) -> float:
            index = max(0, min(len(values) - 1, math.ceil(fraction * len(values)) - 1))
            return values[index]
        return {"p50": quantile(0.50), "p95": quantile(0.95)}

    def snapshot(self) -> dict:
        with self._lock:
            counters = dict(self._counters)
            histograms = {name: list(values) for name, values in self._histograms.items()}
        percentiles = {
            name: self._percentiles_from_values(values)
            for name, values in histograms.items()
        }
        return {"counters": counters, "percentiles_seconds": percentiles}

    @staticmethod
    def _percentiles_from_values(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"p50": None, "p95": None}
        ordered = sorted(values)
        return {
            "p50": ordered[max(0, math.ceil(0.50 * len(ordered)) - 1)],
            "p95": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
        }

    def render_prometheus(self) -> str:
        snapshot = self.snapshot()
        lines = []
        for name in COUNTERS:
            lines.extend([f"# TYPE {name} counter", f"{name} {snapshot['counters'][name]}"])
        with self._lock:
            histogram_values = {name: list(values) for name, values in self._histograms.items()}
        for name in HISTOGRAMS:
            values = histogram_values[name]
            lines.append(f"# TYPE {name} histogram")
            for bound in BUCKETS:
                count = sum(value <= bound for value in values)
                lines.append(f'{name}_bucket{{le="{bound:g}"}} {count}')
            lines.append(f'{name}_bucket{{le="+Inf"}} {len(values)}')
            lines.append(f"{name}_sum {sum(values):.9g}")
            lines.append(f"{name}_count {len(values)}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters = Counter({name: 0 for name in COUNTERS})
            self._histograms = {name: [] for name in HISTOGRAMS}


METRICS = VisualMemoryMetrics()
