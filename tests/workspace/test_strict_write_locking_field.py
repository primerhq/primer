"""Unit tests for ``WorkspaceTemplate.strict_write_locking``."""

from __future__ import annotations

from primer.model.workspace import WorkspaceTemplate


def test_strict_write_locking_defaults_false():
    # Direct construction (no `backend=`) is valid: WorkspaceTemplate.backend
    # has default_factory=lambda: LocalTemplateConfig() (model:454), so this
    # does NOT require model_validate or an explicit backend. `description`
    # is required by the `Describeable` mixin (no default), so it is passed
    # explicitly here -- matching the construction style used by every other
    # WorkspaceTemplate test fixture in this repo (e.g. tests/workspace/k8s/
    # test_k8s_manifest.py's `_template()` helper).
    t = WorkspaceTemplate(id="x", provider_id="p", description="")
    assert t.strict_write_locking is False


def test_strict_write_locking_roundtrips():
    t = WorkspaceTemplate(
        id="x", provider_id="p", description="", strict_write_locking=True,
    )
    assert WorkspaceTemplate.model_validate(t.model_dump()).strict_write_locking is True


def test_legacy_template_without_field_loads():
    # A pre-change stored row (no key) must still parse.
    t = WorkspaceTemplate.model_validate(
        {"id": "x", "provider_id": "p", "description": "", "backend": {"kind": "local"}}
    )
    assert t.strict_write_locking is False
