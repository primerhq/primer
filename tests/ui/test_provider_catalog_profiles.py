"""Profiles nest under LLM instances ONLY (amendment M11 profiles note).

ModelProfile is LLM-only by design: the profiles router hardcodes an
LLMProvider existence check. Embedding, cross-encoder and speech classes
carry provider + model inline instead, so a profiles panel under any of
them would be an unbacked surface.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_only_the_llm_class_is_marked_as_carrying_profiles() -> None:
    src = _read("components/provider-catalog.jsx")
    marked = re.findall(r'key:\s*"(\w+)"[^}]*profiles:\s*true', src)
    assert marked == ["llm"], f"profiles wired to non-LLM classes: {marked}"


def test_the_profiles_panel_is_gated_on_that_flag() -> None:
    src = _read("components/provider-catalog.jsx")
    assert "klass.profiles" in src
    assert "PC_ProfilesPanel" in src


def test_the_panel_reuses_the_existing_profile_modal() -> None:
    """The standalone page folds in, but the editor is reused, not
    reimplemented."""
    src = _read("components/provider-catalog.jsx")
    assert "window.MP_ProfileModal" in src


def test_profiles_are_read_per_selected_llm_instance() -> None:
    src = _read("components/provider-catalog.jsx")
    assert "/models" in src
    assert "discovered_models" in src or "_discover_models" in src


def test_no_speech_class_offers_a_profile_affordance() -> None:
    src = _read("components/provider-catalog.jsx")
    for key in ('key: "stt"', 'key: "tts"', 'key: "embedding"', 'key: "cross_encoder"'):
        entry = src[src.index(key): src.index(key) + 200]
        assert "profiles" not in entry, f"{key} must not carry a profiles flag"
