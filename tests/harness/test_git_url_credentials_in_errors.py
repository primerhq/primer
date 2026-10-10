"""A git_url's own credential never reaches an error text a harness stores or serves (ticket 01a11d32, the harness family).

``_redact`` knew only the token the platform injects itself (``oauth2:<token>@``) and the stored ``git_token``. A PAT kept IN the url (``https://ghp_xxx@github.com/org/repo``, the shape the
harness api accepts and now masks when it serves the row) was left in the stderr git echoed back, in every ``HarnessGitError`` and in ``last_operation_error``, and the two dependency failures
of a fetch stored the dependency's url whole next to the message. Both are masked now; the marker for the platform's own token is unchanged.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from primer.bus.in_memory import InMemoryEventBus
from primer.harness.dispatch import HarnessDispatchDeps, _do_fetch
from primer.harness.git import HarnessGitError, _redact
from primer.model.harness import Harness, HarnessOperation, HarnessStatus

PAT_URL = "https://ghp_abcdefghij@git.example.invalid/org/dep.git"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("fatal: unable to access 'https://ghp_abcdefghij@github.com/org/repo/': The requested URL returned error: 403", "github.com/org/repo"),
        ("fatal: repository 'https://reader:s3cr3t@git.example.com/org/repo.git/' not found", "git.example.com/org/repo.git"),
        ("clone of https://a:b@one.example/x failed, then https://ghp_second@two.example/y failed", "two.example/y"),
    ],
)
def test_a_credential_kept_in_the_url_is_masked_in_git_output(text: str, kept: str) -> None:
    out = _redact(text)

    assert "ghp_abcdefghij" not in out and "s3cr3t" not in out and "ghp_second" not in out and "a:b@" not in out
    assert kept in out, "the host and path stay: they are what a person needs to read"


def test_the_platforms_own_marker_and_the_literal_token_are_unchanged() -> None:
    assert _redact("failed to clone https://oauth2:supersecret@host/p") == "failed to clone https://oauth2:***@host/p"
    assert _redact("echoed ghp_LIVE_TOKEN_xyz here", "ghp_LIVE_TOKEN_xyz") == "echoed *** here"


def test_text_without_a_credential_is_left_alone() -> None:
    text = "fatal: unable to access 'https://github.com/org/repo/@v2': timeout"

    assert _redact(text) == text


def _init_repo(repo_dir: Path, files: dict[str, str]) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"], check=True, capture_output=True)
    return f"file://{repo_dir}"


@pytest.mark.asyncio
async def test_a_failed_dependency_fetch_stores_the_dependency_url_without_its_credential(fake_storage_provider, tmp_path, monkeypatch) -> None:
    parent_url = _init_repo(tmp_path / "parent", {
        "harness.yaml": f"apiVersion: primer/v1\nkind: Harness\nmetadata:\n  name: Parent\ndependencies:\n  - name: dep\n    git_url: {PAT_URL}\n    ref: main\n",
        "overrides.schema.json": '{"type": "object", "properties": {}}',
    })

    async def _fail(**kwargs):
        raise HarnessGitError("clone_failed", "boom")

    monkeypatch.setattr("primer.harness.dispatch.fetch_harness_metadata", _fail)
    deps = HarnessDispatchDeps(storage_provider=fake_storage_provider, event_bus=InMemoryEventBus())
    harness = Harness(
        id="h-1", slug="parent", name="Parent", git_url=parent_url, ref="main", status=HarnessStatus.DRAFT, pending_operation=HarnessOperation.FETCH,
        created_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(Harness).create(harness)

    status, error_json = await _do_fetch(deps, harness)

    assert status == HarnessStatus.ERROR and error_json is not None
    assert "ghp_abcdefghij" not in error_json, error_json
    body = json.loads(error_json)
    assert body["code"] == "dependency_fetch_failed" and body["git_url"].endswith("@git.example.invalid/org/dep.git")
