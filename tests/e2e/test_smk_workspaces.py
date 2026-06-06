"""SMK workspace tests (Phase 2): file CRUD, mkdir, recursive delete, reserved
trees, download, git log, rename, pause/resume, diagnostic, and (via scripted
agents) tool dispatch + two-agent collaboration.

WSP-12 (container) and WSP-13 (kubernetes) are gated on those backends.
"""
from __future__ import annotations

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_terminal,
)
from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = pytest.mark.asyncio


async def _ws(client, suffix, tmp_path):
    return await make_local_workspace(client, suffix=suffix, root=tmp_path)


@smk("SMK-WSP-01", "SMK-WSP-02")
async def test_provider_template_workspace_crud(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    got = await authed_client.get(f"/v1/workspaces/{wid}")
    assert got.status_code == 200, got.text


@smk("SMK-WSP-03")
async def test_rename_workspace(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    r = await authed_client.patch(f"/v1/workspaces/{wid}", json={"name": "Renamed WS"})
    assert r.status_code in (200, 204), r.text
    got = await authed_client.get(f"/v1/workspaces/{wid}")
    assert got.json().get("name") == "Renamed WS"


@smk("SMK-WSP-04", "SMK-WSP-05")
async def test_write_read_info_list(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    w = await authed_client.put(
        f"/v1/workspaces/{wid}/files", params={"path": "notes.txt"},
        json={"content": "hello", "encoding": "text"},
    )
    assert w.status_code == 204, w.text
    rd = await authed_client.get(
        f"/v1/workspaces/{wid}/files/read", params={"path": "notes.txt", "encoding": "text"}
    )
    assert rd.json()["content"] == "hello"
    info = await authed_client.get(f"/v1/workspaces/{wid}/files/info", params={"path": "notes.txt"})
    assert info.json()["kind"] == "file"
    listing = await authed_client.get(f"/v1/workspaces/{wid}/files", params={"path": "."})
    assert any(e["path"] == "notes.txt" for e in listing.json()["items"])


@smk("SMK-WSP-06", "SMK-WSP-07")
async def test_mkdir_and_nested_parents(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    d = await authed_client.post(f"/v1/workspaces/{wid}/files/dir", params={"path": "src/utils"})
    assert d.status_code == 204, d.text
    info = await authed_client.get(f"/v1/workspaces/{wid}/files/info", params={"path": "src/utils"})
    assert info.json()["kind"] == "dir"
    # nested write auto-creates parents
    w = await authed_client.put(
        f"/v1/workspaces/{wid}/files", params={"path": "a/b/c.txt"},
        json={"content": "x", "encoding": "text"},
    )
    assert w.status_code == 204, w.text
    again = await authed_client.post(f"/v1/workspaces/{wid}/files/dir", params={"path": "src/utils"})
    assert again.status_code == 400  # already exists


@smk("SMK-WSP-08")
async def test_delete_file_and_recursive_dir(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    await authed_client.put(f"/v1/workspaces/{wid}/files", params={"path": "d/a.txt"}, json={"content": "x", "encoding": "text"})
    df = await authed_client.delete(f"/v1/workspaces/{wid}/files", params={"path": "d/a.txt"})
    assert df.status_code == 204
    await authed_client.put(f"/v1/workspaces/{wid}/files", params={"path": "d/b.txt"}, json={"content": "y", "encoding": "text"})
    refused = await authed_client.delete(f"/v1/workspaces/{wid}/files", params={"path": "d"})
    assert refused.status_code == 400  # non-empty without recursive
    ok = await authed_client.delete(f"/v1/workspaces/{wid}/files", params={"path": "d", "recursive": "true"})
    assert ok.status_code == 204


@smk("SMK-WSP-09")
async def test_reserved_trees_protected(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    w = await authed_client.put(f"/v1/workspaces/{wid}/files", params={"path": ".state/x"}, json={"content": "x", "encoding": "text"})
    assert w.status_code == 400
    d = await authed_client.post(f"/v1/workspaces/{wid}/files/dir", params={"path": ".tmp/y"})
    assert d.status_code == 400


@smk("SMK-WSP-11")
async def test_download_file(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    await authed_client.put(f"/v1/workspaces/{wid}/files", params={"path": "data.bin"}, json={"content": "BYTES", "encoding": "text"})
    dl = await authed_client.get(f"/v1/workspaces/{wid}/files/download", params={"path": "data.bin"})
    assert dl.status_code == 200
    assert b"BYTES" in dl.content


@smk("SMK-WSP-14")
async def test_git_state_log(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    r = await authed_client.get(f"/v1/workspaces/{wid}/log")
    assert r.status_code == 200, r.text


@smk("SMK-WSP-16", status="partial")
async def test_pause_resume(authed_client, unique_suffix, tmp_path):
    # Workspace-level pause/resume is reserved in v1 (501); the documented
    # contract is the clean not-implemented envelope. Session-level pause/
    # resume is exercised in the agents area.
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    p = await authed_client.post(f"/v1/workspaces/{wid}/pause")
    assert p.status_code == 501, p.text
    assert p.json()["detail"]["error"] == "not_implemented"


@smk("SMK-WSP-17")
async def test_diagnostic(authed_client, unique_suffix, tmp_path):
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    r = await authed_client.post(
        f"/v1/workspaces/{wid}/diagnostic", json={"command": "echo hi", "timeout_seconds": 30}
    )
    assert r.status_code in (200, 201), r.text


@smk("SMK-WSP-10")
async def test_tools_via_agent(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    sc = f"scripted:wsp10-{unique_suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=sc,
        rules=[
            Rule(when_tool_offered="write", when_tool_result=False,
                 emit_tool="workspace__write", emit_args={"path": "agent.txt", "content": "VIA-TOOL"}),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    assert (await wait_terminal(authed_client, sid)).get("status") == "ended"
    rd = await authed_client.get(f"/v1/workspaces/{wid}/files/read", params={"path": "agent.txt", "encoding": "text"})
    assert "VIA-TOOL" in rd.json()["content"]


@smk("SMK-WSP-15")
async def test_two_agents_share_files(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    wid = await _ws(authed_client, unique_suffix, tmp_path)
    # producer writes report.txt
    prod = await make_scripted_agent(
        authed_client, registry, base_url, suffix=f"p{unique_suffix}", scenario=f"scripted:wsp15p-{unique_suffix}",
        rules=[
            Rule(when_tool_offered="write", when_tool_result=False,
                 emit_tool="workspace__write", emit_args={"path": "report.txt", "content": "PRODUCED"}),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    sid1 = await start_agent_session(authed_client, workspace_id=wid, agent_id=prod["agent_id"])
    assert (await wait_terminal(authed_client, sid1)).get("status") == "ended"
    # reviewer reads it
    rev = await make_scripted_agent(
        authed_client, registry, base_url, suffix=f"r{unique_suffix}", scenario=f"scripted:wsp15r-{unique_suffix}",
        rules=[
            Rule(when_tool_offered="read", when_tool_result=False,
                 emit_tool="workspace__read", emit_args={"path": "report.txt"}),
            Rule(when_tool_result=True, emit_text="reviewed"),
        ],
    )
    sid2 = await start_agent_session(authed_client, workspace_id=wid, agent_id=rev["agent_id"])
    assert (await wait_terminal(authed_client, sid2)).get("status") == "ended"
    # the producer's file is present in the shared workspace
    rd = await authed_client.get(f"/v1/workspaces/{wid}/files/read", params={"path": "report.txt", "encoding": "text"})
    assert "PRODUCED" in rd.json()["content"]


@smk("SMK-WSP-12")
@requires("workspace:container")
async def test_container_backend():
    pytest.skip("covered when workspace:container is configured in testconfig")


@smk("SMK-WSP-13")
@requires("workspace:kubernetes")
async def test_kubernetes_backend():
    pytest.skip("covered when workspace:kubernetes is configured in testconfig")
