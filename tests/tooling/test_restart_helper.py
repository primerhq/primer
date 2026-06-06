"""Import + no-bringup behavior check for the restart helper."""
from __future__ import annotations

from tests._support import restart


def test_imports_and_detects_no_bringup(monkeypatch, tmp_path):
    # When no pid file exists, under_bringup() is False and restart_server skips.
    monkeypatch.setattr(restart, "_PID_FILE", tmp_path / "server.pid")
    monkeypatch.setattr(restart, "_CONFIG", tmp_path / "config.yaml")
    assert restart.under_bringup() is False

    import pytest

    with pytest.raises(pytest.skip.Exception):
        restart.restart_server("http://127.0.0.1:8765")
