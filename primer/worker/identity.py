"""Stable, bounded worker identity for metric labels.

``WorkerPool._worker_id`` is a per-start uuid (primer/worker/pool.py) and
MUST stay unique per process: it is the lease-ownership key the claim
engine and the scheduler registry write into rows, so two live pools on
one host sharing an id would let each heartbeat the other's leases.

A metric label needs the opposite property: stability across restarts and
bounded cardinality. This module derives that second identity from the
host name plus a configured index, so a restarted worker keeps its series
instead of orphaning one per process start (12-s7-design.md section 9).
"""

from __future__ import annotations

import re
import socket

from primer.model.scheduler import WorkerConfig

_UNSAFE = re.compile(r"[^a-z0-9_-]+")

# Long enough for a k8s pod name, short enough to keep the label readable.
_MAX_LABEL_LEN = 63


def _sanitise(value: str) -> str:
    cleaned = _UNSAFE.sub("-", value.strip().lower()).strip("-")
    return cleaned[:_MAX_LABEL_LEN] or "unknown"


def stable_worker_label(config: WorkerConfig) -> str:
    """Return the bounded metric label for this process's worker pool."""
    if config.worker_label:
        return _sanitise(config.worker_label)
    return f"{_sanitise(socket.gethostname())}-{config.worker_index}"


__all__ = ["stable_worker_label"]
