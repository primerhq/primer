"""The phone's Inbox card honours what the park's allowlist withheld, and never offers a blind inline Approve (Inbox preview allowlist, slice 2; ruling D3).

``GET /v1/yields/pending`` now says, per approval row, which arguments the card may not show (``hidden_keys``, dotted paths) and who decided that
(``preview``: ``policy`` or ``tool`` when somebody declared the list, ``default`` when the closed-set rule did, ``unstamped`` for a park from before the
stamp). The arguments line draws those as ``name=<hidden>``. The card:

* names what is withheld ("Hidden on this card: note, content");
* offers Approve only when it is not blind: nothing is withheld, or a tool author or the operator declared the list (they chose what matters), or the person has
  tapped Show all and the whole call is on the screen. A card that hides something nobody declared says to open it, or to show all first. Deny is never blind
  (refusing a call cannot do what the call does) and stays offered, as does Review;
* treats a row from an older server (no ``hidden_keys``, no ``preview``) as it always did, and a source it does not know as undeclared (fails closed).

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
        pytest.param("tool", ["note"], False, True, False, id="the tool declared its list: Approve"),
        pytest.param("policy", ["note", "content"], False, True, False, id="the operator declared the list: Approve"),
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


def test_a_declared_list_still_names_what_it_withholds(inbox) -> None:
    view = _view(inbox, _approval("policy", ["content", "entity.config"]))

    assert view["withheld"] == "Hidden on this card: content, entity.config"
    assert view["note"] == "" and view["canApprove"] is True


def test_nothing_withheld_says_nothing(inbox) -> None:
    for approval in (_approval("tool", []), _approval(None, None), _approval("default", None)):
        view = _view(inbox, approval)
        assert view["withheld"] == "" and view["note"] == "", view


def test_a_long_list_of_withheld_names_is_cut_after_six(inbox) -> None:
    names = [f"arg{i}" for i in range(12)]

    view = _view(inbox, _approval("default", names))

    assert view["withheld"] == "Hidden on this card: arg0, arg1, arg2, arg3, arg4, arg5 and 6 more"


@pytest.mark.parametrize("garbage", ["note", {"note": 1}, 7, True])
def test_a_hidden_keys_that_is_not_a_list_names_nothing(inbox, garbage) -> None:
    """The server sends a list. Anything else is a bug on the other side and a card must not crash on it: no names are drawn."""
    view = _view(inbox, _approval("default", garbage))

    assert view["withheld"] == ""
    assert isinstance(view["canApprove"], bool)


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

    assert "var shownAll = !!(full && full.text !== undefined);" in source
    assert "NV_mobileInboxView(it, { username: con.username, role: con.role }, shownAll)" in source
    assert source.index("var fullState = React.useState(null);") < source.index("var shownAll"), "the loaded state has to exist before the view reads it"


def test_the_card_draws_the_withheld_line() -> None:
    assert 'data-testid="nv-mob-ib-withheld"' in _card_source() and "view.withheld" in _card_source()
