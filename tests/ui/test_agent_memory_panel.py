"""The agent Memory section and panel (agent memory T14, ``ui/components/agent-memory.jsx``), mounted in V8 on the agent-form harness.

``AG_MemorySettings`` reads ``GET /agents/<id>/memory/settings`` and the viewer's level from ``GET /agents/<id>/access`` and saves with ``PUT``;
``AG_MemoryPanel`` lists the entries by folder and acts on them. The resources are answered per key (``set_resource``), the saves are recorded.
"""

from __future__ import annotations

import json

from tests.ui._agent_form_harness import agent_form, calls, click, prop, set_resource, text_of, type_into

AGENT = "a1"
DEFAULT_PROMPT = "BUILT-IN REFLECTION PROMPT"
SETTINGS = {
    "id": AGENT, "enabled": False, "promotion": "auto", "teachers": "owner", "idle_minutes": 10, "auto_inject": False, "inject_top_k": 2,
    "inject_min_score": None, "max_index_entries": 40, "max_active": 200, "archive_after_days": 60, "daily_reflections": 50, "search": None,
    "capture_topics": ["github"], "do_not_capture": [], "topics_edited": False, "topics_suggestion": None, "reflection_prompt": None,
    "reflections_day": None, "reflections_count": 0, "last_curated_at": None, "default_reflection_prompt": DEFAULT_PROMPT,
}
EDITABLE = (
    "enabled", "promotion", "teachers", "idle_minutes", "auto_inject", "inject_top_k", "inject_min_score", "max_index_entries", "max_active",
    "archive_after_days", "daily_reflections", "search", "capture_topics", "do_not_capture", "reflection_prompt",
)


def _ok(data: dict) -> dict:
    return {"data": data, "loading": False, "error": None}


def _resources(ctx, *, level: str = "owner", settings: dict | None = None, activity: list | None = None) -> None:
    set_resource(ctx, f"agent-memory:settings:{AGENT}", _ok({**SETTINGS, **(settings or {})}))
    set_resource(ctx, f"agent-memory:access:{AGENT}", _ok({"your_level": level, "owners": [], "grants": []}))
    set_resource(ctx, f"agent-memory:activity:{AGENT}", _ok({"items": activity or []}))


def _mount(ctx, component: str) -> None:
    ctx.eval(f"MR.mount({component}, {{ agentId: {json.dumps(AGENT)}, pushToast: function (t) {{ __recorded.toasts.push(t); }} }})")
    ctx.eval("MR.rerender()")


def _toasts(ctx) -> list[dict]:
    return json.loads(ctx.eval("JSON.stringify(__recorded.toasts)"))


def _entry(name: str = "retry-github-429", status: str = "candidates", **meta) -> dict:
    return {
        "name": name, "status": status, "description": "Retry when rate limited",
        "body": {"steps": [{"id": "s1", "text": "wait"}], "pitfalls": [{"id": "p1", "text": "do not hammer"}], "example": []},
        "meta": {"helpful": 3, "harmful": 1, "pinned": False, "sources": [{"session_id": "s9", "from_turn": 1, "to_turn": 4}], **meta},
    }


def test_settings_section_saves_with_put() -> None:
    with agent_form() as ctx:
        _resources(ctx)
        _mount(ctx, "AG_MemorySettings")
        click(ctx, "memory-enabled")
        type_into(ctx, "memory-idle-minutes", "5")
        click(ctx, "memory-save")
        [put] = [c for c in calls(ctx) if c["method"] == "PUT"]
        assert put["path"] == f"/agents/{AGENT}/memory/settings"
        assert set(put["body"]) == set(EDITABLE), "only the owner's fields are sent, never the system's"
        assert put["body"]["enabled"] is True and put["body"]["idle_minutes"] == 5 and put["body"]["capture_topics"] == ["github"]
        assert [t["kind"] for t in _toasts(ctx)] == ["success"]


def test_controls_disabled_for_non_owner() -> None:
    for level in ("read", "full"):
        with agent_form() as ctx:
            _resources(ctx, level=level)
            _mount(ctx, "AG_MemorySettings")
            for testid in ("memory-enabled", "memory-promotion", "memory-teachers", "memory-auto-inject", "memory-idle-minutes", "memory-save"):
                assert prop(ctx, testid, "disabled") is True, (level, testid)
            click(ctx, "memory-save")
            assert [c for c in calls(ctx) if c["method"] == "PUT"] == []


def test_inbox_approve_moves_entry() -> None:
    with agent_form() as ctx:
        _resources(ctx)
        set_resource(ctx, f"agent-memory:entries:{AGENT}:candidates", _ok({"items": [_entry()]}))
        _mount(ctx, "AG_MemoryPanel")
        click(ctx, "memory-tab-candidates")
        assert "Retry when rate limited" in text_of(ctx, "memory-entry:retry-github-429")
        assert text_of(ctx, "memory-counts:retry-github-429") == "helped 3 · hurt 1"
        click(ctx, "memory-approve:retry-github-429")
        [post] = [c for c in calls(ctx) if c["method"] == "POST"]
        assert post["path"] == f"/agents/{AGENT}/memory/entries/retry-github-429/approve"
        assert [(t["kind"], t["title"]) for t in _toasts(ctx)] == [("success", "Entry approved")]


def test_reflection_prompt_builtin_readonly_and_override_reset() -> None:
    with agent_form() as ctx:
        _resources(ctx)
        _mount(ctx, "AG_MemorySettings")
        assert prop(ctx, "memory-prompt-builtin", "readOnly") is True and prop(ctx, "memory-prompt-builtin", "value") == DEFAULT_PROMPT
        click(ctx, "memory-prompt-own")
        type_into(ctx, "memory-prompt-editor", "my own prompt")
        click(ctx, "memory-save")
        assert [c for c in calls(ctx) if c["method"] == "PUT"][-1]["body"]["reflection_prompt"] == "my own prompt"
    with agent_form() as ctx:
        _resources(ctx, settings={"reflection_prompt": "saved own prompt"})
        _mount(ctx, "AG_MemorySettings")
        assert prop(ctx, "memory-prompt-editor", "value") == "saved own prompt"
        click(ctx, "memory-prompt-reset")
        assert prop(ctx, "memory-prompt-builtin", "readOnly") is True
        click(ctx, "memory-save")
        assert [c for c in calls(ctx) if c["method"] == "PUT"][-1]["body"]["reflection_prompt"] is None


def test_min_score_shows_recent_lookup_scores() -> None:
    lookups = [
        {"id": 3, "type": "memory.lookup", "actor": "memory:a1", "at": "t", "payload": {"scores": [0.81, 0.42]}},
        {"id": 2, "type": "memory.entry.added", "actor": "memory:a1", "at": "t", "payload": {"name": "x"}},
        {"id": 1, "type": "memory.lookup", "actor": "memory:a1", "at": "t", "payload": {"scores": [0.65]}},
    ]
    with agent_form() as ctx:
        _resources(ctx, settings={"auto_inject": True, "inject_min_score": 0.5}, activity=lookups)
        _mount(ctx, "AG_MemorySettings")
        shown = text_of(ctx, "memory-lookup-scores")
        assert "0.81" in shown and "0.42" in shown and "0.65" in shown
        type_into(ctx, "memory-min-score", "0.7")
        click(ctx, "memory-save")
        assert [c for c in calls(ctx) if c["method"] == "PUT"][-1]["body"]["inject_min_score"] == 0.7
    with agent_form() as ctx:
        _resources(ctx, level="full", settings={"auto_inject": True}, activity=lookups)
        _mount(ctx, "AG_MemorySettings")
        assert ctx.eval("MR.find('memory-lookup-scores')") is None, "the activity feed is the owner's"


def test_empty_error_loading_states() -> None:
    key = f"agent-memory:entries:{AGENT}:active"
    with agent_form() as ctx:
        _resources(ctx)
        set_resource(ctx, key, {"data": None, "loading": True, "error": None})
        _mount(ctx, "AG_MemoryPanel")
        assert text_of(ctx, "memory-loading") != ""
    with agent_form() as ctx:
        _resources(ctx)
        set_resource(ctx, key, {"data": None, "loading": False, "error": {"title": "Boom", "detail": "the server said no"}})
        _mount(ctx, "AG_MemoryPanel")
        assert "the server said no" in text_of(ctx, "memory-error")
    with agent_form() as ctx:
        _resources(ctx)
        set_resource(ctx, key, _ok({"items": []}))
        _mount(ctx, "AG_MemoryPanel")
        assert "No active entries" in text_of(ctx, "memory-empty")
