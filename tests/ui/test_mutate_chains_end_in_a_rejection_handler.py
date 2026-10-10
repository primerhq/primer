"""A promise chained onto ``mutate(...)`` must end in a rejection handler (the #608 review nit, finding ADM-18).

``useMutation``'s ``mutate`` returns a promise that is already marked handled (``ui/foundation/use-mutation.js``), so ``create.mutate(body)`` in a click handler
leaves nothing unhandled when the write is refused. That covers THAT promise only. ``create.mutate(body).then(onDone)`` derives a NEW promise from it, and the
derived promise rejects with the same error and has no handler: every refusal the form handles perfectly well is an ``unhandledrejection`` again (and a
Playwright ``pageerror``). The same holds for ``.finally(fn)``.

This pins the count at zero. A chain whose LAST link is ``.catch(fn)`` or ``.then(onDone, onFail)`` is handled; a chain with an ``await`` or a ``return`` in front
of it hands the rejection to the enclosing async function or the caller, like awaiting ``mutate`` itself, and is left alone. Anything else is listed.
The scan reads the source with comments and the insides of string literals blanked out, so a ``)`` in a message or a call in a comment cannot end or start a
match.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "ui"

_MUTATE = re.compile(r"\.mutate\s*\(")
_LINK = re.compile(r"\s*\.(then|catch|finally)\s*\(")
_AWAITED = re.compile(r"\b(?:await|return)\s+[\w$.\[\]?\s]*$")


def _blank(src: str) -> str:
    """The same text with every comment and the inside of every string blanked to spaces (newlines kept), so offsets and line numbers still match.

    A quote opened in JSX text (``Don't``) or in a regex literal would swallow the rest of the file, so a ' or " string ends at the newline.
    Template literals are blanked whole, ``${...}`` included.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if src[k] != "\n":
                    out[k] = " "
            i = j
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                out[k] = " "
            i = j + 1
        elif c == "`":
            j = i + 1
            while j < n and src[j] != "`":
                j += 2 if src[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if src[k] != "\n":
                    out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _close(text: str, open_at: int) -> int:
    """The offset just past the bracket that closes the one at ``open_at`` (-1 when the file ends first)."""
    depth, i = 0, open_at
    while i < len(text):
        if text[i] in "([{":
            depth += 1
        elif text[i] in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _arguments(text: str, start: int, end: int) -> int:
    """How many non-empty top-level arguments the call between ``start`` (after its ``(``) and ``end`` (its ``)``) has."""
    depth, parts, cur = 0, [], []
    for ch in text[start:end]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return sum(1 for p in parts if p.strip())


def scan(src: str) -> tuple[int, list[int]]:
    """``(mutate call sites seen, line numbers of the ones whose chain ends without a rejection handler)``."""
    text = _blank(src)
    sites, bad = 0, []
    for m in _MUTATE.finditer(text):
        sites += 1
        end = _close(text, m.end() - 1)
        if end < 0:
            continue
        last = None
        while True:
            link = _LINK.match(text, end)
            if not link:
                break
            close = _close(text, link.end() - 1)
            if close < 0:
                break
            last = (link.group(1), _arguments(text, link.end(), close - 1))
            end = close
        if last is None:
            continue  # no chain: the promise mutate returns is handled by the hook
        method, args = last
        if method == "catch" or (method == "then" and args >= 2):
            continue
        line_start = text.rfind("\n", 0, m.start()) + 1
        if _AWAITED.search(text[line_start:m.start()]):
            continue
        bad.append(src.count("\n", 0, m.start()) + 1)
    return sites, bad


def _tree(root: Path) -> tuple[int, list[str]]:
    sites, offenders = 0, []
    for path in sorted([*root.rglob("*.js"), *root.rglob("*.jsx")]):
        n, bad = scan(path.read_text(encoding="utf-8"))
        sites += n
        offenders += [f"{path.relative_to(root)}:{line}" for line in bad]
    return sites, offenders


# ---- the scanner, on snippets ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        "create.mutate(body).then(onDone);",
        "create.mutate(body).then((d) => { go(d.id); });",
        "create.mutate(body).then(onDone).then(again);",
        "create.mutate(body).finally(() => setBusy(false));",
        "create.mutate(body).catch(noop).then(onDone);",
        "create.mutate(body)\n    .then(onDone);",
        "onClick={() => del.mutate({ id: row.id, msg: \")\" }).then(refresh)}",
        "x.mutate(a).then(() => 1, );",
    ],
)
def test_a_chain_without_a_rejection_handler_at_the_end_is_listed(snippet: str) -> None:
    sites, bad = scan(snippet)
    assert sites == 1 and len(bad) == 1, snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "create.mutate(body);",
        "create.mutate(body).catch(() => {});",
        "create.mutate(body).then(onDone, onFail);",
        "create.mutate(body).then((d) => d.id, () => {});",
        "create.mutate(body).then(onDone).catch(() => {});",
        "create.mutate(body).finally(done).catch(() => {});",
        "await create.mutate(body).then(onDone);",
        "return create.mutate(body).then(onDone);",
        "const r = await rows[0].create.mutate(body).then(onDone);",
        "// create.mutate(body).then(onDone);",
        "/* create.mutate(body).then(onDone) */ create.mutate(body);",
        "const s = 'create.mutate(body).then(onDone)';",
        "const t = `${a} create.mutate(body).then(onDone)`;",
    ],
)
def test_a_handled_chain_a_plain_call_an_awaited_chain_and_text_that_is_not_code_are_left_alone(snippet: str) -> None:
    _, bad = scan(snippet)
    assert bad == [], snippet


def test_the_line_of_the_call_is_reported() -> None:
    assert scan("a();\nb();\nc.mutate(x)\n  .then(f);\n")[1] == [3]


def test_a_quote_in_jsx_text_does_not_swallow_the_rest_of_the_file() -> None:
    src = "<p>Don't save</p>\nonClick={() => c.mutate(b).then(f)}\n"
    assert scan(src)[1] == [2]


def test_a_tree_with_an_offender_is_listed_by_file_and_line(tmp_path: Path) -> None:
    (tmp_path / "components").mkdir()
    (tmp_path / "components" / "bad.jsx").write_text("const a = 1;\nonClick={() => c.mutate(b).then(f)}\n", encoding="utf-8")
    (tmp_path / "components" / "good.jsx").write_text("onClick={() => c.mutate(b).catch(() => {})}\n", encoding="utf-8")

    sites, offenders = _tree(tmp_path)

    assert sites == 2 and offenders == ["components/bad.jsx:2"]


# ---- the console ---------------------------------------------------------------------------------------------------------------------------------------


def test_no_mutate_chain_in_the_console_ends_without_a_rejection_handler() -> None:
    sites, offenders = _tree(UI)

    assert sites >= 20, f"the scan found {sites} mutate call sites: it is looking at the wrong files (there were 56 when it was written)"
    assert not offenders, (
        "a promise chained onto mutate(...) is a NEW promise: it rejects with the refusal and nothing handles it (an unhandledrejection per 422 or 409). "
        "End the chain with .catch(...) or .then(onDone, onFail), or await it: " + ", ".join(offenders)
    )
