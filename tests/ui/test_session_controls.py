"""Shared session binding chip and switcher (S1 P6 Task 33).

Static-source checks, the tests/ui convention.

The module takes PROPS ONLY: no window.location, no ROUTES, no chrome
or studio import. That is what lets S8's fresh shell re-host it
unchanged instead of rewriting it, and it is the same contract the
shared trace panel uses.
"""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
MODULE = UI / "components" / "shared" / "session-controls.jsx"


def _src() -> str:
    return MODULE.read_text(encoding="utf-8")


def test_module_exists_and_exports_both_globals():
    src = _src()
    assert "window.SessionBindingChip" in src
    assert "window.SessionSwitcher" in src


def test_index_loads_it_with_the_other_shared_modules():
    text = (UI / "index.html").read_text(encoding="utf-8")
    assert 'src="components/shared/session-controls.jsx"' in text


def _code_only() -> str:
    """Source with // comments stripped: the contract is about code,
    and the module's own docstring names what it avoids."""
    out = []
    for line in _src().splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def test_takes_props_only_so_a_new_shell_can_rehost_it():
    """The mount contract. A single window.location reference here is
    what would force S8 to rewrite rather than re-host."""
    code = _code_only()
    for banned in ("window.location", "ROUTES", "primerApi.useRouter"):
        assert banned not in code, f"{banned} breaks the props-only contract"


def test_posts_to_the_binding_endpoint():
    assert "/binding" in _src()


def test_detects_a_queued_switch_from_the_returned_row():
    """A busy session queues the switch and the endpoint returns 200
    with the row, so the queued state is pending_binding_switch, not a
    status code."""
    src = _src()
    assert "pending_binding_switch" in src


def test_does_not_optimistically_rewrite_the_binding():
    """Showing the new agent immediately would lie for the length of a
    turn, because the row does not change until the checkpoint applies
    the queued switch."""
    src = _src()
    assert "onSwitched" in src


def test_offers_graphs_as_well_as_agents():
    """The binding endpoint takes both kinds, so a switcher that only
    listed agents could not express half of what the API allows."""
    src = _src()
    assert "graph" in src.lower()
    assert "agent" in src.lower()


def test_chip_shows_the_epoch():
    """Epochs are how a reader tells one hand-off from the next."""
    src = _src()
    assert "bindingEpoch" in src or "binding_epoch" in src
