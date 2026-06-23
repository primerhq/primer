"""Cookbook recipe (CLI path): package + ship entities as a harness, via primectl.

The ``primectl``-driven sibling of ``test_cookbook_harness_packaging``. Every
setup + operation step is the exact ``primectl`` command the rewritten doc shows,
so the doc's "Via the CLI" path is a tested contract, not prose:

  * ``create -f`` the source entities (pgvector SSP, embedding provider, LLM
    provider, the ``kb`` collection, and the ``kb-qa`` agent);
  * ``create -f`` the OUTBOUND harness that tracks the collection + agent;
  * ``call harness build`` then ``call harness push`` (each async; the doc polls
    ``get harness`` between steps, so this test polls too);
  * ``create -f`` the INBOUND harness against the same bare repo, then
    ``call harness fetch`` and ``call harness install``;
  * ``get collection`` / ``get agent`` to confirm the shipped entities appear
    under their resolved ids on the target side.

The success outcome asserted is the API test's: after build the harness carries a
``bundle_hash`` with no error and stays ``draft``; after push the bare repo holds
a titled commit on ``main`` with the bundle files; the inbound install
materialises ``<slug>__kb`` + ``<slug>__kb-qa`` owned by the inbound harness.

No LLM is needed -- harness build/push/fetch/install are pure storage + git ops --
so this test is not capability-gated. The git_url is a local bare repo created
under tmp_path (``git init --bare``), exactly as the recipe recommends.

Recipe: primerhq.github.io/docs_source/cookbook/harness-packaging.md
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk


_PGVECTOR_DSN = {
    "hostname": "localhost",
    "port": 5432,
    "database": "primer_e2e",
    "username": "primer",
    "password": "primer",
}


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=test", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )
    return out.stdout


def _git_bare(bare: Path, *args: str) -> str:
    """Run a read against a bare repo via ``--git-dir`` (the form the doc shows)."""
    out = subprocess.run(
        ["git", "--git-dir", str(bare), *args],
        check=True, capture_output=True, text=True,
    )
    return out.stdout


def _wait_idle(pc: Primectl, hid: str, *, timeout_s: float = 60.0) -> dict:
    """Poll ``get harness`` until ``pending_operation`` clears (as the doc shows).

    Tolerant of the harness having been deleted (a 404 during teardown polling
    means there is nothing in flight), so it doubles as a post-delete drain.
    """
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        res = pc.run("get", "harness", hid, "-o", "json", "-r", check=False)
        if res.returncode != 0:
            return last
        last = res.json()
        if not last.get("pending_operation"):
            return last
        time.sleep(0.5)
    return last


@smk("SMK-COOKBOOK-CLI-04")
def test_harness_packaging_cli(base_url, unique_suffix, tmp_path):
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-harness-{sfx}"))

    ssp_id = f"ssp-hp-cli-{sfx}"
    emb_id = f"emb-hp-cli-{sfx}"
    llm_id = f"llm-hp-cli-{sfx}"
    coll_id = f"kb-hp-cli-{sfx}"
    agent_id = f"kb-qa-hp-cli-{sfx}"
    out_slug = f"kb-pack-out-{sfx}"[:64]
    in_slug = f"kb-pack-in-{sfx}"[:64]

    # A throwaway bare repo is the push target (exactly as the doc's Testing
    # section recommends: ``git init --bare``).
    bare = tmp_path / "kb-pack.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git_url = f"file://{bare}"

    out_hid: str | None = None
    in_hid: str | None = None
    new_coll = f"{in_slug}__kb"
    new_agent = f"{in_slug}__kb-qa"
    try:
        # --- Source entities to ship (create -f, the doc's Ingredients) ----
        pc.run("create", "-f", manifest(tmp_path, "ssp", "ssp", {
            "id": ssp_id, "provider": "pgvector", "config": _PGVECTOR_DSN,
        }))
        pc.run("create", "-f", manifest(tmp_path, "emb", "embedding_provider", {
            "id": emb_id, "provider": "huggingface",
            "models": [{"name": "all-MiniLM-L6-v2", "dimensions": 384}],
            "config": {}, "limits": {"max_concurrency": 1},
        }))
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": llm_id, "provider": "openchat",
            "models": [{"name": "scripted:default", "context_length": 8192}],
            "config": {"url": "http://127.0.0.1:1/v1", "flavor": "lmstudio"},
            "limits": {"max_concurrency": 1},
        }))
        pc.run("create", "-f", manifest(tmp_path, "col", "collection", {
            "id": coll_id, "description": "IT-support KB",
            "embedder": {"provider_id": emb_id, "model": "all-MiniLM-L6-v2"},
            "search_provider_id": ssp_id,
        }))
        pc.run("create", "-f", manifest(tmp_path, "agent", "agent", {
            "id": agent_id, "description": "Answers from the KB collection.",
            "model": {"provider_id": llm_id, "model_name": "scripted:default"},
            "tools": [],
        }))

        # --- 1. Outbound harness (create -f) -----------------------------
        # `create` echoes "harness/<server-id> created"; the id is assigned by
        # the server (the harness is keyed by slug), so parse it from stdout.
        out_hid = pc.run("create", "-f", manifest(tmp_path, "out", "harness", {
            "slug": out_slug, "name": "KB Q&A pack",
            "description": "Reusable IT-support KB + Q&A agent.",
            "git_url": git_url, "ref": "main", "direction": "outbound",
            "tracked_entities": [
                {"kind": "collection", "template_name": "kb", "source_id": coll_id},
                {"kind": "agent", "template_name": "kb-qa", "source_id": agent_id},
            ],
        })).stdout.split("/", 1)[1].split()[0]

        # --- 2. Build then push (call harness <action>, poll between) -----
        pc.run("call", "harness", "build", out_hid)
        built = _wait_idle(pc, out_hid)
        assert built.get("bundle_hash"), f"build did not set bundle_hash: {built}"
        assert not built.get("last_operation_error"), built
        assert built.get("status") == "draft", (
            f"status should stay 'draft' after build (no push yet): {built}"
        )

        pc.run("call", "harness", "push", out_hid)
        pushed = _wait_idle(pc, out_hid)
        assert not pushed.get("last_error") and not pushed.get("last_operation_error"), pushed
        assert pushed.get("last_pushed_commit"), (
            f"push did not record last_pushed_commit: {pushed}"
        )

        # The bare repo holds the bundle on main: titled commit + bundle files
        # (the exact git reads the doc's Testing section shows).
        subject = _git_bare(bare, "log", "-1", "--format=%s", "main").strip()
        assert subject.startswith(f"primer outbound: {out_slug} @"), (
            f"unexpected push commit subject: {subject!r}"
        )
        listed = set(_git_bare(bare, "ls-tree", "-r", "--name-only", "main").split())
        assert "harness.yaml" in listed, listed
        assert "overrides.schema.json" in listed, listed
        assert "templates/kb.yaml" in listed, listed
        assert "templates/kb-qa.yaml" in listed, listed

        # --- 3. Inbound harness: fetch + install -------------------------
        in_hid = pc.run("create", "-f", manifest(tmp_path, "in", "harness", {
            "slug": in_slug, "name": "KB Q&A pack (install)",
            "git_url": git_url, "ref": "main", "direction": "inbound",
        })).stdout.split("/", 1)[1].split()[0]

        pc.run("call", "harness", "fetch", in_hid)
        fetched = _wait_idle(pc, in_hid)
        assert fetched.get("overrides_schema") is not None, fetched

        pc.run("call", "harness", "install", in_hid)
        installed = _wait_idle(pc, in_hid)
        assert not installed.get("last_error"), installed
        assert installed.get("status") in ("installed", "ready"), installed

        # The shipped entities appear under the inbound harness's resolved ids
        # (<slug>__<template_name>), via the verbs the doc shows.
        gc = pc.run("get", "collection", new_coll, "-o", "json", "-r").json()
        assert gc.get("harness_id") == in_hid, gc
        ga = pc.run("get", "agent", new_agent, "-o", "json", "-r").json()
        assert ga.get("harness_id") == in_hid, ga
    finally:
        # Uninstall the inbound harness first (it owns the resolved entities,
        # which the public CRUD refuses to delete directly), then everything.
        if in_hid is not None:
            pc.run("delete", "harness", in_hid, check=False)
            _wait_idle(pc, in_hid, timeout_s=30.0)
        if out_hid is not None:
            pc.run("delete", "harness", out_hid, check=False)
        for res, ident in (
            ("agent", agent_id), ("collection", coll_id),
            ("llm_provider", llm_id), ("embedding_provider", emb_id),
            ("ssp", ssp_id),
        ):
            pc.run("delete", res, ident, check=False)
