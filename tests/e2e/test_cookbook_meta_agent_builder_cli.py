"""Cookbook recipe (CLI path): meta-agent that builds agents, via primectl.

The ``primectl``-driven sibling of ``test_cookbook_meta_agent_builder``. Every
operator step the rewritten doc shows is performed with the exact ``primectl``
commands:

  * ``create -f`` the embedder + pgvector SSP;
  * ``raw PUT/POST/GET`` the internal-collections config + bootstrap + status
    poll (the subsystem is a singleton with no CRUD resource, so the doc and
    this test use the ``primectl raw`` escape hatch -- the only API-shaped call
    in the recipe, and exactly what the doc flags);
  * ``create -f`` the scripted LLM provider + the ``meta-builder`` agent;
  * ``create -f`` / ``--set`` the local workspace;
  * ``session run`` the meta-agent with a use case -- the AGENT discovers a tool
    via internal search then calls ``create_agent``;
  * ``get agent`` to confirm the freshly built agent appears wired to the
    discovered tool, then ``session run`` it to prove it is runnable.

The success outcome asserted is the API test's: the meta-agent's ``search_tools``
returns the real catalogue hit ``misc__get_datetime`` (read back from the on-disk
transcript), a NEW agent appears wired to ``["misc__get_datetime"]``, and that
agent runs to a clean terminal.

The meta-agent is SCRIPTED via the shared in-process ``mock_llm``; the discovery
+ creation platform paths are REAL. Internal semantic search needs an embedder +
pgvector and a successful bootstrap, so the test is
``@requires("embedder", "pgvector")`` and polls the bootstrap to ``succeeded``.

Recipe: primerhq.github.io/docs_source/cookbook/meta-agent-builder.md
"""
from __future__ import annotations

import json
import time

import pytest

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = [requires("embedder", "pgvector")]


_PGVECTOR_DSN = {
    "hostname": "localhost",
    "port": 5432,
    "database": "primer_e2e",
    "username": "primer",
    "password": "primer",
}

# Mirror the API test: a 384-dim in-process huggingface embedder, NOT the LM
# Studio config embedder. The internal-collections subsystem on the e2e server
# is bootstrapped at 384 dims (all-MiniLM); re-pointing it at a 768-dim embedder
# raises DimensionMismatchError, so the meta-agent recipe activates internal
# search with the matching 384-dim model the catalogue was indexed with.
_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_EMBED_SPEC = {
    "provider": "huggingface",
    "models": [{"name": _EMBED_MODEL, "dim": 384}],
    "config": {"token": "hf-placeholder"},
    "limits": {"max_concurrency": 1},
}


def _bootstrap_internal(pc: Primectl, tmp_path, *, emb_id: str, model: str,
                        ssp_id: str, timeout_s: float = 180.0) -> None:
    """Configure + bootstrap the internal-collections subsystem via raw.

    The subsystem is a singleton (no /v1/internal_collections/{id} item path),
    so primectl exposes no generic resource for it; the doc and this test use
    ``primectl raw`` -- the explicit, clearly-labelled escape hatch -- to PUT
    the config, POST the bootstrap, and GET the status until it succeeds.
    """
    cfg = tmp_path / "ic_config.yaml"
    cfg.write_text(json.dumps({
        "embedding_provider_id": emb_id,
        "embedding_model": model,
        "search_provider_id": ssp_id,
    }))
    pc.run("raw", "PUT", "/v1/internal_collections/config", "-f", str(cfg))
    pc.run("raw", "POST", "/v1/internal_collections/bootstrap")
    deadline = time.monotonic() + timeout_s
    last = "unknown"
    while time.monotonic() < deadline:
        status = pc.run("raw", "GET", "/v1/internal_collections/bootstrap/status",
                        "-o", "json").json()
        last = status.get("status")
        if last == "succeeded":
            return
        if last == "failed":
            pytest.skip(f"internal-collections bootstrap failed: {status.get('error')!r}")
        time.sleep(0.5)
    pytest.skip(f"bootstrap did not complete in {timeout_s}s (last={last!r})")


def _wait_session(pc: Primectl, sid: str, *, timeout_s: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = pc.run("get", "session", sid, "-o", "json", "-r").json()
        if last.get("status") == "ended":
            return last
        time.sleep(1.0)
    return last


def _session_id_from_run(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith("session/") and "started" in line:
            return line.split("/", 1)[1].split()[0]
    return None


@smk("SMK-COOKBOOK-CLI-06")
def test_meta_agent_builder_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-meta-{sfx}"))

    model = _EMBED_MODEL
    emb_id = f"emb-meta-cli-{sfx}"
    ssp_id = f"ssp-meta-cli-{sfx}"
    meta_pid = f"p-meta-cli-{sfx}"
    meta_id = f"meta-builder-cli-{sfx}"
    built_id = f"datetime-agent-cli-{sfx}"
    wp = f"wp-meta-cli-{sfx}"
    tpl = f"tpl-meta-cli-{sfx}"

    scenario = f"scripted:meta-cli-{sfx}"
    # Rule 1: discover tools. Rule 2 (after the search hits): create the
    # datetime agent wired to the tool it "found" (its model points at THIS
    # scripted provider so the built agent is runnable). Rule 3: report done.
    registry.register(scenario, [
        Rule(when_tool_result=False,
             emit_tool="search__search_tools",
             emit_args={"query": "return the current date and time", "top_k": 5}),
        Rule(when_last_tool_result_contains="hits",
             emit_tool="system__create_agent",
             emit_args={"entity": {
                 "id": built_id,
                 "description": "Returns the current date and time on request.",
                 "model": {"provider_id": meta_pid, "model_name": scenario},
                 "tools": ["misc__get_datetime"],
                 "system_prompt": ["Return the current date and time."]}}),
        Rule(when_tool_result=True, emit_text="created datetime-agent"),
    ])

    try:
        # --- Embedder (384-dim hf, matching the bootstrap) + pgvector SSP --
        pc.run("create", "-f", manifest(tmp_path, "emb", "embedding_provider", {
            "id": emb_id, **_EMBED_SPEC,
        }))
        pc.run("create", "-f", manifest(tmp_path, "ssp", "ssp", {
            "id": ssp_id, "provider": "pgvector", "config": _PGVECTOR_DSN,
        }))

        # --- Activate internal semantic search (raw, the doc's escape hatch)
        _bootstrap_internal(pc, tmp_path, emb_id=emb_id, model=model, ssp_id=ssp_id)

        # --- The scripted meta-agent's LLM provider + the meta-builder agent
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": meta_pid, "provider": "openchat",
            "models": [{"name": scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))
        pc.run("create", "-f", manifest(tmp_path, "meta", "agent", {
            "id": meta_id,
            "description": "Builds new agents from a use case by discovering tools.",
            "model": {"provider_id": meta_pid, "model_name": scenario},
            "tools": ["search__search_tools", "system__create_agent"],
            "max_tool_turns": 8,
            "system_prompt": ["You build new agents from a use case by discovering tools."],
        }))

        # --- Local workspace ---------------------------------------------
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "meta cli", "provider_id": wp, "backend": {"kind": "local"},
        }))
        wid = pc.run("create", "workspace", "--set", f"template_id={tpl}").stdout.split("/")[1].split()[0]

        # --- Run the meta-agent with a use case --------------------------
        run = pc.run(
            "session", "run", wid, "--agent", meta_id,
            "-i", "Use case: build an agent that returns the current date and time.",
            "--timeout", "120",
        )
        assert "ended: completed" in run.stdout, run.stdout
        sid = _session_id_from_run(run.stdout)
        assert sid, f"could not parse meta session id:\n{run.stdout}"

        # search_tools ran AND surfaced the real catalogue hit (the agent could
        # not have discovered the tool otherwise). Read the on-disk transcript.
        transcript = pc.run(
            "workspace", "files", "get", wid,
            f".state/sessions/{sid}/messages.jsonl", "--content",
        ).stdout
        assert "search__search_tools" in transcript, transcript
        assert "misc__get_datetime" in transcript, (
            "search_tools did not surface misc__get_datetime in the catalogue; "
            f"the meta-agent could not have discovered it:\n{transcript}"
        )

        # The new agent appears, wired to the discovered tool (get verb).
        built = pc.run("get", "agent", built_id, "-o", "json", "-r").json()
        assert built.get("tools") == ["misc__get_datetime"], built

        # The freshly built agent is immediately runnable.
        run2 = pc.run("session", "run", wid, "--agent", built_id, "-i", "what time is it", "--timeout", "90")
        assert "ended:" in run2.stdout, run2.stdout
        sid2 = _session_id_from_run(run2.stdout)
        assert sid2, run2.stdout
        final2 = _wait_session(pc, sid2)
        assert final2.get("status") == "ended", final2
    finally:
        pc.run("delete", "agent", built_id, check=False)
        pc.run("delete", "agent", meta_id, check=False)
        pc.run("delete", "llm_provider", meta_pid, check=False)
        # Mirror the API test's teardown: drop the internal-collections config
        # (it referenced this test's throwaway providers) BEFORE deleting them,
        # so no dangling reference is left behind.
        pc.run("raw", "DELETE", "/v1/internal_collections/config", check=False)
        pc.run("delete", "embedding_provider", emb_id, check=False)
        pc.run("delete", "ssp", ssp_id, check=False)
