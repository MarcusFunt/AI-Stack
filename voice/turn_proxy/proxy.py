from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
from pathlib import Path
import re
import socket
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
        await server.serve_forever()


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
    args = parser.parse_args(argv)

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
