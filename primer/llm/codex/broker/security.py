"""Explicit administrator trust roots and broker-exclusive credential storage."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import stat
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from primer.llm.codex.boundary import BoundaryError, PolicySnapshot
from primer.llm.codex.request import compact, decode

MAX_RECORD = 262144


def require(condition: bool) -> None:
    if not condition:
        raise BoundaryError("Broker security validation refused")


def text(value: object, maximum: int = 4096) -> str:
    require(type(value) is str and 0 < len(value) <= maximum and all(ord(c) >= 32 for c in value))
    return value


def binding(subject: str, client: str) -> str:
    return hashlib.sha256(compact(["https://auth.openai.com", subject, client]).encode()).hexdigest()


class PrivateStore:
    """One broker process owns one pre-provisioned 0700 directory, for life.

    No search of HOME/CODEX_HOME, import, fallback or implicit creation. The
    broker OS identity and all path ancestors must be provisioned by the host.
    Same-UID code is trusted, not isolated by file modes.
    """
    def __init__(self, directory: Path):
        self._fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._lock = None
        try:
            info = os.fstat(self._fd)
            require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700)
            self._lock = os.open("broker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self._fd)
            self._check(self._lock)
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise BoundaryError("Broker store unavailable") from None

    @staticmethod
    def _check(fd: int) -> None:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)

    @staticmethod
    def _name(name: str) -> str:
        require(name in {"registration", "host", "client_hint", "revocations", "policy_ledger", "policy_session"})
        return name + ".json"

    def require_live(self) -> None:
        # dir_fd=None otherwise silently falls back to cwd.
        require(self._fd is not None and self._lock is not None)

    @property
    def identity(self) -> tuple[int, int]:
        self.require_live()
        info = os.fstat(self._fd)
        return info.st_dev, info.st_ino

    def read(self, name: str) -> dict | None:
        self.require_live()
        try:
            fd = os.open(self._name(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
        except FileNotFoundError:
            return None
        try:
            self._check(fd)
            with os.fdopen(fd, "rb", closefd=False) as file:
                raw = file.read(MAX_RECORD + 1)
            require(len(raw) <= MAX_RECORD)
            value = decode(raw)
            require(type(value) is dict)
            return value
        except Exception:
            raise BoundaryError("Broker credential record invalid") from None
        finally:
            os.close(fd)

    def write(self, name: str, value: dict) -> None:
        self.require_live()
        import secrets
        raw = compact(value).encode()
        require(len(raw) <= MAX_RECORD)
        target = self._name(name)
        temporary = ".pending-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
        try:
            with os.fdopen(fd, "wb", closefd=False) as file:
                file.write(raw)
                file.flush()
                os.fsync(fd)
            os.replace(temporary, target, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            os.fsync(self._fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self._fd)
            except FileNotFoundError:
                pass

    def close(self) -> None:
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class SignedPolicy:
    """All configured authorities must sign an applicable short-lived grant.

    Public keys and complete authority inventory come from trusted host setup,
    never an RPC/config claim. This implements verification/intersection; it
    does NOT discover enterprise authorities or attest the inventory is genuine.
    """
    def __init__(self, keys: dict[str, bytes], audience: str, load):
        require(bool(keys) and len(set(keys.values())) == len(keys))
        self._keys = {text(k): Ed25519PublicKey.from_public_bytes(v) for k, v in keys.items()}
        self._audience = text(audience)
        self._load = load

    def grant(self, account: str | None = None) -> tuple[str, float, frozenset[str]]:
        try:
            bundle = self._load()
            require(type(bundle) is dict and set(bundle) == set(self._keys))
            revisions, expiry, models = [], float("inf"), None
            for ident, key in self._keys.items():
                envelope = bundle[ident]
                require(type(envelope) is dict and set(envelope) == {"payload", "signature"})
                raw = text(envelope["payload"], MAX_RECORD).encode()
                key.verify(base64.b64decode(envelope["signature"], validate=True), raw)
                doc = decode(raw)
                require(set(doc) == {"audience", "revision", "expires_at", "accounts", "models", "required_native_effects", "suspended", "enrollment"})
                require(doc["audience"] == self._audience and type(doc["expires_at"]) in {int, float}
                        and time.time() < doc["expires_at"] <= time.time() + 86400)
                text(doc["revision"])
                require(doc["suspended"] is False and doc["required_native_effects"] == []
                        and type(doc["accounts"]) is list and type(doc["models"]) is list)
                accounts = frozenset(text(v) for v in doc["accounts"])
                allowed = frozenset(text(v) for v in doc["models"])
                require(account in accounts if account else doc["enrollment"] is True)
                expiry = min(expiry, doc["expires_at"])
                models = allowed if models is None else models & allowed
                revisions.append([ident, hashlib.sha256(raw).hexdigest()])
            return hashlib.sha256(compact(sorted(revisions)).encode()).hexdigest(), expiry, models
        except Exception:
            raise BoundaryError("Administrator policy missing, invalid or incompatible") from None

    def snapshot(self, account: str, generation: str, credential_expiry: float) -> PolicySnapshot:
        revision, expiry, models = self.grant(account)
        return PolicySnapshot(account, revision + ":" + generation, min(expiry, credential_expiry), models)
