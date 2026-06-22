"""Cookbook recipe #7 regression: RAG knowledge base + Q&A.

A user collection holds a handful of path-addressed documents (a printer guide,
a password-reset guide). A real embedder (LM Studio, OpenAI-flavoured) +
pgvector SSP index the bodies on PUT. A scripted Q&A agent then calls
``system__search_collection`` for the user's question and answers grounded in
the top hit, citing the source path.

Recipe: primerhq.github.io/docs_source/cookbook/rag-knowledge-base.md

Asserts (the recipe's verified outcome):
  * semantic search matches "how do I add a printer" to printer.md even without
    exact keyword overlap -- the printer doc is the TOP hit (and a password
    query likewise returns the password doc), asserted from the live search
    tool result; and
  * the agent's final answer references the CORRECT source path (printer.md),
    grounding on what it found.

The PUT-by-path route mints an autogen ``document-<hex>`` id; the source PATH
the recipe cites travels on the hit's ``meta.document_name`` (the document name
defaults to the path), so source assertions key on that, not ``document_id``.

The embedding model is real and non-deterministic, so the assertion is on
SEMANTIC relevance + the cited source PATH, not exact answer wording.

The Q&A agent's behaviour is scripted (deterministic mock LLM) so the
search_collection call + the grounded final text are emitted every run; the
embedder, the indexer, and the vector search are REAL.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_terminal,
)
from tests._support.smk import smk
from tests._support.testconfig import load_config, requires

pytestmark = [pytest.mark.asyncio, requires("embedder", "pgvector")]


# --- Real-provider config (LM Studio embedder + pgvector SSP) --------------
def _embedder_cfg() -> dict:
    return load_config()["embedder"]


_PGVECTOR_DSN = {
    "hostname": "localhost",
    "port": 5432,
    "database": "primer_e2e",
    "username": "primer",
    "password": "primer",
}

# The KB docs. Each is path-addressed; the printer doc deliberately avoids the
# words in the test query ("add a printer") so the match is SEMANTIC, not
# keyword.
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

# The question whose only relevant doc is the printer guide.
_PRINTER_QUERY = "how do I add a printer in the office"


async def _make_embedder(client: httpx.AsyncClient, suffix: str) -> str:
    cfg = _embedder_cfg()
    eid = f"emb-rag-{suffix}"
    r = await client.post(
        "/v1/embedding_providers",
        json={
            "id": eid,
            "provider": "openai",
            "models": [{"name": cfg["model"]}],
            "config": {
                "url": cfg["base_url"],
                "api_key": cfg["api_key"],
                "flavor": "lmstudio",
            },
            "limits": {"max_concurrency": 2},
        },
    )
    assert r.status_code in (200, 201, 409), r.text
    return eid


async def _make_ssp(client: httpx.AsyncClient, suffix: str) -> str:
    sid = f"ssp-rag-{suffix}"
    r = await client.post(
        "/v1/ssp",
        json={"id": sid, "provider": "pgvector", "config": _PGVECTOR_DSN},
    )
    assert r.status_code in (200, 201, 409), r.text
    return sid


async def _search_with_retry(
    client: httpx.AsyncClient, cid: str, query: str, *,
    top_k: int = 3, attempts: int = 12, delay_s: float = 1.0,
) -> list[dict]:
    """POST the collection search, retrying until hits land.

    PUT-by-path indexes the body within the request, but the embed call is a
    real remote round-trip, so allow a few retries for the vector to become
    queryable before failing (the recipe documents embedding lag).
    """
    last: list[dict] = []
    for _ in range(attempts):
        r = await client.post(
            f"/v1/collections/{cid}/search",
            json={"query": query, "top_k": top_k},
        )
        assert r.status_code == 200, r.text
        last = r.json().get("hits", [])
        if last:
            return last
        await asyncio.sleep(delay_s)
    return last


@smk("SMK-COOKBOOK-07")
async def test_qa_agent_cites_source(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    registry, base_url = mock_llm
    sfx = unique_suffix
    cid = f"kb-rag-{sfx}"

    cleanup: list[str] = []
    try:
        eid = await _make_embedder(authed_client, sfx)
        sid_ssp = await _make_ssp(authed_client, sfx)

        # --- Create the KB collection bound to (embedder, SSP) -----------
        cfg = _embedder_cfg()
        r = await authed_client.post("/v1/collections", json={
            "id": cid,
            "description": "IT support knowledge base for question answering.",
            "embedder": {"provider_id": eid, "model": cfg["model"]},
            "search_provider_id": sid_ssp,
        })
        assert r.status_code in (200, 201), r.text
        cleanup.append(f"/v1/collections/{cid}")

        # --- Ingest the docs path-addressed (the recipe's PUT?path= path) -
        for path, content in _DOCS.items():
            r = await authed_client.put(
                f"/v1/collections/{cid}/documents",
                params={"path": path},
                json={"content": content},
            )
            assert r.status_code in (200, 201), r.text

        # --- Semantic relevance, asserted directly on the live search ----
        # "add a printer" -> printer.md on top, even without keyword overlap.
        # The source path lives on the hit's meta.document_name (the autogen
        # document_id is opaque), so rank assertions key on that.
        def _src(hit: dict) -> str:
            return str(hit.get("meta", {}).get("document_name", ""))

        printer_hits = await _search_with_retry(authed_client, cid, _PRINTER_QUERY)
        assert printer_hits, "real semantic search returned no hits for the KB"
        for h in printer_hits:
            assert {"document_id", "chunk_id", "score", "text", "meta"} <= set(h), h
        top_src = _src(printer_hits[0])
        assert top_src == _PRINTER_PATH, (
            f"printer query did not rank printer.md first: {[_src(h) for h in printer_hits]}"
        )
        # The sibling password query likewise returns the password doc on top
        # (proves the index discriminates, not just returns the only doc).
        pw_hits = await _search_with_retry(
            authed_client, cid, "I forgot my login and need to reset it")
        assert pw_hits and _src(pw_hits[0]) == _PASSWORD_PATH, (
            f"password query did not rank password.md first: {[_src(h) for h in pw_hits]}"
        )

        # --- The scripted Q&A agent grounds on the top hit + cites it ----
        # Rule 1 (no tool result): search the KB for the question.
        # Rule 2 (search result came back): answer citing the source path.
        scenario = f"scripted:rag-{sfx}"
        grounded_answer = (
            "Open System Settings, choose Printers, click Add and select "
            f"FLOOR3-HP. (Source: {_PRINTER_PATH})"
        )
        agent = await make_scripted_agent(
            authed_client, registry, base_url, suffix=f"rag{sfx}",
            scenario=scenario,
            tools=["system__search_collection"],
            system_prompt=[
                "Answer questions from the kb collection. First call "
                "search_collection, then answer grounded on the top hit and "
                "cite the document path."
            ],
            rules=[
                Rule(when_tool_result=False,
                     emit_tool="system__search_collection",
                     emit_args={"collection_id": cid, "query": _PRINTER_QUERY,
                                "top_k": 3}),
                Rule(when_tool_result=True, emit_text=grounded_answer),
            ],
        )

        wid = await make_local_workspace(authed_client, suffix=f"rag{sfx}", root=tmp_path)
        run = await start_agent_session(
            authed_client, workspace_id=wid, agent_id=agent["agent_id"],
            instructions=_PRINTER_QUERY,
        )
        final = await wait_terminal(authed_client, run, timeout_s=120)
        assert final.get("status") == "ended", final

        # The session's on-disk message log is the source of truth for the
        # full turn record (the turn_log endpoint carries event metadata only,
        # not message text). The local backend roots the workspace at
        # <provider_root>/<wid> and the agent executor commits the transcript
        # under .state/sessions/<sid>/messages.jsonl.
        msgs_file = tmp_path / wid / ".state" / "sessions" / run / "messages.jsonl"
        assert msgs_file.exists(), f"session messages.jsonl missing at {msgs_file}"
        transcript = msgs_file.read_text(encoding="utf-8")

        # The agent SAW the live search result, and that result carried the
        # correct source doc -- this is what makes the grounding real, not
        # scripted. A tool-role message holds the verbatim search output.
        assert '"role":"tool"' in transcript or '"role": "tool"' in transcript, (
            f"agent never received a tool result: {transcript!r}"
        )
        assert "system__search_collection" in transcript, (
            "agent did not call search_collection"
        )
        # The search hit surfaced the correct source path in its meta.
        assert "document_name" in transcript and _PRINTER_PATH in transcript, (
            f"search_collection did not surface {_PRINTER_PATH} to the agent: {transcript!r}"
        )

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
        for url in reversed(cleanup):
            await authed_client.delete(url)
        try:
            await authed_client.delete(f"/v1/ssp/ssp-rag-{sfx}")
        except Exception:
            pass
        try:
            await authed_client.delete(f"/v1/embedding_providers/emb-rag-{sfx}")
        except Exception:
            pass
