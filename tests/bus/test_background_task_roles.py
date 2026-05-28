"""Pin that each known background task declares the expected role."""

from __future__ import annotations

import pytest

from primer.bus.scheduler_tasks import ChatSweeper, HarnessSweeper, TimeoutSweeper, TimerScheduler
from primer.bus.watcher import WatcherManager
from primer.bus.mcp_tasks import McpTaskBridge
from primer.int.coordinator import (
    ROLE_CHAT_SWEEPER, ROLE_HARNESS_SWEEPER, ROLE_MCP_BRIDGE,
    ROLE_TIMEOUT_SWEEPER, ROLE_TIMER_SCHEDULER, ROLE_WATCHER_MANAGER,
)


def test_each_background_task_declares_role():
    assert TimerScheduler.role == ROLE_TIMER_SCHEDULER
    assert TimeoutSweeper.role == ROLE_TIMEOUT_SWEEPER
    assert ChatSweeper.role == ROLE_CHAT_SWEEPER
    assert HarnessSweeper.role == ROLE_HARNESS_SWEEPER
    assert WatcherManager.role == ROLE_WATCHER_MANAGER
    assert McpTaskBridge.role == ROLE_MCP_BRIDGE
