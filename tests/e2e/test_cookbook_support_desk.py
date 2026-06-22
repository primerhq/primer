"""Cookbook recipe #5 regression: omnichannel support desk (KB Q&A core).

This pins the AUTOMATABLE core of the support-desk recipe: a knowledge-base
collection plus a front-line support agent that calls
``system__search_collection`` and answers grounded in the hit, citing the
source. A second agent covers the escalation/specialist leg -- a billing
specialist answering independently from the same KB.

Recipe: primerhq.github.io/docs_source/cookbook/support-desk.md

NOT automated here (human-driven, verified MANUALLY, not CI-automatable): the
LIVE channel chat-inbound path -- a user posting in a Slack/Discord/Telegram
thread, the channel opening a chat bound to the front-line agent, and the
in-channel ``/new`` / ``/agent`` / ``/switch`` / ``/list`` commands + the
``POST /v1/chats/{id}/agent`` handoff. Chats are channel-driven (there is no
"post a message" REST endpoint), so the inbound turn cannot be synthesised in a
hermetic test. The recipe's live Discord round-trip is the manual coverage for
that surface. This module therefore exercises the KB-grounding and the
independent-specialist behaviour that the chat surface sits on top of.

Asserts (the recipe's verified outcome, minus the channel transport):
  * the front-line agent searches the KB and answers grounded on the
    password-reset doc, citing its source path; and
  * the billing specialist independently searches the same KB and answers
    grounded on the billing/refund doc, citing its source.

Agent behaviour is scripted (deterministic mock LLM); the embedder, the
indexer, and the vector search are REAL.
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


def _embedder_cfg() -> dict:
    return load_config()["embedder"]


_PGVECTOR_DSN = {
    "hostname": "localhost",
    "port": 5432,
    "database": "primer_e2e",
    "username": "primer",
    "password": "primer",
}

# Two support docs. The queries deliberately avoid exact keyword overlap so
# the match is semantic.
_PASSWORD_PATH = "password.md"
_BILLING_PATH = "billing.md"
_DOCS = {
    _PASSWORD_PATH: (
        "Resetting account credentials. Open id.company.com, click Forgot "
        "Password, enter your employee email, and follow the reset link. The "
        "reset link expires after 15 minutes."
    ),
    _BILLING_PATH: (
        "Refund policy for paid plans. To request money back, open the "
        "Billing page, choose the charge in question, and click Request "
        "Refund within 30 days of the invoice. Approved refunds return to the "
        "original payment method in 5 to 7 business days."
    ),
}

_PASSWORD_QUERY = "how do I reset my password"
_REFUND_QUERY = "how do I get my money back for a charge"


async def _make_embedder(client: httpx.AsyncClient, suffix: str) -> str:
    cfg = _embedder_cfg()
    eid = f"emb-sup-{suffix}"
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
    sid = f"ssp-sup-{suffix}"
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


def _final_assistant_text(transcript: str) -> str:
    last = ""
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
                    last = part["text"]
    return last


async def _run_kb_agent(
    authed_client, registry, base_url, *, suffix, scenario, cid, query,
    answer, tmp_path,
) -> str:
    """Build + run a scripted KB agent that searches then answers; return the
    on-disk transcript text."""
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=suffix, scenario=scenario,
        tools=["system__search_collection"],
        system_prompt=[
            "Answer from the kb collection. Call search_collection, then "
            "answer grounded on the hit and cite the document path."
        ],
        rules=[
            Rule(when_tool_result=False,
                 emit_tool="system__search_collection",
                 emit_args={"collection_id": cid, "query": query, "top_k": 3}),
            Rule(when_tool_result=True, emit_text=answer),
        ],
    )
    wid = await make_local_workspace(authed_client, suffix=suffix, root=tmp_path)
    run = await start_agent_session(
        authed_client, workspace_id=wid, agent_id=agent["agent_id"],
        instructions=query,
    )
    final = await wait_terminal(authed_client, run, timeout_s=120)
    assert final.get("status") == "ended", final
    msgs_file = tmp_path / wid / ".state" / "sessions" / run / "messages.jsonl"
    assert msgs_file.exists(), f"session messages.jsonl missing at {msgs_file}"
    return msgs_file.read_text(encoding="utf-8")


async def _seed_kb(authed_client, sfx) -> tuple[str, str, str]:
    """Create the KB collection + ingest the support docs. Returns (cid, eid,
    ssp_id)."""
    eid = await _make_embedder(authed_client, sfx)
    ssp_id = await _make_ssp(authed_client, sfx)
    cid = f"kb-sup-{sfx}"
    cfg = _embedder_cfg()
    r = await authed_client.post("/v1/collections", json={
        "id": cid,
        "description": "Support knowledge base.",
        "embedder": {"provider_id": eid, "model": cfg["model"]},
        "search_provider_id": ssp_id,
    })
    assert r.status_code in (200, 201), r.text
    for path, content in _DOCS.items():
        r = await authed_client.put(
            f"/v1/collections/{cid}/documents",
            params={"path": path},
            json={"content": content},
        )
        assert r.status_code in (200, 201), r.text
    return cid, eid, ssp_id


@smk("SMK-COOKBOOK-05")
async def test_kb_qa_agent_answers_grounded(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    """Front-line support agent: search the KB, answer grounded, cite source.

    This is the front-line leg the channel chat would drive in production. The
    channel inbound transport itself (chat thread, /agent switching) is
    human-verified and not automated here (see module docstring)."""
    registry, base_url = mock_llm
    sfx = unique_suffix
    cleanup: list[str] = []
    try:
        cid, eid, ssp_id = await _seed_kb(authed_client, sfx)
        cleanup.append(f"/v1/collections/{cid}")

        # The password query must rank the password doc on top -- proves the
        # KB grounding the agent relies on is real.
        hits = await _search_with_retry(authed_client, cid, _PASSWORD_QUERY)
        assert hits, "real semantic search returned no hits for the KB"
        top_src = str(hits[0].get("meta", {}).get("document_name", ""))
        assert top_src == _PASSWORD_PATH, (
            f"password query did not rank {_PASSWORD_PATH} first: "
            f"{[h.get('meta', {}).get('document_name') for h in hits]}"
        )

        answer = (
            "Go to id.company.com, click Forgot Password, enter your email and "
            f"follow the reset link. (Source: {_PASSWORD_PATH})"
        )
        transcript = await _run_kb_agent(
            authed_client, registry, base_url, suffix=f"fl{sfx}",
            scenario=f"scripted:support-fl-{sfx}", cid=cid,
            query=_PASSWORD_QUERY, answer=answer, tmp_path=tmp_path,
        )
        assert "system__search_collection" in transcript, "front-line agent did not search"
        assert "document_name" in transcript and _PASSWORD_PATH in transcript, (
            f"search did not surface {_PASSWORD_PATH} to the front-line agent: {transcript!r}"
        )
        assert _PASSWORD_PATH in _final_assistant_text(transcript), (
            "front-line answer did not cite the source doc"
        )
    finally:
        for url in reversed(cleanup):
            await authed_client.delete(url)
        await authed_client.delete(f"/v1/ssp/ssp-sup-{sfx}")
        await authed_client.delete(f"/v1/embedding_providers/emb-sup-{sfx}")


@smk("SMK-COOKBOOK-05")
async def test_specialist_agent_answers_independently(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    """Escalation leg: a billing specialist independently searches the SAME KB
    and answers from the billing doc, citing it.

    The recipe's handoff is "switch the chat's agent" -- the message history is
    preserved and the new agent answers afresh. The chat-switch transport is
    channel-driven (manual), but the specialist's KB-grounded answer (the
    behaviour that makes the handoff useful) is exercised here over the same
    knowledge base."""
    registry, base_url = mock_llm
    sfx = unique_suffix
    cleanup: list[str] = []
    try:
        cid, eid, ssp_id = await _seed_kb(authed_client, sfx)
        cleanup.append(f"/v1/collections/{cid}")

        # The refund query must rank the billing doc on top.
        hits = await _search_with_retry(authed_client, cid, _REFUND_QUERY)
        assert hits, "real semantic search returned no hits for the KB"
        top_src = str(hits[0].get("meta", {}).get("document_name", ""))
        assert top_src == _BILLING_PATH, (
            f"refund query did not rank {_BILLING_PATH} first: "
            f"{[h.get('meta', {}).get('document_name') for h in hits]}"
        )

        answer = (
            "Open the Billing page, choose the charge, and click Request "
            f"Refund within 30 days. (Source: {_BILLING_PATH})"
        )
        transcript = await _run_kb_agent(
            authed_client, registry, base_url, suffix=f"sp{sfx}",
            scenario=f"scripted:support-sp-{sfx}", cid=cid,
            query=_REFUND_QUERY, answer=answer, tmp_path=tmp_path,
        )
        assert "system__search_collection" in transcript, "specialist did not search"
        assert "document_name" in transcript and _BILLING_PATH in transcript, (
            f"search did not surface {_BILLING_PATH} to the specialist: {transcript!r}"
        )
        assert _BILLING_PATH in _final_assistant_text(transcript), (
            "specialist answer did not cite the source doc"
        )
    finally:
        for url in reversed(cleanup):
            await authed_client.delete(url)
        await authed_client.delete(f"/v1/ssp/ssp-sup-{sfx}")
        await authed_client.delete(f"/v1/embedding_providers/emb-sup-{sfx}")
