"""Hosted browser regression: a route callback must finish before context close."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.ui_e2e._route_lifecycle import draining_page_routes


@pytest.mark.ui_e2e
def test_inflight_route_fetch_finishes_before_context_disposal(browser):
    entered, release = threading.Event(), threading.Event()
    finished = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/delayed":
                entered.set()
                released = release.wait(5)
                status, body = (200, b"finished") if released else (503, b"not released")
            elif self.path == "/started":
                status, body = 200, json.dumps(entered.is_set()).encode()
            else:
                status, body = 200, b"<html><body>Route lifecycle</body></html>"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = None
    try:
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{server.server_port}/")
        with draining_page_routes(page):
            def passthrough(route):
                response = route.fetch(timeout=5_000)
                assert response.status == 200
                route.fulfill(response=response)
                finished.append(response.text())

            page.route("**/delayed", passthrough)
            page.evaluate("void fetch('/delayed')")
            # Pump Playwright while an independent HTTP thread confirms the
            # callback is blocked inside the real Route.fetch request.
            page.wait_for_function("async () => (await fetch('/started')).json()", timeout=5_000)
            assert entered.is_set() and finished == []
            release.set()
        assert finished == ["finished"]
    finally:
        release.set()
        try:
            if context is not None:
                context.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    assert not thread.is_alive()
    # The next context's first operation must not inherit a callback exception.
    fresh = browser.new_context()
    try:
        fresh.add_init_script("window.__fresh = true")
        assert fresh.new_page().evaluate("1 + 1") == 2
    finally:
        fresh.close()


@pytest.mark.ui_e2e
def test_failed_assertion_releases_a_deliberately_held_route_before_drain(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto("about:blank")
        with pytest.raises(AssertionError, match="original assertion"):
            with draining_page_routes(page) as pending:
                def hold(route):
                    pending.append(route)
                    page.evaluate("window.__routeHeld = true")

                page.route("https://held.invalid/request", hold)
                page.evaluate("void fetch('https://held.invalid/request').catch(() => {})")
                page.wait_for_function("window.__routeHeld === true", timeout=5_000)
                assert len(pending) == 1
                raise AssertionError("original assertion")
        assert page.evaluate("1 + 1") == 2
    finally:
        context.close()
