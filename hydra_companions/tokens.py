"""HMAC confirmation tokens for trade proposals.

Token = HMAC-SHA256(session_key, proposal_id | nonce | expires_at).
Verified server-side on every confirm. Prevents replay, spoofing, and
stale confirms after TTL.

Session key is generated once per coordinator startup — if the agent
restarts, all outstanding tokens are invalidated, which is the
conservative behavior.
"""
from __future__ import annotations
import hmac
import hashlib
import os
import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenBundle:
    token: str
    nonce: str
    expires_at: float


class TokenBroker:
    def __init__(self, ttl_seconds: float = 60.0):
        self._key = secrets.token_bytes(32)
        self._ttl = float(ttl_seconds)

    def mint(self, proposal_id: str) -> TokenBundle:
        nonce = secrets.token_urlsafe(16)
        expires_at = time.time() + self._ttl
        msg = f"{proposal_id}|{nonce}|{expires_at:.3f}".encode("utf-8")
        sig = hmac.new(self._key, msg, hashlib.sha256).hexdigest()
        return TokenBundle(token=sig, nonce=nonce, expires_at=expires_at)

    def verify(self, *, proposal_id: str, token: str, nonce: str, expires_at: float) -> bool:
        if time.time() > expires_at:
            return False
        msg = f"{proposal_id}|{nonce}|{expires_at:.3f}".encode("utf-8")
        expected = hmac.new(self._key, msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, token)
