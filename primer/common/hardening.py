"""Process-level hardening for the API process."""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

_PR_SET_DUMPABLE = 4


def make_non_dumpable() -> bool:
    """Mark this process non-dumpable (``prctl(PR_SET_DUMPABLE, 0)``): its ``/proc/<pid>`` entries become root-owned, so a same-uid child
    (a python tool) cannot read ``/proc/<ppid>/environ`` for the database DSN and provider keys. Linux only; returns False elsewhere or on failure."""
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except OSError:
        logger.warning("could not mark the process non-dumpable", exc_info=True)
        return False
