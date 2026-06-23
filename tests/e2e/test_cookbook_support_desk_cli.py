"""Cookbook recipe (CLI path): omnichannel support desk, driven by primectl.

The ``primectl``-driven sibling of ``test_cookbook_support_desk``. Where that
test pins the KB-grounding core over the REST API (and the live channel
round-trip is human-verified), this one drives the WHOLE desk over the published
CLI path, so the doc's "Via the CLI" chat commands are a tested contract, not
prose:

  * ``primectl create -f`` the embedder, the pgvector SSP, the KB collection,
    the scripted LLM provider, and the two agents (front-line + specialist);
  * ``primectl doc put`` the support docs;
  * ``primectl create -f`` a chat bound to the front-line agent;
  * ``primectl chat say`` the customer's question (which wakes the worker to run
    the turn over REST, no channel/WS needed);
  * ``primectl chat switch`` the chat to the billing specialist (the handoff);
  * ``primectl chat say`` the billing question to the specialist; and
  * ``primectl call chat messages`` to read each grounded reply back.

The success outcome asserted is the same as the API test's: the front-line agent
searches the KB and answers grounded on the password doc citing its path, then
the specialist (after the handoff, with the history preserved) independently
searches the same KB and answers grounded on the billing doc citing its path.

Agent behaviour is scripted (deterministic mock LLM); the embedder, indexer, and
vector search are REAL (LM Studio + pgvector), gated by ``requires``.

Recipe: primerhq.github.io/docs_source/cookbook/support-desk.md
"""
from __future__ import annotations

import json
import time

import httpx
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


def _chat_id_from_create(stdout: str) -> str:
    """Parse the ``chat/<id> created`` line ``create -f`` echoes."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("chat/") and "created" in line:
            return line.split("/", 1)[1].split()[0]
    raise AssertionError(f"could not parse chat id from create output:\n{stdout}")


def _messages(pc: Primectl, chat_id: str) -> list[dict]:
    """Read the full chat transcript via the CLI custom op.

    The chat resource exposes BOTH POST (send) and GET (list) at
    ``/chats/{id}/messages``; the registry keeps the first method on the bare
    ``messages`` action and suffixes the second, so the read-back GET is
    ``call chat messages-get``."""
    out = pc.run(
        "call", "chat", "messages-get", chat_id, "--param", "after_seq=0",
        "-o", "json",
    ).json()
    # The GET returns a paginated find result: {items: [...], ...}.
    return out["items"] if isinstance(out, dict) else out


def _delta_texts(items: list[dict]) -> list[str]:
    out: list[str] = []
    for it in items:
        if it.get("kind") == "assistant_token":
            payload = it.get("payload") or {}
            delta = payload.get("delta") or payload.get("content") or ""
            if delta:
                out.append(delta)
    return out


def _wait_idle_with_answer(
    pc: Primectl, chat_id: str, *, marker: str, timeout_s: float = 120.0,
) -> list[dict]:
    """Poll the chat until it is idle AND the transcript carries ``marker``.

    The reply is not streamed by ``chat say``; the worker drives the turn
    out of band, so we poll the chat row to idle and the messages for the
    grounded answer.
    """
    deadline = time.monotonic() + timeout_s
    last_items: list[dict] = []
    while time.monotonic() < deadline:
        row = pc.run("get", "chat", chat_id, "-r", "-o", "json").json()
        status = row.get("turn_status")
        if status == "idle":
            last_items = _messages(pc, chat_id)
            if any(marker in d for d in _delta_texts(last_items)):
                return last_items
        time.sleep(1.5)
    raise AssertionError(
        f"chat {chat_id} never produced an idle answer containing {marker!r} "
        f"within {timeout_s}s; deltas={_delta_texts(last_items)!r}"
    )


def _wait_agent(
    pc: Primectl, chat_id: str, *, agent_id: str, timeout_s: float = 60.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = pc.run("get", "chat", chat_id, "-r", "-o", "json").json()
        if last.get("agent_id") == agent_id:
            return last
        time.sleep(1.0)
    raise AssertionError(
        f"chat {chat_id} agent_id never switched to {agent_id!r}; last={last!r}"
    )


def _seed_kb_cli(pc: Primectl, *, cfg, sfx, tmp_path, cid, eid, ssp_id) -> None:
    pc.run("create", "-f", manifest(tmp_path, "emb", "embedding_provider", {
        "id": eid, "provider": "openai",
        "models": [{"name": cfg["model"]}],
        "config": {"url": cfg["base_url"], "api_key": cfg["api_key"], "flavor": "lmstudio"},
        "limits": {"max_concurrency": 2},
    }))
    pc.run("create", "-f", manifest(tmp_path, "ssp", "ssp", {
        "id": ssp_id, "provider": "pgvector", "config": _PGVECTOR_DSN,
    }))
    pc.run("create", "-f", manifest(tmp_path, "col", "collection", {
        "id": cid, "description": "Support knowledge base.",
        "embedder": {"provider_id": eid, "model": cfg["model"]},
        "search_provider_id": ssp_id,
    }))
    for path, content in _DOCS.items():
        pc.run("doc", "put", cid, path, "--content", content)


def _wait_search_hit(
    client: httpx.Client, cid: str, query: str, expect_path: str,
    *, attempts: int = 15, delay_s: float = 1.0,
) -> None:
    """Embedding is async; poll the collection search until the doc indexes.

    Uses the operator search endpoint directly (not a recipe operator step;
    this is the test confirming the live index is ready before the agent runs,
    exactly as the API test does with its retry helper)."""
    last: list[dict] = []
    for _ in range(attempts):
        r = client.post(f"/v1/collections/{cid}/search", json={"query": query, "top_k": 3})
        r.raise_for_status()
        last = r.json().get("hits", [])
        if last and str(last[0].get("meta", {}).get("document_name", "")) == expect_path:
            return
        time.sleep(delay_s)
    raise AssertionError(
        f"search never ranked {expect_path!r} first for {query!r}: "
        f"{[h.get('meta', {}).get('document_name') for h in last]}"
    )


@smk("SMK-COOKBOOK-CLI-14")
def test_support_desk_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    token = mint_token(base_url, name=f"cli-sup-{sfx}")
    pc = Primectl(base_url, token)
    # A plain authenticated client (cookie session) only for the async-index
    # readiness poll below; every recipe step is driven through ``pc``.
    probe = httpx.Client(base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0))
    probe.post("/v1/auth/login", json={"username": "e2e", "password": "e2e-password-123"})

    cfg = load_config()["embedder"]
    cid = f"kb-supcli-{sfx}"
    eid = f"emb-supcli-{sfx}"
    ssp_id = f"ssp-supcli-{sfx}"
    pid = f"p-supcli-{sfx}"
    fl_id = f"a-supcli-fl-{sfx}"
    sp_id = f"a-supcli-sp-{sfx}"

    fl_scenario = f"scripted:supcli-fl-{sfx}"
    sp_scenario = f"scripted:supcli-sp-{sfx}"
    fl_answer = (
        "Go to id.company.com, click Forgot Password, enter your email and "
        f"follow the reset link. (Source: {_PASSWORD_PATH})"
    )
    sp_answer = (
        "Open the Billing page, choose the charge, and click Request Refund "
        f"within 30 days. (Source: {_BILLING_PATH})"
    )
    # Front-line: search the KB, then answer grounded citing the source.
    registry.register(fl_scenario, [
        Rule(when_tool_result=False, emit_tool="system__search_collection",
             emit_args={"collection_id": cid, "query": _PASSWORD_QUERY, "top_k": 3}),
        Rule(when_tool_result=True, emit_text=fl_answer),
    ])
    # Specialist: same shape over the billing query.
    registry.register(sp_scenario, [
        Rule(when_tool_result=False, emit_tool="system__search_collection",
             emit_args={"collection_id": cid, "query": _REFUND_QUERY, "top_k": 3}),
        Rule(when_tool_result=True, emit_text=sp_answer),
    ])

    chat_id: str | None = None
    try:
        _seed_kb_cli(pc, cfg=cfg, sfx=sfx, tmp_path=tmp_path,
                     cid=cid, eid=eid, ssp_id=ssp_id)
        # Confirm the live index is ready (async embed) before the agent runs.
        _wait_search_hit(probe, cid, _PASSWORD_QUERY, _PASSWORD_PATH)
        _wait_search_hit(probe, cid, _REFUND_QUERY, _BILLING_PATH)

        # Scripted LLM provider both agents share.
        pc.run("create", "-f", manifest(tmp_path, "llm", "llm_provider", {
            "id": pid, "provider": "openchat",
            "models": [
                {"name": fl_scenario, "context_length": 8192},
                {"name": sp_scenario, "context_length": 8192},
            ],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))
        # Front-line + specialist agents.
        pc.run("create", "-f", manifest(tmp_path, "fl", "agent", {
            "id": fl_id, "description": "Front-line support.",
            "model": {"provider_id": pid, "model_name": fl_scenario},
            "tools": ["system__search_collection"],
            "system_prompt": [
                "You are front-line support. Answer from the kb collection "
                "(call search_collection) and cite the doc."
            ],
        }))
        pc.run("create", "-f", manifest(tmp_path, "sp", "agent", {
            "id": sp_id, "description": "Billing specialist.",
            "model": {"provider_id": pid, "model_name": sp_scenario},
            "tools": ["system__search_collection"],
            "system_prompt": [
                "You are a billing specialist. Answer billing questions in "
                "detail using the kb collection."
            ],
        }))

        # --- Open the chat on the front-line agent (CLI: create -f chat) ----
        chat_id = _chat_id_from_create(
            pc.run("create", "-f", manifest(tmp_path, "chat", "chat", {
                "agent_id": fl_id,
            })).stdout
        )

        # === 1. Customer asks; the front-line searches + answers grounded ===
        pc.run("chat", "say", chat_id, "how do I reset my password?")
        items = _wait_idle_with_answer(pc, chat_id, marker=_PASSWORD_PATH)
        # The front-line searched and the live search surfaced the password doc.
        assert any("system__search_collection" in json.dumps(it) for it in items), (
            f"front-line never searched the KB: {items!r}"
        )
        assert any(_PASSWORD_PATH in json.dumps(it) for it in items), (
            f"search did not surface {_PASSWORD_PATH}: {items!r}"
        )

        # === 2. Handoff: switch the chat to the billing specialist ===
        pc.run("chat", "switch", chat_id, sp_id)
        switched = _wait_agent(pc, chat_id, agent_id=sp_id)
        assert switched["agent_id"] == sp_id, switched
        # History preserved across the switch: the prior grounded answer stays.
        assert any(_PASSWORD_PATH in json.dumps(it) for it in _messages(pc, chat_id)), (
            "switch dropped the prior front-line history"
        )

        # === 3. Billing question; the specialist independently grounds ===
        pc.run("chat", "say", chat_id, "how do I get a refund?")
        items = _wait_idle_with_answer(pc, chat_id, marker=_BILLING_PATH)
        assert any(_BILLING_PATH in json.dumps(it) for it in items), (
            f"specialist search did not surface {_BILLING_PATH}: {items!r}"
        )
    finally:
        if chat_id is not None:
            pc.run("delete", "chat", chat_id, check=False)
        for res, ident in (
            ("agent", fl_id), ("agent", sp_id), ("collection", cid),
            ("llm_provider", pid), ("ssp", ssp_id), ("embedding_provider", eid),
        ):
            pc.run("delete", res, ident, check=False)
        probe.close()
