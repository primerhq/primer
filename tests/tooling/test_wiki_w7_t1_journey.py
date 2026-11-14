"""Safe checks for the CI journey's answer proof and isolated configuration.

Importing the journey starts no server. Actual execution belongs to the live
CI lane; these cases prevent a scripted PONG from substituting for file IO.
"""

import json

import pytest

from primer.api.config import AppConfig
from tests.e2e.test_wiki_w7_t1_lifecycle import (
    ALLOWED,
    WRITES,
    FileAnswerRule,
    assistant_text,
    isolated_config,
    mcp_http_client,
    prepare_wiki,
    transcript_records,
)


def request(value, call_id="w7_file_read"):
    return {"messages": [{"role": "tool", "tool_call_id": call_id, "content": json.dumps(value)}]}


def test_model_answer_is_derived_from_actual_utf8_file_bytes():
    rule = FileAnswerRule()
    for content in ("first café\n", "different contents\n"):
        assert rule.matches(request({"path": "notes/welcome.txt", "encoding": "text", "content": content, "size_bytes": len(content.encode())}))
        assert rule.emit_text == "FILE-ANSWER:" + content
    assert not rule.matches({"messages": [{"role": "user", "content": "FILE-ANSWER:invented"}]})
    assert not rule.matches(request({"content": "invented"}, call_id="another_tool"))


@pytest.mark.parametrize("value", [
    {"type": "not-found", "detail": "missing"},
    {"path": "wrong.txt", "encoding": "text", "content": "invented", "size_bytes": 8},
    {"path": "notes/welcome.txt", "encoding": "text", "content": "", "size_bytes": 0},
    {"path": "notes/welcome.txt", "encoding": "text", "content": "café", "size_bytes": 4},
])
def test_failed_or_inconsistent_file_read_cannot_generate_a_success_answer(value):
    with pytest.raises((AssertionError, KeyError)):
        FileAnswerRule().matches(request(value))


def test_only_durable_assistant_tokens_count_as_the_answer():
    rows = [
        {"kind": "user_input", "payload": {"text": "PONG"}},
        {"kind": "tool_result", "payload": {"text": "PONG"}},
        {"kind": "assistant_token", "payload": {"text": "FILE-"}},
        {"kind": "assistant_token", "payload": {"text": "ANSWER:café\n"}},
    ]
    assert assistant_text(rows) == "FILE-ANSWER:café\n"


def test_isolated_instance_has_real_auth_temp_storage_and_bounded_workers(tmp_path):
    config = AppConfig.model_validate(isolated_config(tmp_path, 18888))
    assert config.auth.enabled
    assert not config.auto_bootstrap
    assert config.db.provider.value == "sqlite"
    assert config.db.config.path == tmp_path / "data.sqlite"
    assert config.worker.concurrency == 2 and config.worker.drain_timeout_seconds == 3
    assert config.default_workspace_template == "w7-template"
    assert {"workspaces__" + name for name in WRITES} <= ALLOWED


@pytest.mark.asyncio
async def test_mcp_client_uses_numeric_sdk_native_timeouts_before_network_io():
    # Construct the exact client used by the journey and build a request;
    # neither operation opens a socket. A foreign httpx.Timeout poisons all
    # four request values and fails these assertions before TCP/anyio.
    async with mcp_http_client("fixture-cookie") as client:
        request = client.build_request("POST", "http://127.0.0.1:1/v1/mcp/", json={})
        assert request.headers["Cookie"] == "fixture-cookie"
        assert request.extensions["timeout"] == {name: 15.0 for name in ("connect", "read", "write", "pool")}
        assert all(isinstance(value, (int, float)) for value in request.extensions["timeout"].values())


def test_scoped_fixture_is_accepted_by_the_real_wiki_loader_and_builder(tmp_path):
    from primer.knowledge.wiki.build import build_wiki
    from primer.knowledge.wiki.pages import load_source_pages
    from tests.docs._wiki import WIKI_DIR, tool_entries

    wiki = prepare_wiki(tmp_path)
    assert wiki == tmp_path / "wiki"
    assert {path.relative_to(wiki).as_posix() for path in wiki.rglob("*.md")} == {
        "index.md", "runbooks/index.md", "runbooks/manage-a-workspace.md",
    }
    source = load_source_pages(wiki)
    assert set(source) == {"index", "runbooks", "runbooks/manage-a-workspace"}
    assert (wiki / "runbooks/manage-a-workspace.md").read_bytes() == (WIKI_DIR / "runbooks/manage-a-workspace.md").read_bytes()
    built = build_wiki(wiki, tool_entries(), legacy_pages={})
    page = built.pages["runbooks/manage-a-workspace"]
    assert page.kind == "guide" and source["runbooks/manage-a-workspace"].body in page.body


def _event(kind, payload):
    from datetime import UTC, datetime

    from primer.model.workspace_session import SessionMessageRecord

    return SessionMessageRecord(seq=1, kind=kind, payload=payload, created_at=datetime.now(UTC)).model_dump(mode="json")


def test_real_event_and_raw_message_shapes_do_not_hide_error_or_cancel_records():
    from primer.model.chat import Message, TextPart, ToolResultPart

    answer = _event("assistant_token", {"text": "FILE-ANSWER:café\n"})
    raw = Message(role="assistant", parts=[TextPart(text="duplicate raw answer")]).model_dump(mode="json")
    tool = Message(role="tool", parts=[ToolResultPart(id="read", output="ok", error=False)]).model_dump(mode="json")
    for kind in ("error", "cancelled", "done"):
        event = _event(kind, {})
        records = transcript_records([answer, raw, tool, event])
        assert records == [answer, event]
        assert assistant_text(records) == "FILE-ANSWER:café\n"
        assert any(row["kind"] in {"error", "cancelled"} for row in records) == (kind != "done")


@pytest.mark.parametrize("row", [
    {}, {"role": "assistant", "parts": []}, {"kind": "unknown", "payload": {}},
    {"kind": "done", "payload": {}}, "not a row",
    {"role": "tool", "parts": [{"type": "tool_result", "id": "read", "output": "failed", "error": True}]},
])
def test_malformed_transcript_or_failed_raw_tool_result_fails_closed(row):
    with pytest.raises((AssertionError, ValueError)):
        transcript_records([row])
