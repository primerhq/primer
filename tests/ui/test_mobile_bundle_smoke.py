"""Boot the bundler and verify the served bundle includes every
mobile primitive + the viewport hook."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from contextlib import contextmanager

import httpx
import pytest

# Tail length for the captured boot log included in a failure message -
# enough to show a Python traceback without dumping an unbounded log.
_LOG_TAIL_CHARS = 4000


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:  # noqa: BLE001 -- best-effort diagnostic read
        return f"<could not read boot log: {exc}>"
    text = data.decode("utf-8", errors="replace")
    if len(text) > _LOG_TAIL_CHARS:
        text = "...(truncated)...\n" + text[-_LOG_TAIL_CHARS:]
    return text or "<boot log is empty>"


@contextmanager
def _primer_running():
    env = os.environ.copy()
    env.setdefault("PRIMER_PORT", "8766")
    # The server's own stdout/stderr used to go to DEVNULL, so a genuine
    # boot failure (config error, DB connection failure, missing dep -
    # anything) surfaced to the test only as "connection refused" from the
    # httpx retry loop below, with no indication of WHY. Captured to a
    # file instead so a failure can quote the server's own output.
    with tempfile.NamedTemporaryFile(
        prefix="primer-boot-smoke-", suffix=".log", delete=False,
    ) as log_file:
        log_path = log_file.name
        proc = subprocess.Popen(
            ["uv", "run", "primer", "api", "--no-worker"],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    try:
        # Wait up to 30s for the server to come up.
        deadline = time.time() + 30
        last_err: Exception | None = None
        last_status: int | None = None
        while time.time() < deadline:
            # Fail fast on a crashed process instead of burning the full
            # 30s busy-polling a port nothing will ever answer on.
            exit_code = proc.poll()
            if exit_code is not None:
                raise RuntimeError(
                    f"primer server process exited early (code={exit_code}) "
                    f"before becoming healthy; boot log:\n{_tail(log_path)}"
                )
            try:
                r = httpx.get("http://127.0.0.1:8766/v1/health", timeout=1.0)
                last_status = r.status_code
                if r.status_code == 200:
                    break
            except Exception as e:  # noqa: BLE001
                last_err = e
            time.sleep(0.5)
        else:
            raise RuntimeError(
                f"server failed to come up within 30s "
                f"(last_status={last_status!r}, last_error={last_err!r}); "
                f"boot log:\n{_tail(log_path)}"
            )
        yield "http://127.0.0.1:8766"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            os.unlink(log_path)
        except OSError:
            pass


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
            # Classic mobile suite (ui/components/shared/*.jsx) - still
            # live; provider-catalog pages stay on this suite by design
            # (not mobile-ported to the uiv2 shell below).
            "useViewport",
            "CardList",
            "BottomSheet",
            "MobileTabs",
            "Fab",
            "sheet-overlay",
            # uiv2 mobile shell (ui/components/console/nv-mobile-shell.jsx,
            # NV_Mobile*-prefixed, US-014 through 214be5f1) - the live
            # console mobile experience. "MobileNav" (the old chrome's
            # drawer component, added 8eba64cb, retired f891557c) had no
            # replacement-in-kind here; anchoring on this instead of
            # dropping the uiv2 shell from coverage entirely.
            "NV_MobileChatScreen",
        ):
            assert symbol in body, f"bundle missing {symbol}"
