"""Cookbook recipe #8 regression: High-Precision Policy Desk.

A compliance / legal Q&A desk over a dense regulatory corpus where retrieval
PRECISION matters. Plain vector search returns fuzzily-relevant, near-duplicate
clauses; this desk turns on **cross-encoder reranking + MMR** on the collection
(``Collection.search = CollectionSearch{cer, mmr}``) so the top results are both
precise (the cross-encoder promotes the clause that truly answers the question)
and non-redundant (MMR drops near-duplicate chunks of the same decoy clause).

Recipe: primerhq.github.io/docs_source/cookbook/policy-desk.md

The corpus is designed so a phrasing-sensitive deadline question
("what is the deadline to notify the regulator of a breach?") makes the three
search modes DISAGREE in a way that proves each augmentation takes effect:

  * **control** (no ``search`` config -> plain vector): ranks the verbose,
    keyword-dense *escalation* decoy FIRST and floods the top-k with its three
    near-duplicate paraphrases -- the precise 72-hour breach clause is only #2;
  * **rerank** (``cer`` only): the cross-encoder reads each ``(query, clause)``
    pair jointly, recognises the terse 72-hour clause as the real answer, and
    promotes it to the TOP -- a demonstrable reordering vs plain vector;
  * **policy-kb** (``cer`` + ``mmr``): MMR additionally collapses the three
    near-duplicate escalation decoys down to one and diversifies the top-k with
    distinct clauses.

The scripted Q&A agent searches the **rerank** collection (precise clause at the
top) and grounds + cites that clause.

The embedder + cross-encoder are REAL (LM Studio nomic embed + a local
HuggingFace ``cross-encoder/ms-marco-MiniLM-L-6-v2`` reranker), so the
assertions are on the RANKING FLIP / de-duplication between the modes and on the
cited source PATH, never on exact scores or answer wording. The source PATH
travels on each hit's ``meta.document_name`` (PUT-by-path mints an opaque
``document-<hex>`` id), so rank assertions key on that.

Gated on the ``cross_encoder`` capability (plus ``embedder`` + ``pgvector``):
SKIPS cleanly where the reranker is not wired.
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

pytestmark = [
    pytest.mark.asyncio,
    requires("cross_encoder", "embedder", "pgvector"),
]


# --- Real-provider config --------------------------------------------------
def _embedder_cfg() -> dict:
    return load_config()["embedder"]


def _cross_encoder_cfg() -> dict:
    return load_config()["cross_encoder"]


_PGVECTOR_DSN = {
    "hostname": "localhost",
    "port": 5432,
    "database": "primer_e2e",
    "username": "primer",
    "password": "primer",
}

# --- The policy corpus -----------------------------------------------------
# The PRECISE answer to the deadline question is the terse 72-hour clause in
# `breach-notification.md`. The corpus also holds a verbose *escalation* decoy
# (`escalation-path.md`) plus two near-duplicate paraphrases of it: all three
# are LEXICALLY dense with the question's vocabulary (notify / regulator /
# breach / notification / deadline) but answer a DIFFERENT question -- WHO
# escalates the decision, not the deadline. A bi-encoder (embedding) over-
# rewards that keyword density and ranks a decoy first; a cross-encoder reads
# the (query, clause) pair jointly and promotes the terse 72-hour clause. The
# three decoy paraphrases also give MMR redundancy to collapse.

_BREACH_PATH = "breach-notification.md"
_DECOY_PATHS = [
    "escalation-path.md",
    "escalation-roster.md",
    "escalation-runbook.md",
]
_ACCESS_PATH = "access-control.md"
_RETENTION_PATH = "retention-window.md"

# The precise answer: terse, low keyword overlap with the verbose decoys.
_BREACH_DOC = (
    "A personal data breach must be notified to the regulator within 72 "
    "hours of discovery."
)

# The escalation decoy + two paraphrases: verbose, notify/regulator/breach/
# deadline keyword-dense, but about WHO escalates the decision, not the
# deadline itself.
_DECOY_BODIES = [
    (
        "When a personal data breach occurs, the team must notify the Data "
        "Protection Officer, who decides whether to notify the regulator. The "
        "notification escalation path and regulator notification roster set out "
        "who is responsible for each breach notification and how the notify-the-"
        "regulator decision and deadline are escalated."
    ),
    (
        "The breach escalation roster names who must notify the regulator. On "
        "any personal data breach, staff notify the Data Protection Officer, who "
        "escalates the regulator notification decision and the notification "
        "deadline along the escalation path."
    ),
    (
        "Escalation runbook for a personal data breach: notify the Data "
        "Protection Officer first; the officer drives the notify-the-regulator "
        "decision, the breach notification, and the escalation of the "
        "notification deadline to the regulator."
    ),
]

_ACCESS_DOC = (
    "Access to production systems requires multi-factor authentication and is "
    "reviewed quarterly by the security team."
)
_RETENTION_DOC = (
    "Breach notification records and regulator correspondence are retained for "
    "six years after a breach was notified to the regulator."
)

# The phrasing-sensitive question whose true answer is the terse 72-hour clause.
_POLICY_QUERY = "What is the deadline to notify the regulator of a personal data breach?"


def _corpus() -> dict[str, str]:
    docs = {
        _BREACH_PATH: _BREACH_DOC,
        _ACCESS_PATH: _ACCESS_DOC,
        _RETENTION_PATH: _RETENTION_DOC,
    }
    for path, body in zip(_DECOY_PATHS, _DECOY_BODIES, strict=True):
        docs[path] = body
    return docs


async def _make_embedder(client: httpx.AsyncClient, suffix: str) -> str:
    cfg = _embedder_cfg()
    eid = f"emb-pol-{suffix}"
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
    sid = f"ssp-pol-{suffix}"
    r = await client.post(
        "/v1/ssp",
        json={"id": sid, "provider": "pgvector", "config": _PGVECTOR_DSN},
    )
    assert r.status_code in (200, 201, 409), r.text
    return sid


async def _make_cross_encoder(
    client: httpx.AsyncClient, suffix: str,
) -> tuple[str, str]:
    cfg = _cross_encoder_cfg()
    cid = f"ce-pol-{suffix}"
    model = cfg["model"]
    r = await client.post(
        "/v1/cross_encoder_providers",
        json={
            "id": cid,
            "provider": "huggingface",
            "models": [{"name": model}],
            "config": {"token": None},
            "limits": {"max_concurrency": 1},
        },
    )
    assert r.status_code in (200, 201, 409), r.text
    return cid, model


async def _seed(client: httpx.AsyncClient, collection_id: str) -> None:
    for path, content in _corpus().items():
        r = await client.put(
            f"/v1/collections/{collection_id}/documents",
            params={"path": path},
            json={"content": content},
        )
        assert r.status_code in (200, 201), r.text


async def _search_with_retry(
    client: httpx.AsyncClient, cid: str, query: str, *,
    top_k: int, attempts: int = 15, delay_s: float = 1.0,
) -> list[dict]:
    """POST the collection search, retrying until hits land.

    PUT-by-path indexes within the request, but the embed call is a real remote
    round-trip; allow retries for the vectors to become queryable.
    """
    last: list[dict] = []
    for _ in range(attempts):
        r = await client.post(
            f"/v1/collections/{cid}/search",
            json={"query": query, "top_k": top_k},
        )
        assert r.status_code == 200, r.text
        last = r.json().get("hits", [])
        if len(last) >= min(top_k, 4):
            return last
        await asyncio.sleep(delay_s)
    return last


def _src(hit: dict) -> str:
    return str(hit.get("meta", {}).get("document_name", ""))


async def _make_collection(
    client: httpx.AsyncClient, *, coll_id: str, desc: str, eid: str,
    model: str, ssp: str, search: dict | None,
) -> None:
    body: dict = {
        "id": coll_id,
        "description": desc,
        "embedder": {"provider_id": eid, "model": model},
        "search_provider_id": ssp,
    }
    if search is not None:
        body["search"] = search
    r = await client.post("/v1/collections", json=body)
    assert r.status_code in (200, 201), r.text


@smk("SMK-COOKBOOK-17")
async def test_policy_desk_rerank_and_mmr(
    authed_client, mock_llm, unique_suffix, tmp_path,
):
    registry, base_url = mock_llm
    sfx = unique_suffix
    cfg = _embedder_cfg()
    model = cfg["model"]

    cleanup: list[str] = []
    try:
        eid = await _make_embedder(authed_client, sfx)
        sid_ssp = await _make_ssp(authed_client, sfx)
        ce_id, ce_model = await _make_cross_encoder(authed_client, sfx)

        cer = {
            "provider_id": ce_id,
            "model": ce_model,
            "top_n": 50,
            "batch_size": 32,
        }

        # Three collections over the SAME corpus + SAME embedder/SSP, differing
        # only in their `search` config: control (plain vector), rerank (CER
        # only), policy-kb (CER + MMR). Comparing their rankings isolates the
        # effect of each augmentation.
        ctrl_id = f"policy-ctrl-{sfx}"
        rerank_id = f"policy-rerank-{sfx}"
        kb_id = f"policy-kb-{sfx}"
        await _make_collection(
            authed_client, coll_id=ctrl_id,
            desc="Control: plain vector ranking, no rerank/MMR.",
            eid=eid, model=model, ssp=sid_ssp, search=None)
        cleanup.append(f"/v1/collections/{ctrl_id}")
        await _make_collection(
            authed_client, coll_id=rerank_id,
            desc="Cross-encoder rerank only (no MMR).",
            eid=eid, model=model, ssp=sid_ssp, search={"cer": cer})
        cleanup.append(f"/v1/collections/{rerank_id}")
        await _make_collection(
            authed_client, coll_id=kb_id,
            desc="High-precision compliance KB: cross-encoder rerank + MMR.",
            eid=eid, model=model, ssp=sid_ssp,
            search={"cer": cer, "mmr": {"lambda_mult": 0.5, "fetch_k": 50}})
        cleanup.append(f"/v1/collections/{kb_id}")

        for cid in (ctrl_id, rerank_id, kb_id):
            await _seed(authed_client, cid)

        # --- Control: plain vector ranks the precise clause WRONG -----------
        ctrl_hits = await _search_with_retry(
            authed_client, ctrl_id, _POLICY_QUERY, top_k=4)
        assert ctrl_hits, "control search returned no hits"
        ctrl_order = [_src(h) for h in ctrl_hits]
        ctrl_top = ctrl_order[0]
        assert ctrl_top != _BREACH_PATH, (
            "control (plain vector) unexpectedly ranked the precise breach "
            f"clause first; the corpus must make vector get it wrong: {ctrl_order}"
        )

        # --- Rerank (CER): cross-encoder promotes the precise clause to #1 --
        rr_hits = await _search_with_retry(
            authed_client, rerank_id, _POLICY_QUERY, top_k=4)
        assert rr_hits, "rerank search returned no hits"
        rr_order = [_src(h) for h in rr_hits]

        # (1) RERANK FLIP: the precise breach clause is now the TOP hit, where
        #     plain vector ranked a decoy first. This is the demonstrable
        #     cross-encoder reordering vs the control.
        assert rr_order[0] == _BREACH_PATH, (
            "cross-encoder rerank did not promote the precise breach clause to "
            f"the top.\n  control (vector): {ctrl_order}\n  rerank (cer): "
            f"{rr_order}"
        )
        assert rr_order[0] != ctrl_top, (
            "rerank top hit equals the control top hit; the cross-encoder did "
            f"not reorder.\n  control: {ctrl_order}\n  rerank: {rr_order}"
        )

        # --- policy-kb (CER + MMR): MMR diversifies the reranked pool -------
        kb_hits = await _search_with_retry(
            authed_client, kb_id, _POLICY_QUERY, top_k=4)
        assert kb_hits, "policy-kb search returned no hits"
        kb_order = [_src(h) for h in kb_hits]

        # (2) MMR DIVERSIFICATION: the control top-k floods with the near-
        #     duplicate escalation paraphrases; policy-kb's top-k holds strictly
        #     FEWER of them (MMR collapsed the redundancy) and is at least as
        #     distinct.
        ctrl_decoys = sum(1 for s in ctrl_order if s in _DECOY_PATHS)
        kb_decoys = sum(1 for s in kb_order if s in _DECOY_PATHS)
        assert ctrl_decoys >= 2, (
            "the control top-k should flood with near-duplicate decoys for MMR "
            f"to have something to collapse: {ctrl_order}"
        )
        assert kb_decoys < ctrl_decoys, (
            "MMR did not reduce the near-duplicate escalation decoys relative to "
            f"the control.\n  control: {ctrl_order}\n  policy-kb: {kb_order}"
        )
        assert len(set(kb_order)) >= len(set(ctrl_order)), (
            "MMR did not diversify: policy-kb top-k is no more distinct than the "
            f"control.\n  control: {ctrl_order}\n  policy-kb: {kb_order}"
        )

        # --- The scripted Q&A agent grounds on the reranked top hit ---------
        # It searches the rerank collection (precise clause at the top) and
        # cites that clause path.
        scenario = f"scripted:policy-{sfx}"
        grounded_answer = (
            "A personal data breach must be reported to the supervisory "
            f"authority within 72 hours. (Source: {_BREACH_PATH})"
        )
        agent = await make_scripted_agent(
            authed_client, registry, base_url, suffix=f"pol{sfx}",
            scenario=scenario,
            tools=["system__search_collection"],
            system_prompt=[
                "You are a compliance policy desk. Answer the question by first "
                "calling search_collection on the policy KB, then answer grounded "
                "on the top reranked hit and cite its document path."
            ],
            rules=[
                Rule(when_tool_result=False,
                     emit_tool="system__search_collection",
                     emit_args={"collection_id": rerank_id,
                                "query": _POLICY_QUERY, "top_k": 4}),
                Rule(when_tool_result=True, emit_text=grounded_answer),
            ],
        )

        wid = await make_local_workspace(
            authed_client, suffix=f"pol{sfx}", root=tmp_path)
        run = await start_agent_session(
            authed_client, workspace_id=wid, agent_id=agent["agent_id"],
            instructions=_POLICY_QUERY,
        )
        final = await wait_terminal(authed_client, run, timeout_s=120)
        assert final.get("status") == "ended", final

        msgs_file = tmp_path / wid / ".state" / "sessions" / run / "messages.jsonl"
        assert msgs_file.exists(), f"session messages.jsonl missing at {msgs_file}"
        transcript = msgs_file.read_text(encoding="utf-8")

        # The agent SAW the live (reranked) search result, and that result put
        # the precise breach clause on top -- this is what makes the grounding
        # real, not scripted.
        assert "system__search_collection" in transcript, (
            "agent did not call search_collection"
        )
        assert '"role":"tool"' in transcript or '"role": "tool"' in transcript, (
            "agent never received a tool result"
        )
        assert _BREACH_PATH in transcript, (
            f"reranked search did not surface {_BREACH_PATH} to the agent"
        )

        # The FINAL assistant message grounds on the reranked hit + cites it.
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
        assert _BREACH_PATH in last_assistant, (
            f"final answer did not cite {_BREACH_PATH}: {last_assistant!r}"
        )
    finally:
        for url in reversed(cleanup):
            await authed_client.delete(url)
        for url in (
            f"/v1/cross_encoder_providers/ce-pol-{sfx}",
            f"/v1/ssp/ssp-pol-{sfx}",
            f"/v1/embedding_providers/emb-pol-{sfx}",
        ):
            try:
                await authed_client.delete(url)
            except Exception:
                pass
