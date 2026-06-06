"""SMK harness tests via the in-repo local-git bundle fixture.

Inbound flow: register -> fetch -> overrides -> install -> managed entities are
write-protected -> uninstall. Uses an agent-only bundle so install is hermetic
(the collection/graph templates need an embedder + SSP). Outbound build/push
(HRN-06/07/08) need a writable remote and are left for the git lane.
"""
from __future__ import annotations

import asyncio

import pytest

from tests._support.harness_git import build_harness_repo
from tests._support.smk import smk

pytestmark = pytest.mark.asyncio


async def _wait_idle(authed_client, hid, *, timeout_s=30.0):
    """Poll until the harness has no pending async operation."""
    for _ in range(int(timeout_s / 0.3)):
        r = await authed_client.get(f"/v1/harnesses/{hid}")
        if r.status_code == 200 and not r.json().get("pending_operation"):
            return r.json()
        await asyncio.sleep(0.3)
    return (await authed_client.get(f"/v1/harnesses/{hid}")).json()


async def _register(authed_client, url, suffix):
    slug = f"smkh-{suffix}"[:64]
    r = await authed_client.post(
        "/v1/harnesses",
        json={"slug": slug, "name": "smk harness", "git_url": url, "ref": "main",
              "direction": "inbound"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


@smk("SMK-HRN-01")
async def test_register_inbound_harness(authed_client, unique_suffix, tmp_path):
    url = build_harness_repo(tmp_path / "repo", name=f"h-{unique_suffix}")
    hid = await _register(authed_client, url, unique_suffix)
    got = await authed_client.get(f"/v1/harnesses/{hid}")
    assert got.status_code == 200, got.text


@smk("SMK-HRN-02")
async def test_fetch_and_overrides(authed_client, unique_suffix, tmp_path):
    url = build_harness_repo(tmp_path / "repo", name=f"h-{unique_suffix}")
    hid = await _register(authed_client, url, unique_suffix)
    fetched = await authed_client.post(f"/v1/harnesses/{hid}/fetch")
    assert fetched.status_code in (200, 202), fetched.text
    h = await _wait_idle(authed_client, hid)
    # fetch caches the overrides schema on the harness
    assert h.get("overrides_schema") is not None, h


@smk("SMK-HRN-03", status="partial")
async def test_install_operation_accepted_and_completes(authed_client, unique_suffix, tmp_path):
    # Provide a provider the rendered agent can reference, set overrides, and
    # install. Asserts the install operation is accepted and completes without
    # error. Full managed-entity creation + write-protect + uninstall
    # (HRN-04/10) require the harness install worker to materialise entities,
    # which does not complete on the hermetic server (see FINDINGS F3); they
    # are validated on the distributed lane.
    pid = f"hp-{unique_suffix}"
    await authed_client.post(
        "/v1/llm_providers",
        json={"id": pid, "provider": "openchat",
              "models": [{"name": "scripted:default", "context_length": 8192}],
              "config": {"url": "http://127.0.0.1:1/v1", "flavor": "lmstudio"},
              "limits": {"max_concurrency": 1}},
    )
    url = build_harness_repo(tmp_path / "repo", name=f"h-{unique_suffix}")
    hid = await _register(authed_client, url, unique_suffix)
    await authed_client.post(f"/v1/harnesses/{hid}/fetch")
    await _wait_idle(authed_client, hid)
    setov = await authed_client.put(
        f"/v1/harnesses/{hid}/overrides",
        json={"provider_id": pid, "model_name": "scripted:default"},
    )
    assert setov.status_code in (200, 204), setov.text
    install = await authed_client.post(f"/v1/harnesses/{hid}/install")
    assert install.status_code in (200, 202), install.text
    h = await _wait_idle(authed_client, hid)
    assert not h.get("last_error"), h
    assert h.get("status") in ("installed", "ready"), h
