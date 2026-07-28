"""PythonConfig: the toolset record that carries a python module's source."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.providers.toolset import PythonConfig, Toolset, ToolsetProviderType


def test_python_is_a_provider_type() -> None:
    assert ToolsetProviderType.PYTHON.value == "python"


def test_defaults_are_the_spec_values() -> None:
    cfg = PythonConfig(source="def f(): pass", source_version=1)
    assert cfg.default_timeout_seconds == 30.0
    assert cfg.allow_network is False
    assert cfg.image is None
    assert cfg.env == {}


def test_timeout_ceiling_is_enforced_at_the_model() -> None:
    # 300s is the server ceiling; a toolset must not be able to declare more.
    with pytest.raises(ValidationError):
        PythonConfig(source="x", source_version=1, default_timeout_seconds=301.0)


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        PythonConfig(source="x", source_version=1, default_timeout_seconds=0.0)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PythonConfig(source="x", source_version=1, sandbox="none")


def test_python_provider_requires_a_config() -> None:
    with pytest.raises(ValidationError):
        Toolset(id="toolset-a", provider=ToolsetProviderType.PYTHON, config=None)


def test_python_provider_accepts_a_python_config() -> None:
    ts = Toolset(
        id="toolset-a",
        provider=ToolsetProviderType.PYTHON,
        config=PythonConfig(source="def f(): pass", source_version=1),
    )
    assert ts.config.source_version == 1


def test_internal_provider_still_rejects_a_config() -> None:
    with pytest.raises(ValidationError):
        Toolset(
            id="toolset-b",
            provider=ToolsetProviderType.INTERNAL,
            config=PythonConfig(source="x", source_version=1),
        )
