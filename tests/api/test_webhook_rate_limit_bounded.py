"""The webhook rate limiter keeps a bounded number of keys whatever tokens a caller invents (W45)."""

from __future__ import annotations

import copy
import time

from primer.api.routers import webhooks


def _fresh_windows(monkeypatch) -> None:
    """An empty limiter map of the module's own type for this test only."""
    windows = copy.copy(webhooks._rate_windows)
    windows.clear()
    monkeypatch.setattr(webhooks, "_rate_windows", windows)


def test_rate_limit_is_sixty_a_minute_per_token(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(webhooks.time, "monotonic", lambda: clock[0])
    _fresh_windows(monkeypatch)
    assert all(webhooks._check_rate_limit("tok-a") for _ in range(60))
    assert webhooks._check_rate_limit("tok-a") is False
    assert webhooks._check_rate_limit("tok-b") is True  # another token has its own window
    clock[0] += 61
    assert webhooks._check_rate_limit("tok-a") is True  # the window slid past the old requests


def test_rate_windows_survive_a_test_reset():
    """Other tests seed ``_rate_windows[token]`` directly; the limiter must keep honouring a seeded list."""
    token = "seeded-" + str(time.monotonic())
    webhooks._rate_windows[token] = [time.monotonic()] * 60
    try:
        assert webhooks._check_rate_limit(token) is False
    finally:
        webhooks._rate_windows.pop(token, None)


# --------------------------------------------------------------------------- W45


def test_W45_the_limiter_map_stays_bounded_under_bogus_tokens(monkeypatch):
    _fresh_windows(monkeypatch)
    monkeypatch.setattr(webhooks, "_RATE_LIMIT_MAX_KEYS", 100)
    for i in range(5000):
        webhooks._check_rate_limit(f"bogus-{i}")
    assert len(webhooks._rate_windows) <= 100


def test_W45_a_recently_used_token_keeps_its_window(monkeypatch):
    """Eviction drops the least recently used token, so a busy real token keeps its count."""
    _fresh_windows(monkeypatch)
    monkeypatch.setattr(webhooks, "_RATE_LIMIT_MAX_KEYS", 10)
    assert all(webhooks._check_rate_limit("real") for _ in range(60))
    for i in range(9):
        webhooks._check_rate_limit(f"bogus-{i}")
    assert webhooks._check_rate_limit("real") is False  # touched last before the flood: still limited
    for i in range(9, 30):
        webhooks._check_rate_limit(f"bogus-{i}")
    assert "real" not in webhooks._rate_windows
