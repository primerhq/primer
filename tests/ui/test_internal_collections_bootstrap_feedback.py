"""internal-collections.jsx's bootstrap mutation must report the REAL
terminal outcome, not an interim "started" state the backend no longer
has.

01a08c05: POST /internal_collections/bootstrap (primer/api/routers/
internal_collections.py) is fully synchronous - its handler `await`s
enable_search(...) inline, no background task, and returns the real
terminal {status, state, error} as its response body. But both
ConfiguredCard's and ActiveCard's `bootstrap` mutations had an
`onSuccess: () => {...}` that never read the `data` argument it was
handed, pushing a fixed "Bootstrap started - running in the background,
leave this page if you like" toast instead - actively false once the
route stopped being asynchronous, and describing a process model the
backend doesn't have. Success/failure feedback instead depended on a
poll-observed `prevStatusRef.current === "running" && curr ===
"succeeded"` transition, which synchronicity makes very unlikely to
ever fire for a bootstrap the client itself triggered (the response
already carries the answer before any poll of the status row can
observe an interim "running" tick).

Static-source checks, matching the rest of this file's suite
(test_internal_collections_mobile.py) - no jsdom in this toolchain.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "ui" / "components" / "internal-collections.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _fn_body(name: str) -> str:
    src = _src()
    start = src.index(f"function {name}(")
    # Matches this file's own function boundaries: top-level functions are
    # separated by a blank line then the next `function `/`// ====` marker.
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


def test_stale_started_in_background_copy_is_gone() -> None:
    src = _src()
    assert "Bootstrap started" not in src
    assert "Running in the background" not in src


def test_configured_card_on_success_reads_the_response_body() -> None:
    body = _fn_body("ConfiguredCard")
    on_success_start = body.index("onSuccess: (data) => {")
    on_success_end = body.index("},\n      onError:", on_success_start)
    on_success = body[on_success_start:on_success_end]
    assert 'data?.status === "succeeded"' in on_success
    assert "Bootstrap complete" in on_success
    assert "onRefresh()" in on_success, (
        "onSuccess must call onRefresh() itself so ConfiguredCard flips to "
        "ActiveCard immediately, not after the page-level 30s poll or the "
        "poll-transition effect (whose own 'running' tick this client is "
        "unlikely to ever observe for its own synchronous request)"
    )
    assert "Bootstrap failed" in on_success
    assert "data?.error" in on_success


def test_active_card_on_success_reads_the_response_body() -> None:
    body = _fn_body("ActiveCard")
    on_success_start = body.index("onSuccess: (data) => {")
    on_success_end = body.index("},\n      onError:", on_success_start)
    on_success = body[on_success_start:on_success_end]
    assert 'data?.status === "succeeded"' in on_success
    assert "Re-bootstrap complete" in on_success
    assert "onRefresh()" in on_success
    assert "Re-bootstrap failed" in on_success
    assert "data?.error" in on_success


def test_poll_transition_fallback_still_present_for_cross_client_sync() -> None:
    """The poll-observed transition effect must NOT be deleted outright -
    it is still the only way a client learns about a bootstrap started
    from elsewhere (a different tab, or the 409-already-running case),
    which never calls this client's own onSuccess."""
    src = _src()
    assert src.count('prev === "running" && curr === "succeeded"') == 2
    assert src.count('prev === "running" && curr === "failed"') == 2


def test_409_already_running_still_defers_to_the_poll_fallback() -> None:
    """The 409 branch genuinely has no terminal outcome to report from
    ITS OWN request (the run belongs to whoever else triggered it) -
    unlike the happy path, this one is correctly left depending on the
    poll-transition effect, not the response body."""
    for name in ("ConfiguredCard", "ActiveCard"):
        body = _fn_body(name)
        on_error_start = body.index("onError: (err) => {")
        on_error_end = body.index("},\n    }\n  );", on_error_start)
        on_error = body[on_error_start:on_error_end]
        assert "err?.status === 409" in on_error
        assert "bootstrapStatus.refetch();" in on_error
