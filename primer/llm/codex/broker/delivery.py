"""Ephemeral receipts for reauthorizing terminal delivery, never credentials.

Receipts confer no offline authority: each delivery must return to the same
broker instance for a fresh signed-policy and registration check. No per-step
session table survives abandoned clients; restart invalidates all receipts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets
import time

from primer.llm.codex.boundary import PolicySnapshot
from primer.llm.codex.request import ModelRequest, compact, decode

from .security import require


def digest(value: object) -> str:
    return hashlib.sha256(compact(value).encode()).hexdigest()


def request_digest(request: ModelRequest) -> str:
    return digest([request.wire(), request.aliases])


def step_digest(request: ModelRequest, nonce: str) -> str:
    require(type(nonce) is str and len(nonce) == 64 and all(c in "0123456789abcdef" for c in nonce))
    return digest([request_digest(request), nonce])


def _policy_digest(policy: PolicySnapshot) -> str:
    return digest([policy.account, policy.revision, policy.expires_at,
                   sorted(policy.models), sorted(policy.required_native_effects), policy.suspended])


class DeliveryReceipts:
    def __init__(self):
        self._key = secrets.token_bytes(32)

    def seal(self, *, account: str, model: str, policy: PolicySnapshot,
             request: str, event: dict, deadline: float) -> str:
        body = compact({"version": 1, "account": account, "model": model,
                        "policy": _policy_digest(policy), "request": request,
                        "event": digest(event), "deadline": deadline}).encode()
        signature = hmac.new(self._key, body, hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(body).decode() + "." + signature

    def authorize(self, receipt: str, *, account: str, request: str, event: str,
                  current: PolicySnapshot) -> dict[str, float]:
        require(type(receipt) is str and len(receipt) <= 8192 and receipt.count(".") == 1)
        encoded, signature = receipt.split(".")
        require(len(signature) == 64 and signature.isascii())
        body = base64.b64decode(encoded, altchars=b"-_", validate=True)
        require(hmac.compare_digest(signature, hmac.new(self._key, body, hashlib.sha256).hexdigest()))
        grant = decode(body)
        require(type(grant) is dict and set(grant) == {
            "version", "account", "model", "policy", "request", "event", "deadline",
        })
        require(grant["version"] == 1 and grant["account"] == account
                and type(request) is str and len(request) == 64 and grant["request"] == request
                and type(event) is str and len(event) == 64 and grant["event"] == event
                and type(grant["deadline"]) in {int, float} and math.isfinite(grant["deadline"])
                and time.monotonic() < grant["deadline"] and _policy_digest(current) == grant["policy"])
        current.admit(account, grant["model"])
        return {"expires_at": current.expires_at, "deadline": grant["deadline"]}
