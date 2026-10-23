"""Profiles left the provider catalog for the LLM Profiles page (design 7, B5; plan 02 task 8).

ModelProfile is LLM-only by design: the profiles router hardcodes an
LLMProvider existence check. Embedding, cross-encoder and speech classes
carry provider + model inline instead, so a profiles surface under any of
them would be an unbacked surface. The catalog keeps only the LLM class's
profiles flag (the per-card count badge); the browse/create/edit panel is
pinned where it lives now (plan 03).
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_only_the_llm_class_is_marked_as_carrying_profiles() -> None:
    src = _read("components/providers/register-common.jsx")
    marked = re.findall(r'key:\s*"(\w+)"[^}]*profiles:\s*true', src)
    assert marked == ["llm"], f"profiles wired to non-LLM classes: {marked}"


def test_the_catalog_has_no_profiles_panel() -> None:
    """The panel left the catalog for the LLM Profiles page (design 7, B5; plan 02 task 8): no panel component, no
    "New profile" create affordance, and no card grid or modal of the profiles module mounted here (plan 03 pins the
    panel where it lives now)."""
    src = _read("components/provider-catalog.jsx")
    for name in ("PC_ProfilesPanel", "New profile", "MP_ProfilesGrid", "MP_ProfileModal"):
        assert name not in src, f"{name} is still in the catalog"


def test_model_profile_is_not_a_provider_type() -> None:
    """The model_profile class chip left PROVIDER_CLASSES with the panel: profiles are browsed and created on the LLM
    Profiles page (design 7, B5), not under a provider class."""
    src = _read("components/provider-catalog.jsx")
    assert 'key: "model_profile"' not in src


def test_no_speech_class_offers_a_profile_affordance() -> None:
    src = _read("components/providers/register-common.jsx")
    for key in ('key: "stt"', 'key: "tts"', 'key: "embedding"', 'key: "cross_encoder"'):
        entry = src[src.index(key): src.index(key) + 200]
        assert "profiles" not in entry, f"{key} must not carry a profiles flag"


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
