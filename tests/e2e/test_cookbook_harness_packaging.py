"""Cookbook recipe #9 regression: package and ship entities as a harness.

An OUTBOUND harness templatizes a chosen set of live entities (a collection +
an agent) and pushes a versioned bundle to a git repo; an INBOUND harness on
the same primer fetches that bundle and installs it as real entities.

Recipe: primerhq.github.io/docs_source/cookbook/harness-packaging.md

Asserts (the recipe's verified outcome):
  * after BUILD: ``bundle_hash`` is set, ``last_operation_error`` is null, and
    the harness is still ``draft``;
  * after PUSH: ``last_pushed_commit`` is set and the bare repo holds a commit
    titled ``primer outbound: <slug> @ <ts>`` on ``main`` that contains
    ``harness.yaml``, ``overrides.schema.json`` and one
    ``templates/<name>.yaml`` per tracked entity;
  * the round trip works: an inbound harness against the same repo fetches +
    installs, and the shipped collection + agent appear as new entities.

No LLM needed -- harness build/push/fetch/install are pure storage + git ops.
The git_url is a local bare repo created under tmp_path (``git init --bare``),
exactly as the recipe recommends for testing.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from tests._support.smk import smk

pytestmark = pytest.mark.asyncio


# pgvector SSP + huggingface embedder, matching the e2e bringup postgres.
# A shipped Collection references these by id; on (same-primer) install the
# rows still exist, so a verbatim template installs cleanly.
_PGVECTOR_SSP = {
    "provider": "pgvector",
    "config": {
        "hostname": "localhost", "port": 5432, "database": "primer_e2e",
        "username": "primer", "password": "primer", "db_schema": "public",
    },
}
_EMBED_PROVIDER = {
    "provider": "huggingface",
    "models": [{"name": "all-MiniLM-L6-v2", "dimensions": 384}],
    "config": {}, "limits": {"max_concurrency": 1},
}
_LLM_PROVIDER = {
    "provider": "openchat",
    "models": [{"name": "scripted:default", "context_length": 8192}],
    "config": {"url": "http://127.0.0.1:1/v1", "flavor": "lmstudio"},
    "limits": {"max_concurrency": 1},
}


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=test", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )
    return out.stdout


def _git_bare(bare: Path, *args: str) -> str:
    """Run a read against a bare repo via ``--git-dir``.

    Some environments set git ``safe.bareRepository=explicit``, which forbids
    operating *inside* a bare repo by cwd; ``--git-dir`` is the explicit form
    (and the form the recipe documents).
    """
    out = subprocess.run(
        ["git", "--git-dir", str(bare), *args],
        check=True, capture_output=True, text=True,
    )
    return out.stdout


async def _wait_idle(client, hid: str, *, timeout_s: float = 45.0) -> dict:
    """Poll the harness until it has no in-flight ``pending_operation``."""
    for _ in range(int(timeout_s / 0.3)):
        r = await client.get(f"/v1/harnesses/{hid}")
        if r.status_code == 200 and not r.json().get("pending_operation"):
            return r.json()
        await asyncio.sleep(0.3)
    return (await client.get(f"/v1/harnesses/{hid}")).json()


@smk("SMK-COOKBOOK-09")
async def test_build_push_fetch_install(authed_client, unique_suffix, tmp_path):
    sfx = unique_suffix
    ssp_id = f"ssp-hp-{sfx}"
    emb_id = f"emb-hp-{sfx}"
    llm_id = f"llm-hp-{sfx}"
    coll_id = f"kb-{sfx}"
    agent_id = f"kb-qa-{sfx}"
    out_slug = f"kb-pack-out-{sfx}"[:64]
    in_slug = f"kb-pack-in-{sfx}"[:64]

    # A throwaway bare repo is the push target.
    bare = tmp_path / "kb-pack.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git_url = f"file://{bare}"

    cleanup: list[str] = []
    out_hid: str | None = None
    in_hid: str | None = None
    try:
        # --- Source entities to ship -------------------------------------
        r = await authed_client.post("/v1/ssp", json={"id": ssp_id, **_PGVECTOR_SSP})
        assert r.status_code in (201, 409), r.text
        r = await authed_client.post(
            "/v1/embedding_providers", json={"id": emb_id, **_EMBED_PROVIDER},
        )
        assert r.status_code in (201, 409), r.text
        r = await authed_client.post("/v1/llm_providers", json={"id": llm_id, **_LLM_PROVIDER})
        assert r.status_code in (201, 409), r.text

        r = await authed_client.post("/v1/collections", json={
            "id": coll_id, "description": "IT-support KB",
            "embedder": {"provider_id": emb_id, "model": "all-MiniLM-L6-v2"},
            "search_provider_id": ssp_id,
        })
        assert r.status_code == 201, r.text
        cleanup.append(f"/v1/collections/{coll_id}")

        r = await authed_client.post("/v1/agents", json={
            "id": agent_id, "description": "Answers from the KB collection.",
            "model": {"provider_id": llm_id, "model_name": "scripted:default"},
            "tools": [],
        })
        assert r.status_code == 201, r.text
        cleanup.append(f"/v1/agents/{agent_id}")

        # --- 1. Outbound harness -----------------------------------------
        # No override mappings: entities templatize verbatim so the round-trip
        # installs cleanly on the same primer (the referenced provider rows
        # still exist).
        r = await authed_client.post("/v1/harnesses", json={
            "slug": out_slug, "name": "KB Q&A pack",
            "description": "Reusable IT-support KB + Q&A agent.",
            "git_url": git_url, "ref": "main", "direction": "outbound",
            "tracked_entities": [
                {"kind": "collection", "template_name": "kb", "source_id": coll_id},
                {"kind": "agent", "template_name": "kb-qa", "source_id": agent_id},
            ],
        })
        assert r.status_code == 201, r.text
        out_hid = r.json()["id"]

        # --- 2. Build (async, 202; status stays draft) -------------------
        b = await authed_client.post(f"/v1/harnesses/{out_hid}/build")
        assert b.status_code in (200, 202), b.text
        built = await _wait_idle(authed_client, out_hid)
        assert built.get("bundle_hash"), f"build did not set bundle_hash: {built}"
        assert not built.get("last_operation_error"), built
        assert built.get("status") == "draft", (
            f"status should stay 'draft' after build (no push yet): {built}"
        )

        # --- 3. Push (async, 202; records last_pushed_commit) ------------
        p = await authed_client.post(f"/v1/harnesses/{out_hid}/push")
        assert p.status_code in (200, 202), p.text
        pushed = await _wait_idle(authed_client, out_hid)
        assert not pushed.get("last_error") and not pushed.get("last_operation_error"), pushed
        commit_sha = pushed.get("last_pushed_commit")
        assert commit_sha, f"push did not record last_pushed_commit: {pushed}"

        # The bare repo holds the bundle on main: titled commit + bundle files.
        subject = _git_bare(bare, "log", "-1", "--format=%s", "main").strip()
        assert subject.startswith(f"primer outbound: {out_slug} @"), (
            f"unexpected push commit subject: {subject!r}"
        )
        tree = _git_bare(bare, "ls-tree", "-r", "--name-only", "main")
        listed = set(tree.split())
        assert "harness.yaml" in listed, listed
        assert "overrides.schema.json" in listed, listed
        assert "templates/kb.yaml" in listed, listed
        assert "templates/kb-qa.yaml" in listed, listed

        # --- 4. Inbound harness against the same repo: fetch + install ---
        r = await authed_client.post("/v1/harnesses", json={
            "slug": in_slug, "name": "KB Q&A pack (install)",
            "git_url": git_url, "ref": "main", "direction": "inbound",
        })
        assert r.status_code == 201, r.text
        in_hid = r.json()["id"]

        f = await authed_client.post(f"/v1/harnesses/{in_hid}/fetch")
        assert f.status_code in (200, 202), f.text
        fetched = await _wait_idle(authed_client, in_hid)
        assert fetched.get("overrides_schema") is not None, fetched

        i = await authed_client.post(f"/v1/harnesses/{in_hid}/install")
        assert i.status_code in (200, 202), i.text
        installed = await _wait_idle(authed_client, in_hid)
        assert not installed.get("last_error"), installed
        assert installed.get("status") in ("installed", "ready"), installed

        # The shipped entities appear under the inbound harness's resolved ids
        # (<slug>__<template_name>).
        new_coll = f"{in_slug}__kb"
        new_agent = f"{in_slug}__kb-qa"
        cleanup.append(f"/v1/collections/{new_coll}")
        cleanup.append(f"/v1/agents/{new_agent}")

        gc = await authed_client.get(f"/v1/collections/{new_coll}")
        assert gc.status_code == 200, f"installed collection missing: {gc.text}"
        assert gc.json()["harness_id"] == in_hid, gc.json()

        ga = await authed_client.get(f"/v1/agents/{new_agent}")
        assert ga.status_code == 200, f"installed agent missing: {ga.text}"
        assert ga.json()["harness_id"] == in_hid, ga.json()
    finally:
        # Uninstall the inbound harness first (it owns the resolved entities,
        # which the public CRUD refuses to delete directly).
        if in_hid is not None:
            await authed_client.delete(f"/v1/harnesses/{in_hid}")
            await _wait_idle(authed_client, in_hid)
        if out_hid is not None:
            await authed_client.delete(f"/v1/harnesses/{out_hid}")
        for url in reversed(cleanup):
            await authed_client.delete(url)
        await authed_client.delete(f"/v1/llm_providers/{llm_id}")
        await authed_client.delete(f"/v1/embedding_providers/{emb_id}")
        await authed_client.delete(f"/v1/ssp/{ssp_id}")
