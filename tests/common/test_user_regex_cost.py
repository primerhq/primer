"""#859 round 3: a guard against a ``regex`` release that changes what one expansion unit costs to compile.

The expansion cap prices a pattern from the engine's parse tree, in units calibrated against ``regex`` 2026.9.3: a plain
near-cap pattern compiles in about 2 ms and 3 MB, and the costly leaves are weighted to match (``\\X`` 8, a set 1 per
property). If a release made a node much dearer, the cap would no longer bound the compile. These shapes are each at or near
the cap and ACCEPTED by the check; each compiles in a child process with a hard timeout and a memory cap, and must stay
under a fixed time and memory bound (about 5x the measured cost), so such a release fails this test instead of production.
"""

from __future__ import annotations

import pytest

from primer.common.user_regex import MAX_REPEAT_EXPANSION, check_user_pattern
from tests._support.isolated import run_isolated

NEAR_CAP_SHAPES = [
    "(?:a{100}){100}",
    r"(?:\X{100}){12}",
    r"(?i)(?:[\p{L}\p{N}\p{P}\p{S}]{100}){20}",
    "(?i)(?:[^a]{100}){100}",
    "(?:.{100}){100}",
    r"(?:\w{100}){100}",
    "(?>a{100}){100}",
    "(?:a{100}+){100}",
    "(?:(?=a{100})b){99}",
    "(?:a{100}?){100}",
    "(?i)(?:İıſK{25}){350}",
    "(?:" + "abcdefghij" * 10 + "){100}",
]

# The bound per compile: about 5x the measured cost of the dearest accepted shape (2.2 ms, 2.9 MB).
MAX_COMPILE_MS = 50
MAX_COMPILE_MB = 16

_CHILD = """
import json, time, regex
def rss_kb():
    return int([l.split()[1] for l in open("/proc/self/status") if l.startswith("VmRSS")][0])
keep, out = [], {}
for p in SHAPES:
    before = rss_kb()
    started = time.perf_counter()
    keep.append(regex.compile(p, regex.VERSION0, cache_pattern=False))
    out[p] = [round((time.perf_counter() - started) * 1000, 1), round((rss_kb() - before) / 1024, 1)]
print(json.dumps(out))
"""


@pytest.mark.parametrize("pattern", NEAR_CAP_SHAPES)
def test_each_shape_is_accepted_near_the_cap(pattern):
    assert MAX_REPEAT_EXPANSION // 4 <= check_user_pattern(pattern) <= MAX_REPEAT_EXPANSION


def test_near_cap_shapes_compile_within_a_fixed_time_and_memory_bound():
    costs = run_isolated(f"SHAPES = {NEAR_CAP_SHAPES!r}\n" + _CHILD, timeout=60, memory_bytes=3_000_000_000)
    over = {p: c for p, c in costs.items() if c[0] > MAX_COMPILE_MS or c[1] > MAX_COMPILE_MB}
    assert not over, over
