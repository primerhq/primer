"""Cookbook recipe (CLI path): RAG knowledge base + Q&A, driven by primectl.

This is the ``primectl``-driven sibling of ``test_cookbook_rag_knowledge_base``.
Where that test drives the recipe over the REST API, this one performs EVERY
setup step with the exact ``primectl`` commands the rewritten doc shows, so the
doc's "Via the CLI" path is a tested contract, not prose:

  * ``primectl create -f`` an embedding provider, a pgvector SSP, the KB
    collection, the scripted LLM provider, and the Q&A agent;
  * ``primectl doc put`` each path-addressed document;
  * ``primectl create -f`` / ``--set`` the local workspace provider, template,
    and workspace;
  * ``primectl session run`` the Q&A session to terminal; and
  * ``primectl workspace files get`` to read the on-disk transcript back.

The success outcome asserted is the same as the API test's: the agent calls
``search_collection``, the live (real embedder + pgvector) search surfaces the
correct source path to the agent, and the final answer cites that path.

The agent's behaviour is scripted via the shared in-process ``mock_llm`` mock
the live server reaches over HTTP (deterministic Rules); the embedder, indexer,
and vector search are REAL (LM Studio + pgvector), gated by ``requires``.

Recipe: primerhq.github.io/docs_source/cookbook/rag-knowledge-base.md
"""
from __future__ import annotations

import json

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

_PRINTER_PATH = "printer.md"
_PASSWORD_PATH = "password.md"
_DOCS = {
    _PRINTER_PATH: (
        "Connecting an office printer. Open System Settings, choose the "
        "Printers panel, click the plus button, and select the FLOOR3-HP "
        "device from the discovered list. Confirm the driver and finish setup."
    ),
    _PASSWORD_PATH: (
        "Resetting your account credentials. Open id.company.com, click "
        "Forgot Password, enter your employee email, and follow the reset "
        "link. The reset link expires after 15 minutes."
    ),
}

_PRINTER_QUERY = "how do I add a printer in the office"


@smk("SMK-COOKBOOK-CLI-01")
def test_rag_qa_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-rag-{sfx}"))

    cid = f"kb-rag-cli-{sfx}"
    eid = f"emb-rag-cli-{sfx}"
    ssp_id = f"ssp-rag-cli-{sfx}"
    pid = f"p-rag-cli-{sfx}"
    aid = f"a-rag-cli-{sfx}"
    wp = f"wp-rag-cli-{sfx}"
    tpl = f"tpl-rag-cli-{sfx}"

    cfg = load_config()["embedder"]
    scenario = f"scripted:rag-cli-{sfx}"
    grounded_answer = (
        "Open System Settings, choose Printers, click Add and select "
        f"FLOOR3-HP. (Source: {_PRINTER_PATH})"
    )
    # Rule 1 (no tool result yet): search the KB. Rule 2 (search came back):
    # answer citing the source path.
    registry.register(scenario, [
        Rule(when_tool_result=False,
             emit_tool="system__search_collection",
             emit_args={"collection_id": cid, "query": _PRINTER_QUERY, "top_k": 3}),
        Rule(when_tool_result=True, emit_text=grounded_answer),
    ])

    try:
        # --- 1. Embedding provider (CLI: create -f) ----------------------
        pc.run("create", "-f", manifest(tmp_path, "emb", "embedding_provider", {
            "id": eid,
            "provider": "openai",
            "models": [{"name": cfg["model"]}],
            "config": {"url": cfg["base_url"], "api_key": cfg["api_key"], "flavor": "lmstudio"},
            "limits": {"max_concurrency": 2},
        }))

        # --- 2. Semantic search provider (pgvector) ----------------------
        pc.run("create", "-f", manifest(tmp_path, "ssp", "ssp", {
            "id": ssp_id, "provider": "pgvector", "config": _PGVECTOR_DSN,
        }))

        # --- 3. The KB collection bound to (embedder, SSP) ---------------
        pc.run("create", "-f", manifest(tmp_path, "col", "collection", {
            "id": cid,
            "description": "IT support knowledge base for question answering.",
            "embedder": {"provider_id": eid, "model": cfg["model"]},
            "search_provider_id": ssp_id,
        }))

        # --- 4. Ingest the docs path-addressed (CLI: doc put) ------------
        for path, content in _DOCS.items():
            pc.run("doc", "put", cid, path, "--content", content)

        # The listing verb the doc shows works (sanity, not the outcome).
        listed = pc.run("doc", "list", cid, "-o", "json").json()
        listed_paths = {d.get("path") for d in listed}
        assert {_PRINTER_PATH, _PASSWORD_PATH} <= listed_paths, listed

        # --- 5. The scripted LLM provider + Q&A agent (CLI: create -f) ---
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid,
            "provider": "openchat",
            "models": [{"name": scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))
        pc.run("create", "-f", manifest(tmp_path, "agent", "agent", {
            "id": aid,
            "description": "Answers questions from the KB with citations.",
            "model": {"provider_id": pid, "model_name": scenario},
            "tools": ["system__search_collection"],
            "max_tool_turns": 6,
            "system_prompt": [
                "You answer questions using the kb collection. First call "
                "search_collection, then answer grounded on the top hit and "
                "cite the document path."
            ],
        }))

        # --- 6. Local workspace (provider + template + workspace) --------
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "rag cli", "provider_id": wp,
            "backend": {"kind": "local"},
        }))
        wid = pc.run("create", "workspace", "--set", f"template_id={tpl}").stdout.split("/")[1].split()[0]

        # --- 7. Run the Q&A session to terminal (CLI: session run) ------
        run = pc.run(
            "session", "run", wid, "--agent", aid,
            "-i", _PRINTER_QUERY, "--timeout", "150",
        )
        assert "ended: completed" in run.stdout, run.stdout

        # --- 8. Read the transcript back via the file verb the doc shows -
        # Find the session id, then read its on-disk message log through the
        # workspace file API (no /v1 call, no host-path knowledge).
        sid = None
        for line in run.stdout.splitlines():
            if line.startswith("session/") and "started" in line:
                sid = line.split("/", 1)[1].split()[0]
                break
        assert sid, f"could not parse session id from run output:\n{run.stdout}"

        rel = f".state/sessions/{sid}/messages.jsonl"
        transcript = pc.run(
            "workspace", "files", "get", wid, rel, "--content",
        ).stdout

        # The agent called search_collection and SAW the live tool result.
        assert "system__search_collection" in transcript, transcript
        assert '"role":"tool"' in transcript or '"role": "tool"' in transcript, transcript
        # The live search surfaced the correct source path to the agent.
        assert "document_name" in transcript and _PRINTER_PATH in transcript, transcript

        # The FINAL assistant message grounds on that hit and cites the source.
        last_assistant = ""
        for line in transcript.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("role") == "assistant":
                for part in obj.get("parts", []):
                    if part.get("type") == "text" and part.get("text"):
                        last_assistant = part["text"]
        assert _PRINTER_PATH in last_assistant, (
            f"final answer did not cite {_PRINTER_PATH}: {last_assistant!r}"
        )
    finally:
        for res, ident in (
            ("collection", cid), ("agent", aid), ("llm_provider", pid),
            ("ssp", ssp_id), ("embedding_provider", eid),
        ):
            pc.run("delete", res, ident, check=False)
