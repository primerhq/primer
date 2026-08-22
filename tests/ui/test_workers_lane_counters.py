"""S7 section 6: per-lane task/latency counters on the workers page."""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "workers.jsx"


def test_reads_the_stats_endpoint() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert '"/workers/stats"' in src
    assert '"workers:stats"' in src


def test_polls_like_the_worker_list() -> None:
    src = SRC.read_text(encoding="utf-8")
    idx = src.index('"workers:stats"')
    assert "pollMs" in src[idx : idx + 400]


def test_renders_a_lane_counter_strip() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert 'data-testid="worker-lane-counters"' in src


def test_shows_tasks_and_mean_latency() -> None:
    src = SRC.read_text(encoding="utf-8")
    idx = src.index('data-testid="worker-lane-counters"')
    frag = src[idx : idx + 1200]
    assert "tasks" in frag
    assert "duration_sum_seconds" in frag
    assert "duration_count" in frag
