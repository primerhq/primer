"""E2E test: worker drain leaves the API responding cleanly throughout the polling window.

Backlog item (reframed):

* T0251 — Original premise ("Worker drain settles to terminal state
  without 5xx during polling") was partially wrong: the HTTP
  ``POST /v1/workers/{id}/drain`` endpoint only flips the scheduler
  row's ``status`` to ``draining``; the worker process keeps serving
  the API. The actual settle-to-dead happens during process shutdown
  (primer/worker/pool.py:drain_and_stop) — NOT during the lifetime
  of the drained API process.

  Reframed contract: after POST /drain, the worker stays in
  ``draining`` status indefinitely (until the process shuts down).
  During a polling window of ~15 s, EVERY call to
  ``GET /v1/workers`` and ``GET /v1/health`` must return a clean
  2xx envelope — no 500 leaks, no /errors/internal, no
  connection-reset under the drain transition. The worker row
  stays present + ``status="draining"`` throughout.

  This pins the defensive contract: operators poll for drain state
  changes; the API surface must remain stable during that window
  even though the drained worker is no longer claiming new work.

  Must run alone (no parallel session-creating tests in the same
  iteration) — draining the only worker would stall any concurrent
  session that needed a worker.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0251_drain_polling_window_stays_clean(
    client: httpx.AsyncClient,
) -> None:
    """T0251 (reframed) — Drain the live worker; then for ~15 s,
    poll ``/v1/workers`` + ``/v1/health`` at ~0.5 s cadence and
    assert every response is a clean 2xx envelope with the worker
    visible in status=draining.

    Defends:
    * No 5xx leaks under the drain transition.
    * No /errors/internal envelopes on either endpoint.
    * The worker row is observable + identifies as ``draining``
      throughout (not dropped from the list, not flipped to a
      different status, not deregistered while the process is up).
    """
    # 1. Locate the live worker (--run-worker mode → exactly 1).
    r = await client.get("/v1/workers")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, f"expected ≥ 1 registered worker; got {r.json()!r}"
    worker_id = items[0]["id"]
    pre_drain_status = items[0]["status"]
    assert pre_drain_status in ("active", "draining", "dead"), (
        f"unexpected pre-drain status {pre_drain_status!r}"
    )

    # 2. Drain — must return 204.
    r = await client.post(f"/v1/workers/{worker_id}/drain")
    assert r.status_code == 204, r.text

    # 3. Polling window — ~15 s, ~0.5 s cadence (30 polls).
    deadline = time.monotonic() + 15.0
    workers_calls = 0
    health_calls = 0
    drained_seen = False
    bad_responses: list[dict] = []
    statuses_seen: set[str] = set()

    while time.monotonic() < deadline:
        # Issue both calls in parallel to amplify churn.
        gr_workers, gr_health = await asyncio.gather(
            client.get("/v1/workers"),
            client.get("/v1/health"),
            return_exceptions=True,
        )
        for label, response in (
            ("workers", gr_workers),
            ("health", gr_health),
        ):
            if isinstance(response, BaseException):
                bad_responses.append({
                    "endpoint": label,
                    "kind": "exception",
                    "exc": repr(response),
                })
                continue
            if label == "workers":
                workers_calls += 1
            else:
                health_calls += 1
            # Must be 2xx.
            if response.status_code >= 400:
                bad_responses.append({
                    "endpoint": label,
                    "kind": "http",
                    "status": response.status_code,
                    "body": response.text[:300],
                })
                continue
            # And the body must not carry an /errors/internal envelope
            # (defensive — should never happen on 2xx).
            try:
                body = response.json()
            except ValueError:
                bad_responses.append({
                    "endpoint": label,
                    "kind": "non-json",
                    "body": response.text[:300],
                })
                continue
            err_type = str(body.get("type", "")) if isinstance(body, dict) else ""
            if "internal" in err_type:
                bad_responses.append({
                    "endpoint": label,
                    "kind": "internal-type-on-2xx",
                    "type": err_type,
                })
                continue

            # /v1/workers shape probe.
            if label == "workers":
                items = body.get("items") if isinstance(body, dict) else None
                if not isinstance(items, list):
                    bad_responses.append({
                        "endpoint": label,
                        "kind": "bad-shape",
                        "body": str(body)[:200],
                    })
                    continue
                # Our drained worker must be present + tagged draining.
                row = next(
                    (w for w in items if w.get("id") == worker_id), None,
                )
                if row is None:
                    bad_responses.append({
                        "endpoint": label,
                        "kind": "worker-missing",
                        "items": [w.get("id") for w in items],
                    })
                    continue
                statuses_seen.add(row.get("status", ""))
                if row.get("status") == "draining":
                    drained_seen = True

        await asyncio.sleep(0.5)

    # ---- Assertions ----
    assert not bad_responses, (
        f"polling window produced {len(bad_responses)} bad responses:\n"
        + "\n".join(f"  {b!r}" for b in bad_responses[:5])
    )
    # Sanity: we actually polled (drain didn't make the API freeze).
    assert workers_calls >= 10, (
        f"only {workers_calls} successful /v1/workers calls in 15s"
    )
    assert health_calls >= 10, (
        f"only {health_calls} successful /v1/health calls in 15s"
    )
    # The worker must have been visibly drained at least once during
    # the window (the very first poll might race with the drain
    # write, but most polls should see status=draining).
    assert drained_seen, (
        f"never observed status=draining after POST /drain; "
        f"statuses_seen={statuses_seen!r}"
    )
    # No other statuses than {draining, active} should appear
    # (active may briefly appear if the scheduler row hadn't
    # propagated; dead would mean the process died which violates
    # this contract).
    assert "dead" not in statuses_seen, (
        f"worker flipped to dead during the polling window; "
        f"statuses_seen={statuses_seen!r} — drain_and_stop should "
        "NOT run for an HTTP drain"
    )
