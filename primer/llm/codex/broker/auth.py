"""Dedicated SIWC registration, signed identity validation and rotation.

Management-side API only. Never expose its methods on Primer's model socket.
No personal Codex credentials, env tokens, client secret or API-key fallback.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt

from primer.llm.codex.boundary import BoundaryError
from .http import AUTHORIZE, DISCOVERY, ISSUER, RESOURCE, TOKEN, FixedHTTP
from .security import PrivateStore, SignedPolicy, binding, require, text

SCOPES = frozenset({"openid", "profile", "email", "offline_access", "resource.invoke", "chatgpt.tokens.use.direct"})


class SignIn:
    """One registration per broker store, isolated by issued client + subject.

    A real management listener/UI must bind its loopback port before begin().
    This API never launches a browser, starts a listener or imports credentials.
    """
    def __init__(self, store: PrivateStore, policy: SignedPolicy, http: FixedHTTP):
        self._store, self._policy, self._http = store, policy, http
        self._pending = None
        self._candidate = None
        self._candidate_deadline = 0.0
        self._lock = asyncio.Lock()
        self._record = store.read("registration")
        self._selected = None
        self._continuation = store.read("client_hint")
        if self._continuation is not None:
            require(set(self._continuation) == {"client_id"})
            require(text(self._continuation["client_id"]) != "dynamic_agent_client")
        revocations = store.read("revocations") or {"items": []}
        require(set(revocations) == {"items"} and type(revocations["items"]) is list
                and len(revocations["items"]) <= 32)
        self._revocations = revocations["items"]
        for item in self._revocations:
            require(type(item) is dict and set(item) == {"client_id", "refresh_token"})
            text(item["client_id"])
            text(item["refresh_token"], 65536)
        if self._record is not None:
            if self._record.get("reauthorization_required") is True:
                require(set(self._record) == {"reauthorization_required", "subject", "client_id", "account"})
                self._selected = {key: self._record[key] for key in ("subject", "client_id", "account")}
                require(self._selected["account"] == binding(text(self._selected["subject"]), text(self._selected["client_id"])))
                self._record = None
            else:
                self._validate_record(self._record)
                self._selected = {key: self._record[key] for key in ("subject", "client_id", "account")}
        host = store.read("host")
        if host is None:
            host = {"id": secrets.token_urlsafe(32)}
            store.write("host", host)
        require(set(host) == {"id"})
        self._host = text(host["id"])

    @staticmethod
    def _validate_record(record: dict) -> None:
        require(set(record) == {"subject", "client_id", "account", "access_token", "refresh_token", "id_token",
                                "expires_at", "scopes", "generation", "nonce"})
        for name in ("subject", "client_id", "account", "access_token", "refresh_token", "id_token", "generation", "nonce"):
            text(record[name], 65536)
        require(record["client_id"] != "dynamic_agent_client"
                and record["account"] == binding(record["subject"], record["client_id"])
                and type(record["expires_at"]) in {int, float}
                and 0 < record["expires_at"] < float("inf")
                and type(record["scopes"]) is list and SCOPES <= set(record["scopes"]))

    def begin(self, callback_uri: str) -> str:
        self._policy.grant()  # Before even constructing an authorization URL.
        require(not self._lock.locked() and self._candidate is None and not self._revocations)
        uri = urlsplit(callback_uri)
        require(uri.scheme == "http" and uri.hostname == "127.0.0.1"
                and uri.netloc == f"127.0.0.1:{uri.port}" and uri.port is not None
                and uri.path == "/auth/callback" and not uri.query and not uri.fragment)
        pending = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
                   "verifier": secrets.token_urlsafe(48), "redirect": callback_uri,
                   "expires": time.monotonic() + 300,
                   "client": (self._selected or self._continuation or {}).get("client_id")}
        self._pending = pending
        params = {"client_id": pending["client"] or "dynamic_agent_client", "ext_agent_host_id": self._host,
                  "response_type": "code", "redirect_uri": callback_uri, "scope": " ".join(sorted(SCOPES)),
                  "resource": RESOURCE, "state": pending["state"], "nonce": pending["nonce"],
                  "code_challenge_method": "S256", "code_challenge": base64.urlsafe_b64encode(
                      hashlib.sha256(pending["verifier"].encode()).digest()).decode().rstrip("=")}
        if pending["client"] is None:
            params["agent_name_hint"] = "Primer"
        # Hints deliberately omitted: no credential is ever put into a URL.
        return AUTHORIZE + "?" + urlencode(params)

    async def _identity(self, token: str, client: str, nonce: str | None) -> str:
        metadata = await self._http.json(DISCOVERY)
        require(metadata.get("issuer") == ISSUER and metadata.get("token_endpoint") == TOKEN
                and metadata.get("authorization_endpoint") == AUTHORIZE)
        uri = metadata.get("jwks_uri")
        require(type(uri) is str and self._http._auth_metadata_url(uri))
        jwks = await self._http.json(uri)
        try:
            header = jwt.get_unverified_header(token)
            require(header.get("alg") == "RS256" and not set(header) & {"jku", "jwk", "x5u", "crit"})
            keys = [key for key in jwks["keys"] if key.get("kid") == header.get("kid")]
            require(type(header.get("kid")) is str and len(keys) == 1)
            key = keys[0]
            require(key.get("kty") == "RSA" and key.get("use", "sig") == "sig"
                    and key.get("alg", "RS256") == "RS256" and "d" not in key
                    and key.get("key_ops", ["verify"]) == ["verify"])
            claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=["RS256"],
                                audience=client, issuer=ISSUER,
                                options={"require": ["iss", "aud", "exp", "iat", "sub"]})
            require(claims.get("azp", client) == client)
            if type(claims["aud"]) is list and len(claims["aud"]) > 1:
                require(claims.get("azp") == client)
            if nonce is not None:
                require(type(claims.get("nonce")) is str and secrets.compare_digest(claims["nonce"], nonce))
            return text(claims["sub"])
        except Exception:
            raise BoundaryError("SIWC signed identity validation failed") from None

    async def _tokens(self, result: dict, client: str, nonce: str, *, previous: dict | None = None) -> dict:
        require(result.get("token_type", "").lower() == "bearer"
                and type(result.get("expires_in")) is int and 0 < result["expires_in"] <= 86400)
        scopes = frozenset(text(result.get("scope")).split())
        require(SCOPES <= scopes)
        access, refresh = text(result.get("access_token"), 65536), text(result.get("refresh_token"), 65536)
        identity = result.get("id_token")
        if identity is None and previous is not None:
            subject, identity = previous["subject"], previous["id_token"]
        else:
            identity = text(identity, 65536)
            subject = await self._identity(identity, client, None if previous is not None else nonce)
        account = binding(subject, client)
        if self._selected is not None:
            require(account == self._selected["account"])
        if previous is not None:
            self._policy.grant(account)
        record = {"subject": subject, "client_id": client, "account": account, "access_token": access,
                  "refresh_token": refresh, "id_token": identity, "expires_at": time.time() + result["expires_in"],
                  "scopes": sorted(scopes), "generation": secrets.token_hex(16), "nonce": nonce}
        self._validate_record(record)
        return record

    async def complete(self, callback_query: str) -> dict:
        try:
            return await self._complete(callback_query)
        except Exception:
            raise BoundaryError("SIWC authorization attempt refused") from None

    async def _complete(self, callback_query: str) -> dict:
        async with self._lock:
            pending = self._pending
            require(pending is not None and pending["expires"] > time.monotonic())
            require(len(callback_query) <= 16384)
            values = parse_qs(callback_query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
            require(not set(values) - {"state", "code", "client_id", "scope", "error", "error_description"}
                    and all(len(v) == 1 for v in values.values()))
            require(secrets.compare_digest(values.get("state", [""])[0], pending["state"]))
            # Only a well-formed matching callback consumes the attempt. This
            # precedes denial handling and all exchange I/O: replay is refused.
            self._pending = None
            require("error" not in values)
            client = values.get("client_id", [pending["client"]])[0]
            text(client)
            require(client != "dynamic_agent_client" and (pending["client"] is None or client == pending["client"]))
            self._policy.grant()
            if self._selected is None:
                # Continuation is NOT verified identity or an authority grant.
                # Persist before exchange so invalid_grant/restart can reuse it.
                self._continuation = {"client_id": client}
                self._store.write("client_hint", self._continuation)
            result = await self._http.json(TOKEN, form={"grant_type": "authorization_code", "client_id": client,
                "code": text(values.get("code", [None])[0]), "code_verifier": pending["verifier"],
                "redirect_uri": pending["redirect"], "resource": RESOURCE})
            record = await self._tokens(result, client, pending["nonce"])
            self._candidate = record
            self._candidate_deadline = time.monotonic() + 300
            self._policy.grant()  # If revoked, candidate remains for explicit disposal.
            return {"issuer": ISSUER, **{key: record[key] for key in ("subject", "client_id", "account")}}

    def commit_registration(self) -> str:
        """Management-only explicit consent + current signed account grant.

        The safe identity returned by complete() lets an administrator authorize
        a previously unknown opaque binding. Staged tokens are never inferable
        over IPC and remain unusable until this operation succeeds.
        """
        require(not self._lock.locked() and self._candidate is not None
                and self._candidate_deadline > time.monotonic()
                and self._candidate["expires_at"] > time.time())
        record = self._candidate
        self._policy.grant(record["account"])
        self._store.write("registration", record)
        self._candidate = None
        self._record = record
        self._selected = {key: record[key] for key in ("subject", "client_id", "account")}
        return record["account"]

    def snapshot(self):
        require(self._record is not None)
        record = self._record
        return self._policy.snapshot(record["account"], record["generation"], record["expires_at"])

    async def credential(self, account: str) -> tuple[str, str]:
        """Broker-private only. Serialize rotation; never retry unknown outcomes."""
        async with self._lock:
            require(self._record is not None and account == self._record["account"])
            self._policy.grant(account)
            record = self._record
            if record["expires_at"] <= time.time() + 60:
                # Any failed/cancelled/unknown refresh requires explicit reauth.
                # Persistently quarantine the old rotating credential BEFORE I/O.
                self._record = None
                self._store.write("registration", {"reauthorization_required": True, **self._selected})
                try:
                    result = await self._http.json(TOKEN, form={"grant_type": "refresh_token",
                        "client_id": record["client_id"], "refresh_token": record["refresh_token"], "resource": RESOURCE})
                    updated = await self._tokens(result, record["client_id"], record["nonce"], previous=record)
                    self._store.write("registration", updated)
                    self._record = updated
                except BaseException:
                    self._record = None
                    raise
                record = updated
            require(record["expires_at"] > time.time())
            self._policy.grant(account)
            return record["access_token"], record["generation"]

    async def sign_out(self) -> None:
        """Disable locally and revoke active AND staged renewable sessions.

        Failed/unconfirmed revocations remain in a private, non-authorizing
        queue for explicit retry. New enrollment refuses until disposal is
        confirmed. Verified registration identity survives sign-out/restart.
        """
        async with self._lock:
            record, candidate = self._record, self._candidate
            self._record = self._candidate = self._pending = None
            if self._selected is None and candidate is not None:
                self._selected = {key: candidate[key] for key in ("subject", "client_id", "account")}
            for source in (record, candidate):
                if source is not None:
                    item = {key: source[key] for key in ("client_id", "refresh_token")}
                    if item not in self._revocations:
                        self._revocations.append(item)
            if self._selected is not None:
                self._store.write("registration", {"reauthorization_required": True, **self._selected})
            self._store.write("revocations", {"items": self._revocations})
            if not self._revocations:
                return
            try:
                # Absolute bound includes metadata and every revocation, not
                # just each socket read. Cancellation leaves the queue intact.
                async with asyncio.timeout(10):
                    metadata = await self._http.json(DISCOVERY)
                    require(metadata.get("issuer") == ISSUER)
                    endpoint = text(metadata.get("revocation_endpoint"))
                    for item in tuple(self._revocations):
                        try:
                            await self._http.revoke(endpoint, item["client_id"], item["refresh_token"])
                        except Exception:
                            continue  # Attempt other distinct sessions too.
                        self._revocations.remove(item)
                        self._store.write("revocations", {"items": self._revocations})
                require(not self._revocations)
            except Exception:
                raise BoundaryError("SIWC remote revocation unconfirmed; local credentials disabled") from None
