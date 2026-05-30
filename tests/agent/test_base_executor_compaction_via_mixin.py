"""Confirms _BaseAgentExecutor imports compaction_mixin's should_compact."""

from __future__ import annotations

import pytest


def test_base_module_exposes_mixin_should_compact() -> None:
    from primer.agent import base as base_mod
    assert hasattr(base_mod, "_mixin_should_compact")
