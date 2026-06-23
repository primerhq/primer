from pathlib import Path

from primer.api.app import _resolve_ui_dir


def test_prefers_packaged_ui_dir(tmp_path, monkeypatch):
    pkg = tmp_path / "primer"
    (pkg / "api").mkdir(parents=True)
    packaged_ui = pkg / "_ui"
    packaged_ui.mkdir()
    fake_app = pkg / "api" / "app.py"
    fake_app.write_text("")
    monkeypatch.setattr("primer.api.app.__file__", str(fake_app))
    assert _resolve_ui_dir() == packaged_ui


def test_falls_back_to_repo_ui_dir(tmp_path, monkeypatch):
    repo = tmp_path
    pkg = repo / "primer"
    (pkg / "api").mkdir(parents=True)
    repo_ui = repo / "ui"
    repo_ui.mkdir()
    fake_app = pkg / "api" / "app.py"
    fake_app.write_text("")
    monkeypatch.setattr("primer.api.app.__file__", str(fake_app))
    assert _resolve_ui_dir() == repo_ui
