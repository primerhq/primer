"""Build a local bare-git repository holding a sample harness bundle.

Lets the harness lane run hermetically (inbound install + outbound push) with
no network. testconfig.harness overrides with a real remote.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_AGENT_TEMPLATE = """\
kind: agent
name: assistant
spec:
  description: "{{ overrides.description | default('A harness test assistant') }}"
  model:
    provider_id: "{{ overrides.provider_id }}"
    model_name: "{{ overrides.model_name | default('scripted:default') }}"
  tools: []
  system_prompt:
    - "You are a harness-installed assistant."
"""

_COLLECTION_TEMPLATE = """\
kind: collection
name: kb
spec:
  description: "harness test collection"
  embedder:
    provider_id: "{{ overrides.embedder_provider_id }}"
    model: "{{ overrides.embedder_model }}"
  search_provider_id: "{{ overrides.search_provider_id }}"
  system: false
"""

_GRAPH_TEMPLATE = """\
kind: graph
name: flow
spec:
  description: "harness test graph"
  max_iterations: 3
  nodes:
    - {kind: begin, id: start}
    - kind: agent
      id: step
      agent_id: "{{ resolved.assistant }}"
      input_template: "go"
    - {kind: end, id: done, output_template: "{{ '{{ nodes.step.text }}' }}"}
  edges:
    - {kind: static, from_node: start, to_node: step}
    - {kind: static, from_node: step, to_node: done}
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=test", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def build_harness_repo(
    root: Path, *, name: str = "test-harness", agent_only: bool = True
) -> str:
    """Create a bare git repo with a harness bundle on `main`; return file:// url.

    agent_only (default) ships just the agent template so the bundle installs
    hermetically (the collection/graph templates need an embedder + SSP).
    """
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "harness.yaml").write_text(
        f"apiVersion: primer/v1\nkind: Harness\nname: {name}\n", encoding="utf-8"
    )
    (work / "overrides.schema.json").write_text(
        '{"type": "object", '
        '"properties": {"provider_id": {"type": "string"}, '
        '"model_name": {"type": "string"}, '
        '"description": {"type": "string"}, '
        '"embedder_provider_id": {"type": "string"}, '
        '"embedder_model": {"type": "string"}, '
        '"search_provider_id": {"type": "string"}}, '
        '"required": ["provider_id"]}\n',
        encoding="utf-8",
    )
    tdir = work / "templates"
    tdir.mkdir(exist_ok=True)
    (tdir / "assistant.yaml").write_text(_AGENT_TEMPLATE, encoding="utf-8")
    if not agent_only:
        (tdir / "kb.yaml").write_text(_COLLECTION_TEMPLATE, encoding="utf-8")
        (tdir / "flow.yaml").write_text(_GRAPH_TEMPLATE, encoding="utf-8")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "initial harness bundle")
    bare = root / "remote.git"
    _git(root, "clone", "-q", "--bare", str(work), str(bare))
    return f"file://{bare}"


def empty_remote(root: Path) -> str:
    """Create an empty bare repo (for outbound push tests); return file:// url."""
    bare = root / "outbound.git"
    bare.mkdir(parents=True, exist_ok=True)
    _git(bare, "init", "-q", "--bare", "-b", "main")
    return f"file://{bare}"
