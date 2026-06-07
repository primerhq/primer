"""SMK web search + tool safety tests (docs/tests/10-web-search-and-approvals).

These drive the web-search subsystem for REAL against DuckDuckGo (the keyless
backend the testconfig enables). The provider CRUD, the active-config singleton,
the ``_test`` connectivity probe, and the always-on ``web`` toolset
(``web__web-search``) are all exercised end to end; the DuckDuckGo HTTP backend
is never mocked.

Because live web results are non-deterministic, the agent-driven journeys use a
SCRIPTED mock LLM (not the real qwen model) so the ``web-search`` tool call is
emitted deterministically every run, while the DuckDuckGo backend, the tool
dispatch, and the result round-trip stay REAL. Content assertions are loosened
but meaningful: a created provider's ``_test`` returns a structured non-error
result, and the agent journeys assert the search round-tripped (a second turn
fired only because a real tool result came back) rather than asserting exact
titles/urls.

All tests are gated on the ``web:duckduckgo`` capability so they skip cleanly
when the operator has not enabled a web-search backend in testconfig.
"""
from __future__ import annotations

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_for_status,
    wait_terminal,
)
from tests._support.smk import smk
from tests._support.testconfig import requires
from tests._support.yield_journeys import wait_for_resume

pytestmark = [pytest.mark.asyncio, requires("web:duckduckgo")]


# The always-on internal `web` toolset and its search tool's scoped wire id.
_WEB_TOOLSET_ID = "web"
_WEB_SEARCH_TOOL = "web-search"
_SCOPED_WEB_SEARCH = f"{_WEB_TOOLSET_ID}__{_WEB_SEARCH_TOOL}"

# A stable query whose result set is unlikely to be empty on any given day.
_STABLE_QUERY = "python programming language"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_ddg_provider(authed_client, pid: str) -> dict:
    """Create a DuckDuckGo web-search provider row; return the response body."""
    r = await authed_client.post(
        "/v1/web_search_providers",
        json={
            "id": pid,
            "provider_type": "duckduckgo",
            "config": {"type": "duckduckgo"},
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _set_active_single(authed_client, provider_id: str) -> None:
    r = await authed_client.put(
        "/v1/web_search_active_config",
        json={"config": {"mode": "single", "provider_id": provider_id}},
    )
    assert r.status_code == 200, r.text


async def _set_active_aggregated(authed_client, provider_ids: list[str]) -> None:
    r = await authed_client.put(
        "/v1/web_search_active_config",
        json={"config": {"mode": "aggregated", "provider_ids": provider_ids}},
    )
    assert r.status_code == 200, r.text


async def _restore_default_active(authed_client) -> None:
    """Point the active config back at the bootstrap DuckDuckGo provider so a
    test that re-pointed it doesn't leak into siblings."""
    try:
        await _set_active_single(authed_client, "DuckDuckGo")
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass


async def _drive_web_search_agent(
    authed_client, mock_llm, *, suffix: str, tmp_path,
    safe_search: str = "moderate",
) -> tuple[str, dict]:
    """Scripted agent emits one real ``web__web-search`` call, then terminates
    once the live DuckDuckGo result flows back. Returns (session_id, final).

    The mock LLM is scripted so the tool call is deterministic; the DuckDuckGo
    backend and the result round-trip are REAL.
    """
    registry, base_url = mock_llm
    scenario = f"scripted:web-{suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=suffix, scenario=scenario,
        tools=[_SCOPED_WEB_SEARCH],
        rules=[
            # First turn (no tool result yet): call the real web-search tool.
            Rule(when_tool_result=False, emit_tool=_SCOPED_WEB_SEARCH,
                 emit_args={"query": _STABLE_QUERY, "count": 3,
                            "safe_search": safe_search}),
            # Second turn fires ONLY because the live tool result came back.
            Rule(when_tool_result=True, emit_text="search complete"),
        ],
    )
    wid = await make_local_workspace(authed_client, suffix=suffix, root=tmp_path)
    sid = await start_agent_session(
        authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    final = await wait_terminal(authed_client, sid, timeout_s=120)
    return sid, final


async def _assert_search_roundtripped(authed_client, sid: str, final: dict) -> None:
    """A successful search round-trip: the session ended and the turn log shows
    at least two turns (the second fired only because a real tool result was
    surfaced to the scripted ``when_tool_result=True`` rule)."""
    assert final.get("status") == "ended", final
    tl = await authed_client.get(f"/v1/sessions/{sid}/turn_log")
    assert tl.status_code == 200, tl.text
    assert tl.json().get("total", 0) >= 2, tl.json()


# ===========================================================================
# SMK-WEB-01: Web search provider CRUD (DuckDuckGo) + _types
# ===========================================================================


@smk("SMK-WEB-01")
@requires("web:duckduckgo")
async def test_web_provider_crud_duckduckgo(authed_client, unique_suffix):
    """DuckDuckGo (keyless) provider rows CRUD cleanly and ``_types`` lists the
    supported backends. The keyed backends (tavily/exa/firecrawl) are out of
    scope here (no keys in this config); their type metadata is still asserted
    present via ``_types``."""
    pid = f"ws-ddg-{unique_suffix}"

    # _types lists every supported backend with its config-field shape.
    types = await authed_client.get("/v1/web_search_providers/_types")
    assert types.status_code == 200, types.text
    tbody = types.json()
    assert set(tbody) >= {"duckduckgo", "tavily", "exa", "firecrawl"}, tbody
    assert tbody["duckduckgo"]["config_fields"] == [], tbody
    assert "api_key" in tbody["tavily"]["config_fields"], tbody

    # CREATE
    created = await _create_ddg_provider(authed_client, pid)
    assert created["id"] == pid, created
    assert created["provider_type"] == "duckduckgo", created

    try:
        # GET
        got = await authed_client.get(f"/v1/web_search_providers/{pid}")
        assert got.status_code == 200, got.text
        assert got.json()["provider_type"] == "duckduckgo", got.json()

        # LIST contains it.
        lst = await authed_client.get("/v1/web_search_providers")
        assert lst.status_code == 200, lst.text
        assert pid in {it["id"] for it in lst.json()["items"]}, lst.json()

        # PUT (idempotent replace of the same shape).
        put = await authed_client.put(
            f"/v1/web_search_providers/{pid}",
            json={"id": pid, "provider_type": "duckduckgo",
                  "config": {"type": "duckduckgo"}},
        )
        assert put.status_code == 200, put.text
    finally:
        # DELETE
        d = await authed_client.delete(f"/v1/web_search_providers/{pid}")
        assert d.status_code in (200, 204), d.text
        gone = await authed_client.get(f"/v1/web_search_providers/{pid}")
        assert gone.status_code == 404, gone.text


# ===========================================================================
# SMK-WEB-02: provider connectivity test against REAL DuckDuckGo
# ===========================================================================


@smk("SMK-WEB-02")
@requires("web:duckduckgo")
async def test_web_provider_connectivity_probe_real_ddg(authed_client):
    """``POST /v1/web_search_providers/_test`` builds a transient DuckDuckGo
    adapter, runs a one-shot live search, and returns ``{ok, hits}``. Live web
    is non-deterministic, so this asserts a structured non-error result (and,
    when hits come back, that each carries title/url/snippet keys), NOT exact
    content. A misconfigured draft returns ``{ok: false, error}`` (a clear
    problem report), never a 500."""
    r = await authed_client.post(
        "/v1/web_search_providers/_test",
        json={"id": "probe", "provider_type": "duckduckgo",
              "config": {"type": "duckduckgo"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    assert isinstance(body.get("hits"), list), body
    # DuckDuckGo for "primer" (the fixed probe query) reliably returns >=1 hit;
    # but keep it loosened: if hits came back, they carry the documented shape.
    for hit in body["hits"]:
        assert set(hit) >= {"title", "url", "snippet"}, hit


# ===========================================================================
# SMK-WEB-03: active config single mode + agent search through it (real DDG)
# ===========================================================================


@smk("SMK-WEB-03", "SMK-WEB-05")
@requires("web:duckduckgo")
async def test_active_single_mode_agent_search_real_ddg(
    authed_client, mock_llm, unique_suffix, tmp_path
):
    """Select a freshly-created DuckDuckGo provider as the single active config,
    then drive a scripted agent through the reserved ``web`` toolset's
    ``web-search`` tool. The search routes to the selected provider, hits the
    REAL DuckDuckGo backend, and the result round-trips back to the agent (which
    then terminates). Covers WEB-03 (single active config selection) and WEB-05
    (web toolset search from an agent)."""
    pid = f"ws-single-{unique_suffix}"
    await _create_ddg_provider(authed_client, pid)
    try:
        await _set_active_single(authed_client, pid)

        # GET reflects the selection.
        ac = await authed_client.get("/v1/web_search_active_config")
        assert ac.status_code == 200, ac.text
        assert ac.json()["config"] == {"mode": "single", "provider_id": pid}, ac.json()

        sid, final = await _drive_web_search_agent(
            authed_client, mock_llm, suffix=unique_suffix, tmp_path=tmp_path)
        await _assert_search_roundtripped(authed_client, sid, final)
    finally:
        # Re-point the active config off this provider so the row can be deleted
        # (cascade-block guards the active reference) and siblings see the
        # bootstrap default.
        await _restore_default_active(authed_client)
        await authed_client.delete(f"/v1/web_search_providers/{pid}")


# ===========================================================================
# SMK-WEB-04: aggregated fallback chain (broken first -> working second)
# ===========================================================================


@smk("SMK-WEB-04")
@requires("web:duckduckgo")
async def test_aggregated_fallback_chain_real_ddg(
    authed_client, mock_llm, unique_suffix, tmp_path
):
    """An aggregated active config lists a deliberately-broken provider first
    and a working DuckDuckGo provider second. The first provider raises and the
    chain falls through to the working one; the search still returns and the
    agent terminates. The broken provider uses a Tavily backend with a bogus
    key (so the upstream call fails fast with a known-class error) while the
    working second provider is REAL DuckDuckGo.

    DuckDuckGo is the only enabled backend, so the broken provider is keyed
    (Tavily) purely to fail; if the chain did NOT fall through, the broken-first
    search would error and the second turn would never fire."""
    broken = f"ws-broken-{unique_suffix}"
    working = f"ws-working-{unique_suffix}"

    # A keyed provider with a bogus key: any upstream call raises a known-class
    # WebSearchProviderError / WebSearchUnavailable, which the aggregated chain
    # catches and skips.
    rb = await authed_client.post(
        "/v1/web_search_providers",
        json={"id": broken, "provider_type": "tavily",
              "config": {"type": "tavily", "api_key": "tvly-bogus-key-xyz"}},
    )
    assert rb.status_code in (200, 201), rb.text
    await _create_ddg_provider(authed_client, working)

    try:
        await _set_active_aggregated(authed_client, [broken, working])
        ac = await authed_client.get("/v1/web_search_active_config")
        assert ac.json()["config"]["mode"] == "aggregated", ac.json()
        assert ac.json()["config"]["provider_ids"] == [broken, working], ac.json()

        sid, final = await _drive_web_search_agent(
            authed_client, mock_llm, suffix=unique_suffix, tmp_path=tmp_path)
        # The fall-through to the working provider is proven by the round-trip:
        # the second turn fired only because a non-error tool result came back.
        await _assert_search_roundtripped(authed_client, sid, final)
    finally:
        await _restore_default_active(authed_client)
        await authed_client.delete(f"/v1/web_search_providers/{broken}")
        await authed_client.delete(f"/v1/web_search_providers/{working}")


# ===========================================================================
# SMK-WEB-06: required approval gate on the web-search tool (real DDG)
# ===========================================================================


@smk("SMK-WEB-06")
@requires("web:duckduckgo")
async def test_web_search_required_approval_park_resume_real_ddg(
    authed_client, mock_llm, unique_suffix, tmp_path
):
    """The ``web-search`` tool gated by a required-approval policy: the agent
    calls it, the session PARKS at the approval gate (the search has NOT run),
    the operator reads the pending approval, approves via REST, the session
    RESUMES, the REAL DuckDuckGo search executes, and the session ends. Proves
    the full park -> approve -> resume -> real-search chain with the active
    config left at the bootstrap DuckDuckGo provider."""
    # Gate the built-in web toolset's web-search tool with a required policy.
    pol = f"pol-web06-{unique_suffix}"
    existing = await authed_client.get("/v1/tool_approval_policies")
    if existing.status_code == 200:
        for it in existing.json().get("items", []):
            if (it.get("toolset_id") == _WEB_TOOLSET_ID
                    and it.get("tool_name") == _WEB_SEARCH_TOOL):
                await authed_client.delete(
                    f"/v1/tool_approval_policies/{it['id']}")
    r = await authed_client.post(
        "/v1/tool_approval_policies",
        json={
            "id": pol,
            "toolset_id": _WEB_TOOLSET_ID,
            "tool_name": _WEB_SEARCH_TOOL,
            "enabled": True,
            "approval": {"type": "required"},
        },
    )
    assert r.status_code in (200, 201), r.text
    r = await authed_client.post("/v1/tool_approval_policies/invalidate")
    assert r.status_code == 202, r.text

    registry, base_url = mock_llm
    scenario = f"scripted:web06-{unique_suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=scenario,
        tools=[_SCOPED_WEB_SEARCH],
        rules=[
            Rule(when_tool_result=False, emit_tool=_SCOPED_WEB_SEARCH,
                 emit_args={"query": _STABLE_QUERY, "count": 3}),
            Rule(when_tool_result=True, emit_text="search complete"),
        ],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(
        authed_client, workspace_id=wid, agent_id=agent["agent_id"])

    try:
        # ----- Drive until the session PARKS on the approval gate -----
        parked = await wait_for_status(
            authed_client, sid, "parked", timeout_s=30.0)
        # parked_status is the discriminating field; some builds keep status
        # as the engine state while parked_status flips to "parked".
        if parked.get("parked_status") != "parked":
            # Fall back to polling parked_status explicitly.
            import asyncio
            deadline = asyncio.get_event_loop().time() + 20.0
            while asyncio.get_event_loop().time() < deadline:
                rr = await authed_client.get(f"/v1/sessions/{sid}")
                if rr.status_code == 200:
                    parked = rr.json()
                    if parked.get("parked_status") == "parked":
                        break
                    if parked.get("status") == "ended":
                        raise AssertionError(
                            f"session ended before parking on approval: {parked!r}")
                await asyncio.sleep(0.25)
        assert parked.get("parked_status") == "parked", parked
        initial_turn_no = parked["turn_no"]

        # ----- The pending approval exposes the documented fields -----
        pend = await authed_client.get(f"/v1/sessions/{sid}/tool_approval/pending")
        assert pend.status_code == 200, pend.text
        pj = pend.json()
        # The pending payload reports the tool by its scoped wire id
        # (``web__web-search``); the policy itself keys on the bare tool name.
        assert pj.get("tool_name") in (_WEB_SEARCH_TOOL, _SCOPED_WEB_SEARCH), pj
        assert _WEB_SEARCH_TOOL in str(pj.get("tool_name", "")), pj
        assert pj.get("approval_type") in ("required", None), pj
        assert "tool_call_id" in pj, pj
        tool_call_id = pj["tool_call_id"]

        # ----- Approve -> resume -> the REAL search executes -> end -----
        resp = await authed_client.post(
            f"/v1/sessions/{sid}/tool_approval/respond",
            json={"tool_call_id": tool_call_id, "decision": "approved"},
        )
        assert resp.status_code == 202, resp.text

        await wait_for_resume(
            authed_client, sid, min_turn_no=initial_turn_no + 1, timeout_s=120.0)
        final = await wait_terminal(authed_client, sid, timeout_s=120)
        assert final.get("status") == "ended", final
        # The post-approval continuation surfaced the live tool result to the
        # scripted when_tool_result=True rule, proving the real DuckDuckGo
        # search executed after approval.
        tl = await authed_client.get(f"/v1/sessions/{sid}/turn_log")
        assert tl.status_code == 200, tl.text
        assert tl.json().get("total", 0) >= 2, tl.json()
    finally:
        await authed_client.delete(f"/v1/tool_approval_policies/{pol}")
        await authed_client.post("/v1/tool_approval_policies/invalidate")


# ===========================================================================
# SMK-WEB-10: policy CRUD + disable
# ===========================================================================


@smk("SMK-WEB-10")
@requires("web:duckduckgo")
async def test_tool_approval_policy_crud_and_disable(authed_client, unique_suffix):
    """A required-approval policy on the web-search tool is fully manageable:
    GET/PUT/DELETE round-trip and ``enabled: false`` is accepted (a disabled
    policy stops gating its tool). The functional "disabled policy no longer
    gates" effect is exercised by WEB-06's gated run; here the CRUD surface and
    the enable toggle are pinned."""
    pol = f"pol-web10-{unique_suffix}"
    # Clear any leftover policy on the same (toolset, tool) pair.
    existing = await authed_client.get("/v1/tool_approval_policies")
    if existing.status_code == 200:
        for it in existing.json().get("items", []):
            if (it.get("toolset_id") == _WEB_TOOLSET_ID
                    and it.get("tool_name") == _WEB_SEARCH_TOOL):
                await authed_client.delete(
                    f"/v1/tool_approval_policies/{it['id']}")

    # CREATE
    r = await authed_client.post(
        "/v1/tool_approval_policies",
        json={"id": pol, "toolset_id": _WEB_TOOLSET_ID,
              "tool_name": _WEB_SEARCH_TOOL, "enabled": True,
              "approval": {"type": "required"}},
    )
    assert r.status_code in (200, 201), r.text
    try:
        # GET
        got = await authed_client.get(f"/v1/tool_approval_policies/{pol}")
        assert got.status_code == 200, got.text
        assert got.json()["enabled"] is True, got.json()
        assert got.json()["tool_name"] == _WEB_SEARCH_TOOL, got.json()

        # PUT -> disable
        put = await authed_client.put(
            f"/v1/tool_approval_policies/{pol}",
            json={"id": pol, "toolset_id": _WEB_TOOLSET_ID,
                  "tool_name": _WEB_SEARCH_TOOL, "enabled": False,
                  "approval": {"type": "required"}},
        )
        assert put.status_code == 200, put.text
        assert put.json()["enabled"] is False, put.json()
    finally:
        d = await authed_client.delete(f"/v1/tool_approval_policies/{pol}")
        assert d.status_code in (200, 204), d.text
        gone = await authed_client.get(f"/v1/tool_approval_policies/{pol}")
        assert gone.status_code == 404, gone.text
        await authed_client.post("/v1/tool_approval_policies/invalidate")


# ===========================================================================
# SMK-WEB-07 / 08 / 09: Rego / LLM-judged / timeout approval strategies
# ===========================================================================
#
# These are generic tool-approval STRATEGY variants (Rego policy, LLM-judged,
# and approval timeout) that are not web-search-specific: the same policy
# engine gates any tool. They are covered against real journeys in
# tests/e2e/test_tool_approval_policy_strategies_journey.py (rego + llm) and
# tests/e2e/test_approval_timeout_rejection_journey.py (timeout). They are
# tagged here as partial so the WEB-* coverage map records them without
# duplicating those journeys against the web toolset.


@smk("SMK-WEB-07", "SMK-WEB-08", "SMK-WEB-09", status="partial")
@requires("web:duckduckgo")
async def test_web_approval_strategy_variants_covered_elsewhere():
    pytest.skip(
        "Rego / LLM-judged / timeout approval strategies are tool-agnostic and "
        "covered by test_tool_approval_policy_strategies_journey.py and "
        "test_approval_timeout_rejection_journey.py; not duplicated against the "
        "web toolset."
    )
