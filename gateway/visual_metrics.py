from __future__ import annotations

import threading


class GatewayVisualMetrics:
    """Low-cardinality gateway counters for contextual VLM routing."""

    NAMES = ("qwen_escalations_total", "qwen_escalations_avoided_total")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = {name: 0 for name in self.NAMES}

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in self.NAMES or isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("unknown counter or invalid increment")
        with self._lock:
            self._values[name] += amount

    def render_prometheus(self) -> str:
        with self._lock:
            values = dict(self._values)
        lines = []
        for name in self.NAMES:
            lines.extend([f"# TYPE {name} counter", f"{name} {values[name]}"])
        return "\n".join(lines) + "\n"

    def counter(self, name: str) -> int:
        if name not in self.NAMES:
            raise ValueError("unknown counter")
        with self._lock:
            return self._values[name]

    def reset(self) -> None:
        with self._lock:
            self._values = {name: 0 for name in self.NAMES}


VISUAL_METRICS = GatewayVisualMetrics()
