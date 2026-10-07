"""The harness tests clone local bare repos over ``file://``, which git.py refuses unless the operator opts in.

The opt-in (``PRIMER_HARNESS_ALLOW_FILE_URLS=1``) is set for every test in this directory; a test that pins the default unsets it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _allow_file_git_urls(monkeypatch):
    monkeypatch.setenv("PRIMER_HARNESS_ALLOW_FILE_URLS", "1")
