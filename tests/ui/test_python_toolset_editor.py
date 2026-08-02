"""The python toolset authoring surface."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
EDITOR = UI / "components" / "toolsets" / "python-editor.jsx"
CODE_EDITOR = UI / "components" / "toolsets" / "python-code-editor.jsx"
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
    for tid in ("python-editor", "python-save",
                "python-derived-tools", "python-tool-row",
                "python-isolation-level", "python-registration-error",
                "python-derived-empty",
                # the builder surface
                "python-add-function", "python-outline", "python-live-status"):
        assert f'"{tid}"' in src, tid


def test_the_editing_surface_testid_lives_with_the_editor() -> None:
    # python-source moved to python-code-editor.jsx when the textarea became
    # a CodeMirror mount. Both the real editor and its no-CM6 fallback carry
    # it, so tests targeting "the place you type" keep working either way.
    src = CODE_EDITOR.read_text(encoding="utf-8")
    assert src.count('data-testid="python-source"') == 2


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


def test_a_new_python_toolset_starts_empty() -> None:
    # This used to seed PY_STARTER on create, justified as "an empty module
    # cannot register". That was simply false: register_module("") returns []
    # and PythonConfig(source="") validates. The seed's only real effect was
    # that every new toolset shipped a live, agent-callable `greet` tool the
    # operator never wrote.
    src = TOOLSETS.read_text(encoding="utf-8")
    start = src.index('if (provider === "python")')
    block = src[start:src.index("}", start)]
    assert 'source: ""' in block, "create must not seed a source"
    assert "PY_STARTER" not in block


def test_a_template_is_still_available_on_demand() -> None:
    # Removing the create-time seed must not remove the template. The single
    # PY_STARTER became PY_SCAFFOLDS -- one per tool shape, each carrying the
    # contract as comments -- reached from the "Add function" menu rather
    # than a lone "Insert example" button.
    src = CODE_EDITOR.read_text(encoding="utf-8")
    assert "PY_SCAFFOLDS" in src
    assert "@primer_tool()" in src
    assert "Use when" in src
    assert "Args:" in src
    assert 'data-testid="python-add-function"' in EDITOR.read_text(encoding="utf-8")


def test_the_empty_state_is_reachable() -> None:
    # The editor has always had a "no tools yet" branch, but seeding on create
    # made it dead code through the UI: every new toolset arrived with greet
    # already registered.
    src = EDITOR.read_text(encoding="utf-8")
    assert 'data-testid="python-derived-empty"' in src
    assert "No tools yet" in src


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


def test_the_error_is_read_from_the_raw_envelope() -> None:
    """ApiError is the wrong place to read a registration failure from.

    It exposes no `.extensions`, and for ANY 422 it rewrites title/detail into
    a generic "Data is incomplete" form message. A registration error carries
    the function name and line, which is the entire reason it renders inline,
    so it has to come off the raw envelope.
    """
    src = EDITOR.read_text(encoding="utf-8")
    assert "err.envelope && err.envelope.extensions" in src
    code = _code_only(src)
    assert "err.extensions" not in code, (
        "ApiError has no .extensions; reading it silently yields {}"
    )


def test_apierror_still_has_no_extensions_property() -> None:
    # Pins the assumption above against the shared class, so this breaks
    # loudly if ApiError ever grows one rather than silently going stale.
    api = (UI / "foundation" / "api.js").read_text(encoding="utf-8")
    assert "this.envelope = envelope;" in api
    assert "this.extensions" not in api
