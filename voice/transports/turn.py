from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TurnCredentials:
    username: str
    credential: str
    expires_at: int


def make_turn_credentials(
    secret: str,
    *,
    now: int | None = None,
    ttl_seconds: int = 300,
    nonce: str | None = None,
) -> TurnCredentials:
    if not secret:
        raise ValueError("TURN shared secret is required")
    if ttl_seconds <= 0:
        raise ValueError("TURN credential TTL must be positive")
    nonce = nonce or secrets.token_urlsafe(18)
    if ":" in nonce:
        raise ValueError("TURN credential nonce cannot contain ':'")

    expires_at = (int(time.time()) if now is None else int(now)) + int(ttl_seconds)
    username = f"{expires_at}:{nonce}"
    digest = hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1)
    credential = base64.b64encode(digest.digest()).decode("ascii")
    return TurnCredentials(username=username, credential=credential, expires_at=expires_at)


def client_ice_servers(
    credentials: TurnCredentials,
    *,
    hostname: str,
    port: int = 8446,
) -> list[dict[str, object]]:
    hostname = hostname.strip().rstrip(".")
    if not hostname or not credentials.username or not credentials.credential:
        return []
    if not 1 <= port <= 65_535:
        raise ValueError("TURN port must be between 1 and 65535")
    return [{
        "urls": f"turns:{hostname}:{port}?transport=tcp",
        "username": credentials.username,
        "credential": credentials.credential,
    }]
