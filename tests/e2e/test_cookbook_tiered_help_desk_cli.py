"""Cookbook recipe (CLI path): tiered help desk with supervisor sign-off.

The ``primectl``-driven sibling of ``test_cookbook_tiered_help_desk``. Where that
test drives the full chat-HITL loop over the chat WebSocket, this one drives the
identical desk over the published CLI path, so the doc's "Via the CLI" chat
commands are a tested contract:

  * ``primectl create -f`` the embedder, the pgvector SSP, the KB collection, the
    scripted LLM provider, the two agents, the chat, and the required
    tool-approval policy;
  * ``primectl call tool_approval_policy invalidate`` to refresh the resolver;
  * ``primectl chat say`` the customer's refund request (waking the worker to run
    the turn over REST, no channel/WS);
  * ``primectl chat say`` the customer's inline ``ask_user`` answer (consumed as
    the pending call's tool_result, the soft-yield resume); and
  * ``primectl chat say`` the supervisor's approve/reject decision.

The same outcome the WS test asserts is checked back here: the soft-yield
``ask_user`` (pending at idle, no park), the ``switch_to_agent`` handoff (with
history preserved), and the supervisor-gated refund resolved BOTH ways (approve
runs it + records approved; reject denies + records rejected).

Agent behaviour is scripted (deterministic mock LLM); the embedder, indexer, and
vector search are REAL. The approve/reject DECISION is operator-driven (the
supervisor's message), never scripted into the model. The gated refund stands in
with the built-in ``misc__uuid_v4`` (it runs for real on approve).

NOTE: the durable approval-record audit read (``GET /v1/tool_approval/records``)
has no first-class primectl verb, so it is read via ``primectl raw`` here. This
is the operator-side residual leg the migration tracks; the recipe's primary CLI
surface (``chat say``/``switch``, ``create -f``, the policy invalidate) is
first-class.

Recipe: primerhq.github.io/docs_source/cookbook/tiered-help-desk.md
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

_REFUND_PATH = "refund-policy.md"
_PASSWORD_PATH = "password.md"
_DOCS = {
    _REFUND_PATH: (
        "Refund policy for paid plans. To request money back, open the "
        "Billing page, choose the charge in question, and click Request "
        "Refund within 30 days of the invoice. Approved refunds return to "
        "the original payment method in 5 to 7 business days. Refunds above "
        "500 dollars require a supervisor sign-off."
    ),
    _PASSWORD_PATH: (
        "Resetting account credentials. Open id.company.com, click Forgot "
        "Password, enter your employee email, and follow the reset link. "
        "The reset link expires after 15 minutes."
    ),
}

_REFUND_QUERY = "how do I get my money back for a charge"
_ASK_USER_REPLY = "It was 900 dollars."
_ASK_USER_REPLY_MARKER = "900 dollars"
_GATED_TOOL = "uuid_v4"  # stands in for billing__issue_refund


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _chat_id_from_create(stdout: str) -> str:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("chat/") and "created" in line:
            return line.split("/", 1)[1].split()[0]
    raise AssertionError(f"could not parse chat id from create output:\n{stdout}")


def _chat_row(pc: Primectl, chat_id: str) -> dict:
    return pc.run("get", "chat", chat_id, "-r", "-o", "json").json()


def _messages(pc: Primectl, chat_id: str) -> list[dict]:
    out = pc.run(
        "call", "chat", "messages-get", chat_id, "--param", "after_seq=0",
        "-o", "json",
    ).json()
    return out["items"] if isinstance(out, dict) else out


def _tool_results(items: list[dict]) -> list[dict]:
    return [it for it in items if it.get("kind") == "tool_result"]


def _delta_texts(items: list[dict]) -> list[str]:
    out: list[str] = []
    for it in items:
        if it.get("kind") == "assistant_token":
            payload = it.get("payload") or {}
            delta = payload.get("delta") or payload.get("content") or ""
            if delta:
                out.append(delta)
    return out


def _wait_pending(
    pc: Primectl, chat_id: str, *, expect_mode: str, timeout_s: float = 90.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = _chat_row(pc, chat_id)
        ptc = last.get("pending_tool_call")
        if ptc is not None and ptc.get("mode") == expect_mode and \
                last.get("turn_status") == "idle":
            return last
        time.sleep(1.0)
    raise AssertionError(
        f"chat {chat_id} never recorded a {expect_mode!r} pending_tool_call at "
        f"idle within {timeout_s}s; last={last!r}"
    )


def _wait_agent(
    pc: Primectl, chat_id: str, *, agent_id: str, timeout_s: float = 90.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = _chat_row(pc, chat_id)
        if last.get("agent_id") == agent_id:
            return last
        time.sleep(1.0)
    raise AssertionError(
        f"chat {chat_id} agent_id never switched to {agent_id!r}; last={last!r}"
    )


def _wait_cleared(
    pc: Primectl, chat_id: str, *, timeout_s: float = 90.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = _chat_row(pc, chat_id)
        if last.get("pending_tool_call") is None and \
                last.get("turn_status") == "idle":
            return last
        time.sleep(1.0)
    raise AssertionError(
        f"chat {chat_id} pending_tool_call never cleared within {timeout_s}s; "
        f"last={last!r}"
    )


def _wait_search_hit(
    client: httpx.Client, cid: str, query: str, expect_path: str,
    *, attempts: int = 15, delay_s: float = 1.0,
) -> None:
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


@smk("SMK-COOKBOOK-CLI-15")
def test_tiered_help_desk_cli(base_url, mock_llm, unique_suffix, tmp_path):
    registry, mock_base_url = mock_llm
    sfx = unique_suffix
    pc = Primectl(base_url, mint_token(base_url, name=f"cli-thd-{sfx}"))
    probe = httpx.Client(base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0))
    probe.post("/v1/auth/login", json={"username": "e2e", "password": "e2e-password-123"})

    cfg = load_config()["embedder"]
    cid = f"kb-thdcli-{sfx}"
    eid = f"emb-thdcli-{sfx}"
    ssp_id = f"ssp-thdcli-{sfx}"
    pid = f"p-thdcli-{sfx}"

    answer = (
        "Open the Billing page, choose the charge, and click Request Refund "
        f"within 30 days. (Source: {_REFUND_PATH})"
    )

    # Seed the KB once; reused by both the approve and reject runs.
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
        "id": cid, "description": "Tiered help-desk knowledge base.",
        "embedder": {"provider_id": eid, "model": cfg["model"]},
        "search_provider_id": ssp_id,
    }))
    for path, content in _DOCS.items():
        pc.run("doc", "put", cid, path, "--content", content)
    _wait_search_hit(probe, cid, _REFUND_QUERY, _REFUND_PATH)

    # One scripted LLM provider carrying both agents' scenarios per run.
    base_cleanup = [
        ("collection", cid), ("ssp", ssp_id), ("embedding_provider", eid),
    ]

    def _run_desk(*, decision: str) -> None:
        """Drive one full chat conversation; ``decision`` is the supervisor's
        reply ("yes" -> approve, "no" -> reject)."""
        local = f"{decision}-{sfx}"
        fl_id = f"a-thdcli-fl-{local}"
        bill_id = f"a-thdcli-bill-{local}"
        pol_id = f"thdcli-pol-{local}"
        fl_scenario = f"scripted:thdcli-fl-{local}"
        bill_scenario = f"scripted:thdcli-bill-{local}"

        # Front-line: search -> answer + ask_user -> (after the consumed reply)
        # switch_to_agent. Discriminated on the tool-result OUTPUT the model
        # sees (search hits carry the refund path; the consumed ask_user reply
        # carries the customer's verbatim answer).
        registry.register(fl_scenario, [
            Rule(when_tool_result=False, emit_tool="system__search_collection",
                 emit_args={"collection_id": cid, "query": _REFUND_QUERY, "top_k": 3},
                 emit_tool_call_id="call_search"),
            Rule(when_last_tool_result_contains=_ASK_USER_REPLY_MARKER,
                 emit_tool="system__switch_to_agent",
                 emit_args={"agent_id": bill_id,
                            "prompt": "Customer wants a 900 dollar refund. "
                                      "Please process it."},
                 emit_tool_call_id="call_switch"),
            Rule(when_last_tool_result_contains=_REFUND_PATH,
                 emit_tool="system__ask_user",
                 emit_args={"prompt": f"{answer} What is the charge amount?"},
                 emit_tool_call_id="call_ask"),
            Rule(when_tool_result=True, emit_text=answer),
        ])
        # Billing specialist: issue the (gated) refund, then report.
        registry.register(bill_scenario, [
            Rule(when_tool_result=False, emit_tool=f"misc__{_GATED_TOOL}",
                 emit_args={}, emit_tool_call_id="call_refund"),
            Rule(when_tool_result=True,
                 emit_text="Your refund request has been processed."),
        ])

        pc.run("create", "-f", manifest(tmp_path, f"llm-{local}", "llm_provider", {
            "id": pid + f"-{decision}", "provider": "openchat",
            "models": [
                {"name": fl_scenario, "context_length": 8192},
                {"name": bill_scenario, "context_length": 8192},
            ],
            "config": {"url": mock_base_url, "flavor": "lmstudio"},
            "limits": {"max_concurrency": 4},
        }))
        pc.run("create", "-f", manifest(tmp_path, f"fl-{local}", "agent", {
            "id": fl_id, "description": "Front-line support.",
            "model": {"provider_id": pid + f"-{decision}", "model_name": fl_scenario},
            "tools": ["system__search_collection", "system__ask_user",
                      "system__switch_to_agent"],
            "system_prompt": [
                "You are front-line support. Search the kb collection and "
                "answer grounded; ask the customer for the charge amount, then "
                "hand off to billing."
            ],
        }))
        pc.run("create", "-f", manifest(tmp_path, f"bill-{local}", "agent", {
            "id": bill_id, "description": "Billing specialist; issues refunds.",
            "model": {"provider_id": pid + f"-{decision}", "model_name": bill_scenario},
            "tools": [f"misc__{_GATED_TOOL}"],
            "system_prompt": [
                "You are a billing specialist. Issue the refund the customer "
                "requested; large refunds require a supervisor sign-off."
            ],
        }))

        # Gate the refund with a required policy + invalidate the resolver.
        pc.run("create", "-f", manifest(tmp_path, f"pol-{local}", "tool_approval_policy", {
            "id": pol_id, "toolset_id": "misc", "tool_name": _GATED_TOOL,
            "enabled": True, "approval": {"type": "required"},
            "timeout_seconds": 600,
        }))
        pc.run("call", "tool_approval_policy", "invalidate")

        chat_id: str | None = None
        cleanup = [
            ("tool_approval_policy", pol_id), ("agent", fl_id),
            ("agent", bill_id), ("llm_provider", pid + f"-{decision}"),
        ]
        try:
            chat_id = _chat_id_from_create(
                pc.run("create", "-f", manifest(tmp_path, f"chat-{local}", "chat", {
                    "agent_id": fl_id,
                })).stdout
            )

            # === 1. Customer asks about a refund (CLI: chat say) ===
            pc.run("chat", "say", chat_id, "I want a refund for a 900 dollar charge.")

            # === 2. Front-line searched, answered grounded, soft-yielded on
            #        ask_user (NO park) ===
            body = _wait_pending(pc, chat_id, expect_mode="ask_user")
            assert body["turn_status"] == "idle", body
            assert "parked_status" not in body, body
            items = _messages(pc, chat_id)
            assert any("system__search_collection" in json.dumps(it) for it in items), (
                f"front-line never searched the KB: {items!r}"
            )
            assert any(_REFUND_PATH in json.dumps(tr.get("payload") or {})
                       for tr in _tool_results(items)), (
                f"search did not surface {_REFUND_PATH}: {items!r}"
            )
            deltas = _delta_texts(items)
            assert any(_REFUND_PATH in d for d in deltas), (
                f"grounded answer did not cite {_REFUND_PATH}: {deltas}"
            )
            assert any("charge amount" in d for d in deltas), (
                f"inline ask_user question not surfaced: {deltas}"
            )

            # === 3. Customer answers the inline question; consumed as the
            #        ask_user result; front-line hands off ===
            pc.run("chat", "say", chat_id, _ASK_USER_REPLY)
            switched = _wait_agent(pc, chat_id, agent_id=bill_id)
            assert switched["agent_id"] == bill_id, switched
            after = _messages(pc, chat_id)
            joined = json.dumps(after)
            assert _REFUND_PATH in joined, "switch dropped the prior KB history"
            assert any("900 dollar charge" in str((it.get("payload") or {}))
                       for it in after), "switch dropped the original request"

            # === 4. The specialist's refund tripped the approval gate ===
            gate = _wait_pending(pc, chat_id, expect_mode="approval")
            assert gate["pending_tool_call"]["mode"] == "approval", gate
            assert "parked_status" not in gate, gate

            # === 5. The supervisor resolves the gate (CLI: chat say) ===
            pc.run("chat", "say", chat_id, decision)
            cleared = _wait_cleared(pc, chat_id)
            assert cleared["pending_tool_call"] is None, cleared

            items = _messages(pc, chat_id)
            trs = _tool_results(items)
            assert trs, f"no tool_result after the gate resolved: {items!r}"
            last_tr = trs[-1]
            payload = last_tr.get("payload") or {}
            if decision == "yes":
                assert not payload.get("error"), (
                    f"approved refund did not run cleanly: {last_tr!r}"
                )
            else:
                assert payload.get("error"), f"rejected refund still ran: {last_tr!r}"
                assert "declin" in json.dumps(payload).lower(), last_tr

            # The durable audit trail. No first-class verb for the records read,
            # so this is the tracked operator-side ``raw`` residual leg.
            rec_status = "approved" if decision == "yes" else "rejected"
            recs = pc.run(
                "raw", "GET", "/v1/tool_approval/records",
                "--param", f"status={rec_status}", "--param", "length=100",
                "-o", "json",
            ).json()
            ours = [r for r in recs.get("items", []) if r.get("chat_id") == chat_id]
            assert ours, (
                f"no {rec_status!r} ToolApprovalRecord for chat {chat_id}: "
                f"{recs.get('items')!r}"
            )
            assert ours[0]["decision"] == rec_status, ours[0]
        finally:
            if chat_id is not None:
                pc.run("delete", "chat", chat_id, check=False)
            for res, ident in cleanup:
                pc.run("delete", res, ident, check=False)

    try:
        _run_desk(decision="yes")   # APPROVE: the gated refund runs.
        _run_desk(decision="no")    # REJECT: the gated refund is denied.
    finally:
        for res, ident in base_cleanup:
            pc.run("delete", res, ident, check=False)
        probe.close()
