from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable


class InvocationCancelled(Exception):
    """Raised when an invocation's cancellation token has been cancelled."""


class CancellationToken:
    """Thread-safe, one-shot cancellation signal that can link to a parent."""

    def __init__(self, *, parent: CancellationToken | None = None) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._callbacks: list[Callable[[str | None], None]] = []
        if parent is not None:
            parent.add_callback(self.cancel)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def cancel(self, reason: str | None = None) -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._event.set()
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback(reason)
            except Exception:
                continue
        return True

    def add_callback(self, callback: Callable[[str | None], None]) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            if self._event.is_set():
                reason = self._reason
            else:
                self._callbacks.append(callback)
                return
        callback(reason)

    async def wait(self) -> str | None:
        await asyncio.to_thread(self._event.wait)
        return self.reason

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise InvocationCancelled(self.reason or "invocation cancelled")

    def child(self) -> CancellationToken:
        return CancellationToken(parent=self)
