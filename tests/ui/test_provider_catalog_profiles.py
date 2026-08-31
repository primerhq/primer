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


def test_the_panel_lists_profile_rows_not_just_model_names() -> None:
    """A profile is the deletable entity; a bare model-name list gives an
    operator nothing to act on.

    RETARGET (platform wave P4): the panel used to render a bare <ul> of
    rows itself; it now delegates rendering to MP_ProfileCard/
    MP_ProfilesGrid (model-profiles.jsx), so model_name lives there, not
    in the panel's own source. The panel's own responsibility - fetching
    /model_profiles and filtering to this provider - is unchanged.
    """
    panel_src = _read("components/provider-catalog.jsx")
    assert "/model_profiles" in panel_src
    assert "r.provider_id === providerId" in panel_src or "provider_id ===" in panel_src
    card_src = _read("components/model-profiles.jsx")
    assert "model_name" in card_src


def test_profiles_can_be_deleted_from_the_panel() -> None:
    """RETARGET (platform wave P4): delete now lives on MP_ProfileCard,
    wired in via MP_ProfilesGrid rather than reimplemented in the
    panel."""
    src = _read("components/model-profiles.jsx")
    assert 'data-testid={`profile-card-delete-${profile.id}`}' in src
    assert "/model_profiles/${encodeURIComponent(profile.id)}" in src


def test_harness_managed_rows_hide_mutation() -> None:
    """Mirrors the backend's 409-on-public-CRUD discipline
    (managed_by_field="harness_id"). RETARGET (platform wave P4): the
    guard moved onto MP_ProfileCard itself so it survives wherever the
    card is mounted next, not just this one panel."""
    src = _read("components/model-profiles.jsx")
    assert "harness_id" in src
    assert "harnessManaged" in src


def test_a_delete_conflict_renders_inline_not_as_a_toast() -> None:
    """409 means an agent still points at it; the operator needs to act on
    that without losing their place. RETARGET (platform wave P4): the
    per-row error now lives on MP_ProfileCard, scoped per card instead
    of one panel-wide error string."""
    src = _read("components/model-profiles.jsx")
    assert "setErr" in src
    assert 'data-testid={`profile-card-error-${profile.id}`}' in src


def test_profile_delete_is_gated_behind_a_confirm() -> None:
    """The dialog that regressed on the old page
    (tests/ui/test_modal_open_prop.py) stays gated here. RETARGET
    (platform wave P4): confirmDelete is now a plain per-card boolean
    (React.useState(false)) rather than a panel-wide id comparison -
    a stronger form of the same gate, since there is no id to compare
    against at all, so the old confirmProfile?.id-style bug class
    cannot recur."""
    src = _read("components/model-profiles.jsx")
    assert "const [confirmDelete, setConfirmDelete] = React.useState(false);" in src
    assert 'data-testid={`profile-card-delete-confirm-${profile.id}`}' in src
