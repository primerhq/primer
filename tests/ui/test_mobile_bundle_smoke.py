"""Boot the bundler and verify the served bundle includes every
mobile primitive + the viewport hook."""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager

import httpx
import pytest


@contextmanager
def _primer_running():
    env = os.environ.copy()
    env.setdefault("PRIMER_BIND_PORT", "8766")
    proc = subprocess.Popen(
        ["uv", "run", "primer", "api", "--no-worker"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        # Wait up to 30s for the server to come up.
        deadline = time.time() + 30
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                r = httpx.get("http://127.0.0.1:8766/healthz", timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.5)
        else:
            raise RuntimeError(f"server failed to come up: {last_err}")
        yield "http://127.0.0.1:8766"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.skipif(
    os.environ.get("PRIMER_RUN_BOOT_SMOKE") != "1",
    reason="set PRIMER_RUN_BOOT_SMOKE=1 to run the bundle smoke test",
)
def test_bundle_contains_mobile_primitives() -> None:
    with _primer_running() as base:
        r = httpx.get(f"{base}/console/_app.js", timeout=15)
        assert r.status_code == 200
        body = r.text
        for symbol in (
            "useViewport",
            "CardList",
            "BottomSheet",
            "MobileTabs",
            "Fab",
            "MobileNav",
            "sheet-overlay",
        ):
            assert symbol in body, f"bundle missing {symbol}"
