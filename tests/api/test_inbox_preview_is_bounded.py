"""The Inbox preview cannot stall the poll (PR 503 round 3: a server-freeze regression found in review).

``GET /v1/yields/pending`` is polled by every open console and runs synchronously on the event loop. Its previews scrub text with regular
expressions, and the first version of the scrubber had patterns that are quadratic (``(?<!\\w)[\\w.\\-]*WORDS[\\w.\\-]*`` starts a scan after every ``.``
or ``-`` and backtracks two nested stars; the JWT pattern rescans every run; ``_one_line``'s leading ``\\s*`` rescans every space of a run), and the ask/wait
``prompt`` was scrubbed BEFORE it was bounded: ``"token" * 8000`` took seconds, 200 000 characters took minutes, and the whole server was
blocked for that long on every poll by one parked session.

Three defences, each pinned here:

* every pattern is linear (``test_every_scrub_step_is_linear_on_hostile_text``: 40 000 characters of each hostile shape in milliseconds; the
  work is done in a forked child with a hard kill, so a regression fails in seconds instead of hanging the suite);
* text is bounded BEFORE it is scrubbed (the prompt) and a whole walk has a total character budget (``_approval_preview`` on many large
  strings);
* the route itself hands the scrubber a bounded amount of text for a hostile row.

Time is a BACKSTOP in this file, never the check. A loaded shared runner takes 100 times longer than an idle machine over a few milliseconds of
work (CI measured 1.27 s for a route answer that takes about 30 ms), so a tight wall-clock bound is a flake. What is pinned is the WORK (the
characters the scrubber is handed, counted by a spy) and, for the regex steps, the growth: a linear pattern needs about 10 ms for these inputs, a
quadratic one tens of seconds, and ``_LINEAR_BACKSTOP_S`` sits two orders of magnitude above the first and well below the second.

The preview also never shows a credential used as a NAME: argument keys (the ``key=`` prefix and ``argument_keys``) and nested keys are scrubbed
like values, and a container cut at its member cap says how many members it left out.
"""

from __future__ import annotations

import multiprocessing
import time
from typing import Any

import pytest

from tests.api.test_workspace_yields_pending import (  # noqa: F401  (fixtures are used by name)
    _approval_state,
    _create_workspace,
    _make_session,
    app,
    client,
    pr,
    sp,
    wsr,
)
from primer.model.workspace_session import WorkspaceSession


# 40 000 characters of a hostile shape take about 10 ms through a linear pattern and tens of seconds through a quadratic one.
_LINEAR_BACKSTOP_S = 2.0
# Something that takes this long is hung or unbounded, whatever the machine.
_HANG_BACKSTOP_S = 10.0


def _timed_in_a_child(fn_name: str, *args: Any, kill_after_s: float = 6.0) -> float | None:
    """Seconds ``primer.api.routers.workspaces.<fn_name>(*args)`` takes, run in a forked child; ``None`` when it is still running at the
    deadline (it is killed), so a quadratic or cubic pattern fails a test in seconds instead of holding the suite for minutes."""
    ctx = multiprocessing.get_context("fork")
    out = ctx.Queue()

    def child() -> None:
        from primer.api.routers import workspaces as w

        started = time.perf_counter()
        getattr(w, fn_name)(*args)
        out.put(time.perf_counter() - started)

    process = ctx.Process(target=child)
    process.start()
    process.join(kill_after_s)
    if process.is_alive():
        process.kill()
        process.join()
        return None
    return out.get(timeout=2)


_HOSTILE = {
    "a secret word repeated": "token" * 8000,
    "hyphenated secret words": "-" + "token-" * 6700,
    "a token prefix repeated": "sk-" * 13000,
    "jwt-looking runs": "eyJ-" * 10000,
    "underscored words": "a_" * 20000,
    "an unterminated quote": 'password="' + "a" * 40000,
    "basic and a long word": "basic " + "a" * 40000,
    "assignments": "key=" * 10000,
    "secret flags": "--password " * 3600,
    "bearers": "bearer " * 6000,
    "userinfo-looking": "://a:" * 10000,
    "spaces": " " * 40000,
    "tabs after words": "a\t" * 20000,
    "newlines": "a\n" * 20000,
    "dotted words": ("token." * 6000),
    "a secret word and then spaces": "token" + " " * 40000,
    "quote openers": 'a="' * 13000,
    "partial secret words": "passw" * 8000,
    "key flags": "--key " * 6600,
    "dashes": "-" * 40000,
    # the shapes the pass/pw, mysql -p and -u user:password rules could be tripped by (ticket 01a11b7d)
    "pass words": "pass" * 10000,
    "pass components": "pass-" * 8000,
    "pass assignments": "db_pass " * 5000,
    "pass flags": "--pass " * 5700,
    "pw assignments": "pw=" * 13000,
    "mysql commands": "mysql " * 6600,
    "mysql with a bare -p": "mysql -p " * 4400,
    "mysql with attached -p": "mysql -pa " * 4000,
    "mysql then a long option run": "mysql" + " x" * 20000,
    "mysql with a separator in each": "mysql a ; " * 4000,
    "user flags": "-u a:" * 10000,
    "user flags with values": "--user a:b " * 3600,
    "user flags without a colon": "-u ab " * 6600,
    # the shapes the review of the first version found (PR 577): '=' in the user part, quotes, attached values, a long mysql window
    "user flags with equals": "-u=" * 20000,
    "long user flags with equals": "--user=" * 8000,
    "a quote opener per user flag": '-u"' * 13000,
    "an unclosed quote then colons": '-u"' + ":" * 40000,
    "an unclosed single quote then colons": "-u'" + ":" * 40000,
    "attached user flags": "-uab:" * 8000,
    "capital user flags": "-U " * 13000,
    "mysql with a long option run": "mysql" + " -x" * 13000,
    "mysql dump family names": "mariadb-dump " * 3000,
    "camel case components": "dbPass" * 6600,
    "camel case assignments": "adminPw=" * 5000,
}


@pytest.mark.parametrize("shape", sorted(_HOSTILE))
def test_every_scrub_step_is_linear_on_hostile_text(shape: str) -> None:
    text = _HOSTILE[shape]
    assert len(text) >= 20_000
    elapsed = _timed_in_a_child("_scrub_text", text)
    assert elapsed is not None, f"_scrub_text was still running after 6 s on {shape!r} ({len(text)} chars)"
    assert elapsed < _LINEAR_BACKSTOP_S, f"_scrub_text took {elapsed:.2f} s on {shape!r} ({len(text)} chars)"


@pytest.mark.parametrize("shape", ["spaces", "tabs after words", "newlines", "a secret word repeated"])
def test_collapsing_line_breaks_is_linear_on_hostile_text(shape: str) -> None:
    elapsed = _timed_in_a_child("_one_line", _HOSTILE[shape])
    assert elapsed is not None and elapsed < _LINEAR_BACKSTOP_S, f"_one_line took {elapsed} s on {shape!r}"


def test_a_preview_of_many_large_hostile_strings_is_bounded_by_a_total_budget(monkeypatch) -> None:
    """Each string is capped, and so is the whole walk: 400 members of 3 600 characters of the worst shape is not 1.4 MB of regex work."""
    from primer.api.routers import workspaces as w

    arguments = {f"k{i:03d}": "-token" * 600 for i in range(400)}
    arguments["command"] = "token" * 8000
    call = {"name": "t", "arguments": arguments}
    elapsed = _timed_in_a_child("_approval_preview", call)          # a backstop: a hang is killed here, so the in-process run below is safe
    assert elapsed is not None, "the preview was still running after 6 s"
    assert elapsed < _HANG_BACKSTOP_S, f"the preview took {elapsed:.2f} s"
    seen = _recording_scrubber(monkeypatch)
    w._approval_preview(call)
    assert sum(seen) <= w._REDACT_BUDGET + 4000, f"{sum(seen)} characters were scrubbed for one preview"
    assert max(seen) <= w._REDACT_MAX_TEXT


def test_a_preview_of_a_huge_nested_blob_is_bounded(monkeypatch) -> None:
    from primer.api.routers import workspaces as w

    blob = {"rows": [["token" * 400] * 50 for _ in range(50)], "text": "sk-" * 20000}
    call = {"name": "t", "arguments": {"note": blob}}
    elapsed = _timed_in_a_child("_approval_preview", call)
    assert elapsed is not None and elapsed < _HANG_BACKSTOP_S, f"the preview took {elapsed} s"
    seen = _recording_scrubber(monkeypatch)
    w._approval_preview(call)
    assert sum(seen) <= w._REDACT_BUDGET + 4000, f"{sum(seen)} characters were scrubbed for one preview"
    assert max(seen) <= w._REDACT_MAX_TEXT


def test_the_ask_prompt_is_cut_before_it_is_scrubbed() -> None:
    from primer.api.routers import workspaces as w

    assert hasattr(w, "_attention_prompt"), "the prompt needs one function that bounds and then scrubs it"
    elapsed = _timed_in_a_child("_attention_prompt", "token" * 40_000)
    assert elapsed is not None and elapsed < _LINEAR_BACKSTOP_S, f"a 200 000-character prompt took {elapsed} s"
    assert w._attention_prompt("Use Bearer sk-live-abcdef123456 to call it") == "Use Bearer <redacted> to call it"
    assert len(w._attention_prompt("q" * 1000)) <= 240


# --- the route -----------------------------------------------------------------------------------------------------------------


def _nested(depth: int) -> dict:
    node: dict = {"leaf": "token"}
    for _ in range(depth):
        node = {"x": node}
    return node


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
async def test_a_hostile_row_does_not_stall_the_poll_for_everyone(client, sp, monkeypatch) -> None:
    """One parked session with a 200 000-character prompt and an argument blob made of the worst shapes: the route hands the scrubber a bounded
    amount of text (the WORK is what is pinned; the clock only backstops a hang, since a loaded runner took 1.27 s for what takes milliseconds)."""
    from primer.api.routers import workspaces as w

    seen = _recording_scrubber(monkeypatch)
    wid = await _create_workspace(client)
    ask = _approval_state("tc-h1", None, tool_name="ask_user")
    ask["yielded"]["resume_metadata"]["prompt"] = "token" * 40_000
    call = {"id": "tc-h2", "name": "bash", "arguments": {
        "command": "-token-" * 30_000, "note": "sk-" * 60_000, "rows": [["token" * 400] * 40 for _ in range(40)], "deep": _nested(5000),
    }}
    for sid, state in (("sess-h-1", ask), ("sess-h-2", _approval_state("tc-h2", call))):
        await sp.get_storage(WorkspaceSession).create(_make_session(sid, wid, parked_status="parked", parked_state=state))

    started = time.perf_counter()
    resp = await client.get("/v1/yields/pending")
    elapsed = time.perf_counter() - started

    assert resp.status_code == 200, resp.text[:300]
    rows = {i["session_id"]: i for i in resp.json()["items"]}
    assert len(rows["sess-h-1"]["prompt"]) <= 240 and len(rows["sess-h-2"]["approval"]["arguments"]) <= 240
    assert seen, "the scrubber was never reached, so nothing here was bounded by anything"
    assert sum(seen) <= w._PROMPT_SCAN_CHARS + w._REDACT_BUDGET + 4000, f"{sum(seen)} characters were scrubbed for one poll of two rows"
    assert max(seen) <= w._REDACT_MAX_TEXT, f"the scrubber was handed {max(seen)} characters at once"
    assert elapsed < _HANG_BACKSTOP_S, f"the poll took {elapsed:.2f} s for one hostile row"


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
async def test_deeply_nested_arguments_answer_200_through_the_route(client, sp) -> None:
    """A nesting depth that used to raise RecursionError inside the walker (a 500 for every console)."""
    wid = await _create_workspace(client)
    call = {"id": "tc-d1", "name": "bash", "arguments": {"deep": _nested(5000)}}
    await sp.get_storage(WorkspaceSession).create(
        _make_session("sess-d-1", wid, parked_status="parked", parked_state=_approval_state("tc-d1", call)),
    )
    resp = await client.get("/v1/yields/pending")
    assert resp.status_code == 200, resp.text[:300]
    row = {i["session_id"]: i for i in resp.json()["items"]}["sess-d-1"]
    assert row["approval"]["truncated"] is True and "leaf" not in row["approval"]["arguments"]


# --- credentials used as names -------------------------------------------------------------------------------------------------


def _preview(arguments: Any) -> dict:
    from primer.api.routers.workspaces import _approval_preview

    return _approval_preview({"name": "t", "arguments": arguments})


GHP = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def test_a_credential_used_as_an_argument_name_is_scrubbed_in_the_line_and_in_the_names() -> None:
    got = _preview({GHP: "x", "path": "p"})
    assert GHP not in got["arguments"] and "abcdefghijklmnopqrstuvwxyz0123456789" not in got["arguments"]
    assert "<redacted>=x" in got["arguments"]
    assert all(GHP not in k and "abcdefghij" not in k for k in got["argument_keys"]) and "<redacted>" in got["argument_keys"]


def test_a_credential_used_as_a_nested_name_is_scrubbed() -> None:
    got = _preview({"headers": {"sk-abcdefghij1234567890": "v", "accept": "json"}})
    assert "abcdefghij1234567890" not in got["arguments"] and "accept" in got["arguments"]


def test_a_credential_used_as_a_name_inside_a_json_string_is_scrubbed() -> None:
    got = _preview({"payload": f'{{"{GHP}": 1}}'})
    assert "abcdefghijklmnopqrstuvwxyz" not in got["arguments"]


def test_ordinary_names_are_shown_as_they_are() -> None:
    got = _preview({"path": "p", "workspace_id": "w1", "Content-Type": "json"})
    assert got["argument_keys"] == ["path", "Content-Type", "workspace_id"]
    assert got["arguments"] == "path=p, Content-Type=json, workspace_id=w1"


# --- a cut container says so ---------------------------------------------------------------------------------------------------


def test_a_mapping_cut_at_the_member_cap_says_how_many_it_left_out() -> None:
    from primer.api.routers.workspaces import _redact

    got, changed = _redact({f"k{i:02d}": i for i in range(60)})
    assert changed is True and len(got) == 51
    assert got["..."] == "<10 more>"


def test_a_list_cut_at_the_member_cap_says_how_many_it_left_out() -> None:
    from primer.api.routers.workspaces import _redact

    got, changed = _redact(list(range(60)))
    assert changed is True and len(got) == 51 and got[-1] == "<10 more>"


def test_a_container_at_the_cap_exactly_is_not_marked() -> None:
    from primer.api.routers.workspaces import _redact

    got, changed = _redact(list(range(50)))
    assert changed is False and len(got) == 50


# --- the bounds themselves are pinned, not only their effect on speed (review of PR 503 round 3) ------------------------------------
# Every pattern is linear now, so a missing bound would no longer show up as a slow test; it shows up as a scrubber that is handed text it was
# never meant to see. These watch what the scrubber is given.


def _recording_scrubber(monkeypatch) -> list[int]:
    from primer.api.routers import workspaces as w

    seen: list[int] = []
    real = w._scrub_text

    def recording(text: str) -> str:
        seen.append(len(text))
        return real(text)

    monkeypatch.setattr(w, "_scrub_text", recording)
    return seen


def test_the_prompt_is_cut_before_the_scrubber_is_given_it(monkeypatch) -> None:
    from primer.api.routers import workspaces as w

    seen = _recording_scrubber(monkeypatch)
    w._attention_prompt("word " * 20_000)
    assert seen and max(seen) <= w._PROMPT_SCAN_CHARS, f"the scrubber was handed {max(seen)} characters"


def test_one_preview_hands_the_scrubber_a_bounded_total(monkeypatch) -> None:
    """200 members of 1 500 characters each: the walk's budget, not the size of the call, decides how much text is scrubbed (the names add a
    little: each is cut to 200 characters before it is looked at)."""
    from primer.api.routers import workspaces as w

    seen = _recording_scrubber(monkeypatch)
    arguments = {f"k{i:03d}": "word " * 300 for i in range(200)}
    w._approval_preview({"name": "t", "arguments": arguments})
    assert sum(seen) <= w._REDACT_BUDGET + 4000, f"{sum(seen)} characters were scrubbed for one preview"
    assert max(seen) <= w._REDACT_MAX_TEXT


def test_a_string_is_cut_to_its_cap_before_the_scrubber_is_given_it(monkeypatch) -> None:
    from primer.api.routers import workspaces as w

    seen = _recording_scrubber(monkeypatch)
    w._redact("word " * 5_000)
    assert seen == [w._REDACT_MAX_TEXT] or max(seen) <= w._REDACT_MAX_TEXT


# --- a cut never leaves half a secret --------------------------------------------------------------------------------------------

_DIGEST = "0123456789abcdef" * 4
# Nine 100-character tokens (each becomes "<redacted> " and shrinks the text a lot), then padding, then a 64-character digest that the cut
# lands inside after 21 characters: too short for the 33-character rule, and (since the scrubbed text is short) well inside what is drawn.
_CUT_INSIDE_A_DIGEST = ("sk-" + "a" * 97 + " ") * 9 + " " * 70 + _DIGEST


def test_a_prompt_cut_inside_a_token_does_not_show_the_front_half_of_it() -> None:
    from primer.api.routers import workspaces as w

    assert len(_CUT_INSIDE_A_DIGEST) > w._PROMPT_SCAN_CHARS
    shown = w._attention_prompt(_CUT_INSIDE_A_DIGEST)
    assert "0123456789" not in shown, f"half a digest is drawn: {shown!r}"


def test_a_string_cut_inside_a_token_does_not_show_the_front_half_of_it() -> None:
    from primer.api.routers import workspaces as w

    text = ("sk-" + "a" * 97 + " ") * 19 + " " * 70 + _DIGEST        # 1989 characters before the digest: the 2000-character cap lands inside it
    scrubbed, changed = w._redact(text)
    assert changed is True and "0123456789" not in scrubbed


@pytest.mark.parametrize(("text", "limit", "kept"), [
    ("alpha beta gamma", 8, "alpha"),                 # the cut lands inside "beta": the partial token goes
    ("alpha beta gamma", 10, "alpha beta"),           # the cut lands on the space after "beta": nothing is lost
    ("alpha beta gamma", 99, "alpha beta gamma"),     # under the limit: untouched
    ("alpha beta\tgamma", 7, "alpha"),                # any whitespace is a boundary
    ("x" * 50, 10, "x" * 10),                         # one token longer than the limit: its head is all there is
])
def test_the_bounding_helper_never_ends_on_half_a_token(text: str, limit: int, kept: str) -> None:
    from primer.api.routers import workspaces as w

    assert w._bounded(text, limit) == kept
