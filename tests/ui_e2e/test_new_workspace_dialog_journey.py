"""UI E2E: the new-workspace dialog (platform revamp Plan 08, Task 13; design 14.3). Lead-scheduled: written with the dialog, run by the lead.

* ``test_create_a_workspace_with_extra_env_files_and_commands``: a Local template seeded by REST, picked in "From template"; a name, one extra env row, a
  Text file under "Extra files" and a command under "Extra start-up commands"; Create enters the new workspace (``#/w/<wid>``), and REST shows the
  overrides (the env value masked) and both files (the seeded one and the one the command wrote).
* ``test_size_section_follows_the_template_kind``: a Kubernetes, a Docker and a Local template seeded by REST (storing them needs no cluster and no
  daemon); the Kubernetes one fills in 2 / 4 / 10, the Docker one has no Disk, the Local one has no SIZE section.
* ``test_more_than_200_templates_are_offered`` (Review Focus 3): 205 Local templates seeded by REST; the selector offers every one of them, and its search
  finds the last.
* ``test_the_dialog_on_a_phone``: 390x844, the dialog is the kit's bottom sheet, nothing overflows sideways and Create is on screen.

A template's item in "From template" is ``nv-nw-template:<id>`` (the kit's item test id, ``<selector testid>:<value>``), behind its search box
``nv-nw-template:search``. "Create a template" beside the dropdown (``nv-nw-new-template``: the template overlay over the dialog, and the template it makes
handed back, selected) is driven by ``tests/ui_e2e/test_workspace_chain_create_journey.py``; the empty state's own button needs an install with no
template, and every install has the reserved ``local-default``, so ``tests/ui/test_new_workspace_dialog.py`` pins that one.
"""

from __future__ import annotations

import re
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import default_wid, open_overlay

pytestmark = smk("SMK-UI-06", status="partial")

PHONE = {"width": 390, "height": 844}
GIB = 2**30


def _post(base_url: str, path: str, body: dict) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(path, json=body)
        assert r.status_code == 201, r.text


def _seed_local(base_url: str, provider_id: str, root: str) -> None:
    _post(base_url, "/v1/workspace_providers", {"id": provider_id, "provider": "local", "config": {"kind": "local", "root_path": root}})


def _cleanup(base_url: str, paths: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for path in paths:
            try:
                c.delete(path)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def _open_dialog(page: Page, console_url: str):
    open_overlay(page, console_url, default_wid(console_url), "new-workspace")
    dialog = page.get_by_test_id("nv-overlay:new-workspace")
    expect(dialog.get_by_test_id("nv-nw-template")).to_be_visible(timeout=20_000)
    return dialog


def _pick(dialog, template_id: str) -> None:
    dialog.get_by_test_id("nv-nw-template").click()
    dialog.get_by_test_id("nv-nw-template:search").fill(template_id)
    dialog.get_by_test_id(f"nv-nw-template:{template_id}").click()
    expect(dialog.get_by_test_id("nv-nw-template")).to_contain_text(template_id)


def _read(base_url: str, wid: str, path: str, *, wait_s: float = 0.0) -> httpx.Response:
    """The file's read answer; with ``wait_s``, asked again until it is there (a start-up command runs as the workspace is made)."""
    deadline = time.monotonic() + wait_s
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        while True:
            r = c.get(f"/v1/workspaces/{wid}/files/read", params={"path": path})
            if r.status_code == 200 or time.monotonic() >= deadline:
                return r
            time.sleep(0.5)


def test_create_a_workspace_with_extra_env_files_and_commands(page: Page, base_url: str, console_url: str, unique_suffix: str, tmp_path) -> None:
    provider_id = f"nwd-prov-{unique_suffix}"
    template_id = f"nwd-tpl-{unique_suffix}"
    name = f"nwd space {unique_suffix}"
    created: str | None = None
    _seed_local(base_url, provider_id, str(tmp_path))
    _post(base_url, "/v1/workspace_templates", {"id": template_id, "description": "nwd local", "provider_id": provider_id, "backend": {"kind": "local"}})
    try:
        dialog = _open_dialog(page, console_url)
        _pick(dialog, template_id)
        expect(dialog.get_by_test_id("nv-nw-summary")).to_have_text(f"nwd local · Local on {provider_id}")
        expect(dialog.get_by_test_id("nv-nw-size")).to_have_count(0)
        dialog.get_by_test_id("nv-nw-name").fill(name)
        dialog.get_by_test_id("nv-nw-env:add").click()
        dialog.get_by_test_id("nv-nw-env:0:key").fill("GREETING")
        dialog.get_by_test_id("nv-nw-env:0:value").fill("hello")
        dialog.get_by_test_id("nv-nw-files-fold:toggle").click()
        dialog.get_by_test_id("nv-nw-files-add").click()
        dialog.get_by_test_id("nv-nw-files-path:0").fill("hello.txt")
        dialog.get_by_test_id("nv-nw-files-content:0").fill("hi there")
        dialog.get_by_test_id("nv-nw-init-fold:toggle").click()
        dialog.get_by_test_id("nv-nw-init").fill("echo hi > init.txt")
        dialog.get_by_test_id("nv-nw-create").click()

        page.wait_for_url(re.compile(r"#/w/ws-[0-9a-f]+"), timeout=30_000)
        created = page.url.split("#/w/", 1)[1].split("?")[0].split("#")[0]
        expect(dialog).not_to_be_visible(timeout=10_000)
        expect(page.get_by_test_id("nv-toasts")).to_contain_text(f"Workspace created: {name}", timeout=10_000)
        expect(page.locator(f'[data-testid="nv-rail-ws:{created}"][data-selected="true"]')).to_be_visible(timeout=15_000)

        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            row = c.get(f"/v1/workspaces/{created}").json()
        assert row["name"] == name and row["template_id"] == template_id
        overrides = row["overrides"]
        assert set(overrides["env"]) == {"GREETING"} and overrides["env"]["GREETING"] != "hello", "the env value is served masked"
        assert [f["path"] for f in overrides["files"]] == ["hello.txt"]
        assert overrides["init_commands"] == ["echo hi > init.txt"]
        assert not overrides.get("resources"), "a Local template has no size, and nothing was sized"

        seeded = _read(base_url, created, "hello.txt")
        if seeded.status_code >= 500:
            pytest.skip(f"workspace file read answered {seeded.status_code}: the server cannot reach the host's tmp path (as U0072/U0080)")
        assert seeded.status_code == 200 and seeded.json()["content"] == "hi there", seeded.text
        written = _read(base_url, created, "init.txt", wait_s=20.0)
        assert written.status_code == 200 and written.json()["content"].strip() == "hi", written.text
    finally:
        _cleanup(base_url, ([f"/v1/workspaces/{created}"] if created else [])
                 + [f"/v1/workspace_templates/{template_id}", f"/v1/workspace_providers/{provider_id}"])


def test_size_section_follows_the_template_kind(page: Page, base_url: str, console_url: str, unique_suffix: str, tmp_path) -> None:
    k8s_provider, docker_provider, local_provider = f"nwd-k8s-{unique_suffix}", f"nwd-dock-{unique_suffix}", f"nwd-loc-{unique_suffix}"
    k8s_tpl, docker_tpl, local_tpl = f"nwd-k8s-tpl-{unique_suffix}", f"nwd-dock-tpl-{unique_suffix}", f"nwd-loc-tpl-{unique_suffix}"
    _post(base_url, "/v1/workspace_providers", {
        "id": k8s_provider, "provider": "kubernetes",
        "config": {"kind": "kubernetes", "variant": "system", "connection": {"kind": "in_cluster"}, "namespace": "primer", "reachability": {"kind": "in_cluster"}},
    })
    _post(base_url, "/v1/workspace_providers", {
        "id": docker_provider, "provider": "container",
        "config": {"kind": "container", "runtime": "docker", "connection": {"kind": "socket", "socket_path": "/var/run/docker.sock"},
                   "reachability": {"kind": "host_port", "bind_host": "127.0.0.1"}},
    })
    _seed_local(base_url, local_provider, str(tmp_path))
    _post(base_url, "/v1/workspace_templates", {
        "id": k8s_tpl, "description": "nwd k8s", "provider_id": k8s_provider,
        "backend": {"kind": "kubernetes", "image": "rt:1.4", "pvc_size": "10Gi"}, "resources": {"cpu_cores": 2, "memory_bytes": 4 * GIB},
    })
    _post(base_url, "/v1/workspace_templates", {
        "id": docker_tpl, "description": "nwd docker", "provider_id": docker_provider, "backend": {"kind": "container", "cpu_cores": 1, "memory_bytes": 2 * GIB},
    })
    _post(base_url, "/v1/workspace_templates", {"id": local_tpl, "description": "nwd local", "provider_id": local_provider, "backend": {"kind": "local"}})
    try:
        dialog = _open_dialog(page, console_url)
        _pick(dialog, k8s_tpl)
        expect(dialog.get_by_test_id("nv-nw-summary")).to_have_text(f"nwd k8s · Kubernetes on {k8s_provider} · 2 CPU · 4 GiB · 10 GiB disk")
        expect(dialog.get_by_test_id("nv-nw-cpu")).to_have_value("2")
        expect(dialog.get_by_test_id("nv-nw-memory")).to_have_value("4")
        expect(dialog.get_by_test_id("nv-nw-disk")).to_have_value("10")
        _pick(dialog, docker_tpl)
        expect(dialog.get_by_test_id("nv-nw-cpu")).to_have_value("1")
        expect(dialog.get_by_test_id("nv-nw-memory")).to_have_value("2")
        expect(dialog.get_by_test_id("nv-nw-disk")).to_have_count(0)
        _pick(dialog, local_tpl)
        expect(dialog.get_by_test_id("nv-nw-size")).to_have_count(0)
    finally:
        _cleanup(base_url, [f"/v1/workspace_templates/{t}" for t in (k8s_tpl, docker_tpl, local_tpl)]
                 + [f"/v1/workspace_providers/{p}" for p in (k8s_provider, docker_provider, local_provider)])


@pytest.mark.timeout(300)
def test_more_than_200_templates_are_offered(page: Page, base_url: str, console_url: str, unique_suffix: str, tmp_path) -> None:
    provider_id = f"nwd-many-{unique_suffix}"
    prefix = f"nwd-many-{unique_suffix}-"
    ids = [f"{prefix}{i:03d}" for i in range(205)]
    _seed_local(base_url, provider_id, str(tmp_path))
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            for tid in ids:
                r = c.post("/v1/workspace_templates", json={"id": tid, "description": "one of many", "provider_id": provider_id, "backend": {"kind": "local"}})
                assert r.status_code == 201, r.text
        dialog = _open_dialog(page, console_url)
        dialog.get_by_test_id("nv-nw-template").click()
        dialog.get_by_test_id("nv-nw-template:search").fill(prefix)
        expect(dialog.locator(f'[data-testid^="nv-nw-template:{prefix}"]')).to_have_count(205, timeout=15_000)
        dialog.get_by_test_id("nv-nw-template:search").fill(ids[-1])
        dialog.get_by_test_id(f"nv-nw-template:{ids[-1]}").click()
        expect(dialog.get_by_test_id("nv-nw-template")).to_contain_text(ids[-1])
    finally:
        _cleanup(base_url, [f"/v1/workspace_templates/{tid}" for tid in ids] + [f"/v1/workspace_providers/{provider_id}"])


def test_the_dialog_on_a_phone(page: Page, console_url: str) -> None:
    page.set_viewport_size(PHONE)
    dialog = _open_dialog(page, console_url)
    widths = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert widths[0] <= widths[1], f"no horizontal overflow: {widths}"
    box = dialog.bounding_box()
    assert box and box["width"] >= PHONE["width"] - 2, f"the dialog is a full-width sheet on a phone: {box}"
    expect(page.get_by_test_id("nv-nw-create")).to_be_in_viewport()
