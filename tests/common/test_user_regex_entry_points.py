"""Round 2 of #859 (B1): every real entry point refuses the review's bypass patterns before the engine compiles them.

Each pattern is cheap to compile under stdlib ``re`` (0.2 ms) but the ``regex`` engine unrolls its counted repeats: with
round 1's scanner fooled by a comment group, verbose whitespace or an inline flag, ``regex.compile`` ran for minutes holding
the GIL, or raised ``MemoryError``. Every check below runs in a child process with a hard timeout and a memory cap, so a
regression fails the test instead of hanging the lane.
"""

from __future__ import annotations

import pytest

from tests._support.isolated import run_isolated

BYPASSES = [
    "(?#[)(?:a{65535}){65535}]",
    "(?:(?:a{300})(?#){300})(?#){300}",
    "(?x)(?:(?:a{300}) {300}) {300}",
    "(?:(?:a{300})(?i){300})(?i){300}",
    "(?x)#[\n(?:a{1000}){1000}]",
    "(?x)(?:a{10 00}){10 00}",
    "(?i:[)(?:a{65535}){65535}]",
    "(?:a{65535}){65535}",
    "(?:a{1000}){1000}",
]

# Each snippet defines ``attempt(pattern)``, which raises when the entry point refuses the pattern.
ENTRY_POINTS = {
    "compile_user_regex one-shot": """
from primer.common.user_regex import compile_user_regex
def attempt(p): compile_user_regex(p)
""",
    "compile_user_regex stored": """
from primer.common.user_regex import compile_user_regex
def attempt(p): compile_user_regex(p, stored=True)
""",
    "validate_text_pattern": """
from primer.model.event_matcher import validate_text_pattern
def attempt(p): validate_text_pattern(p)
""",
    "workspace grep": """
from primer.workspace.local.tools.grep import GrepArgs, compile_grep_pattern
def attempt(p): compile_grep_pattern(GrepArgs(pattern=p))
""",
    "collection grep": """
import asyncio
from primer.knowledge.grep import grep_collection
class _Store:
    async def list(self, collection_id, prefix=None): return []
    async def get(self, document_id): return ""
def attempt(p): asyncio.run(grep_collection(_Store(), collection_id="c", pattern=p))
""",
    "event filter field regex": """
from primer.events.filters import validate_filter
from primer.model.event import EventFilter, FieldMatcher
def attempt(p): validate_filter(EventFilter(fields=[FieldMatcher(path="payload.x", op="regex", value=p)]))
""",
    "event filter field regex at match time": """
from datetime import datetime, timezone
from primer.events.filters import matches
from primer.model.event import Event, EventFilter, FieldMatcher
def attempt(p):
    event = Event(id=1, event_type="x", occurred_at=datetime.now(timezone.utc), actor="system", payload={"x": "a"})
    if not matches(event, EventFilter(fields=[FieldMatcher(path="payload.x", op="regex", value=p)])):
        raise ValueError("no match (refused)")
""",
    "graph output_schema pattern": """
from primer.common.bounded_jsonschema import validate_bounded
def attempt(p): validate_bounded("a", {"type": "string", "pattern": p})
""",
}

_RUNNER = """
import json, time
verdicts = {}
for p in PATTERNS:
    started = time.perf_counter()
    try:
        attempt(p)
        verdict = "ACCEPTED"
    except Exception as exc:  # noqa: BLE001
        verdict = "refused: " + type(exc).__name__ + ": " + str(exc)[:120]
    verdicts[p] = [verdict, round(time.perf_counter() - started, 3)]
print(json.dumps(verdicts))
"""


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_every_entry_point_refuses_the_bypass_patterns_before_compiling(entry):
    snippet = ENTRY_POINTS[entry] + f"PATTERNS = {BYPASSES!r}\n" + _RUNNER
    verdicts = run_isolated(snippet, timeout=60)
    accepted = {p: v for p, v in verdicts.items() if v[0] == "ACCEPTED"}
    assert not accepted, accepted
    slow = {p: v for p, v in verdicts.items() if v[1] > 1.0}
    assert not slow, slow
