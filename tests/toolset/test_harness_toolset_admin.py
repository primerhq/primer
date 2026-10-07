"""The harness system tools that write or act are admin-only, and they validate git_url / ref (security review 2026-10-08).

AUTHZ-03 / FS-01: an install writes arbitrary entities (a stdio MCP toolset included), so a harness write is an admin function on
every surface; the tool manager and the MCP endpoint both enforce the static ``required_role``. AUTHZ-04: a bad git_url or ref is
refused before it is stored. SEC-03: a new git_url never inherits the stored token.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from primer.model.harness import Harness, HarnessStatus
from primer.model.principal import PrincipalRef
from primer.model.yield_ import ToolContext
from primer.toolset.harness import build_harness_toolset_provider
from tests.toolset.test_harness_toolset import _SP, _EventBus

_WRITES = (
    "harness__register",
    "harness__update",
    "harness__update_overrides",
    "harness__fetch",
    "harness__install",
    "harness__sync",
    "harness__uninstall",
)
_READS = ("harness__list", "harness__get")


def _provider(sp=None):
    return build_harness_toolset_provider(storage_provider=sp or _SP(), event_bus=_EventBus())


def test_every_harness_write_requires_an_admin():
    provider = _provider()
    for name in _WRITES:
        assert provider.required_role(name) == "admin", name


def test_the_harness_reads_stay_user_tier():
    provider = _provider()
    for name in _READS:
        assert provider.required_role(name) == "user", name


@pytest.mark.parametrize("git_url", ["--upload-pack=x", "ext::sh -c id", "file:///etc", "http://h/r"])
async def test_register_refuses_an_unsafe_git_url(monkeypatch, git_url):
    monkeypatch.delenv("PRIMER_HARNESS_ALLOW_FILE_URLS", raising=False)
    sp = _SP()
    result = await _provider(sp).call(
        tool_name="harness__register", arguments={"name": "x", "slug": "bad-url", "git_url": git_url},
    )
    assert result.is_error, result.output
    assert (await sp.get_storage(Harness).list(None)).items == []


async def test_register_and_update_refuse_an_unsafe_ref():
    sp = _SP()
    provider = _provider(sp)
    bad = await provider.call(
        tool_name="harness__register",
        arguments={"name": "x", "slug": "bad-ref", "git_url": "https://h.example/r", "ref": "--output=x"},
    )
    assert bad.is_error, bad.output

    await sp.get_storage(Harness).create(
        Harness(id="hns_1", slug="ok-harness", name="x", git_url="https://h.example/r",
                created_at=datetime.now(timezone.utc)),
    )
    upd = await provider.call(tool_name="harness__update", arguments={"id": "hns_1", "ref": "-b"})
    assert upd.is_error, upd.output
    assert (await sp.get_storage(Harness).get("hns_1")).ref == "main"


async def test_update_cannot_repoint_git_url_and_the_masked_token_keeps_the_real_one():
    # harness__update has no git_url argument, so the token can never follow a new remote through it (SEC-03); a token
    # sent back as the served mask keeps the stored one rather than storing the mask.
    sp = _SP()
    await sp.get_storage(Harness).create(
        Harness(id="hns_1", slug="ok-harness", name="x", git_url="https://h.example/r",
                git_token=SecretStr("secret"), created_at=datetime.now(timezone.utc)),
    )
    provider = _provider(sp)
    from primer.toolset.harness import TOOL_UPDATE

    assert "git_url" not in TOOL_UPDATE.args_schema["properties"]

    result = await provider.call(
        tool_name="harness__update",
        arguments={"id": "hns_1", "git_url": "https://evil.example/r", "git_token": "**********"},
    )
    assert not result.is_error, result.output
    stored = await sp.get_storage(Harness).get("hns_1")
    assert stored.git_url == "https://h.example/r"
    assert stored.git_token.get_secret_value() == "secret"


async def test_install_records_who_asked():
    sp = _SP()
    await sp.get_storage(Harness).create(
        Harness(id="hns_1", slug="ok-harness", name="x", git_url="https://h.example/r",
                status=HarnessStatus.READY, overrides_schema={"type": "object"},
                created_at=datetime.now(timezone.utc)),
    )
    admin = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
    ctx = ToolContext(tool_call_id="tc", session_id=None, workspace_id=None, initiated_by=admin)

    result = await _provider(sp).call(tool_name="harness__install", arguments={"id": "hns_1"}, ctx=ctx)

    assert not result.is_error, result.output
    assert (await sp.get_storage(Harness).get("hns_1")).operation_requested_by == admin
