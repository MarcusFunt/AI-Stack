from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence

if __package__:
    from .proxy import stun_binding_ready
else:  # Docker copies this beside turn_proxy_health.py as a standalone wrapper.
    from turn_proxy_health import stun_binding_ready


def _stop_child(process: subprocess.Popen, *, grace_seconds: float = 8.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def supervise(
    command: Sequence[str],
    *,
    probe: Callable[[], bool] | None = None,
    interval: float = 5.0,
    startup_failure_limit: int = 12,
    health_failure_limit: int = 3,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Exit nonzero on a dead or persistently unhealthy coturn child for Docker to restart."""
    if not command:
        raise ValueError("coturn command is empty")
    readiness_probe = probe or (lambda: stun_binding_ready("127.0.0.1", 3478))
    process = popen(list(command))
    stopping = False
    previous_handlers: dict[int, object] = {}

    if threading.current_thread() is threading.main_thread():
        def request_stop(_signum, _frame):
            nonlocal stopping
            stopping = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, request_stop)

    ready_once = False
    failures = 0
    try:
        while True:
            if stopping:
                _stop_child(process)
                return 0
            return_code = process.poll()
            if return_code is not None:
                return return_code if return_code != 0 else 1

            if readiness_probe():
                ready_once = True
                failures = 0
            else:
                failures += 1
                limit = health_failure_limit if ready_once else startup_failure_limit
                if failures >= limit:
                    _stop_child(process)
                    return 1
            sleep(interval)
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main(argv: Sequence[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if command and command[0] == "--":
        command = command[1:]
    return supervise(command)


if __name__ == "__main__":
    raise SystemExit(main())
