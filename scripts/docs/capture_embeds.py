"""Capture light+dark screenshots of every docs embed, from fixtures only.

This is a BUILD-ONLY harness: there is no live primer backend. We

  1. build the console JSX bundle offline (build_jsx_bundle) into the embed
     harness dir as _app.js,
  2. serve the worktree over a throwaway local http.server,
  3. for each embed id in primer/user_docs/_fixtures/registry.json, load the
     standalone harness page (scripts/docs/embed_harness/index.html) for
     theme=light then theme=dark, wait for data-embed-status="done" (NOT a
     fixed sleep), and screenshot the rendered embed host iframe into
     <out_dir>/_embeds/<id>-<theme>.png.

The build later substitutes ```embed:<id>``` fences with a <picture> that
references these PNGs.

Usage:
    uv run python -m scripts.docs.capture_embeds [out_dir]

Default out_dir is dist/docs. Exit code is non-zero if any embed failed to
reach "done" or produced no PNG; a per-id summary is printed either way.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

# Worktree root = parent of scripts/ = parent of this file's grandparent.
ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = ROOT / "scripts" / "docs" / "embed_harness"
APP_JS = HARNESS_DIR / "_app.js"
REGISTRY = ROOT / "primer" / "user_docs" / "_fixtures" / "registry.json"
HARNESS_URL_PATH = "/scripts/docs/embed_harness/index.html"

THEMES = ("light", "dark")
PORT = 8899
# Per (id, theme) wait budget for data-embed-status="done".
DONE_TIMEOUT_MS = 30000


def _build_bundle() -> None:
    """Build the console JSX bundle offline into the harness dir as _app.js."""
    from primer.api._jsx_bundle import build_jsx_bundle

    print("building _app.js bundle (offline, no server)...", flush=True)
    _etag, body = build_jsx_bundle(ROOT / "ui")
    APP_JS.write_bytes(body)
    print(f"  wrote {APP_JS} ({len(body)} bytes)", flush=True)


def _start_server() -> tuple[socketserver.TCPServer, threading.Thread]:
    """Start a quiet static http.server rooted at the worktree."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def log_message(self, *args):  # silence per-request logging
            pass

    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print(f"static server on http://127.0.0.1:{PORT} (root={ROOT})", flush=True)
    return httpd, thread


async def _capture(out_dir: Path, ids: list[str]) -> dict[str, dict]:
    from playwright.async_api import async_playwright

    embeds_dir = out_dir / "_embeds"
    embeds_dir.mkdir(parents=True, exist_ok=True)

    base = f"http://127.0.0.1:{PORT}{HARNESS_URL_PATH}"
    results: dict[str, dict] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for eid in ids:
            per_theme: dict[str, dict] = {}
            for theme in THEMES:
                page = await browser.new_page(viewport={"width": 1366, "height": 900})
                url = f"{base}?embed={eid}&theme={theme}"
                info: dict = {"ok": False}
                try:
                    await page.goto(url, wait_until="load", timeout=DONE_TIMEOUT_MS)
                    # Wait deterministically for the harness completion signal.
                    await page.wait_for_selector(
                        'html[data-embed-status="done"]',
                        timeout=DONE_TIMEOUT_MS,
                        state="attached",
                    )
                    res = await page.evaluate("() => window.__HARNESS_RESULT || null")
                    info["harness"] = res
                    if res and res.get("error"):
                        raise RuntimeError(res["error"])
                    # Screenshot the host iframe element (contains the rendered embed).
                    el = await page.query_selector("#host")
                    if el is None:
                        raise RuntimeError("#host iframe not found")
                    out_png = embeds_dir / f"{eid}-{theme}.png"
                    await el.screenshot(path=str(out_png))
                    info["ok"] = out_png.exists() and out_png.stat().st_size > 0
                    info["png"] = str(out_png)
                except Exception as e:  # noqa: BLE001 - record and continue
                    # If we never reached "done", capture the current status for the report.
                    try:
                        status = await page.evaluate(
                            "() => document.documentElement.getAttribute('data-embed-status')"
                        )
                    except Exception:  # noqa: BLE001
                        status = None
                    info["error"] = f"{type(e).__name__}: {e}"
                    info["status_attr"] = status
                finally:
                    await page.close()
                per_theme[theme] = info
            results[eid] = per_theme
        await browser.close()
    return results


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (ROOT / "dist" / "docs")
    out_dir = out_dir if out_dir.is_absolute() else (ROOT / out_dir)

    ids = json.loads(REGISTRY.read_text())["embeds"]
    print(f"{len(ids)} embed ids; {len(THEMES)} themes -> {len(ids) * len(THEMES)} PNGs expected", flush=True)

    _build_bundle()
    httpd, _thread = _start_server()
    try:
        results = asyncio.run(_capture(out_dir, ids))
    finally:
        httpd.shutdown()
        httpd.server_close()
        print("static server stopped", flush=True)

    # Per-id summary + failure accounting.
    ok = 0
    failures: list[str] = []
    print("\n=== per-id summary ===", flush=True)
    for eid in ids:
        per = results.get(eid, {})
        cells = []
        for theme in THEMES:
            info = per.get(theme, {})
            if info.get("ok"):
                ok += 1
                cells.append(f"{theme}=OK")
            else:
                cells.append(f"{theme}=FAIL({info.get('error') or info.get('status_attr')})")
                failures.append(f"{eid}/{theme}: {info.get('error') or info.get('status_attr')}")
        print(f"  {eid:28s} {'  '.join(cells)}", flush=True)

    expected = len(ids) * len(THEMES)
    print(f"\n{ok}/{expected} PNGs captured -> {out_dir / '_embeds'}", flush=True)
    if failures:
        print(f"\n{len(failures)} FAILURE(S):", flush=True)
        for f in failures:
            print(f"  - {f}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
