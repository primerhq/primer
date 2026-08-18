"""Static checks for the Studio2 rail + navigator (plan task 5)."""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_ten_groups_with_chords() -> None:
    # The chats rail entry went with the chat UI in S1 P7.
    src = _read("components/studio2/s2-rail.jsx")
    for gid, chord in [("work", "s"), ("agents", "a"),
                       ("graphs", "g"), ("knowledge", "k"), ("files", "f"),
                       ("compute", "m"), ("toolsets", "t"), ("autom", "u"),
                       ("services", "v"), ("system", "y")]:
        assert f'"{gid}"' in src and f'"{chord}"' in src


def test_nav_commands_registered() -> None:
    src = _read("components/studio2/s2-rail.jsx")
    assert '"nav:" + g.chord' in src


def test_nav_uses_live_resources() -> None:
    src = _read("components/studio2/s2-nav.jsx")
    assert "useResource" in src and "apiFetch" in src
    assert "/sessions?limit=200" in src and "/agents?limit=200" in src


def test_nav_keyboard_rows() -> None:
    src = _read("components/studio2/s2-nav.jsx")
    for key in ['"ArrowDown"', '"Enter"', '"Escape"', '"j"', '"k"']:
        assert key in src


def test_sessions_group_by_workspace_with_status_dots() -> None:
    src = _read("components/studio2/s2-nav.jsx")
    assert "workspace_id" in src
    assert "S2_statusColor" in src
    # Design-pack rule 5: dots map to a real state enum only.
    assert "running" in src and "waiting" in src and "failed" in src


def test_legacy_rows_reach_unmigrated_groups() -> None:
    src = _read("components/studio2/s2-nav.jsx")
    assert "S2_LEGACY_ROUTES" in src


def test_quick_index_reads_resource_cache() -> None:
    src = _read("components/studio2/s2-nav.jsx")
    assert "window.S2_QuickIndex" in src
    assert "peekData" in src


def test_shell_mounts_rail_and_nav() -> None:
    src = _read("components/studio2/s2-shell.jsx")
    assert "S2_Rail" in src and "S2_Nav" in src
