"""Cookbook recipe (CLI path): high-precision policy desk, driven by primectl.

The ``primectl``-driven sibling of ``test_cookbook_policy_desk``. It performs
every setup step with the exact ``primectl`` commands the rewritten doc shows
(``create -f`` the cross-encoder provider + the three collections that differ
only in their ``search`` config, ``doc put`` the corpus, ``create -f`` the
agent, ``session run`` the query) and asserts the recipe's verified outcome
purely through the CLI:

  * CONTROL (no ``search`` config -> plain vector) ranks a verbose escalation
    decoy FIRST, flooding the top-k with its near-duplicate paraphrases;
  * RERANK (``cer`` only) promotes the terse 72-hour breach clause to the TOP
    (a demonstrable cross-encoder reordering vs the control); and
  * KB (``cer`` + ``mmr``) collapses the near-duplicate decoys so its top-k is
    strictly less redundant than the control's.

The three searches are issued with ``primectl call collection search`` (the
per-collection POST search), reading the same ``hits`` the API test reads.
The scripted Q&A agent then searches the rerank collection and cites the
promoted clause; the answer is read back via ``primectl workspace files get``.

Embedder + cross-encoder are REAL (LM Studio + a local HuggingFace reranker);
the agent is scripted via the shared in-process ``mock_llm``. Gated on
``cross_encoder`` + ``embedder`` + ``pgvector``.

Recipe: primerhq.github.io/docs_source/cookbook/policy-desk.md
"""
from __future__ import annotations

import json

import pytest

from tests._support.mock_llm import Rule
from tests._support.primectl_driver import Primectl, manifest, mint_token
from tests._support.smk import smk
from tests._support.testconfig import load_config, requires

pytestmark = [requires("cross_encoder", "embedder", "pgvector")]


_PGVECTOR_DSN = {
    "hostname": "localhost",
    "port": 5432,
    "database": "primer_e2e",
    "username": "primer",
    "password": "primer",
}

_BREACH_PATH = "breach-notification.md"
_DECOY_PATHS = ["escalation-path.md", "escalation-roster.md", "escalation-runbook.md"]
_ACCESS_PATH = "access-control.md"
_RETENTION_PATH = "retention-window.md"

_BREACH_DOC = (
    "A personal data breach must be notified to the regulator within 72 "
    "hours of discovery."
)
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


def _src(hit: dict) -> str:
    return str(hit.get("meta", {}).get("document_name", ""))


def _search(pc: Primectl, tmp_path, cid: str, query: str, *, top_k: int,
            attempts: int = 15) -> list[dict]:
    """POST a collection search via the CLI, retrying until hits land.

    PUT-by-path indexes within the request, but the embed call is a real remote
    round-trip; allow retries for the vectors to become queryable.
    """
    import time

    qfile = manifest_body(tmp_path, f"q-{cid}", {"query": query, "top_k": top_k})
    last: list[dict] = []
    for _ in range(attempts):
        out = pc.run("call", "collection", "search", cid, "-f", qfile, "-o", "json")
        last = out.json().get("hits", [])
        if len(last) >= min(top_k, 4):
            return last
        time.sleep(1.0)
    return last


def manifest_body(tmp_path, name: str, body: dict) -> str:
    """Write a bare JSON request body (for ``call ... -f``)."""
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(body))
    return str(path)


@smk("SMK-COOKBOOK-CLI-02")
def test_policy_desk_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-pol-{sfx}"))

    cfg = load_config()["embedder"]
    ce_cfg = load_config()["cross_encoder"]
    model = cfg["model"]
    ce_model = ce_cfg["model"]

    eid = f"emb-pol-cli-{sfx}"
    ssp_id = f"ssp-pol-cli-{sfx}"
    ce_id = f"ce-pol-cli-{sfx}"
    pid = f"p-pol-cli-{sfx}"
    aid = f"a-pol-cli-{sfx}"
    wp = f"wp-pol-cli-{sfx}"
    tpl = f"tpl-pol-cli-{sfx}"
    ctrl_id = f"policy-ctrl-cli-{sfx}"
    rerank_id = f"policy-rerank-cli-{sfx}"
    kb_id = f"policy-kb-cli-{sfx}"

    scenario = f"scripted:policy-cli-{sfx}"
    grounded_answer = (
        "A personal data breach must be reported to the supervisory "
        f"authority within 72 hours. (Source: {_BREACH_PATH})"
    )
    registry.register(scenario, [
        Rule(when_tool_result=False,
             emit_tool="system__search_collection",
             emit_args={"collection_id": rerank_id, "query": _POLICY_QUERY, "top_k": 4}),
        Rule(when_tool_result=True, emit_text=grounded_answer),
    ])

    cleanup = [("collection", ctrl_id), ("collection", rerank_id), ("collection", kb_id),
               ("agent", aid), ("llm_provider", pid), ("cross_encoder_provider", ce_id),
               ("ssp", ssp_id), ("embedding_provider", eid)]
    try:
        # --- Providers (embedder + pgvector SSP + cross-encoder) --------
        pc.run("create", "-f", manifest(tmp_path, "emb", "embedding_provider", {
            "id": eid, "provider": "openai", "models": [{"name": model}],
            "config": {"url": cfg["base_url"], "api_key": cfg["api_key"], "flavor": "lmstudio"},
            "limits": {"max_concurrency": 2},
        }))
        pc.run("create", "-f", manifest(tmp_path, "ssp", "ssp", {
            "id": ssp_id, "provider": "pgvector", "config": _PGVECTOR_DSN,
        }))
        pc.run("create", "-f", manifest(tmp_path, "ce", "cross_encoder_provider", {
            "id": ce_id, "provider": "huggingface", "models": [{"name": ce_model}],
            "config": {"token": None}, "limits": {"max_concurrency": 1},
        }))

        cer = {"provider_id": ce_id, "model": ce_model, "top_n": 50, "batch_size": 32}

        # --- Three collections over the SAME corpus, differing only in search.
        pc.run("create", "-f", manifest(tmp_path, "ctrl", "collection", {
            "id": ctrl_id, "description": "Control: plain vector ranking.",
            "embedder": {"provider_id": eid, "model": model}, "search_provider_id": ssp_id,
        }))
        pc.run("create", "-f", manifest(tmp_path, "rr", "collection", {
            "id": rerank_id, "description": "Cross-encoder rerank only (no MMR).",
            "embedder": {"provider_id": eid, "model": model}, "search_provider_id": ssp_id,
            "search": {"cer": cer},
        }))
        pc.run("create", "-f", manifest(tmp_path, "kb", "collection", {
            "id": kb_id, "description": "High-precision compliance KB: rerank + MMR.",
            "embedder": {"provider_id": eid, "model": model}, "search_provider_id": ssp_id,
            "search": {"cer": cer, "mmr": {"lambda_mult": 0.5, "fetch_k": 50}},
        }))

        # --- Seed the same corpus into each collection (doc put) --------
        for cid in (ctrl_id, rerank_id, kb_id):
            for path, content in _corpus().items():
                pc.run("doc", "put", cid, path, "--content", content)

        # --- Control: plain vector ranks the precise clause WRONG -------
        ctrl_hits = _search(pc, tmp_path, ctrl_id, _POLICY_QUERY, top_k=4)
        assert ctrl_hits, "control search returned no hits"
        ctrl_order = [_src(h) for h in ctrl_hits]
        ctrl_top = ctrl_order[0]
        assert ctrl_top != _BREACH_PATH, (
            f"control (plain vector) unexpectedly ranked breach clause first: {ctrl_order}"
        )

        # --- Rerank (CER): cross-encoder promotes the precise clause ----
        rr_hits = _search(pc, tmp_path, rerank_id, _POLICY_QUERY, top_k=4)
        assert rr_hits, "rerank search returned no hits"
        rr_order = [_src(h) for h in rr_hits]
        assert rr_order[0] == _BREACH_PATH, (
            f"rerank did not promote the breach clause.\n control: {ctrl_order}\n rerank: {rr_order}"
        )
        assert rr_order[0] != ctrl_top, (
            f"rerank top equals control top; no reorder.\n control: {ctrl_order}\n rerank: {rr_order}"
        )

        # --- KB (CER + MMR): MMR diversifies the reranked pool ----------
        kb_hits = _search(pc, tmp_path, kb_id, _POLICY_QUERY, top_k=4)
        assert kb_hits, "policy-kb search returned no hits"
        kb_order = [_src(h) for h in kb_hits]
        ctrl_decoys = sum(1 for s in ctrl_order if s in _DECOY_PATHS)
        kb_decoys = sum(1 for s in kb_order if s in _DECOY_PATHS)
        assert ctrl_decoys >= 2, f"control top-k should flood with decoys: {ctrl_order}"
        assert kb_decoys < ctrl_decoys, (
            f"MMR did not reduce decoys.\n control: {ctrl_order}\n policy-kb: {kb_order}"
        )
        assert len(set(kb_order)) >= len(set(ctrl_order)), (
            f"MMR did not diversify.\n control: {ctrl_order}\n policy-kb: {kb_order}"
        )

        # --- The scripted Q&A agent grounds on the reranked top hit -----
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [{"name": scenario, "context_length": 8192}],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))
        pc.run("create", "-f", manifest(tmp_path, "agent", "agent", {
            "id": aid, "description": "Answers compliance questions with citations.",
            "model": {"provider_id": pid, "model_name": scenario},
            "tools": ["system__search_collection"], "max_tool_turns": 6,
            "system_prompt": [
                "You are a compliance policy desk. Answer by first calling "
                "search_collection on the policy KB, then answer grounded on the "
                "top reranked hit and cite its document path."
            ],
        }))
        pc.run("create", "-f", manifest(tmp_path, "wp", "workspace_provider", {
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        }))
        pc.run("create", "-f", manifest(tmp_path, "tpl", "workspace_template", {
            "id": tpl, "description": "policy cli", "provider_id": wp, "backend": {"kind": "local"},
        }))
        wid = pc.run("create", "workspace", "--set", f"template_id={tpl}").stdout.split("/")[1].split()[0]

        run = pc.run("session", "run", wid, "--agent", aid, "-i", _POLICY_QUERY, "--timeout", "150")
        assert "ended: completed" in run.stdout, run.stdout

        sid = None
        for line in run.stdout.splitlines():
            if line.startswith("session/") and "started" in line:
                sid = line.split("/", 1)[1].split()[0]
                break
        assert sid, f"could not parse session id:\n{run.stdout}"

        transcript = pc.run(
            "workspace", "files", "get", wid,
            f".state/sessions/{sid}/messages.jsonl", "--content",
        ).stdout
        assert "system__search_collection" in transcript, transcript
        assert '"role":"tool"' in transcript or '"role": "tool"' in transcript, transcript
        assert _BREACH_PATH in transcript, transcript

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
        for res, ident in cleanup:
            pc.run("delete", res, ident, check=False)
