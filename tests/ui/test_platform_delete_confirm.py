"""Deleting a built-in agent from a Platform card says what it does (ADM-13).

Setup completeness is derived live (``primer/bootstrap/setup_state.py``): when the ``operator`` or
``builder`` agent row is missing, ``GET /v1/auth/status`` reports ``setup_complete: false`` and the
console gate sends every admin to the setup checklist and parks every other user on a waiting screen.
The agents card used to delete either one behind the same generic "Permanently delete ...?" prompt, so
one click and one OK locked everyone out of the console without a word of warning.

The confirm text and the whole confirm-then-DELETE flow are pure functions in ``nv-platform.jsx``
(``NV_deleteConfirm``, ``NV_deleteRow``), driven here through MiniRacer against the real source with a
fake dialog, fetch, toast and refetch. The id list the console warns about is pinned against the real
setup predicate, so it cannot drift from the backend.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLAT = (ROOT / "ui" / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")

# Every V8 isolate this file creates, closed after each test (tests/ui peaks at over a gigabyte for the
# isolates nobody disposes).
_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _helpers_src() -> str:
    """The pure helpers: from NV_fact (the first helper in the file) up to the page table. No JSX, no
    window.* dependency, so they are evaluated in isolation rather than transpiling the whole file."""
    start = PLAT.index("function NV_fact(")
    end = PLAT.index("// Per-entity page config.")
    return PLAT[start:end]


_REFUSAL = '{ detail: "agent is referenced", requestId: "req-9" }'


def _ctx(*, confirm: bool = True, rejects: bool = False, rejection: str = _REFUSAL):
    """``dialogs`` records every confirmDialog argument, ``calls`` every apiFetch (method, path), ``toasts``
    every (message, extra) pair; ``refetched`` counts list refreshes. ``rejection`` is the JS object a refused
    DELETE rejects with."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(
        "var dialogs = [], calls = [], toasts = [], refetched = 0;"
        "var env = {"
        f"  confirmDialog: function (o) {{ dialogs.push(o); return Promise.resolve({str(confirm).lower()}); }},"
        "  apiFetch: function (method, path) { calls.push([method, path]); return "
        + (f"Promise.reject({rejection})" if rejects else "Promise.resolve(null)")
        + "; },"
        "  toast: function (msg, extra) { toasts.push([msg, extra || null]); },"
        "  refetch: function () { refetched++; },"
        "};"
    )
    ctx.eval(_helpers_src())
    return ctx


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _delete(ctx, nav: str, row: dict, path: str):
    ctx.eval(
        f"var outcome = null; NV_deleteRow(env, {json.dumps(nav)}, {json.dumps(row)}, {json.dumps(path)})"
        ".then(function (r) { outcome = r; });"
    )
    return _js(ctx, "outcome")


# ---- the confirm text ------------------------------------------------------------------------------


@pytest.mark.parametrize("agent_id", ["operator", "builder"])
def test_deleting_a_setup_agent_says_it_reopens_setup_and_how_to_get_it_back(agent_id: str) -> None:
    ctx = _ctx()
    _delete(ctx, "agents", {"id": agent_id}, f"/agents/{agent_id}")

    dialog = _js(ctx, "dialogs[0]")
    text = dialog["message"]
    assert agent_id in text
    assert "not set up" in text, "it must say the install counts as not set up"
    assert "setup checklist" in text and "waits" in text, "it must say who is sent where"
    assert "restart" in text and "Re-run seed" in text, "it must say how the agent comes back"
    assert "default definition" in text, "it must say the re-created agent loses the user's edits"
    assert dialog["title"] == f"Delete {agent_id}"
    assert dialog["danger"] is True


@pytest.mark.parametrize("agent_id", ["planner", "explorer", "tool-runner", "my-agent"])
def test_any_other_agent_keeps_the_plain_confirm(agent_id: str) -> None:
    ctx = _ctx()
    _delete(ctx, "agents", {"id": agent_id}, f"/agents/{agent_id}")

    assert _js(ctx, "dialogs[0].message") == (
        f"Permanently delete {agent_id}? Referenced entities refuse deletion."
    )


@pytest.mark.parametrize("nav", ["graphs", "toolsets", "channels", "profiles"])
def test_only_the_agents_page_warns_an_entity_named_operator_is_not_the_agent(nav: str) -> None:
    ctx = _ctx()
    _delete(ctx, nav, {"id": "operator"}, f"/{nav}/operator")

    assert _js(ctx, "dialogs[0].message") == "Permanently delete operator? Referenced entities refuse deletion."


# ---- the flow around it ------------------------------------------------------------------------------


def test_declining_the_confirm_deletes_nothing() -> None:
    ctx = _ctx(confirm=False)

    assert _delete(ctx, "agents", {"id": "operator"}, "/agents/operator") is False
    assert _js(ctx, "calls") == []
    assert _js(ctx, "toasts") == []
    assert _js(ctx, "refetched") == 0


def test_confirming_deletes_the_row_at_its_path_then_says_so_and_refreshes_the_list() -> None:
    ctx = _ctx()

    assert _delete(ctx, "agents", {"id": "operator"}, "/agents/operator") is True
    assert _js(ctx, "calls") == [["DELETE", "/agents/operator"]]
    assert _js(ctx, "toasts") == [["Deleted operator", None]]
    assert _js(ctx, "refetched") == 1


def test_a_refused_delete_toasts_the_refusal_as_an_error_with_its_request_id_and_keeps_the_list() -> None:
    ctx = _ctx(rejects=True)

    assert _delete(ctx, "agents", {"id": "my-agent"}, "/agents/my-agent") is False
    assert _js(ctx, "toasts") == [
        ["Delete refused: agent is referenced", {"kind": "error", "requestId": "req-9"}],
    ]
    assert _js(ctx, "refetched") == 0


def test_a_refusal_without_a_detail_falls_back_to_the_error_message() -> None:
    ctx = _ctx(rejects=True, rejection='{ message: "network down" }')

    assert _delete(ctx, "agents", {"id": "my-agent"}, "/agents/my-agent") is False
    # requestId is undefined here, and JSON drops undefined keys.
    assert _js(ctx, "toasts") == [["Delete refused: network down", {"kind": "error"}]]


def test_the_dialog_title_prefers_the_name_and_the_message_the_id() -> None:
    """The prompt the generic delete always showed: kept for every entity that has a name, and it stays a
    destructive (red) confirm."""
    ctx = _ctx()
    _delete(ctx, "graphs", {"id": "g-1", "name": "Nightly"}, "/graphs/g-1")

    assert _js(ctx, "dialogs[0].title") == "Delete Nightly"
    assert _js(ctx, "dialogs[0].message") == "Permanently delete g-1? Referenced entities refuse deletion."
    assert _js(ctx, "dialogs[0].danger") is True


# ---- wiring and drift --------------------------------------------------------------------------------


def test_the_card_delete_goes_through_the_tested_flow() -> None:
    """``del`` in the page component is a thin caller: the decision lives in NV_deleteRow (tested above)."""
    m = re.search(r"function del\(row\)\s*\{[\s\S]{0,1200}?\n  \}\n", PLAT)
    assert m, "the card's delete handler moved"
    assert "NV_deleteRow(" in m.group(0)
    assert not re.search(r"confirmDialog\(", m.group(0)), "the prompt is built inside NV_deleteRow, not duplicated here"
    assert "confirmDialog: confirmDialog" in m.group(0), "the console's own dialog is what NV_deleteRow is given"
    # The page's nav id decides whether the setup warning applies, so it must be the page's own, not a constant.
    assert re.search(r"\},\s*nav,\s*row,\s*path\)", m.group(0)), "del() must pass the page's nav, the row and its path"
    # The toast must forward its second argument: that is the error styling and the request id of a refusal.
    assert "con.toast(msg, extra)" in m.group(0)


async def test_the_ids_the_console_warns_about_are_exactly_the_agents_whose_absence_reopens_setup(
    fake_storage_provider,
) -> None:
    """Delete each seeded agent in a seeded install and ask the real predicate whether setup reopened.

    The console cannot import the Python constants, so the list lives in nv-platform.jsx; this is what
    stops it drifting (a renamed reserved id, or a third agent added to the setup predicate)."""
    from primer.bootstrap.defaults import (
        RESERVED_BUILDER_AGENT,
        RESERVED_EXPLORER_AGENT,
        RESERVED_OPERATOR_AGENT,
        RESERVED_PLANNER_AGENT,
        RESERVED_TOOL_RUNNER_AGENT,
    )
    from primer.bootstrap.seed import ensure_seeded_agents
    from primer.bootstrap.setup_state import (
        MISSING_BUILDER_AGENT,
        MISSING_OPERATOR_AGENT,
        evaluate_setup_state,
    )
    from primer.model.agent import Agent
    from primer.model.model_profile import ModelProfile

    sp = fake_storage_provider
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id="llm-1--qwen", description="default", provider_id="llm-1", model_name="qwen", context_length=32000,
        )
    )
    await ensure_seeded_agents(sp)
    agents = sp.get_storage(Agent)
    reopens: set[str] = set()
    for agent_id in (
        RESERVED_OPERATOR_AGENT,
        RESERVED_BUILDER_AGENT,
        RESERVED_PLANNER_AGENT,
        RESERVED_EXPLORER_AGENT,
        RESERVED_TOOL_RUNNER_AGENT,
    ):
        row = await agents.get(agent_id)
        assert row is not None, f"{agent_id} was not seeded"
        await agents.delete(agent_id)
        missing = set((await evaluate_setup_state(sp)).missing)
        if missing & {MISSING_OPERATOR_AGENT, MISSING_BUILDER_AGENT}:
            reopens.add(agent_id)
        await agents.create(row)

    ctx = _ctx()
    assert set(_js(ctx, "NV_SETUP_AGENT_IDS")) == reopens


# ---- entities that hold data say what the delete takes with them (ADM-31) -------------------------------------------------


@pytest.mark.parametrize(
    "nav,row,must_say",
    [
        # DELETE /v1/workspaces/{id} (docs/agents/workspaces.md): backend teardown, and every open session ends workspace_lost, permanently.
        ("workspaces", {"id": "ws-1"}, ["tears down", "ends every open session", "workspace_lost", "cannot be resumed"]),
        # Deleting a Collection deletes its Documents, their content and its vector chunks (docs/agents/knowledge.md), with no 409 for a non-empty one;
        # a reused id starts empty (the T0336 e2e).
        ("collections", {"id": "wiki"}, ["Every document", "search index", "cannot be undone", "starts empty"]),
        # trigger::delete cascade-deletes subscriptions (docs/agents/triggers-and-subscriptions.md).
        ("triggers", {"id": "tr-1"}, ["subscriptions are deleted with it", "will run again"]),
        # services router: every version (and its artifacts) is deleted before the service row; the public URL is /svc/{name}/.
        ("services", {"id": "service-1", "name": "status-page"}, ["Every published version", "/svc/status-page/ stops answering"]),
    ],
)
def test_deleting_an_entity_that_holds_data_says_what_goes_with_it(nav: str, row: dict, must_say: list[str]) -> None:
    ctx = _ctx()
    _delete(ctx, nav, row, f"/{nav}/{row['id']}")

    message = _js(ctx, "dialogs[0].message")
    for phrase in must_say:
        assert phrase in message, (nav, phrase, message)
    assert message.startswith("Permanently delete "), message
    # These four have no "referenced" refusal, so the generic sentence would state something untrue about them.
    assert "Referenced entities refuse deletion" not in message, message


def test_the_service_prompt_names_the_service_not_its_generated_id() -> None:
    """The public URL is built from the name, and the card shows the generated id: the prompt has to give the name the operator knows."""
    ctx = _ctx()
    _delete(ctx, "services", {"id": "service-1b2c3d", "name": "status-page"}, "/services/service-1b2c3d")

    message = _js(ctx, "dialogs[0].message")
    assert message.startswith("Permanently delete status-page?"), message
    assert "service-1b2c3d" not in message
    assert _js(ctx, "dialogs[0].title") == "Delete status-page"


@pytest.mark.parametrize("nav", ["graphs", "toolsets", "channels", "profiles", "templates", "harnesses", "approvals"])
def test_every_other_entity_keeps_the_plain_prompt_this_change_only_names_what_it_checked(nav: str) -> None:
    """Unchanged on purpose: only the four entities whose delete consequence was read from the code or the docs get their own copy."""
    ctx = _ctx()
    _delete(ctx, nav, {"id": "x-1"}, f"/{nav}/x-1")

    assert _js(ctx, "dialogs[0].message") == "Permanently delete x-1? Referenced entities refuse deletion."
