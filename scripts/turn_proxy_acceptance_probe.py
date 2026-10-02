from __future__ import annotations

import argparse
from pathlib import Path
import socket
import sys


def _host_stun_probe() -> int:
    scripts_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(scripts_dir))
    from host_agent import voice_turn_listener_ready

    ready = voice_turn_listener_ready()
    print("Loopback STUN: " + ("passed" if ready else "failed"))
    return 0 if ready else 1


def _container_probe() -> int:
    values = {}
    for line in Path("/proc/1/status").read_text(encoding="ascii").splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"CapEff", "CapBnd"}:
            values[key] = int(value.strip(), 16)
    if set(values) != {"CapEff", "CapBnd"}:
        print("PID 1 capability masks are unavailable")
        return 1
    print("PID 1 CapEff={:x} CapBnd={:x}".format(values["CapEff"], values["CapBnd"]))
    if values["CapEff"] != 0 or values["CapBnd"] != 0:
        return 1

    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(3)
    try:
        connection.connect(("1.1.1.1", 443))
    except OSError:
        print("External TCP egress: blocked")
        return 0
    finally:
        connection.close()
    print("External TCP egress: unexpectedly allowed")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="TURN proxy acceptance probe")
    parser.add_argument("mode", choices=("host-stun", "container", "self-test"))
    args = parser.parse_args()
    if args.mode == "host-stun":
        return _host_stun_probe()
    if args.mode == "container":
        return _container_probe()
    print("PowerShell/Python probe handoff: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
