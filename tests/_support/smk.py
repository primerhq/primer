"""Tag a test with the docs/tests SMK ids it implements."""
from __future__ import annotations

import pytest


def smk(*ids: str, status: str = "full"):
    if status not in ("full", "partial"):
        raise ValueError(f"status must be full|partial, got {status!r}")
    return pytest.mark.smk(list(ids), status=status)
