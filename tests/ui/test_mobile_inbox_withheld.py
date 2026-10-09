"""The phone's Inbox card honours what the park's allowlist withheld, and never offers a blind inline Approve (Inbox preview allowlist, slice 2; ruling D3).

``GET /v1/yields/pending`` now says, per approval row, which arguments the card may not show (``hidden_keys``, dotted paths) and who decided that
(``preview``: ``policy`` or ``tool`` when somebody declared the list, ``default`` when the closed-set rule did, ``unstamped`` for a park from before the
stamp). The arguments line draws those as ``name=<hidden>``. The card:

* names what is withheld ("Hidden on this card: note, content");
* offers Approve only when it is not blind: nothing is withheld, or the person has tapped Show all and the whole call is on the screen. A card is BLIND whenever
  something is withheld, WHATEVER the source (review of #643, ruling on D3): the declared list decides what is drawn, and who declared it (``policy``, ``tool``,
  ``default``, ``unstamped``) only changes the note's words. A tool's author hiding the python source of ``update_python_toolset_source`` or an agent's system prompt is
  not a reason to approve it unread. Deny is never blind (refusing a call cannot do what the call does) and stays offered, as does Review;
* decides blindness from the RAW ``hidden_keys`` (an argument with the empty name counts, so does a list of garbage, or a ``hidden_keys`` that is not a list), and a
  value that was withheld whole (a bare string or list as the arguments: ``arguments`` is ``<hidden>`` and ``hidden_keys`` is empty) is blind too;
* says "and more" when the server's list is at its cap of 12 (the real count is not known), instead of a number that undercounts;
* treats a row from an older server (no ``hidden_keys``, no ``preview``) as it always did.

The pure view runs in V8 (fixture shared with ``test_mobile_inbox.py``); the layout and the whole flow run in a real browser in
``tests/ui_e2e/test_mobile_inbox_journey.py``.
"""

from __future__ import annotations

import json

import pytest

from tests.ui.test_mobile_inbox import SHELL, _item, _who, inbox  # noqa: F401  (inbox is a fixture)


def _approval(preview, hidden, **extra) -> dict:
    row = {"tool_name": "mcp__crm__update_contact", "arguments": "note=<hidden>", "truncated": True}
    if preview is not None:
        row["preview"] = preview
    if hidden is not None:
        row["hidden_keys"] = hidden
    return {**row, **extra}


def _view(inbox, approval, shown: bool = False, who: str | None = None, **item) -> dict:
    row = json.dumps(_item("approval", approval=approval, **item))
    return inbox.call(f"NV_mobileInboxView({row}, {who or _who()}, {json.dumps(shown)})")


@pytest.mark.parametrize(
    ("preview", "hidden", "shown", "approve", "blind"),
    [
        pytest.param("default", ["note"], False, False, True, id="default: something hidden, not shown: no Approve"),
        pytest.param("default", ["note"], True, True, True, id="default: something hidden, the whole call shown: Approve"),
        pytest.param("unstamped", ["note"], False, False, True, id="unstamped (a park from before the stamp) is the default rule"),
        pytest.param("unstamped", ["note"], True, True, True, id="unstamped, shown"),
        pytest.param("tool", ["note"], False, False, True, id="the tool declared its list, something is hidden: still no Approve"),
        pytest.param("tool", ["note"], True, True, True, id="the tool declared its list, the whole call shown: Approve"),
        pytest.param("policy", ["note", "content"], False, False, True, id="the operator declared the list, something is hidden: still no Approve"),
        pytest.param("policy", ["note", "content"], True, True, True, id="the operator declared the list, the whole call shown: Approve"),
        pytest.param("default", [], False, True, False, id="default with nothing hidden: Approve"),
        pytest.param("tool", [], False, True, False, id="tool with nothing hidden: Approve"),
        pytest.param(None, None, False, True, False, id="an older server (no hidden_keys, no preview): as it always did"),
        pytest.param("future", ["note"], False, False, True, id="a source this console does not know fails closed"),
        pytest.param(None, ["note"], False, False, True, id="something hidden and nobody said who decided: closed"),
    ],
)
def test_approve_is_offered_only_when_the_card_is_not_blind(inbox, preview, hidden, shown: bool, approve: bool, blind: bool) -> None:
    view = _view(inbox, _approval(preview, hidden), shown)

    assert view["canApprove"] is approve, view
    assert view["blind"] is blind, view
    assert view["canDeny"] is True, "Deny is never blind: refusing a call cannot do what the call does"


def test_a_blind_card_says_why_and_what_to_do(inbox) -> None:
    view = _view(inbox, _approval("default", ["note"]))

    assert view["canApprove"] is False
    assert "Show all" in view["note"] and "review" in view["note"].lower(), view["note"]
    assert view["withheld"] == "Hidden on this card: note"


def test_a_card_whose_whole_call_is_on_the_screen_has_nothing_to_warn_about(inbox) -> None:
    view = _view(inbox, _approval("default", ["note"]), shown=True)

    assert view["canApprove"] is True and view["note"] == ""
    assert view["withheld"] == "Hidden on this card: note", "what the CARD hides is still named; the full call is below it"


def test_a_declared_list_still_names_what_it_withholds_and_is_still_blind(inbox) -> None:
    view = _view(inbox, _approval("policy", ["content", "entity.config"]))

    assert view["withheld"] == "Hidden on this card: content, entity.config"
    assert view["blind"] is True and view["canApprove"] is False
    assert "Show all" in view["note"] and "review" in view["note"].lower(), view["note"]


def test_who_declared_the_list_changes_the_words_of_the_note_and_nothing_else(inbox) -> None:
    declared = _view(inbox, _approval("tool", ["note"]))
    operator = _view(inbox, _approval("policy", ["note"]))
    undeclared = _view(inbox, _approval("default", ["note"]))

    assert declared["note"] and operator["note"] and undeclared["note"]
    assert declared["note"] != undeclared["note"] and operator["note"] != undeclared["note"], "the trust shows in the words"
    for view in (declared, operator, undeclared):
        assert (view["blind"], view["canApprove"], view["canDeny"]) == (True, False, True), view


# The default-gated tools whose cards a fresh install shows (primer/toolset/crud.py PREVIEW_ARGS): the rows the real route draws for them.
CRUD_SHAPED_ROWS = [
    pytest.param(
        _approval("tool", ["source"], tool_name="crud__update_python_toolset_source", arguments="toolset_id=my-tools, source=<hidden>"),
        id="update_python_toolset_source: the python source is hidden",
    ),
    pytest.param(
        _approval(
            "tool", ["entity.system_prompt", "entity.compaction_prompt"], tool_name="crud__create_agent",
            arguments='entity={"id": "a1", "model": {"profile_id": "p"}, "system_prompt": "<hidden>"}',
        ),
        id="create_agent: the system prompt is hidden",
    ),
    pytest.param(
        _approval("tool", ["entity.nodes.input_template"], tool_name="crud__update_graph", arguments='id=g1, entity={"id": "g1", "nodes": [{"kind": "agent"'),
        id="update_graph: the node templates are hidden",
    ),
]


@pytest.mark.parametrize("approval", CRUD_SHAPED_ROWS)
def test_a_crud_shaped_tool_row_is_not_approved_inline_unread(inbox, approval: dict) -> None:
    """The design note itself says the python source and the system prompt stay behind Show all."""
    blind = _view(inbox, approval)
    read = _view(inbox, approval, shown=True)

    assert (blind["blind"], blind["canApprove"], blind["canDeny"]) == (True, False, True), blind
    assert "Show all" in blind["note"], blind["note"]
    assert (read["canApprove"], read["note"]) == (True, ""), read


def test_the_call_tool_composite_is_blind_too(inbox) -> None:
    """A policy on ``system__call_tool`` itself shows ``toolset_id`` and ``tool_name`` and withholds the inner arguments: that is the whole of what the call does."""
    approval = _approval("policy", ["arguments"], tool_name="system__call_tool", arguments="toolset_id=workspaces, tool_name=run_command, arguments=<hidden>")

    view = _view(inbox, approval)

    assert (view["blind"], view["canApprove"]) == (True, False), view
    assert view["withheld"] == "Hidden on this card: arguments"
    assert _view(inbox, approval, shown=True)["canApprove"] is True


def test_a_value_withheld_whole_is_blind(inbox) -> None:
    """Arguments that are not an object (a bare string or list) are withheld whole by the server: ``arguments`` is ``<hidden>`` and ``hidden_keys`` is EMPTY, so no
    name can be listed. The card says so, and is blind."""
    for preview in ("default", "tool", "policy", "unstamped"):
        view = _view(inbox, _approval(preview, [], arguments="<hidden>"))

        assert (view["blind"], view["canApprove"]) == (True, False), (preview, view)
        assert "whole" in view["withheld"].lower() and "arguments" in view["withheld"].lower(), view["withheld"]
        assert _view(inbox, _approval(preview, [], arguments="<hidden>"), shown=True)["canApprove"] is True


def test_an_older_server_row_with_hidden_looking_arguments_is_not_made_blind(inbox) -> None:
    """No ``preview`` key at all: a server from before the allowlist. Its arguments are what they always were."""
    view = _view(inbox, _approval(None, None, arguments="<hidden>"))

    assert (view["blind"], view["canApprove"]) == (False, True), view


def test_an_argument_with_the_empty_name_is_withheld_and_blind(inbox) -> None:
    """``hidden_keys`` is read raw: ``{"": "rm -rf /"}`` withheld lists the name ``""``, which a filter for non-empty names threw away, leaving the card not blind."""
    view = _view(inbox, _approval("tool", [""]))

    assert (view["blind"], view["canApprove"]) == (True, False), view
    assert view["withheld"] and "unnamed" in view["withheld"].lower(), view["withheld"]


def test_the_empty_name_is_listed_among_the_others(inbox) -> None:
    view = _view(inbox, _approval("default", ["note", "", "content"]))

    assert view["withheld"] == "Hidden on this card: note, (unnamed), content"


def test_nothing_withheld_says_nothing(inbox) -> None:
    for approval in (_approval("tool", []), _approval(None, None), _approval("default", None)):
        view = _view(inbox, approval)
        assert view["withheld"] == "" and view["note"] == "", view


def test_a_list_of_nine_withheld_names_is_cut_after_six_with_the_count(inbox) -> None:
    names = [f"arg{i}" for i in range(9)]

    view = _view(inbox, _approval("default", names))

    assert view["withheld"] == "Hidden on this card: arg0, arg1, arg2, arg3, arg4, arg5 and 3 more"


def test_a_list_at_the_servers_cap_says_and_more_because_the_count_is_not_known(inbox) -> None:
    """The server sends at most 12 names (``_ATTENTION_KEY_COUNT``). A list of 12 may be the first 12 of 40: "and 6 more" undercounted."""
    names = [f"arg{i}" for i in range(12)]

    view = _view(inbox, _approval("default", names))

    assert view["withheld"] == "Hidden on this card: arg0, arg1, arg2, arg3, arg4, arg5 and more"


@pytest.mark.parametrize("garbage", ["note", {"note": 1}, 7, True])
def test_a_hidden_keys_that_is_not_a_list_names_nothing_and_is_blind(inbox, garbage) -> None:
    """The server sends a list. Anything else is a bug on the other side, and a card must not crash on it: no names are drawn, and a card that cannot tell what was
    withheld is blind."""
    for preview in ("default", "tool", "policy"):
        view = _view(inbox, _approval(preview, garbage))

        assert view["withheld"] == ""
        assert (view["blind"], view["canApprove"]) == (True, False), (preview, view)


@pytest.mark.parametrize("garbage", [[None], [1, 2], [{"a": 1}], [None, "note"]])
def test_a_list_of_non_names_is_blind_and_names_only_the_names(inbox, garbage) -> None:
    view = _view(inbox, _approval("tool", garbage))

    assert (view["blind"], view["canApprove"]) == (True, False), view
    assert view["withheld"] in ("", "Hidden on this card: note"), view["withheld"]


def test_someone_who_may_not_decide_it_is_told_that_and_not_about_the_hidden_arguments(inbox) -> None:
    only_bob = {"kind": "users", "users": ["bob"], "roles": []}

    view = _view(inbox, _approval("default", ["note"]), approvers=only_bob)

    assert view["notApprover"] is True and view["canApprove"] is False and view["canDeny"] is False
    assert "approver" in view["note"].lower()


def test_an_approver_who_has_not_looked_is_blocked_and_one_who_has_is_not(inbox) -> None:
    only_bob = {"kind": "users", "users": ["bob"], "roles": []}

    blind = _view(inbox, _approval("default", ["note"]), who=_who("bob"), approvers=only_bob)
    shown = _view(inbox, _approval("default", ["note"]), shown=True, who=_who("bob"), approvers=only_bob)

    assert (blind["canApprove"], shown["canApprove"]) == (False, True)


def test_a_card_with_no_call_id_still_cannot_be_decided_at_all(inbox) -> None:
    view = _view(inbox, _approval("tool", []), tool_call_id=None)

    assert view["canApprove"] is False and view["canDeny"] is False


# ---- the component hands the view the one thing it cannot know: whether the whole call is on the screen ------------------------------------------------------------------


def _card_source() -> str:
    start = SHELL.index("function NV_MobileInboxCard(")
    return SHELL[start:SHELL.index("\n}\n", start)]


def test_the_card_tells_the_view_when_the_whole_call_has_been_loaded() -> None:
    source = _card_source()

    assert "var full = NV_inboxFullFor(fullState[0], it);" in source
    assert "var shownAll = !!(full && full.text !== undefined);" in source
    assert "NV_mobileInboxView(it, { username: con.username, role: con.role }, shownAll)" in source
    assert source.index("var fullState = React.useState(null);") < source.index("var shownAll"), "the loaded state has to exist before the view reads it"
    assert source.count("callId: it.tool_call_id") == 2, "the loading state and the loaded text both name the call they belong to"
    assert source.count("parkedAt: it.created_at") == 2, "and the park they were loaded for (the raw call id is not unique across rounds)"


PARKED = "2026-10-09T10:00:00+00:00"
PARKED_AGAIN = "2026-10-09T10:00:07+00:00"


def _full_for(state, call_id: str, created_at: str | None = PARKED):
    """``NV_inboxFullFor`` evaluated on its own (V8), so the shared fixture of test_mobile_inbox.py is untouched."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        start = SHELL.index("function NV_inboxFullFor")
        ctx.eval(SHELL[start:SHELL.index("\n}\n", start) + 3])
        item = {"tool_call_id": call_id, **({"created_at": created_at} if created_at is not None else {})}
        return json.loads(ctx.eval(f"JSON.stringify(NV_inboxFullFor({json.dumps(state)}, {json.dumps(item)}) || null)"))
    finally:
        ctx.close()


def test_the_text_loaded_by_show_all_unlocks_only_the_call_it_belongs_to() -> None:
    """The card is keyed by SESSION. When the poll brings a NEW call for the same session (the first one was decided, the agent parked on another), the text loaded for
    the old call must not unlock Approve for a call the person has not read: the decision names the new call's id."""
    loaded = {"text": "the whole call", "callId": "tc-1", "parkedAt": PARKED}

    assert _full_for(loaded, "tc-1") == loaded
    assert _full_for(loaded, "tc-2") is None
    assert _full_for(None, "tc-1") is None
    loading = {"loading": True, "callId": "tc-1", "parkedAt": PARKED}
    assert _full_for(loading, "tc-1") == loading      # loading is not loaded: the card checks .text
    assert _full_for({"text": "no call named", "parkedAt": PARKED}, "tc-1") is None


def test_the_text_loaded_for_one_park_does_not_unlock_a_later_park_that_reuses_the_call_id() -> None:
    """The raw call id is NOT unique across rounds: Ollama's ``call_{idx}`` restarts per stream and Gemini falls back to the same ids. If call A (``call_0``) is decided
    elsewhere and B parks as ``call_0`` before the 10 s poll, B used to inherit A's unlock: it showed A's text and its Approve posted B. The park time is the second key."""
    loaded = {"text": "A's arguments", "callId": "call_0", "parkedAt": PARKED}

    assert _full_for(loaded, "call_0", PARKED) == loaded
    assert _full_for(loaded, "call_0", PARKED_AGAIN) is None, "same id, parked again: not the call that was read"
    assert _full_for({"loading": True, "callId": "call_0", "parkedAt": PARKED}, "call_0", PARKED_AGAIN) is None
    assert _full_for({"text": "legacy state", "callId": "call_0"}, "call_0", PARKED) is None, "a state with no park time never unlocks a row that has one"
    assert _full_for(loaded, "call_0", None) is None, "nor does a row with no park time unlock a state that has one"


def test_two_missing_park_times_are_not_a_match() -> None:
    """``undefined === undefined``: a state and a row that BOTH lack a park time matched on the call id alone, which is the key that is not unique across rounds. Without a park
    time on both sides the text is not known to belong to this park, and the card stays locked (it fails closed; Open to review is still there)."""
    for state in ({"text": "A's arguments", "callId": "call_0"}, {"text": "A's arguments", "callId": "call_0", "parkedAt": None}, {"loading": True, "callId": "call_0"}):
        assert _full_for(state, "call_0", None) is None, state


def test_the_card_draws_the_withheld_line() -> None:
    assert 'data-testid="nv-mob-ib-withheld"' in _card_source() and "view.withheld" in _card_source()


def test_the_docs_do_not_call_a_declared_list_trusted() -> None:
    """The card is blind whenever anything is withheld, whoever declared the list (review of #643): a sentence that says a declared list is trusted contradicts the rule and the code."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for doc in ("docs/dev/subsystems/ui-pages.md", "docs/agents/tool-approval.md"):
        text = (root / doc).read_text(encoding="utf-8")

        assert "(`tool`, `policy`) is trusted" not in text and "declared is trusted" not in text, doc
