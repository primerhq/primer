"""Comparing two off-golden fixtures: every differing path, and WHICH TURNS changed.

The fixture is a list of turns and a flat list of every LLM call. A turn "unit" is the turn's record plus the
calls it made, with the calls numbered from 0 inside the turn, so a unit depends only on that turn's own
setup and the code: a turn that makes one more call than before does not renumber, and so does not change, the
turns after it. The scenario gives every turn its own session for the same reason (``off_golden.py``).

``scripts/capture_off_golden.py`` uses :func:`changed_turns` to refuse a re-capture that changes a turn the
author did not declare, and ``tests/agent/test_off_mode_golden.py`` uses :func:`differences` to list ALL
differing paths, not only the first.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Keys that describe the capture, not the behaviour: never compared.
METADATA_KEYS = ("captured_from", "captured_from_subject", "recaptures")


def differences(got: Any, want: Any, path: str = "") -> list[str]:
    """Every path at which ``got`` and ``want`` differ, in a stable order."""
    out: list[str] = []
    _walk(got, want, path, out)
    return out


def _walk(got: Any, want: Any, path: str, out: list[str]) -> None:
    if type(got) is not type(want):
        out.append(f"{path or '/'}: type {type(got).__name__} != {type(want).__name__}")
    elif isinstance(got, dict):
        for key in sorted(set(got) | set(want)):
            if key not in got or key not in want:
                out.append(f"{path}/{key}: present in only one of them")
            else:
                _walk(got[key], want[key], f"{path}/{key}", out)
    elif isinstance(got, list):
        if len(got) != len(want):
            out.append(f"{path}: {len(got)} items != {len(want)}")
        for i, (a, b) in enumerate(zip(got, want)):
            _walk(a, b, f"{path}[{i}]", out)
    elif got != want:
        out.append(f"{path}: {got!r} != {want!r}")


def behaviour(fixture: dict[str, Any]) -> dict[str, Any]:
    """The fixture without its capture metadata."""
    return {k: v for k, v in fixture.items() if k not in METADATA_KEYS}


def turn_units(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """One unit per turn: the turn's record and its own calls, numbered from 0 within the turn."""
    units: list[dict[str, Any]] = []
    start = 0
    for turn in fixture["turns"]:
        count = turn["llm_calls"]
        calls = [{**call, "call": k} for k, call in enumerate(fixture["calls"][start:start + count])]
        units.append({"turn": turn, "calls": calls})
        start += count
    return units


def changed_turns(new: dict[str, Any], old: dict[str, Any]) -> dict[int, list[str]]:
    """The 1-based turns whose unit differs between ``new`` and ``old``, each with its differing paths.

    A different NUMBER of turns (the scenario changed shape) is reported under turn 0 AND turn by turn as
    well: the turns both fixtures have are compared, and a turn only ``new`` has is reported as new. So
    declaring turn 0 alone does not excuse a change to any other turn: each one still has to be declared.
    """
    new_units, old_units = turn_units(new), turn_units(old)
    out: dict[int, list[str]] = {}
    if len(new_units) != len(old_units):
        out[0] = [f"turns: {len(new_units)} != {len(old_units)}"]
    for i, (a, b) in enumerate(zip(new_units, old_units), start=1):
        found = differences(a, b)
        if found:
            out[i] = found
    for i in range(len(old_units) + 1, len(new_units) + 1):
        out[i] = [f"turn {i} is new"]
    return out


def unit_sha256(fixture: dict[str, Any], turn: int) -> str:
    """A digest of one turn's unit (canonical JSON), for a constant a test file can pin."""
    unit = turn_units(fixture)[turn - 1]
    return hashlib.sha256(json.dumps(unit, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def call_sha256(fixture: dict[str, Any], index: int) -> str:
    """A digest of one call (canonical JSON), by its index in the fixture's flat list."""
    call = fixture["calls"][index]
    return hashlib.sha256(json.dumps(call, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
