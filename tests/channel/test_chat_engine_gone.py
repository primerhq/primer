"""S6 P5 / crosscheck C4: the carved-out chat engine is gone.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 8. S1 P7 kept the
headless engine alive only so channels kept working until the
thread-mapped replacement (P3) landed. P3 has landed, so it goes here.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("module", [
    "primer.chat",
    "primer.chat.dispatch",
    "primer.chat.executor",
    "primer.chat.enqueue",
    "primer.chat.pending",
    "primer.chat.tick_router",
    "primer.chat.usage_cache",
    "primer.model.chats",
])
def test_engine_modules_are_unimportable(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_the_engine_files_are_gone():
    assert not (REPO / "primer" / "chat").exists()
    assert not (REPO / "primer" / "model" / "chats.py").exists()


def test_the_external_row_helper_survived_the_move():
    """The audit-row flip is not chat plumbing: yields.py still calls it."""
    from primer.session.external_calls import flip_external_row

    assert callable(flip_external_row)


def test_the_sweeper_and_the_chat_storage_dep_are_gone():
    from primer.api import deps
    from primer.bus import scheduler_tasks

    assert not hasattr(scheduler_tasks, "ChatSweeper")
    assert not hasattr(deps, "get_chat_storage")
