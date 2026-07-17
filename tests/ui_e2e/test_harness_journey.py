"""E2E: harness register → fetch → install lifecycle against a local bare repo."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-09")


def _bootstrap_bare_repo(tmp_path: Path) -> str:
    work = tmp_path / "src"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    (work / "harness.yaml").write_text(
        "apiVersion: primer/v1\n"
        "kind: Harness\n"
        "metadata:\n"
        "  name: e2e-harness\n"
        "  description: e2e test fixture\n"
        "  version: '1.0.0'\n"
    )
    (work / "overrides.schema.json").write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["llm"],
        "properties": {
            "llm": {
                "type": "object",
                "required": ["provider_id", "model_name"],
                "properties": {
                    "provider_id": {"type": "string"},
                    "model_name": {"type": "string"},
                },
            },
        },
    }))
    (work / "templates").mkdir()
    (work / "templates" / "assistant.yaml").write_text(
        "kind: agent\n"
        "name: assistant\n"
        "spec:\n"
        "  description: e2e assistant\n"
        "  model:\n"
        "    provider_id: '{{ overrides.llm.provider_id }}'\n"
        "    model_name: '{{ overrides.llm.model_name }}'\n"
        "  tools: []\n"
        "  system_prompt:\n"
        "    - 'You are an e2e assistant.'\n"
    )
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=work, check=True)
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(work), str(bare)],
        check=True,
    )
    return f"file://{bare}"


def _poll(
    client: httpx.Client,
    hid: str,
    want_status: str,
    timeout_s: float = 60.0,
) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = client.get(f"/v1/harnesses/{hid}")
        assert r.status_code == 200, r.text
        h = r.json()
        if h.get("status") == want_status:
            return h
        if h.get("status") == "error":
            raise AssertionError(
                f"harness entered error state: {h.get('last_operation_error')}"
            )
        time.sleep(0.2)
    raise AssertionError(
        f"timeout after {timeout_s}s waiting for status={want_status!r}"
    )


def test_harness_register_fetch_install_via_rest(tmp_path: Path, base_url: str) -> None:
    git_url = _bootstrap_bare_repo(tmp_path)
    suffix = os.urandom(4).hex()
    slug = f"e2e-{suffix}"

    # Seed an LLMProvider so the rendered Agent passes Pydantic validation
    # (Agent.model references a real provider_id).
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": f"e2e-llm-{suffix}",
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "e2e-model", "context_length": 2048}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, r.text

        # Register
        r = c.post("/v1/harnesses", json={
            "name": "E2E Harness",
            "slug": slug,
            "git_url": git_url,
            "ref": "main",
        })
        assert r.status_code == 201, r.text
        hid = r.json()["id"]

        # Fetch
        r = c.post(f"/v1/harnesses/{hid}/fetch")
        assert r.status_code == 202, r.text
        _poll(c, hid, "ready", timeout_s=30)

        # Update overrides
        overrides = {
            "llm": {
                "provider_id": f"e2e-llm-{suffix}",
                "model_name": "e2e-model",
            },
        }
        r = c.put(f"/v1/harnesses/{hid}/overrides", json=overrides)
        assert r.status_code == 200, r.text

        # Install
        r = c.post(f"/v1/harnesses/{hid}/install")
        assert r.status_code == 202, r.text
        _poll(c, hid, "installed", timeout_s=60)

        # Assert the rendered Agent exists
        expected_agent_id = f"{slug}__assistant"
        r = c.get(f"/v1/agents/{expected_agent_id}")
        assert r.status_code == 200, r.text
        agent = r.json()
        assert agent["harness_id"] == hid
        assert agent["model"]["provider_id"] == f"e2e-llm-{suffix}"

        # PUT to the managed agent should be rejected. The generic
        # managed-entity guard (wired via _crud's managed_by_field) now
        # returns code "managed_entity" (see tests/api/
        # test_managed_entity_locks.py for the canonical contract).
        r = c.put(f"/v1/agents/{expected_agent_id}", json=agent)
        assert r.status_code == 409, r.text
        assert r.json()["extensions"]["code"] == "managed_entity"

        # Cleanup: delete (uninstall) the harness
        r = c.delete(f"/v1/harnesses/{hid}")
        assert r.status_code == 202, r.text
        # Wait for the harness row to disappear
        deadline = time.time() + 30
        while time.time() < deadline:
            r = c.get(f"/v1/harnesses/{hid}")
            if r.status_code == 404:
                break
            time.sleep(0.5)
        # Agent should also be gone
        r = c.get(f"/v1/agents/{expected_agent_id}")
        assert r.status_code == 404, r.text

        # LLM provider cleanup
        c.delete(f"/v1/llm_providers/e2e-llm-{suffix}")
