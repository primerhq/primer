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



# RETIRED (Plan 03, Task 6): test_profiles_can_be_deleted_from_the_panel, test_harness_managed_rows_hide_mutation,
# test_a_delete_conflict_renders_inline_not_as_a_toast and test_profile_delete_is_gated_behind_a_confirm pinned the source of MP_ProfileCard, which
# nothing has mounted since the panel left the catalog (#804); the card is deleted. On the LLM Profiles page a profile is deleted from its card's
# menu (tests/ui/test_platform_list_page.py::test_every_deletable_entry_gets_one_danger_item_in_its_menu), behind NV_deleteRow's confirm, and a
# refusal (409 in_use_by, say) is a toast in the words of window.primerApi.readRefusal (tests/ui/test_platform_delete_confirm.py), as on every
# Platform page; a harness-managed profile has no Delete and says who manages it
# (tests/ui/test_llm_profiles_page.py::test_a_managed_profile_has_no_delete_and_says_who_manages_it).
