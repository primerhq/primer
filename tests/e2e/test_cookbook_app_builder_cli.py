"""Cookbook recipe (CLI path): describe-to-deploy app builder, via primectl.

The ``primectl``-driven sibling of ``test_cookbook_app_builder``. The operator
work the rewritten doc shows is performed with the exact ``primectl`` commands:

  * ``create -f`` the embedder, pgvector SSP, the digest LLM provider (for the
    summarizer the builder creates), the scripted builder LLM provider, and the
    ``app-builder`` agent;
  * ``create -f`` / ``--set`` the local workspace;
  * ``session run`` the builder agent with the one-line app description -- the
    AGENT then drives the internal CRUD tools to assemble the mini-app;
  * ``call trigger subscriptions`` to wire the ``graph_fresh_session``
    subscription and ``call trigger fire-now`` to run the assembled app once;
  * ``get collection/agent/graph/trigger`` to prove every entity persisted,
    ``call collection search`` to prove the seeded doc is searchable, and
    ``workspace files get`` to read the fired graph's on-disk completed state.

The success outcome asserted is the API test's: the builder finishes
``completed`` having called the CRUD tools in assembly order; the collection,
seeded (searchable) doc, summarizer agent, graph, and scheduled trigger all
persist; firing the trigger once runs the assembled graph app to terminal
``completed`` with an on-disk state file.

The builder's tool sequence is SCRIPTED via the shared in-process ``mock_llm``
(one CRUD call per turn, rules ordered LATEST-FIRST and keyed on a token unique
to the previous result -- the same sequencing trap the API test pins); the CRUD
platform paths (validation, persistence, indexing, dispatch) are REAL. Internal
semantic search needs an embedder + pgvector, so the test is
``@requires("embedder", "pgvector")``.

Recipe: primerhq.github.io/docs_source/cookbook/app-builder.md
"""
from __future__ import annotations

import json
import time

import pytest

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk
from tests._support.testconfig import load_config, requires

pytestmark = [requires("embedder", "pgvector")]


_PGVECTOR_DSN = {
    "hostname": "localhost",
    "port": 5432,
    "database": "primer_e2e",
    "username": "primer",
    "password": "primer",
}

_DOC_PATH = "news/today.md"
_DOC_BODY = (
    "Today's headlines: the platform shipped generic collections; "
    "the channel media pipeline went live; the release pipeline is staged."
)
_DIGEST_LINE = "Digest: generic collections shipped; channel media live; release staged."


def _manifest_body(tmp_path, name: str, body: dict) -> str:
    """Write a bare JSON request body (for ``call ... -f``)."""
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(body))
    return str(path)


def _wait_session(pc: Primectl, sid: str, *, timeout_s: float = 120.0) -> dict:
    """Poll ``get session`` to a terminal status (the doc's CLI run pattern)."""
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


@smk("SMK-COOKBOOK-CLI-05")
def test_app_builder_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-app-{sfx}"))

    cfg = load_config()["embedder"]
    coll_id = f"digest-kb-cli-{sfx}"
    summarizer_id = f"summarizer-cli-{sfx}"
    graph_id = f"digest-graph-cli-{sfx}"
    trigger_slug = f"nightly-digest-cli-{sfx}"
    ssp_id = f"ssp-app-cli-{sfx}"
    emb_id = f"emb-app-cli-{sfx}"
    builder_id = f"app-builder-cli-{sfx}"
    builder_pid = f"p-builder-cli-{sfx}"
    digest_pid = f"p-digest-cli-{sfx}"
    wp = f"wp-app-cli-{sfx}"
    tpl = f"tpl-app-cli-{sfx}"

    builder_scenario = f"scripted:builder-cli-{sfx}"
    digest_scenario = f"scripted:digest-cli-{sfx}"
    registry.register(digest_scenario, [Rule(emit_text=_DIGEST_LINE)])

    # The runnable graph the builder creates: begin -> summarizer -> end.
    graph_body = {
        "id": graph_id,
        "description": "Summarize today's news into a one-line digest.",
        "nodes": [
            {"kind": "begin", "id": "begin"},
            {"kind": "agent", "id": "summ", "agent_id": summarizer_id,
             "input_template": "Summarize today's news into a digest."},
            {"kind": "end", "id": "end", "output_template": "{{ nodes.summ.text }}"},
        ],
        "edges": [
            {"kind": "static", "from_node": "begin", "to_node": "summ"},
            {"kind": "static", "from_node": "summ", "to_node": "end"},
        ],
    }

    # Scripted builder: one CRUD call per turn. Rules ordered LATEST-FIRST,
    # keyed on a token unique to the previous step's result (the sequencing
    # trap the API test documents). The builder stops after trigger__create;
    # the operator wires the subscription + fires (as the doc step 3 shows).
    registry.register(builder_scenario, [
        Rule(when_last_tool_result_contains='"nodes"',
             emit_tool="trigger__create",
             emit_args={"slug": trigger_slug, "name": "Nightly digest",
                        "config": {"kind": "scheduled", "cron": "0 2 1 1 *",
                                   "timezone": "UTC", "catchup": "none"}},
             emit_tool_call_id="c5"),
        Rule(when_last_tool_result_contains='"system_prompt"',
             emit_tool="system__create_graph",
             emit_args={"entity": graph_body}, emit_tool_call_id="c4"),
        Rule(when_last_tool_result_contains='"document-',
             emit_tool="system__create_agent",
             emit_args={"entity": {
                 "id": summarizer_id,
                 "description": "Summarizes the news into a digest.",
                 "model": {"provider_id": digest_pid, "model_name": digest_scenario},
                 "tools": [],
                 "system_prompt": ["Return a one-line digest of the news."]}},
             emit_tool_call_id="c3"),
        Rule(when_last_tool_result_contains='"embedder"',
             emit_tool="system__put_document",
             emit_args={"collection_id": coll_id, "path": _DOC_PATH,
                        "content": _DOC_BODY, "title": "Today"},
             emit_tool_call_id="c2"),
        Rule(when_tool_result=False,
             emit_tool="system__create_collection",
             emit_args={"entity": {
                 "id": coll_id, "description": "News digest knowledge base",
                 "embedder": {"provider_id": emb_id, "model": cfg["model"]},
                 "search_provider_id": ssp_id}},
             emit_tool_call_id="c1"),
        Rule(when_last_tool_result_contains=trigger_slug,
             emit_text="Built the digest app: collection, doc, agent, graph, trigger."),
    ])

    trigger_id: str | None = None
    build_sid: str | None = None
    try:
        # --- Embedder + pgvector SSP (so the built collection is searchable) -
        pc.run("create", "-f", manifest(tmp_path, "emb", "embedding_provider", {
            "id": emb_id, "provider": "openai", "models": [{"name": cfg["model"]}],
            "config": {"url": cfg["base_url"], "api_key": cfg["api_key"], "flavor": "lmstudio"},
            "limits": {"max_concurrency": 2},
        }))
        pc.run("create", "-f", manifest(tmp_path, "ssp", "ssp", {
            "id": ssp_id, "provider": "pgvector", "config": _PGVECTOR_DSN,
        }))

        # --- The digest LLM provider the summarizer (built by the agent) uses
        pc.run("create", "-f", manifest(tmp_path, "digestllm", "llm_provider", {
            "id": digest_pid, "provider": "openchat",
            "models": [{"name": digest_scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))

        # --- The scripted builder LLM provider + the app-builder agent ------
        pc.run("create", "-f", manifest(tmp_path, "builderllm", "llm_provider", {
            "id": builder_pid, "provider": "openchat",
            "models": [{"name": builder_scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))
        pc.run("create", "-f", manifest(tmp_path, "builder", "agent", {
            "id": builder_id,
            "description": "Provisions a whole mini-app from one request.",
            "model": {"provider_id": builder_pid, "model_name": builder_scenario},
            "tools": [
                "system__create_collection", "system__put_document",
                "system__create_agent", "system__create_graph",
                "trigger__create", "trigger__create_subscription",
                "trigger__fire_now",
            ],
            "max_tool_turns": 12,
            "system_prompt": [
                "You build a whole mini-app from one request using the CRUD "
                "tools: create the collection, seed a doc, create the agent, "
                "create the graph, then create a scheduled trigger."
            ],
        }))

        # --- Local workspace ---------------------------------------------
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "app cli", "provider_id": wp, "backend": {"kind": "local"},
        }))
        wid = pc.run("create", "workspace", "--set", f"template_id={tpl}").stdout.split("/")[1].split()[0]

        # --- Run the builder: the AGENT provisions the whole app ----------
        run = pc.run(
            "session", "run", wid, "--agent", builder_id,
            "-i", ("Build a nightly news-digest app: create the collection, "
                   "seed a doc, create the summarizer agent, create the digest "
                   "graph, and create a scheduled trigger."),
            "--timeout", "150",
        )
        assert "ended: completed" in run.stdout, run.stdout
        build_sid = _session_id_from_run(run.stdout)
        assert build_sid, f"could not parse builder session id:\n{run.stdout}"

        # The builder called the CRUD tools in assembly order (no single-tool
        # loop): read the on-disk transcript via the file verb the doc shows.
        transcript = pc.run(
            "workspace", "files", "get", wid,
            f".state/sessions/{build_sid}/messages.jsonl", "--content",
        ).stdout
        for tool in ("system__create_collection", "system__put_document",
                     "system__create_agent", "system__create_graph", "trigger__create"):
            assert tool in transcript, f"builder never called {tool}:\n{transcript}"

        # --- (1) Every entity the agent created PERSISTED (get verbs) ------
        assert pc.run("get", "collection", coll_id, "-o", "json", "-r").json()["id"] == coll_id
        assert pc.run("get", "agent", summarizer_id, "-o", "json", "-r").json()["id"] == summarizer_id
        assert pc.run("get", "graph", graph_id, "-o", "json", "-r").json()["id"] == graph_id

        triggers = pc.run("get", "trigger", "-o", "json").json()
        items = triggers if isinstance(triggers, list) else triggers.get("items", [])
        trigger = next((t for t in items if t.get("slug") == trigger_slug), None)
        assert trigger is not None, f"trigger {trigger_slug!r} not created: {items}"
        trigger_id = trigger["id"]

        # --- (2) The seeded doc is SEARCHABLE (call collection search) -----
        qfile = _manifest_body(tmp_path, "q", {"query": "channel media pipeline", "top_k": 5})
        hits: list = []
        for _ in range(15):
            hits = pc.run("call", "collection", "search", coll_id, "-f", qfile, "-o", "json").json().get("hits", [])
            if hits:
                break
            time.sleep(1.0)
        assert hits, "the doc the agent seeded via put_document is not searchable"

        # --- (3) The assembled app RUNS. Wire the graph_fresh_session
        # subscription (call trigger subscriptions) and fire it once. -------
        subfile = _manifest_body(tmp_path, "sub", {
            "config": {"kind": "graph_fresh_session", "graph_id": graph_id, "workspace_id": wid},
            "payload_template": json.dumps({"run": "manual"}),
            "parallelism": "skip",
        })
        sub = pc.run("call", "trigger", "subscriptions", trigger_id, "-f", subfile, "-o", "json").json()
        sub_id = sub["id"]

        fire = pc.run("call", "trigger", "fire-now", trigger_id, "-o", "json").json()
        assert not fire.get("skipped"), fire
        run_sid = None
        for res in fire.get("results", []):
            if res.get("ok") and res.get("artefact_id"):
                run_sid = res["artefact_id"]
                break
        assert run_sid is not None, f"fire_now dispatched no graph session: {fire}"

        run_final = _wait_session(pc, run_sid)
        assert run_final.get("status") == "ended", run_final
        assert run_final.get("ended_reason") == "completed", (
            f"the assembled app's fired graph did not complete: {run_final}"
        )
        assert (run_final.get("metadata") or {}).get("subscription_id") == sub_id, run_final

        # The fired graph wrote a completed on-disk state (read via the file verb).
        state = json.loads(pc.run(
            "workspace", "files", "get", wid,
            f".state/graphs/{run_sid}/state.json", "--content",
        ).stdout)
        assert state.get("ended_reason") == "completed", state
    finally:
        if trigger_id is not None:
            pc.run("delete", "trigger", trigger_id, check=False)
        for res, ident in (
            ("graph", graph_id), ("agent", summarizer_id), ("agent", builder_id),
            ("collection", coll_id), ("llm_provider", builder_pid),
            ("llm_provider", digest_pid), ("ssp", ssp_id), ("embedding_provider", emb_id),
        ):
            pc.run("delete", res, ident, check=False)
