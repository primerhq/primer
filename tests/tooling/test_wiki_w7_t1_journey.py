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
