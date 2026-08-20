"""A component may not read a hook value a SIBLING component fetched.

Found the hard way: ``AG_NewAgentModal`` rendered a tts_voice picker
gated on ``caps.data.speech`` and filled from ``voices.data``, but only
``AgentsPage`` ever called ``useCapabilities()`` / fetched the voice
list. Opening the modal and switching to its Advanced tab threw
``ReferenceError: caps is not defined``, which took down the whole tab
-- including the unrelated fields on it.

Nothing caught it. These files are transpiled and exercised as source by
the unit lane, so a name that only resolves at RENDER time inside one
branch of one component is invisible there; and the ui_e2e journey that
would have hit it was drowned in a lane that failed on its first wait.

This gate is deliberately narrow: it looks for a handful of hook-shaped
locals being read inside a top-level component that does not declare
them. It will not catch every scope error, but it catches the one that
actually shipped, and it costs nothing to run.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"

# Hook results that are fetched per-component and read as ``<name>.data``.
# A component reading one it never fetched is the bug this gate is for.
WATCHED = ("caps", "voices", "profiles", "toolsCatalogue", "toolsets")

# Top-level declarations start at column 0: these files are plain script
# globals, not modules, so every component sits at the left margin.
_TOP_LEVEL = re.compile(r"^(?:function|const|let|var)\s+([A-Za-z_$][\w$]*)", re.M)

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")


def _without_comments(source: str) -> str:
    """Blank out comments, keeping line numbers intact.

    Prose mentions sibling modules by filename ("see toolsets.jsx"),
    which reads exactly like a property access to a regex.
    """
    def _blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    return _LINE_COMMENT.sub(_blank, _BLOCK_COMMENT.sub(_blank, source))


def _components(source: str) -> list[tuple[str, str, int]]:
    """Return ``(name, body, start_line)`` per top-level declaration."""
    marks = [(m.start(), m.group(1)) for m in _TOP_LEVEL.finditer(source)]
    if not marks:
        return []
    marks.append((len(source), "<eof>"))
    out = []
    for i in range(len(marks) - 1):
        start, name = marks[i]
        out.append((name, source[start:marks[i + 1][0]],
                    source[:start].count("\n") + 1))
    return out


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(UI.rglob("*.jsx")):
        source = _without_comments(path.read_text(encoding="utf-8"))
        for name, body, line in _components(source):
            for watched in WATCHED:
                # ``<name>.<ident>`` only: a bare "<name>." also matches
                # prose ending a sentence, which is not a reference.
                if not re.search(rf"\b{watched}\.\w", body):
                    continue
                declared = re.search(
                    rf"(?:const|let|var)\s+{watched}\b|"
                    rf"\{{[^}}\n]*\b{watched}\b[^}}\n]*\}}\s*=|"
                    rf"function\s+\w+\s*\([^)]*\b{watched}\b",
                    body,
                )
                if declared:
                    continue
                rel = path.relative_to(UI.parent)
                found.append(f"{rel}:{line}: {name}() reads {watched!r} "
                             f"without fetching it")
    return found


def test_no_component_reads_a_siblings_hook_value() -> None:
    offenders = _offenders()
    assert not offenders, (
        "these resolve to nothing at render time and throw ReferenceError, "
        "taking the whole branch down with them: " + "; ".join(offenders)
    )


def test_the_gate_can_actually_see_an_offender() -> None:
    """The shape that shipped, so a rewrite that stops matching shows up."""
    source = (
        "function Page() {\n"
        "  const caps = window.primerApi.useCapabilities();\n"
        "  return <div>{caps.data ? 'y' : 'n'}</div>;\n"
        "}\n"
        "function Modal() {\n"
        "  return <div>{caps.data.speech ? 'y' : 'n'}</div>;\n"
        "}\n"
    )
    hits = [
        name for name, body, _ in _components(source)
        if re.search(r"\bcaps\.\w", body)
        and not re.search(r"(?:const|let|var)\s+caps\b", body)
    ]
    assert hits == ["Modal"], hits


# ---------------------------------------------------------------------------
# Shell globals
# ---------------------------------------------------------------------------

_WINDOW_CALL = re.compile(r"window\.(SH_[A-Za-z0-9_]+)\s*\(")
_WINDOW_DEF = re.compile(r"window\.(SH_[A-Za-z0-9_]+)\s*=")


def test_every_shell_global_called_is_a_shell_global_defined() -> None:
    """``window.SH_x(...)`` has to resolve to something.

    These files are plain scripts that publish their exports with an
    explicit ``window.SH_x = SH_x``, so a call to a name nobody exported
    is not a compile error anywhere -- it throws "not a function" the
    first time a user reaches that path, and only then.

    FIVE were live at once: SH_openOverlayFromShim, SH_OVERLAY_OPEN_DOC,
    SH_OVERLAY_OPEN_OVERLAY, SH_OVERLAY_SWITCH_WORKSPACE and SH_traceRef.
    Between them they meant a provider, an agent, a graph, a collection,
    an approval's session, a workspace and a trace tab all failed to
    open -- every one of them the primary click on its surface.
    """
    called: dict[str, set[str]] = {}
    defined: set[str] = set()
    for path in sorted(UI.rglob("*.js")) + sorted(UI.rglob("*.jsx")):
        source = _without_comments(path.read_text(encoding="utf-8"))
        for m in _WINDOW_CALL.finditer(source):
            called.setdefault(m.group(1), set()).add(
                str(path.relative_to(UI.parent))
            )
        for m in _WINDOW_DEF.finditer(source):
            defined.add(m.group(1))

    missing = {
        name: sorted(places) for name, places in called.items()
        if name not in defined
    }
    assert not missing, (
        "these throw \"not a function\" the first time anyone clicks the "
        f"thing that calls them: {missing}"
    )
