"""Pin that each known background task declares the expected role."""

from __future__ import annotations

from primer.bus.mcp_tasks import McpTaskBridge
from primer.bus.scheduler_tasks import (
    HarnessSweeper,
    StuckSessionSweeper,
    TimeoutSweeper,
    TimerScheduler,
)
from primer.bus.watcher import WatcherManager
from primer.int.coordinator import (
    ROLE_HARNESS_SWEEPER,
    ROLE_MCP_BRIDGE,
    ROLE_STUCK_SESSION_SWEEPER,
    ROLE_TIMEOUT_SWEEPER,
    ROLE_TIMER_SCHEDULER,
    ROLE_WATCHER_MANAGER,
)


def test_each_background_task_declares_role():
    assert TimerScheduler.role == ROLE_TIMER_SCHEDULER
    assert TimeoutSweeper.role == ROLE_TIMEOUT_SWEEPER
    assert StuckSessionSweeper.role == ROLE_STUCK_SESSION_SWEEPER
    assert HarnessSweeper.role == ROLE_HARNESS_SWEEPER
    assert WatcherManager.role == ROLE_WATCHER_MANAGER
    assert McpTaskBridge.role == ROLE_MCP_BRIDGE
