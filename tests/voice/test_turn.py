from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
from dataclasses import asdict

from voice.transports.turn import client_ice_servers, make_turn_credentials


class TurnCredentialTests(unittest.TestCase):
    def test_turn_rest_credentials_expire_after_five_minutes(self):
        credentials = make_turn_credentials(
            "test-shared-secret", now=1_700_000_000, ttl_seconds=300, nonce="call-123"
        )
        username = "1700000300:call-123"
        expected = base64.b64encode(
            hmac.new(
                b"test-shared-secret", username.encode("utf-8"), hashlib.sha1
            ).digest()
        ).decode("ascii")

        self.assertEqual(credentials.username, username)
        self.assertEqual(credentials.expires_at, 1_700_000_300)
        self.assertEqual(credentials.credential, expected)

    def test_turn_credentials_use_random_nonce_and_never_return_shared_secret(self):
        shared_secret = "private-turn-secret"
        first = make_turn_credentials(shared_secret, now=1_700_000_000)
        second = make_turn_credentials(shared_secret, now=1_700_000_000)

        self.assertNotEqual(first.username, second.username)
        self.assertNotIn(shared_secret, repr(asdict(first)))
        self.assertNotIn(shared_secret, repr(asdict(second)))
        self.assertNotEqual(first.credential, shared_secret)

    def test_client_ice_servers_use_tls_tcp_and_fail_closed_without_hostname(self):
        credentials = make_turn_credentials(
            "test-shared-secret", now=1_700_000_000, nonce="call-123"
        )

        self.assertEqual(
            client_ice_servers(credentials, hostname="voice.example.ts.net"),
            [{
                "urls": "turns:voice.example.ts.net:8447?transport=tcp",
                "username": credentials.username,
                "credential": credentials.credential,
            }],
        )
        self.assertEqual(client_ice_servers(credentials, hostname=""), [])


if __name__ == "__main__":
    unittest.main()
