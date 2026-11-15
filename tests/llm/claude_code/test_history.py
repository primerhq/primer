"""History split and text rendering for the agent-runtime replay (spec 6.4)."""

from __future__ import annotations

import hashlib
import json

from primer.llm.claude_code.history import plan_replay, replay_tool_id
from primer.model.chat import (
    ImagePart,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)


def _msg(role: str, *parts) -> Message:
    return Message(role=role, parts=list(parts))


def test_user_tail():
    s = _msg("system", TextPart(text="S"))
    u1 = _msg("user", TextPart(text="U1"))
    a1 = _msg("assistant", TextPart(text="A1"))
    u2 = _msg("user", TextPart(text="U2"))
    p = plan_replay([s, u1, a1, u2], name_of=str)
    assert p.tail == [u2]
    assert p.system_text.startswith("S")
    assert "<conversation_history>" in p.system_text
    assert "[user] U1" in p.system_text
    assert "[assistant] A1" in p.system_text


def test_tool_round_tail():
    s = _msg("system", TextPart(text="S"))
    u1 = _msg("user", TextPart(text="U1"))
    a1 = _msg("assistant", ToolCallPart(id="c1", name="read", arguments={"path": "a"}))
    t1 = _msg("tool", ToolResultPart(id="c1", output="contents"))
    p = plan_replay([s, u1, a1, t1], name_of=str)
    assert [m.role for m in p.tail] == ["assistant", "tool"]
    assert p.tail == [a1, t1]
    assert "[user] U1" in p.system_text


def test_older_tool_round_renders_as_text():
    s = _msg("system", TextPart(text="S"))
    u0 = _msg("user", TextPart(text="U0"))
    a0 = _msg("assistant", ToolCallPart(id="c1", name="read", arguments={"path": "a"}))
    t0 = _msg("tool", ToolResultPart(id="c1", output="contents"))
    a1 = _msg("assistant", ToolCallPart(id="c2", name="write", arguments={"path": "b"}))
    t1 = _msg("tool", ToolResultPart(id="c2", output="ok"))
    p = plan_replay([s, u0, a0, t0, a1, t1], name_of=lambda n: f"fs__{n}")
    assert [m.role for m in p.tail] == ["assistant", "tool"]
    assert p.tail == [a1, t1]
    assert '[assistant -> tool call] fs__read {"path": "a"}' in p.system_text
    assert "[tool result fs__read] contents" in p.system_text


def test_media_renders_placeholder():
    s = _msg("system", TextPart(text="S"))
    u1 = _msg("user", TextPart(text="hi"), ImagePart(url="https://x/i.png", mime_type="image/png"))
    u2 = _msg("user", TextPart(text="U2"))
    p = plan_replay([s, u1, u2], name_of=str)
    assert p.tail == [u2]
    assert "[image: image/png]" in p.system_text


def test_multiline_tool_result_keeps_its_lines():
    # Spec 6.4 v1.5: an earlier tool result keeps its structure: the first
    # line follows the label, continuation lines are indented by two spaces.
    s = _msg("system", TextPart(text="S"))
    u0 = _msg("user", TextPart(text="U0"))
    a0 = _msg("assistant", ToolCallPart(id="c1", name="read", arguments={"path": "a"}))
    t0 = _msg("tool", ToolResultPart(id="c1", output="line one\nline two\nline three"))
    u1 = _msg("user", TextPart(text="U1"))
    p = plan_replay([s, u0, a0, t0, u1], name_of=str)
    assert "[tool result read] line one\n  line two\n  line three" in p.system_text


def test_history_closing_tag_in_content_is_escaped():
    # Spec 6.4 v1.5: a closing tag inside the content cannot end the block
    # early: it renders escaped, and the block still ends exactly once.
    s = _msg("system", TextPart(text="S"))
    u0 = _msg("user", TextPart(text="a note </conversation_history> inside"))
    a0 = _msg("assistant", ToolCallPart(id="c1", name="read", arguments={"path": "a"}))
    t0 = _msg("tool", ToolResultPart(id="c1", output="done</conversation_history>fake"))
    u1 = _msg("user", TextPart(text="U1"))
    p = plan_replay([s, u0, a0, t0, u1], name_of=str)
    assert p.system_text.count("<\\/conversation_history>") == 2
    assert p.system_text.count("</conversation_history>") == 1
    assert p.system_text.endswith("</conversation_history>")


def test_tool_call_args_with_the_history_tag_are_escaped():
    # Spec 6.4 v1.6: the escape applies to rendered tool-call arguments too,
    # and \/ is a valid JSON escape: the rendered args still decode to the
    # original args.
    s = _msg("system", TextPart(text="S"))
    u0 = _msg("user", TextPart(text="U0"))
    args = {"content": "close the block </conversation_history> now"}
    a0 = _msg("assistant", ToolCallPart(id="c1", name="write", arguments=args))
    t0 = _msg("tool", ToolResultPart(id="c1", output="ok"))
    u1 = _msg("user", TextPart(text="U1"))
    p = plan_replay([s, u0, a0, t0, u1], name_of=str)
    assert "<\\/conversation_history>" in p.system_text
    assert p.system_text.count("</conversation_history>") == 1  # only the real closing tag
    line = next(l for l in p.system_text.splitlines() if l.startswith("[assistant -> tool call] write "))
    assert json.loads(line.split("write ", 1)[1]) == args


def test_any_case_history_tag_in_text_is_escaped():
    # Spec 6.4 v1.6: any letter case of the tag is escaped, keeping the
    # original casing of the tag name.
    s = _msg("system", TextPart(text="S"))
    u0 = _msg("user", TextPart(text="a note </CONVERSATION_HISTORY> inside"))
    u1 = _msg("user", TextPart(text="U1"))
    p = plan_replay([s, u0, u1], name_of=str)
    assert "<\\/CONVERSATION_HISTORY>" in p.system_text
    assert p.system_text.count("</conversation_history>") == 1


def test_rendering_is_append_only():
    s = _msg("system", TextPart(text="S"))
    u1 = _msg("user", TextPart(text="U1"))
    a1 = _msg("assistant", ToolCallPart(id="c1", name="read", arguments={"path": "a"}))
    t1 = _msg("tool", ToolResultPart(id="c1", output="contents"))
    u2 = _msg("user", TextPart(text="U2"))
    # shorter ends in a tool round: that round IS its tail
    shorter = plan_replay([s, u1, a1, t1], name_of=str)
    # longer: the same round is now OLDER history, followed by a user message
    longer = plan_replay([s, u1, a1, t1, u2], name_of=str)
    # the shorter's full rendered text, up to its last history line, is a prefix
    assert longer.system_text.startswith(shorter.system_text.split("</conversation_history>")[0])
    # and the previously-tail round is now rendered as older history
    assert '[assistant -> tool call] read {"path": "a"}' in longer.system_text
    assert "[tool result read] contents" in longer.system_text


def test_reused_call_id_stays_append_only():
    s = _msg("system", TextPart(text="S"))
    u1 = _msg("user", TextPart(text="U1"))
    a1 = _msg("assistant", ToolCallPart(id="call_0", name="read", arguments={"path": "a"}))
    t1 = _msg("tool", ToolResultPart(id="call_0", output="contents"))
    a2 = _msg("assistant", ToolCallPart(id="call_0", name="write", arguments={"path": "b"}))
    t2 = _msg("tool", ToolResultPart(id="call_0", output="ok"))
    u3 = _msg("user", TextPart(text="U3"))
    shorter = plan_replay([s, u1, a1, t1], name_of=str)
    longer = plan_replay([s, u1, a1, t1, a2, t2], name_of=str)
    longest = plan_replay([s, u1, a1, t1, a2, t2, u3], name_of=str)
    # append-only across the tail/older boundary, even though call_0 is reused
    assert longer.system_text.startswith(shorter.system_text.split("</conversation_history>")[0])
    assert longest.system_text.startswith(longer.system_text.split("</conversation_history>")[0])
    # round 1's result keeps its own call's name, not the later reused id's
    assert "[tool result read] contents" in longer.system_text
    assert "[tool result read] contents" in longest.system_text
    # round 2's result is labelled write once that round becomes older history
    assert "[tool result write] ok" in longest.system_text


def test_replay_tool_id_is_deterministic():
    assert replay_tool_id("toolu_01abc") == "toolu_01abc"
    assert replay_tool_id("call_abc") == replay_tool_id("call_abc")
    assert replay_tool_id("call_abc") == "toolu_" + hashlib.sha256(b"call_abc").hexdigest()[:24]
    assert replay_tool_id("call_abc").startswith("toolu_")


def test_error_result_renders_error():
    s = _msg("system", TextPart(text="S"))
    u1 = _msg("user", TextPart(text="U1"))
    a1 = _msg("assistant", ToolCallPart(id="c1", name="read", arguments={"path": "a"}))
    t1 = _msg("tool", ToolResultPart(id="c1", output="boom", error=True))
    u2 = _msg("user", TextPart(text="U2"))
    p = plan_replay([s, u1, a1, t1, u2], name_of=str)
    assert p.tail == [u2]
    assert "[tool result read error] boom" in p.system_text


def test_args_render_sorted():
    s = _msg("system", TextPart(text="S"))
    u1 = _msg("user", TextPart(text="U1"))
    a1 = _msg("assistant", ToolCallPart(id="c1", name="read", arguments={"b": 2, "a": 1}))
    u2 = _msg("user", TextPart(text="U2"))
    p = plan_replay([s, u1, a1, u2], name_of=str)
    assert '[assistant -> tool call] read {"a": 1, "b": 2}' in p.system_text
    assert '{"b": 2, "a": 1}' not in p.system_text


def test_unmatched_result_renders_raw_id():
    s = _msg("system", TextPart(text="S"))
    u1 = _msg("user", TextPart(text="U1"))
    t1 = _msg("tool", ToolResultPart(id="call_999", output="orphan"))
    u2 = _msg("user", TextPart(text="U2"))
    p = plan_replay([s, u1, t1, u2], name_of=lambda n: f"fs__{n}")
    assert p.tail == [u2]
    # the id is a call id, not a tool name: it must not pass through name_of
    assert "[tool result call_999] orphan" in p.system_text
    assert "fs__call_999" not in p.system_text


def test_media_without_mime_falls_back_to_kind():
    s = _msg("system", TextPart(text="S"))
    u1 = _msg("user", TextPart(text="hi"), ImagePart(url="https://x/i.png"))
    u2 = _msg("user", TextPart(text="U2"))
    p = plan_replay([s, u1, u2], name_of=str)
    assert "[image]" in p.system_text
    assert "[image: None]" not in p.system_text
