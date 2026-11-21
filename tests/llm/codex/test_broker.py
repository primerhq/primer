"""Synthetic signed identities and mock TLS transport; never real login/data."""
import asyncio
import base64
import json
import os
import socket
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from primer.llm.codex.boundary import BoundaryError
from primer.llm.codex.broker.auth import SCOPES, SignIn
from primer.llm.codex.broker.http import AUTHORIZE, DISCOVERY, ISSUER, MODELS, RESOURCE, RESPONSES, TOKEN, FixedHTTP
from primer.llm.codex.broker.security import PrivateStore, SignedPolicy, binding
from primer.llm.codex.broker.service import Broker, BrokerServer
from primer.llm.codex.request import compact, prepare_request
from tests.llm.codex.test_bridge import frames, message


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class World:
    def __init__(self, directory, key):
        directory.chmod(0o700)
        self.store = PrivateStore(directory)
        self.key = key
        self.admin = ed25519.Ed25519PrivateKey.generate()
        self.account = binding("dedicated-subject", "issued-client")
        self.document = {"audience": "test-broker", "revision": "1", "expires_at": time.time() + 3600,
                         "accounts": [self.account], "models": ["model"], "required_native_effects": [],
                         "suspended": False, "enrollment": True}
        self.bundle = {}
        self.sign()
        self.policy = SignedPolicy({"administrator": self.admin.public_key().public_bytes_raw()}, "test-broker", lambda: self.bundle)
        self.calls = []
        self.claim_changes = {}
        self.token_changes = {}
        self.fail_refresh = False
        self.catalog = [{"slug": "model", "display_name": "Test model", "visibility": "list"},
                        {"slug": "hidden", "display_name": "Hidden", "visibility": "hide"}]
        self.http = FixedHTTP(_test_transport=httpx.MockTransport(self.respond))
        self.auth = SignIn(self.store, self.policy, self.http)
        self.broker = Broker(self.auth, self.http)
        self.nonce = None
        self.initial = None

    def sign(self):
        payload = compact(self.document)
        self.bundle["administrator"] = {"payload": payload, "signature": base64.b64encode(self.admin.sign(payload.encode())).decode()}

    async def respond(self, request):
        self.calls.append(request)
        url = str(request.url)
        if url == DISCOVERY:
            return httpx.Response(200, json={"issuer": ISSUER, "token_endpoint": TOKEN,
                "authorization_endpoint": AUTHORIZE, "jwks_uri": ISSUER + "/.well-known/jwks.json",
                "revocation_endpoint": ISSUER + "/api/accounts/oauth/revoke"})
        if url == ISSUER + "/.well-known/jwks.json":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "fixture", "alg": "RS256", "use": "sig"}]})
        if url == TOKEN:
            form = parse_qs(request.content.decode())
            assert form["resource"] == [RESOURCE] and form["client_id"] == ["issued-client"]
            if form["grant_type"] == ["refresh_token"]:
                if self.fail_refresh:
                    return httpx.Response(503, text="dedicated-test-access MUST NOT LEAK")
                result = {**self.initial, "access_token": "rotated-access", "refresh_token": "rotated-refresh"}
                result.pop("id_token", None)
                return httpx.Response(200, json=result)
            assert form["redirect_uri"] == ["http://127.0.0.1:12345/auth/callback"]
            assert len(form["code_verifier"][0]) >= 43
            claims = {"iss": ISSUER, "sub": "dedicated-subject", "aud": "issued-client",
                      "exp": int(time.time()) + 300, "iat": int(time.time()), "nonce": self.nonce, **self.claim_changes}
            result = {"token_type": "Bearer", "expires_in": 3600, "scope": " ".join(SCOPES),
                      "access_token": "dedicated-test-access", "refresh_token": "dedicated-test-refresh",
                      "id_token": jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "fixture"}), **self.token_changes}
            self.initial = result
            return httpx.Response(200, json=result)
        if url == ISSUER + "/api/accounts/oauth/revoke":
            assert parse_qs(request.content.decode())["token_type_hint"] == ["refresh_token"]
            return httpx.Response(200)
        if url == MODELS:
            assert request.headers["authorization"] in {"Bearer dedicated-test-access", "Bearer rotated-access"}
            return httpx.Response(200, json={"models": self.catalog})
        if url == RESPONSES:
            data = b"".join(b"data: " + compact(frame).encode() + b"\n\n" for frame in frames())
            return httpx.Response(200, content=data, headers={"content-type": "text/event-stream"})
        raise AssertionError("Unexpected outbound endpoint")

    def start(self):
        url = self.auth.begin("http://127.0.0.1:12345/auth/callback")
        params = parse_qs(urlsplit(url).query)
        self.nonce = params["nonce"][0]
        return params

    async def login(self):
        params = self.start()
        await self.auth.complete(urlencode({"state": params["state"][0], "code": "dedicated-test-code", "client_id": "issued-client"}))
        return self.auth.commit_registration()

    async def close(self):
        await self.broker.close()
        self.store.close()


@pytest.fixture
async def world(tmp_path, rsa_key):
    instance = World(tmp_path, rsa_key)
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_registration_signed_identity_discovery_and_actual_bridge(world):
    assert await world.login() == world.account
    assert await world.broker.models(world.account) == [{"slug": "model", "display_name": "Test model"}]
    output = [e async for e in world.broker.step(world.account, prepare_request("model", [message()]))]
    assert output[-1]["type"] == "done" and not world.broker._bridges
    persisted = world.store.read("registration")
    assert persisted["account"] == world.account and persisted["client_id"] == "issued-client"
    assert world.start()["client_id"] == ["issued-client"]
    assert "agent_name_hint" not in world.start() and "id_token_hint" not in world.start()
    assert oct(os.stat(world.store._fd).st_mode & 0o777) == "0o700"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"iss": "https://evil.invalid"}, {"aud": "other"},
    {"exp": 1}, {"nonce": "wrong"}, {"sub": "other-account"}, {"azp": "other"},
    {"aud": ["issued-client", "other"]}])
async def test_identity_substitution_refused_before_storage(world, changes):
    world.claim_changes = changes
    with pytest.raises(BoundaryError):
        await world.login()
    assert world.store.read("registration") is None
    assert not any(str(r.url) == MODELS for r in world.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"scope": "openid profile email"}, {"token_type": "MAC"},
    {"expires_in": True}, {"expires_in": 0}, {"id_token": "forged.unsigned.token"}, {"refresh_token": ""}])
async def test_invalid_token_receipt_never_activates(world, changes):
    world.token_changes = changes
    with pytest.raises(BoundaryError):
        await world.login()
    assert world.store.read("registration") is None


@pytest.mark.asyncio
async def test_state_duplicate_denial_and_consumption_before_http(world):
    params = world.start()
    for query in ["state=wrong&code=x&client_id=issued-client",
                  f'state={params["state"][0]}&state=duplicate&code=x',
                  f'state={params["state"][0]}&error=access_denied']:
        with pytest.raises(BoundaryError):
            await world.auth.complete(query)
    assert world.calls == []
    await world.login()
    before = len(world.calls)
    params = world.start()
    with pytest.raises(BoundaryError):
        await world.auth.complete(urlencode({"state": params["state"][0], "code": "x", "client_id": "other-client"}))
    assert len(world.calls) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "signature", "expired", "native", "suspended", "audience"])
async def test_administrator_policy_refuses_before_authorization_or_network(world, change):
    if change == "missing":
        world.bundle.clear()
    elif change == "signature":
        world.bundle["administrator"]["signature"] = base64.b64encode(b"x" * 64).decode()
    else:
        key, value = {"expired": ("expires_at", 1), "native": ("required_native_effects", ["shell"]),
                      "suspended": ("suspended", True), "audience": ("audience", "other")}[change]
        world.document[key] = value
        world.sign()
    with pytest.raises(BoundaryError):
        world.start()
    assert world.calls == []


@pytest.mark.asyncio
async def test_all_authorities_intersect_and_unknown_requirements_fail(world):
    second = ed25519.Ed25519PrivateKey.generate()
    second_doc = {**world.document, "models": ["other"]}
    payload = compact(second_doc)
    bundle = {**world.bundle, "enterprise": {"payload": payload, "signature": base64.b64encode(second.sign(payload.encode())).decode()}}
    policy = SignedPolicy({"administrator": world.admin.public_key().public_bytes_raw(),
                           "enterprise": second.public_key().public_bytes_raw()}, "test-broker", lambda: bundle)
    assert policy.grant(world.account)[2] == frozenset()
    del bundle["enterprise"]
    with pytest.raises(BoundaryError):
        policy.grant(world.account)
    world.document["unknown_requirement"] = True
    world.sign()
    with pytest.raises(BoundaryError):
        world.policy.grant()


@pytest.mark.asyncio
async def test_serial_refresh_and_quarantined_unknown_outcome(world):
    await world.login()
    world.auth._record["expires_at"] = time.time() + 5
    old_revision = world.auth.snapshot().revision
    results = await asyncio.gather(world.auth.credential(world.account), world.auth.credential(world.account))
    assert results[0] == results[1] and results[0][0] == "rotated-access"
    assert world.auth.snapshot().revision != old_revision
    assert sum(b"grant_type=refresh_token" in r.content for r in world.calls) == 1
    world.auth._record["expires_at"] = time.time() + 5
    world.fail_refresh = True
    with pytest.raises(BoundaryError, match="upstream"):
        await world.auth.credential(world.account)
    assert world.auth._record is None
    assert world.store.read("registration") == {"reauthorization_required": True, **world.auth._selected}
    before = len(world.calls)
    with pytest.raises(BoundaryError):
        await world.auth.credential(world.account)
    assert len(world.calls) == before
    restored = SignIn(world.store, world.policy, world.http)
    assert parse_qs(urlsplit(restored.begin("http://127.0.0.1:12345/auth/callback")).query)["client_id"] == ["issued-client"]


@pytest.mark.asyncio
async def test_account_and_native_request_refused_before_transport(world):
    await world.login()
    before = len(world.calls)
    with pytest.raises(BoundaryError):
        await world.broker.models("other")
    from primer.llm.codex.request import ModelRequest
    invalid = ModelRequest(compact({"model": "model", "tools": [{"type": "shell"}], "input": [], "store": False, "stream": True}), ())
    with pytest.raises(BoundaryError):
        _ = [e async for e in world.broker.step(world.account, invalid)]
    assert len(world.calls) == before


@pytest.mark.asyncio
async def test_policy_revoked_between_stream_events_unwinds(world):
    await world.login()
    stream = world.broker.step(world.account, prepare_request("model", [message()]))
    assert (await anext(stream))["type"] == "text_delta"
    world.document["suspended"] = True
    world.sign()
    with pytest.raises(BoundaryError):
        await anext(stream)
    assert not world.broker._bridges


@pytest.mark.asyncio
async def test_fixed_transport_rejects_redirect_and_off_origin(world):
    with pytest.raises(BoundaryError):
        await world.http.json("https://evil.invalid/models", bearer="dedicated-test-access")
    assert world.calls == []
    http = FixedHTTP(_test_transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={"location": "https://evil.invalid"})))
    try:
        with pytest.raises(BoundaryError):
            await http.json(MODELS, bearer="dedicated-test-access")
    finally:
        await http.close()


def test_private_store_permissions_symlink_and_single_writer(tmp_path):
    tmp_path.chmod(0o700)
    store = PrivateStore(tmp_path)
    try:
        with pytest.raises(BoundaryError):
            PrivateStore(tmp_path)
        store.write("host", {"id": "one"})
        assert (tmp_path / "host.json").stat().st_mode & 0o777 == 0o600
        (tmp_path / "registration.json").symlink_to(tmp_path / "host.json")
        with pytest.raises((BoundaryError, OSError)):
            store.read("registration")
        with pytest.raises(BoundaryError):
            store.read("../outside")
    finally:
        store.close()
    tmp_path.chmod(0o755)
    with pytest.raises(BoundaryError):
        PrivateStore(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["auth/login", "auth/token", "config/reload", "command/exec", "mcp/call", "models/list"])
async def test_actual_unix_peer_protocol_has_no_auth_or_raw_escape(world, tmp_path, method):
    await world.login()
    path = str(tmp_path / "ipc.sock")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(path)
    sock.listen()
    server = BrokerServer(world.broker, peer_uid=os.getuid(), account=world.account)
    listener = await server.serve(sock)
    try:
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write((compact({"method": method, "params": {}}) + "\n").encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 2)
        assert b"dedicated-test-access" not in raw and b"dedicated-test-refresh" not in raw
        if method == "models/list":
            assert json.loads(raw)["models"][0]["slug"] == "model"
        else:
            assert json.loads(raw) == {"error": "broker_refused"}
        writer.close()
        await writer.wait_closed()
    finally:
        listener.close()
        await listener.wait_closed()
        await server.close()


@pytest.mark.asyncio
async def test_wrong_os_peer_gets_no_output(world, tmp_path):
    path = str(tmp_path / "peer.sock")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(path)
    sock.listen()
    server = BrokerServer(world.broker, peer_uid=os.getuid() + 1, account=world.account)
    listener = await server.serve(sock)
    try:
        reader, writer = await asyncio.open_unix_connection(path)
        assert await asyncio.wait_for(reader.read(), 1) == b""
        assert world.calls == []
        writer.close()
        await writer.wait_closed()
    finally:
        listener.close()
        await listener.wait_closed()
        await server.close()


@pytest.mark.asyncio
async def test_signout_removes_tokens_retains_registration_and_refuses_stream(world):
    await world.login()
    await world.auth.sign_out()
    assert world.auth._record is None
    assert "access_token" not in world.store.read("registration")
    with pytest.raises(BoundaryError):
        await world.broker.models(world.account)
    assert world.start()["client_id"] == ["issued-client"]


@pytest.mark.asyncio
async def test_cancelled_refresh_persists_quarantine_before_network(world):
    await world.login()
    world.auth._record["expires_at"] = time.time() + 1
    entered = asyncio.Event()
    original = world.http.json
    async def wait(url, **kwargs):
        if url == TOKEN:
            assert world.auth._record is None
            assert "refresh_token" not in world.store.read("registration")
            entered.set()
            await asyncio.Future()
        return await original(url, **kwargs)
    world.http.json = wait
    task = asyncio.create_task(world.auth.credential(world.account))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.auth._record is None
    assert world.start()["client_id"] == ["issued-client"]


@pytest.mark.asyncio
async def test_metadata_cannot_redirect_jwks_off_origin(world):
    original = world.http.json
    async def metadata(url, **kwargs):
        result = await original(url, **kwargs)
        if url == DISCOVERY:
            result["jwks_uri"] = "https://evil.invalid/jwks"
        return result
    world.http.json = metadata
    with pytest.raises(BoundaryError):
        await world.login()
    assert not any(str(r.url).startswith("https://evil.invalid") for r in world.calls)
    assert world.store.read("registration") is None


@pytest.mark.asyncio
async def test_peer_disconnect_cancels_and_drains_actual_model_channel(world, tmp_path):
    await world.login()
    entered, closed = asyncio.Event(), asyncio.Event()
    async def hold(wire, bearer):
        try:
            entered.set()
            await asyncio.Future()
            yield {}  # Async-generator shape; unreachable.
        finally:
            closed.set()
    world.http.events = hold
    path = str(tmp_path / "cancel.sock")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(path)
    sock.listen()
    server = BrokerServer(world.broker, peer_uid=os.getuid(), account=world.account)
    listener = await server.serve(sock)
    writer = None
    try:
        reader, writer = await asyncio.open_unix_connection(path)
        request = prepare_request("model", [message()])
        writer.write((compact({"method": "step/start", "params": {"request": request.wire(), "aliases": []}}) + "\n").encode())
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 1)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(closed.wait(), 1)
        # Explicit shutdown also joins the connection's owned finalization.
        await asyncio.wait_for(server.close(), 1)
        assert not server._active and not world.broker._bridges
    finally:
        if writer is not None:
            writer.close()
        listener.close()
        await listener.wait_closed()
        await server.close()


@pytest.mark.asyncio
async def test_new_identity_can_be_approved_after_validation_without_token_exposure(world):
    world.document["accounts"] = []
    world.sign()
    params = world.start()
    result = await world.auth.complete(urlencode({"state": params["state"][0], "code": "x", "client_id": "issued-client"}))
    assert result == {"issuer": ISSUER, "subject": "dedicated-subject", "client_id": "issued-client", "account": world.account}
    assert world.store.read("registration") is None
    with pytest.raises(BoundaryError):
        await world.auth.credential(world.account)
    with pytest.raises(BoundaryError):
        world.auth.commit_registration()
    world.document["accounts"] = [result["account"]]
    world.sign()
    assert world.auth.commit_registration() == world.account
    assert world.auth._candidate is None
    with pytest.raises(BoundaryError):
        world.auth.commit_registration()


@pytest.mark.asyncio
async def test_staged_identity_expires_or_signout_clears_it(world):
    params = world.start()
    await world.auth.complete(urlencode({"state": params["state"][0], "code": "x", "client_id": "issued-client"}))
    world.auth._candidate_deadline = 0
    with pytest.raises(BoundaryError):
        world.auth.commit_registration()
    await world.auth.sign_out()
    assert world.auth._candidate is None and world.store.read("registration") is None
