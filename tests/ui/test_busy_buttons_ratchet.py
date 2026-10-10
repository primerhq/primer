"""A ratchet on buttons that go natively ``disabled`` while their request is out (board task 01a12480-2176).

A focused button that turns ``disabled`` drops the focus to ``<body>``: the keyboard user loses the place they were in, and a focus trap can at best put them back inside the dialog. The cure is ``<Btn busy={...}>``
(``ui/components/shared.jsx``; ``aria-disabled`` and ``aria-busy`` instead of ``disabled``, the click refused, the button still focusable), documented in ``docs/dev/subsystems/ui-foundation.md``. This file counts the
buttons that are still the old way, per file, and the counts may only go DOWN: a new one fails, and a file that got below its count fails until ``BASELINE`` is lowered (so a number is never a cushion for a later one).

What is counted: a ``<Btn`` or ``<button`` opening tag whose ``disabled={...}`` expression names a request in flight (``busy``, ``saving``, ``loading``, ``pending``, ``sending``, ... in any case or as a part of a longer
name: ``createBusy``, ``del.loading``, ``attachmentsPending``). ``disabled={!canSubmit}`` and ``disabled={referencing.length > 0}`` are not requests and are not counted; a button with both
(``disabled={!canSubmit || busy}``) is, and is fixed by splitting it: ``disabled={!canSubmit} busy={busy}``. Comments are not code (a ``//`` that starts its line ends it before a ``/*`` is looked for).

What the scan does NOT see (review of #732, N8): a ``disabled`` that arrives through a spread (``{...props}``); only the FIRST ``disabled={`` of a tag is read (a second one, which React keeps, is invisible); ``disabled = {x}`` with spaces;
an alias (``const inFlight = del.loading``); and an in-flight name that is not in the list (``testing``, ``fetching``, ``isMutating``, ``installing``, ``refreshing``). ``busy || x``, ``x || busy``, a ternary and a nested JSX
expression ARE counted. The ticket for widening it (aliases, ``<input>``/``<select>``/``<textarea>``) is T1 of the review.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.ui.test_graph_builder_clickables_ratchet import opening_tag, strip_comments

UI = Path(__file__).resolve().parents[2] / "ui"

# a name that says "a request is out"; a part of a longer identifier counts (isRunning, attachmentsPending, slugChecking)
IN_FLIGHT = re.compile(
    r"\w*(?:[Bb]usy|[Ss]aving|[Ss]ubmitting|[Ss]kipping|[Pp]ending|[Ll]oading|[Ww]orking|[Dd]eleting|[Cc]reating|[Rr]unning|[Ss]ending|[Ff]iring|[Pp]robing|[Rr]etrying|[Ss]topping|[Dd]raining|[Cc]hecking)\w*"
)
BUTTON = re.compile(r"<(?:Btn|button)\b")
DISABLED = re.compile(r"(?<![\w-])disabled=\{")

# the buttons that are still natively disabled while a request is out, per file (can only shrink)
BASELINE: dict[str, int] = {
    "admin_users.jsx": 7,
    "agents.jsx": 3,
    "api_tokens.jsx": 3,
    "approvals.jsx": 4,
    "auth.jsx": 3,
    "channel_rules.jsx": 2,
    "channels.jsx": 1,
    "console/nv-mobile-shell.jsx": 3,
    "console/nv-overlays.jsx": 1,
    "console/nv-platform.jsx": 1,
    "console/nv-session-doc.jsx": 3,
    "console/nv-system.jsx": 1,
    "graph-builder/gb-dryrun.jsx": 1,
    "graph-builder/gb-palette.jsx": 1,
    "graph-builder/graph-builder.jsx": 2,
    "graphs.jsx": 1,
    "harness_outbound_builder.jsx": 4,
    "harnesses.jsx": 8,
    "internal-collections.jsx": 5,
    "knowledge.jsx": 9,
    "linked_accounts.jsx": 2,
    "mcp.jsx": 3,
    "model-profiles.jsx": 2,
    "provider-catalog.jsx": 1,
    "provider-form.jsx": 4,
    "semantic-search.jsx": 4,
    "services.jsx": 2,
    "session-detail.jsx": 4,
    "sessions-list.jsx": 3,
    "setup-wizard.jsx": 5,
    "shared/pager.jsx": 2,
    "shared/session-controls.jsx": 1,
    "shared/transcript.jsx": 2,
    "shell/sh-activity.jsx": 2,
    "sso_admin.jsx": 5,
    "toolsets.jsx": 4,
    "toolsets/python-editor.jsx": 1,
    "triggers.jsx": 17,
    "workers.jsx": 3,
    "workspaces.jsx": 9,
    "workspaces/providers.jsx": 1,
    "workspaces/templates.jsx": 1,
}


def disabled_expression(tag: str) -> str | None:
    """The text between the braces of ``disabled={...}`` in an opening tag, or ``None`` when the tag has none."""
    m = DISABLED.search(tag)
    if not m:
        return None
    depth, i = 1, m.end()
    while depth and i < len(tag):
        depth += {"{": 1, "}": -1}.get(tag[i], 0)
        i += 1
    return tag[m.end() : i - 1]


def busy_disabled(text: str) -> list[tuple[int, str]]:
    """``(line, expression)`` of every ``<Btn>`` or ``<button>`` in ``text`` whose ``disabled`` names a request in flight."""
    text = strip_comments(text)
    found = []
    for m in BUTTON.finditer(text):
        expression = disabled_expression(opening_tag(text, m.start()))
        if expression is not None and IN_FLIGHT.search(expression):
            found.append((text.count("\n", 0, m.start()) + 1, " ".join(expression.split())))
    return found


def counts(root: Path = UI) -> dict[str, int]:
    """Per file under ``root`` (keyed relative to ``root/components`` for a component, else to ``root``: ``ui/app.jsx`` is a file too)."""
    out = {}
    for path in sorted(root.rglob("*.jsx")):
        if "vendor" in path.parts:
            continue
        n = len(busy_disabled(path.read_text(encoding="utf-8")))
        if n:
            base = root / "components"
            out[(path.relative_to(base) if path.is_relative_to(base) else path.relative_to(root)).as_posix()] = n
    return out


def test_no_more_buttons_go_disabled_while_their_request_is_out_than_the_baseline() -> None:
    now = counts()
    assert now == BASELINE, (
        "a button was made natively `disabled` for the length of its request (use <Btn busy={...}>, docs/dev/subsystems/ui-foundation.md), or one was fixed (lower the number in BASELINE): "
        f"{ {k: (now.get(k, 0), BASELINE.get(k, 0)) for k in sorted(set(now) | set(BASELINE)) if now.get(k, 0) != BASELINE.get(k, 0)} }"
    )


def test_the_scan_sees_what_it_claims_to() -> None:
    text = (
        "<Btn disabled={busy}>a</Btn>\n"
        "<Btn disabled={!canSubmit || create.loading}>b</Btn>\n"
        "<button disabled={sending || attachmentsPending}>c</button>\n"
        "<Btn disabled={!canSubmit} busy={busy}>d</Btn>\n"
        "<Btn disabled={referencing.length > 0}>e</Btn>\n"
        "<Btn onClick={go}>f</Btn>\n"
        "<Btn\n  kind=\"danger\"\n  disabled={chs.length > 0 || del.loading}\n  onClick={() => del.mutate()}\n>g</Btn>\n"
    )
    assert busy_disabled(text) == [
        (1, "busy"),
        (2, "!canSubmit || create.loading"),
        (3, "sending || attachmentsPending"),
        (7, "chs.length > 0 || del.loading"),
    ]


def test_a_button_that_is_busy_but_not_disabled_is_not_counted() -> None:
    assert busy_disabled("<Btn busy={create.loading} onClick={go}>x</Btn>") == []
    assert busy_disabled('<Btn aria-busy="true" aria-disabled="true">x</Btn>') == []


def test_the_name_matches_by_part_and_in_any_case() -> None:
    for name in ("busy", "isBusy", "createBusy", "busyId", "saving", "submitting", "skipping", "isPending", "loadingEarlier", "chatLoading", "isRunning", "firing", "slugChecking", "draining"):
        assert busy_disabled(f"<Btn disabled={{{name}}}>x</Btn>"), name
    for name in ("canSubmit", "dirty", "locked", "isRevoked", "!ready", "page === 1", "draft == null"):
        assert busy_disabled(f"<Btn disabled={{{name}}}>x</Btn>") == [], name


def test_comments_are_not_code() -> None:
    assert busy_disabled("// <Btn disabled={busy}>x</Btn>\n/* <button disabled={saving}>y</button> */\n") == []


def test_an_arrow_function_in_another_attribute_does_not_end_the_tag_early() -> None:
    assert busy_disabled("<Btn onClick={() => { if (a > b) go(); }} disabled={busy}>x</Btn>") == [(1, "busy")]


def test_no_raw_button_is_given_a_busy_attribute() -> None:
    """``busy`` is ``Btn``'s prop. On a ``<button>`` it is an unknown attribute React passes to the DOM (and warns about): a raw button spells ``aria-busy`` and ``aria-disabled``."""
    offenders = []
    for path in sorted(UI.rglob("*.jsx")):
        if "vendor" in path.parts:
            continue
        text = strip_comments(path.read_text(encoding="utf-8"))
        for m in re.finditer(r"<button\b", text):
            if re.search(r"(?<![\w-])busy=", opening_tag(text, m.start())):
                offenders.append(f"{path.relative_to(UI)}:{text.count(chr(10), 0, m.start()) + 1}")
    assert offenders == []


def test_a_busy_button_after_a_line_comment_that_holds_a_block_opener_is_counted() -> None:
    """Review of #732, B1: a ``//`` comment quoting a glob (``primer/channel/*/``) hid the rest of the file from the scan. channel_rules.jsx, nv-session-doc.jsx and graphs.jsx each have one; the real counts are 153 on main and
    142 at the head of #732, not 151 and 140."""
    assert busy_disabled("// see a/*\n<Btn disabled={busy}>x</Btn>\n") == [(2, "busy")]
    assert busy_disabled("// a/*\n<button disabled={saving}>x</button>\n/* b */\n<Btn disabled={create.loading}>y</Btn>\n") == [(2, "saving"), (4, "create.loading")]


def test_a_file_directly_under_ui_is_counted_and_does_not_raise(tmp_path: Path) -> None:
    """Review of #732, N7: ``relative_to(UI / "components")`` raised a bare ValueError for ``ui/app.jsx``, ``ui/design-canvas.jsx`` and ``ui/tweaks-panel.jsx``."""
    (tmp_path / "components").mkdir()
    (tmp_path / "app.jsx").write_text("<Btn disabled={busy}>x</Btn>\n", encoding="utf-8")
    (tmp_path / "components" / "page.jsx").write_text("<Btn disabled={busy}>x</Btn>\n<Btn disabled={saving}>y</Btn>\n", encoding="utf-8")
    assert counts(tmp_path) == {"app.jsx": 1, "page.jsx": 2}
