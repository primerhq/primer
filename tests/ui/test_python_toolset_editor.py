"""The python toolset authoring surface."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
EDITOR = UI / "components" / "toolsets" / "python-editor.jsx"
TOOLSETS = UI / "components" / "toolsets.jsx"


def _code_only(src: str) -> str:
    out = []
    for line in src.splitlines():
        idx = line.find("//")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


def test_the_editor_exists_and_exports() -> None:
    src = EDITOR.read_text(encoding="utf-8")
    assert "function PythonToolsetEditor(" in src
    assert "window.PythonToolsetEditor" in src


def test_it_exposes_its_testids() -> None:
    src = EDITOR.read_text(encoding="utf-8")
    for tid in ("python-editor", "python-source", "python-save",
                "python-derived-tools", "python-tool-row",
                "python-isolation-level", "python-registration-error",
                "python-derived-empty"):
        assert f'"{tid}"' in src, tid


def test_every_isolation_level_has_operator_facing_copy() -> None:
    # A generic "sandboxed" badge would be a lie on rlimit-only. An operator
    # deciding whether to trust a tool needs to know which level they are on.
    src = EDITOR.read_text(encoding="utf-8")
    for level in ("container", "seccomp", "sandbox-exec", "rlimit-only"):
        assert level in src, level
    assert "are NOT" in src, "rlimit-only must state what it does not cover"


def test_registration_errors_render_inline_with_the_line() -> None:
    # The error carries a line number; a toast slides away while the operator
    # is still looking for it.
    src = EDITOR.read_text(encoding="utf-8")
    assert "python-registration-error" in src
    assert "lineno" in src
    code = _code_only(src)
    assert "pushToast" in code  # success still toasts
    assert code.index("setRegError") < code.index("window.PythonToolsetEditor")


def test_the_save_invalidates_every_reader_of_this_toolset() -> None:
    # Three readers: this editor, the parent detail view (its own key), and the
    # runtime facts. Missing any leaves a stale view after an edit.
    src = EDITOR.read_text(encoding="utf-8")
    for key in ('"toolset:" + toolsetId',
                '"toolset-detail:" + toolsetId',
                '"toolset-runtime:" + toolsetId'):
        assert key in src, key


def test_the_runtime_route_is_what_supplies_the_level() -> None:
    src = EDITOR.read_text(encoding="utf-8")
    assert "/runtime" in src
    assert "isolation_level" in src


def test_save_is_disabled_until_the_source_changes() -> None:
    src = EDITOR.read_text(encoding="utf-8")
    assert "disabled={!dirty || save.loading}" in src


def test_the_config_tab_becomes_the_editor_for_python() -> None:
    src = TOOLSETS.read_text(encoding="utf-8")
    assert 'ts?.provider === "python"' in src
    assert "window.PythonToolsetEditor" in src


def test_python_is_offered_in_the_create_form() -> None:
    src = TOOLSETS.read_text(encoding="utf-8")
    assert '<option value="python">' in src
    assert 'provider === "python"' in src


def test_a_new_python_toolset_starts_from_a_registrable_example() -> None:
    # An empty module cannot register, so create would fail validation and the
    # operator would never reach the editor.
    src = EDITOR.read_text(encoding="utf-8")
    assert "PY_STARTER" in src
    assert "@primer_tool()" in src
    assert "Use when" in src
    assert "Args:" in src


def test_registered_before_the_toolsets_page() -> None:
    lines = (UI / "index.html").read_text(encoding="utf-8").splitlines()
    reg = [i for i, ln in enumerate(lines) if 'type="text/babel"' in ln and "src=" in ln]

    def idx(frag: str) -> int:
        for i in reg:
            if frag in lines[i]:
                return i
        raise AssertionError(f"{frag} is not registered")

    assert idx("toolsets/python-editor.jsx") < idx("components/toolsets.jsx")


def test_the_bundle_builds_with_the_editor() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    _etag, body = build_jsx_bundle(UI)
    assert "window.PythonToolsetEditor" in body.decode("utf-8")
