"""The shared tool alias rule: length-limited, with an optional sanitizing mode."""

from __future__ import annotations

import hashlib

from primer.llm.agent_runtime.alias import tool_alias


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


def test_tool_alias_limits():
    # claude_code's limits: 56/47, character content irrelevant.
    short = "system__list_agents"
    assert tool_alias(short, max_len=56, keep_len=47) == short
    long_cc = "x" * 50 + "__one_tool"
    a = tool_alias(long_cc, max_len=56, keep_len=47)
    assert len(a) == 56 and a.startswith("x" * 47 + "_") and a.endswith(_sha(long_cc))
    assert tool_alias("fs/read file", max_len=56, keep_len=47) == "fs/read file"

    # codex's limits: 64/55.
    short_cx = "y" * 64
    assert tool_alias(short_cx, max_len=64, keep_len=55) == short_cx
    long_cx = "y" * 70
    b = tool_alias(long_cx, max_len=64, keep_len=55)
    assert len(b) == 64 and b == "y" * 55 + "_" + _sha(long_cx)

    # sanitizing mode (codex): pass-through only when short AND [A-Za-z0-9_-]+.
    safe = "a_tool-1"
    assert tool_alias(safe, max_len=64, keep_len=55, sanitize=True) == safe
    assert tool_alias(safe, max_len=56, keep_len=47, sanitize=True) == safe
    unsafe = "fs/read file"
    c = tool_alias(unsafe, max_len=64, keep_len=55, sanitize=True)
    assert c == "fs_read_file" + "_" + _sha(unsafe)
    # a long id: sanitize the FIRST 55 characters, then the hash of the FULL id.
    long_unsafe = "y" * 53 + "z/ q" + "w" * 11
    d = tool_alias(long_unsafe, max_len=64, keep_len=55, sanitize=True)
    assert len(d) == 64 and d == "y" * 53 + "z_" + "_" + _sha(long_unsafe)
