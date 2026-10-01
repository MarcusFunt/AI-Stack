from __future__ import annotations

import asyncio
import ipaddress
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from voice.turn_proxy import proxy


class TurnProxyTests(unittest.TestCase):
    def test_turn_proxy_acceptance_script_checks_security_and_stun(self):
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "test-turn-proxy.ps1"
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("voice_turn_listener_ready", script)
        self.assertIn("NetworkSettings.Ports", script)
        self.assertIn("3478/tcp", script)
        self.assertIn("CapEff", script)
        self.assertIn("CapBnd", script)
        self.assertIn("1.1.1.1", script)
        self.assertIn("443", script)
        self.assertNotIn(".Config.Env", script)
        self.assertNotIn(".env", script.lower())
        self.assertNotIn("tailscale serve", script.lower())

    def test_resolve_upstream_accepts_one_private_ipv4(self):
        answer = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.23.0.4", 3478))
        with patch("voice.turn_proxy.proxy.socket.getaddrinfo", return_value=[answer]):
            self.assertEqual(proxy.resolve_upstream("coturn", 3478), ipaddress.IPv4Address("10.23.0.4"))

    def test_resolve_upstream_rejects_public_loopback_unspecified_and_ambiguous_answers(self):
        answers = [
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 3478))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 3478))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("0.0.0.0", 3478))],
            [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.23.0.4", 3478)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.23.0.5", 3478)),
            ],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 3478))],
        ]
        for result in answers:
            with self.subTest(result=result), patch("voice.turn_proxy.proxy.socket.getaddrinfo", return_value=result):
                with self.assertRaises((ValueError, RuntimeError)):
                    proxy.resolve_upstream("coturn", 3478)

    def test_firewall_defaults_to_drop_and_allows_only_coturn_tcp(self):
        with patch("voice.turn_proxy.proxy.default_route_interface", return_value="eth1"):
            commands = proxy.build_firewall_rules(ipaddress.IPv4Address("172.24.0.6"))
        rendered = [" ".join(command) for command in commands]

        for family in ("iptables", "ip6tables"):
            for chain in ("INPUT", "OUTPUT", "FORWARD"):
                self.assertIn(f"{family} --wait -P {chain} DROP", rendered)
        outbound_new = [
            command
            for command in rendered
            if "-A OUTPUT" in command and "--ctstate NEW" in command
        ]
        self.assertEqual(len(outbound_new), 1)
        self.assertIn("-p tcp", outbound_new[0])
        self.assertIn("-d 172.24.0.6", outbound_new[0])
        self.assertIn("--dport 3478", outbound_new[0])
        self.assertNotIn("--dport 53", " ".join(rendered))
        self.assertNotIn("-A OUTPUT -j ACCEPT", rendered)
        inbound_new = [
            command
            for command in rendered
            if "-A INPUT" in command and "--ctstate NEW" in command
        ]
        self.assertEqual(len(inbound_new), 1)
        self.assertIn("-i eth1", inbound_new[0])

    def test_default_route_interface_selects_the_publish_bridge(self):
        route_table = """Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
eth0 0050A8C0 00000000 0001 0 0 0 00F0FFFF 0 0 0
eth1 00000000 0160A8C0 0003 0 0 0 00000000 0 0 0
"""

        self.assertEqual(proxy.default_route_interface(route_table), "eth1")

    def test_default_route_interface_rejects_missing_or_ambiguous_routes(self):
        for route_table in (
            "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n",
            """Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
eth0 00000000 0100000A 0003 0 0 0 00000000 0 0 0
eth1 00000000 0160A8C0 0003 0 0 0 00000000 0 0 0
""",
        ):
            with self.subTest(route_table=route_table):
                with self.assertRaises(RuntimeError):
                    proxy.default_route_interface(route_table)

    def test_drop_privilege_command_clears_all_capability_sets(self):
        command = proxy.drop_privilege_argv("/app/turn_proxy/proxy.py")

        self.assertEqual(command[0], "/usr/bin/setpriv")
        self.assertIn("--bounding-set=-all", command)
        self.assertIn("--inh-caps=-all", command)
        self.assertIn("--ambient-caps=-all", command)
        self.assertIn("--no-new-privs", command)
        self.assertIn("/app/turn_proxy/proxy.py", command)

    def test_startup_failure_prevents_proxy_exec_when_firewall_install_fails(self):
        with (
            patch.dict("os.environ", {"TURN_PROXY_ENABLED": "1"}),
            patch("voice.turn_proxy.proxy.resolve_upstream", return_value=ipaddress.IPv4Address("172.24.0.6")),
            patch("voice.turn_proxy.proxy.apply_firewall", side_effect=OSError("iptables failed")),
            patch("voice.turn_proxy.proxy.os.execvpe") as exec_process,
        ):
            with self.assertRaises(OSError):
                proxy.main([])
        exec_process.assert_not_called()

    def test_serve_refuses_to_bind_when_capabilities_remain(self):
        with (
            patch.dict("os.environ", {"TURN_PROXY_ENABLED": "1"}),
            patch("voice.turn_proxy.proxy.read_capability_masks", return_value=(1, 0)),
            patch("voice.turn_proxy.proxy.asyncio.run") as run_server,
        ):
            with self.assertRaises(RuntimeError):
                proxy.main(["--serve-ip", "172.24.0.6"])
        run_server.assert_not_called()

    def test_proxy_connection_forwards_bytes_and_closes_on_upstream_failure(self):
        async def exercise():
            completed = asyncio.Event()

            async def upstream_handler(reader, writer):
                payload = await reader.readexactly(4)
                writer.write(payload.upper())
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            upstream_server = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
            upstream_port = upstream_server.sockets[0].getsockname()[1]

            async def proxy_handler(reader, writer):
                try:
                    await proxy.proxy_connection(
                        reader,
                        writer,
                        upstream_ip="127.0.0.1",
                        upstream_port=upstream_port,
                    )
                finally:
                    completed.set()

            proxy_server = await asyncio.start_server(proxy_handler, "127.0.0.1", 0)
            proxy_port = proxy_server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(b"ping")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(4), timeout=2), b"PING")
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(completed.wait(), timeout=2)
            proxy_server.close()
            upstream_server.close()
            await proxy_server.wait_closed()
            await upstream_server.wait_closed()

            failure_completed = asyncio.Event()

            async def failing_proxy_handler(failure_reader, failure_writer):
                try:
                    async def refuse_upstream(*_args, **_kwargs):
                        raise ConnectionRefusedError("upstream unavailable")

                    with patch("voice.turn_proxy.proxy.asyncio.open_connection", new=refuse_upstream):
                        await proxy.proxy_connection(
                            failure_reader,
                            failure_writer,
                            upstream_ip="127.0.0.1",
                            upstream_port=3478,
                        )
                finally:
                    failure_completed.set()

            failing_server = await asyncio.start_server(failing_proxy_handler, "127.0.0.1", 0)
            failing_port = failing_server.sockets[0].getsockname()[1]
            failure_reader, failure_writer = await asyncio.open_connection("127.0.0.1", failing_port)
            await asyncio.wait_for(failure_completed.wait(), timeout=2)
            self.assertEqual(await asyncio.wait_for(failure_reader.read(), timeout=2), b"")
            failure_writer.close()
            await failure_writer.wait_closed()
            failing_server.close()
            await failing_server.wait_closed()

        asyncio.run(exercise())

    def test_proxy_connection_times_out_and_closes_when_upstream_stalls(self):
        async def exercise():
            completed = asyncio.Event()

            async def stalled_upstream(*_args, **_kwargs):
                await asyncio.Event().wait()

            async def proxy_handler(reader, writer):
                try:
                    with (
                        patch.object(proxy, "_UPSTREAM_CONNECT_TIMEOUT_SECONDS", 0.05, create=True),
                        patch("voice.turn_proxy.proxy.asyncio.open_connection", new=stalled_upstream),
                    ):
                        await proxy.proxy_connection(
                            reader,
                            writer,
                            upstream_ip="172.24.0.6",
                            upstream_port=3478,
                        )
                finally:
                    completed.set()

            server = await asyncio.start_server(proxy_handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await asyncio.wait_for(completed.wait(), timeout=2)
                self.assertEqual(await asyncio.wait_for(reader.read(), timeout=2), b"")
            finally:
                writer.close()
                await writer.wait_closed()
                server.close()
                await server.wait_closed()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
