"""Unit tests for `_strip_json_fences` -- the gate-parser fence tolerance.

A local model often wraps a response_format verdict in ```json fences even
when a JSON schema is requested; without stripping them the node's `parsed`
is lost and json_path gates silently fall through (e.g. a loop that never
converges). These tests pin the common fence shapes plus the raw-JSON
passthrough.
"""
import json

from primer.graph._agent_node import _strip_json_fences


def test_raw_json_unchanged() -> None:
    raw = '{"sufficient": true, "gaps": []}'
    assert json.loads(_strip_json_fences(raw)) == {"sufficient": True, "gaps": []}


def test_json_lang_fence_stripped() -> None:
    fenced = '```json\n{"sufficient": true, "gaps": [], "feedback": "ok"}\n```'
    assert json.loads(_strip_json_fences(fenced)) == {
        "sufficient": True,
        "gaps": [],
        "feedback": "ok",
    }


def test_bare_fence_stripped() -> None:
    fenced = '```\n{"done": false}\n```'
    assert json.loads(_strip_json_fences(fenced)) == {"done": False}


def test_surrounding_whitespace_and_trailing_fence() -> None:
    fenced = '  ```json\n{"score": 100}\n```  '
    assert json.loads(_strip_json_fences(fenced)) == {"score": 100}


def test_no_fence_with_whitespace() -> None:
    assert _strip_json_fences('  {"a": 1}  ') == '{"a": 1}'
