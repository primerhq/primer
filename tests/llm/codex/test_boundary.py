"""Actual bridge admission controls; no model/auth/service/DB access."""
from dataclasses import replace

import pytest

from primer.llm.codex.boundary import (
    BoundaryError, BridgeConfig, PolicySnapshot, UnavailablePolicyAuthority,
    open_app_server,
)
from primer.llm.codex.bridge import ModelOnlyBridge
from primer.llm.codex.harness import RecordedChannel, RecordedPolicy, dispatch
from primer.llm.codex.request import ModelRequest, compact, prepare_request
from primer.model.chat import Message, TextPart


def request():
    return prepare_request("test-model", [Message(role="user", parts=[TextPart(text="hello")])])


@pytest.mark.parametrize("kind", ["shell", "apply_patch", "hooks", "notify", "shell_snapshot", "mcp",
                                 "managed_mcp", "cloud_mcp", "plugin", "code_mode", "refresh", "reload",
                                 "prewarm", "image", "web", "subagent", "future_unknown"])
@pytest.mark.asyncio
async def test_no_native_construction(kind, monkeypatch):
    import primer.llm.codex.harness as harness
    monkeypatch.setattr(harness, "RecordedChannel", lambda _: pytest.fail("constructed"))
    with pytest.raises(BoundaryError):
        await dispatch({"id": 1, "method": kind, "params": {}}, [])


@pytest.mark.parametrize("config", [{"mcp_servers": {}}, {"hooks": []}, {"executable": "/usr/bin/codex"},
                                     {"config": {}}, {"refresh": True}, {"native_tools": []},
                                     {"protocol": True}, {"max_calls": 0}, {"deadline_seconds": float("nan")},
                                     {"cleanup_seconds": False}])
def test_hostile_config_refused(config):
    with pytest.raises(BoundaryError):
        BridgeConfig.from_mapping(config)


def test_stock_app_server_never_constructed(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("native process started"))
    with pytest.raises(BoundaryError):
        open_app_server("codex", tool_policy=[], flags=["--read-only"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["expired", "account", "suspended", "model", "mandatory", "missing"])
async def test_policy_fails_before_transport_constructor(mutation):
    policy = RecordedPolicy("test-model")
    fields = {"expired": {"expires_at": 0}, "account": {"account": "other"},
              "suspended": {"suspended": True}, "model": {"models": frozenset()},
              "mandatory": {"required_native_effects": frozenset({"managed_mcp"})},
              "missing": {"revision": ""}}[mutation]
    policy.snapshot = replace(policy.snapshot, **fields)
    constructed = []
    bridge = ModelOnlyBridge(authority=policy, account="offline", transport_factory=lambda wire: constructed.append(wire))
    with pytest.raises(BoundaryError):
        _ = [e async for e in bridge.stream(request())]
    assert not constructed


@pytest.mark.asyncio
async def test_unavailable_live_policy_is_closed():
    bridge = ModelOnlyBridge(authority=UnavailablePolicyAuthority(), account="offline",
                             transport_factory=lambda _: pytest.fail("constructed"))
    with pytest.raises(BoundaryError, match="No verified"):
        _ = [e async for e in bridge.stream(request())]


@pytest.mark.asyncio
@pytest.mark.parametrize("native", ["shell", "apply_patch", "mcp", "web_search", "code_interpreter", "image_generation"])
async def test_native_request_refused_before_constructor(native):
    wire = request().wire()
    wire["tools"] = [{"type": native}]
    bridge = ModelOnlyBridge(authority=RecordedPolicy("test-model"), account="offline",
                             transport_factory=lambda _: pytest.fail("constructed"))
    with pytest.raises(BoundaryError):
        _ = [e async for e in bridge.stream(ModelRequest(compact(wire), ()))]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["command/exec", "process/spawn", "config/write", "thread/start", "thread/resume",
                                   "thread/fork", "mcpServer/refresh", "account/login/start", "fs/writeFile",
                                   "plugin/install", "turn/start"])
async def test_rpc_escape_refused_before_channel_constructor(method, monkeypatch):
    import primer.llm.codex.harness as harness
    monkeypatch.setattr(harness, "RecordedChannel", lambda _: pytest.fail("constructed"))
    with pytest.raises(BoundaryError):
        await dispatch({"id": 1, "method": method, "params": {}}, [])


@pytest.mark.asyncio
async def test_constructor_canary_positive_then_denied(tmp_path):
    """Positive control proves the marker would be written if reached."""
    marker = tmp_path / "constructor"
    def constructor(wire):
        marker.write_text("constructed")
        return RecordedChannel([])
    constructor({})
    assert marker.read_text() == "constructed"
    marker.unlink()
    policy = RecordedPolicy("test-model")
    policy.snapshot = replace(policy.snapshot, required_native_effects=frozenset({"startup_hook"}))
    bridge = ModelOnlyBridge(authority=policy, account="offline", transport_factory=constructor)
    with pytest.raises(BoundaryError):
        _ = [e async for e in bridge.stream(request())]
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["shell", "patch", "hook", "notify", "snapshot", "managed_mcp", "plugin", "refresh", "prewarm", "unknown"])
async def test_every_required_native_effect_denies_before_transport(effect):
    policy = RecordedPolicy("test-model")
    policy.snapshot = replace(policy.snapshot, required_native_effects=frozenset({effect}))
    bridge = ModelOnlyBridge(authority=policy, account="offline", transport_factory=lambda _: pytest.fail("constructed"))
    with pytest.raises(BoundaryError):
        _ = [e async for e in bridge.stream(request())]


@pytest.mark.parametrize("fields", [{"models": {"test-model"}}, {"expires_at": float("inf")},
                                    {"expires_at": True}, {"required_native_effects": []}, {"suspended": "false"}])
def test_mutable_or_coerced_policy_is_not_authority(fields):
    with pytest.raises(BoundaryError):
        PolicySnapshot(**({"account": "a", "revision": "r", "expires_at": 123.0,
                           "models": frozenset({"m"})} | fields))


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    '{"model":"test-model","model":"injected","input":[],"tools":[],"store":false,"stream":true}',
    '{"model":"test-model","input":[],"tools":[],"store":false,"stream":true,"temperature":NaN}',
])
async def test_ambiguous_json_refused_before_constructor(raw):
    bridge = ModelOnlyBridge(authority=RecordedPolicy("test-model"), account="offline",
                             transport_factory=lambda _: pytest.fail("constructed"))
    with pytest.raises(BoundaryError):
        _ = [e async for e in bridge.stream(ModelRequest(raw, ()))]
