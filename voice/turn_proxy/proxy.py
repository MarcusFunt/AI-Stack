from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Sequence


_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_LISTEN_PORT = 3478
_UPSTREAM_PORT = 3478
_BUFFER_SIZE = 64 * 1024
_UPSTREAM_CONNECT_TIMEOUT_SECONDS = 8
_DNS_RESOLUTION_ATTEMPTS = 8
_DNS_RETRY_INTERVAL_SECONDS = 0.25
_STUN_TIMEOUT_SECONDS = 2.0
_UPSTREAM_READY_ATTEMPTS = 4
_UPSTREAM_READY_RETRY_SECONDS = 1.0
_UPSTREAM_HEALTH_INTERVAL_SECONDS = 10.0
_UPSTREAM_HEALTH_FAILURE_LIMIT = 3
_STUN_MAGIC_COOKIE = 0x2112A442


def stun_binding_ready(host: str, port: int, *, timeout: float = _STUN_TIMEOUT_SECONDS) -> bool:
    """Return true only when a matching STUN Binding Success reaches this TCP endpoint."""
    transaction_id = secrets.token_bytes(12)
    request = struct.pack("!HHI", 0x0001, 0, _STUN_MAGIC_COOKIE) + transaction_id
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((str(host), port), timeout=timeout) as connection:
            connection.sendall(request)

            def receive_exactly(size: int) -> bytes | None:
                payload = bytearray()
                while len(payload) < size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    connection.settimeout(remaining)
                    part = connection.recv(size - len(payload))
                    if not part:
                        return None
                    payload.extend(part)
                return bytes(payload)

            header = receive_exactly(20)
            if header is None:
                return False
            message_type, body_length, cookie = struct.unpack("!HHI", header[:8])
            if (
                message_type != 0x0101
                or body_length > 4096
                or body_length % 4 != 0
                or cookie != _STUN_MAGIC_COOKIE
                or header[8:20] != transaction_id
            ):
                return False
            body = receive_exactly(body_length)
            if body is None:
                return False

        offset = 0
        has_address = False
        while offset < len(body):
            if len(body) - offset < 4:
                return False
            attribute_type, attribute_length = struct.unpack("!HH", body[offset:offset + 4])
            value_start = offset + 4
            value_end = value_start + attribute_length
            padded_end = value_start + ((attribute_length + 3) & ~3)
            if value_end > len(body) or padded_end > len(body):
                return False
            value = body[value_start:value_end]
            if attribute_type in (0x0001, 0x0020):
                expected_length = {1: 8, 2: 20}.get(value[1]) if len(value) >= 2 else None
                if len(value) < 2 or value[0] != 0 or attribute_length != expected_length:
                    return False
                has_address = True
            offset = padded_end
        return has_address
    except (OSError, struct.error, ValueError):
        return False


def _is_rfc1918(address: ipaddress.IPv4Address) -> bool:
    return any(address in network for network in _PRIVATE_NETWORKS)


def default_route_interface(route_table: str | None = None) -> str:
    """Return the single interface with the IPv4 default gateway."""
    if route_table is None:
        try:
            route_table = Path("/proc/net/route").read_text(encoding="ascii")
        except OSError as exc:
            raise RuntimeError("could not read the proxy IPv4 route table") from exc

    interfaces: set[str] = set()
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
        except ValueError as exc:
            raise RuntimeError("proxy IPv4 route table contains malformed flags") from exc
        if flags & 0x3 != 0x3:
            continue
        interface = fields[0]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface):
            raise RuntimeError("proxy default route has an invalid interface name")
        interfaces.add(interface)

    if len(interfaces) != 1:
        raise RuntimeError(f"expected exactly one default-route interface, got {len(interfaces)}")
    return next(iter(interfaces))


def resolve_upstream(host: str, port: int) -> ipaddress.IPv4Address:
    """Resolve a service to exactly one RFC1918 IPv4 address."""
    if not host or not host.strip():
        raise ValueError("upstream hostname is empty")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("upstream port must be between 1 and 65535")
    for attempt in range(_DNS_RESOLUTION_ATTEMPTS):
        try:
            answers = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
            break
        except OSError as exc:
            if attempt == _DNS_RESOLUTION_ATTEMPTS - 1:
                raise RuntimeError(f"could not resolve upstream {host!r}") from exc
            time.sleep(_DNS_RETRY_INTERVAL_SECONDS)

    addresses: set[ipaddress.IPv4Address] = set()
    for answer in answers:
        try:
            address = ipaddress.IPv4Address(answer[4][0])
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError("upstream returned a malformed IPv4 address") from exc
        if not _is_rfc1918(address):
            raise ValueError(f"upstream address is not RFC1918 private IPv4: {address}")
        addresses.add(address)

    if len(addresses) != 1:
        raise ValueError(f"expected exactly one private upstream IPv4 address, got {len(addresses)}")
    return next(iter(addresses))


def build_firewall_rules(
    upstream_ip: ipaddress.IPv4Address,
    *,
    listen_port: int = _LISTEN_PORT,
    upstream_port: int = _UPSTREAM_PORT,
) -> list[list[str]]:
    """Build fail-closed IPv4/IPv6 filter rules for the proxy namespace."""
    address = ipaddress.IPv4Address(upstream_ip)
    if not _is_rfc1918(address):
        raise ValueError("firewall upstream must be an RFC1918 private IPv4 address")
    listen_interface = default_route_interface()
    for port in (listen_port, upstream_port):
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("firewall ports must be between 1 and 65535")

    commands: list[list[str]] = []
    for binary in ("iptables", "ip6tables"):
        for chain in ("INPUT", "OUTPUT", "FORWARD"):
            commands.append([binary, "--wait", "-P", chain, "DROP"])
        for chain in ("INPUT", "OUTPUT", "FORWARD"):
            commands.append([binary, "--wait", "-F", chain])

    for binary in ("iptables", "ip6tables"):
        commands.extend(
            [
                [binary, "--wait", "-A", "INPUT", "-i", "lo", "-j", "ACCEPT"],
                [binary, "--wait", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
                [
                    binary,
                    "--wait",
                    "-A",
                    "INPUT",
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "ESTABLISHED,RELATED",
                    "-j",
                    "ACCEPT",
                ],
                [
                    binary,
                    "--wait",
                    "-A",
                    "OUTPUT",
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "ESTABLISHED,RELATED",
                    "-j",
                    "ACCEPT",
                ],
            ]
        )
    commands.extend(
        [
            [
                "iptables",
                "--wait",
                "-A",
                "INPUT",
                "-i",
                listen_interface,
                "-p",
                "tcp",
                "--dport",
                str(listen_port),
                "-m",
                "conntrack",
                "--ctstate",
                "NEW",
                "-j",
                "ACCEPT",
            ],
            [
                "iptables",
                "--wait",
                "-A",
                "OUTPUT",
                "-p",
                "tcp",
                "-d",
                str(address),
                "--dport",
                str(upstream_port),
                "-m",
                "conntrack",
                "--ctstate",
                "NEW",
                "-j",
                "ACCEPT",
            ],
        ]
    )
    return commands


def apply_firewall(upstream_ip: ipaddress.IPv4Address) -> None:
    for command in build_firewall_rules(upstream_ip):
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


def drop_privilege_argv(script_path: str) -> list[str]:
    return [
        "/usr/bin/setpriv",
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--no-new-privs",
        "--",
        sys.executable,
        script_path,
    ]


def read_capability_masks(status_path: str = "/proc/self/status") -> tuple[int, int]:
    values: dict[str, int] = {}
    with open(status_path, encoding="ascii") as status_file:
        for line in status_file:
            key, separator, value = line.partition(":")
            if separator and key in {"CapEff", "CapBnd"}:
                values[key] = int(value.strip(), 16)
    if set(values) != {"CapEff", "CapBnd"}:
        raise RuntimeError("could not read effective and bounding capability masks")
    return values["CapEff"], values["CapBnd"]


def verify_no_capabilities() -> None:
    effective, bounding = read_capability_masks()
    if effective != 0 or bounding != 0:
        raise RuntimeError(
            f"refusing to listen with capabilities remaining (effective={effective:x}, bounding={bounding:x})"
        )


async def _copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(_BUFFER_SIZE):
            writer.write(chunk)
            await writer.drain()
    finally:
        try:
            if not writer.is_closing() and writer.can_write_eof():
                writer.write_eof()
                await writer.drain()
        except (ConnectionError, OSError):
            pass


async def proxy_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    upstream_ip: str,
    upstream_port: int,
) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    copy_tasks: list[asyncio.Task[None]] = []
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(upstream_ip, upstream_port),
            timeout=_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
        )
        copy_tasks = [
            asyncio.create_task(_copy_stream(client_reader, upstream_writer)),
            asyncio.create_task(_copy_stream(upstream_reader, client_writer)),
        ]
        await asyncio.gather(*copy_tasks)
    except (ConnectionError, OSError, asyncio.TimeoutError):
        for task in copy_tasks:
            if not task.done():
                task.cancel()
        if copy_tasks:
            await asyncio.gather(*copy_tasks, return_exceptions=True)
    finally:
        writers: Sequence[asyncio.StreamWriter] = (
            (client_writer, upstream_writer) if upstream_writer is not None else (client_writer,)
        )
        for writer in writers:
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in writers), return_exceptions=True)


async def _serve(upstream_ip: ipaddress.IPv4Address) -> None:
    await _wait_for_upstream_ready(upstream_ip)
    server = await asyncio.start_server(
        lambda reader, writer: proxy_connection(
            reader,
            writer,
            upstream_ip=str(upstream_ip),
            upstream_port=_UPSTREAM_PORT,
        ),
        host="0.0.0.0",
        port=_LISTEN_PORT,
        family=socket.AF_INET,
    )
    async with server:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(server.serve_forever())
            tasks.create_task(_watch_upstream(upstream_ip))


async def _wait_for_upstream_ready(upstream_ip: ipaddress.IPv4Address) -> None:
    for attempt in range(_UPSTREAM_READY_ATTEMPTS):
        if await asyncio.to_thread(stun_binding_ready, str(upstream_ip), _UPSTREAM_PORT):
            return
        if attempt + 1 < _UPSTREAM_READY_ATTEMPTS:
            await asyncio.sleep(_UPSTREAM_READY_RETRY_SECONDS)
    raise RuntimeError("coturn upstream did not pass the TCP STUN readiness check")


async def _watch_upstream(
    upstream_ip: ipaddress.IPv4Address,
    *,
    interval: float = _UPSTREAM_HEALTH_INTERVAL_SECONDS,
    failures: int = _UPSTREAM_HEALTH_FAILURE_LIMIT,
) -> None:
    consecutive_failures = 0
    while True:
        await asyncio.sleep(interval)
        ready = await asyncio.to_thread(stun_binding_ready, str(upstream_ip), _UPSTREAM_PORT)
        if ready:
            consecutive_failures = 0
            continue
        consecutive_failures += 1
        if consecutive_failures >= failures:
            raise RuntimeError("upstream STUN health failed; exiting so the container can restart")


def _exec_unprivileged(upstream_ip: ipaddress.IPv4Address) -> None:
    script_path = os.path.abspath(__file__)
    command = drop_privilege_argv(script_path)
    command.extend(["--serve-ip", str(upstream_ip)])
    os.execvpe(command[0], command, os.environ.copy())
    raise RuntimeError("setpriv exec unexpectedly returned")


def _enabled() -> bool:
    return os.environ.get("TURN_PROXY_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private coturn TCP forwarding proxy")
    parser.add_argument("--serve-ip")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args(argv)

    if args.healthcheck:
        return 0 if stun_binding_ready("127.0.0.1", _LISTEN_PORT) else 1

    if not _enabled():
        return 0

    if args.serve_ip is None:
        upstream_ip = resolve_upstream("coturn", _UPSTREAM_PORT)
        apply_firewall(upstream_ip)
        _exec_unprivileged(upstream_ip)
        return 1

    upstream_ip = ipaddress.IPv4Address(args.serve_ip)
    if not _is_rfc1918(upstream_ip):
        raise ValueError("serve address must be an RFC1918 private IPv4 address")
    verify_no_capabilities()
    asyncio.run(_serve(upstream_ip))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
