"""FD3 — guard against duplicate top-level declarations across the bundle.

The console has NO bundler/module system: the server concatenates every
`ui/**/*.jsx` + `ui/foundation/*.js` into ONE flat global scope (top-level
`const/let` are rewritten to `var`). So two files that each declare a
top-level `function Foo(` / `const Foo =` silently collide — the last one
loaded wins, with no load-time error. That is exactly how the global ⌘K
command palette broke (`CommandPalette` declared in both chrome.jsx and
studio-palette.jsx). This test fails loudly on any such collision so the
next one can't ship silently.
"""

import re
from collections import defaultdict
from pathlib import Path

_UI = Path(__file__).resolve().parents[2] / "ui"

# Top-level (column-0, not indented) declaration of a named binding.
_DECL = re.compile(r"^(?:export\s+)?(?:function|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)")


def _bundled_files() -> list[Path]:
    files: list[Path] = []
    files += sorted((_UI / "components").rglob("*.jsx"))
    files += sorted(_UI.glob("*.jsx"))
    files += sorted((_UI / "foundation").glob("*.js"))
    return files


def test_no_duplicate_top_level_declarations_across_bundle() -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    for f in _bundled_files():
        rel = str(f.relative_to(_UI))
        for line in f.read_text().splitlines():
            m = _DECL.match(line)
            if m:
                owners[m.group(1)].append(rel)

    collisions = {
        name: sorted(set(files))
        for name, files in owners.items()
        if len(set(files)) > 1
    }
    assert not collisions, (
        "Duplicate top-level declarations share the flat bundle scope "
        "(last-loaded wins, silently). Rename one:\n"
        + "\n".join(f"  {name}: {files}" for name, files in sorted(collisions.items()))
    )
