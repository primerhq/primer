"""UI E2E: Platform › Workspace templates (platform revamp Plan 08, Task 11; design 14.2). Lead-scheduled: written with the page, run by the lead.

* ``test_create_edit_and_delete_a_template``: "+ Template" on a seeded Local provider, the card it makes, an edit of its description from the card, and
  Delete from the card's menu.
* ``test_an_edit_keeps_every_field_it_did_not_show`` (Review Focus 1): a Kubernetes template with every field set (no cluster is needed to store one) is
  opened, only its description is changed and saved, and the stored row equals the one before but for the description (the env and url passwords the
  server serves masked are restored by the server).
* ``test_the_built_in_template_is_read_only``: ``local-default`` opens read-only with the reason, with no Save, and its card has no menu.
* ``test_create_a_template_from_the_palette``: "Create Workspace Template" opens the page and its create overlay.
* ``test_the_template_page_on_a_phone``: 390x844, the page under More › Workspace templates, no horizontal overflow, "+ Template" opens the overlay.

This journey replaces ``test_workspace_templates_journey.py`` (the old list, modal and detail page), whose detail page the template overlay replaced at
``overlay=workspaces:templates:<id>``. A provider's option in "Runs on" is ``ws-template-provider:<id>`` (the kit's item test id).
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import default_wid, open_mobile_platform_nav, open_shell, open_view, run_verb

pytestmark = smk("SMK-UI-06", status="partial")

PHONE = {"width": 390, "height": 844}


def _seed_local_provider(base_url: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": provider_id, "provider": "local", "config": {"kind": "local", "root_path": f"/tmp/{provider_id}"},
        })
        assert r.status_code == 201, r.text


def _seed_k8s_provider(base_url: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": provider_id, "provider": "kubernetes",
            "config": {"kind": "kubernetes", "variant": "system", "connection": {"kind": "in_cluster"}, "namespace": "primer",
                       "reachability": {"kind": "in_cluster"}},
        })
        assert r.status_code == 201, r.text


def _cleanup(base_url: str, template_ids: list[str], provider_ids: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for tid in template_ids:
            try:
                c.delete(f"/v1/workspace_templates/{tid}")
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        for pid in provider_ids:
            try:
                c.delete(f"/v1/workspace_providers/{pid}")
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def _open_page(page: Page, console_url: str) -> None:
    open_view(page, console_url, default_wid(console_url), "platform:templates")
    expect(page.get_by_test_id("wt-templates-page")).to_be_visible(timeout=20_000)


def _find_card(page: Page, template_id: str):
    """The card of one template, filtered to, so a shared server's other templates cannot push it to another page."""
    page.get_by_test_id("wt-filter").fill(template_id)
    card = page.get_by_test_id(f"pk-card:{template_id}")
    expect(card).to_be_visible(timeout=15_000)
    return card


def test_create_edit_and_delete_a_template(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"wt-prov-{unique_suffix}"
    template_id = f"wt-tpl-{unique_suffix}"
    _seed_local_provider(base_url, provider_id)
    try:
        _open_page(page, console_url)
        page.get_by_test_id("wt-new").click()
        overlay = page.get_by_test_id("wt-overlay")
        expect(overlay).to_be_visible(timeout=10_000)
        overlay.get_by_test_id("ws-template-provider").click()
        overlay.get_by_test_id(f"ws-template-provider:{provider_id}").click()
        expect(overlay.get_by_test_id("wt-local")).to_be_visible()
        overlay.get_by_test_id("ws-template-id").fill(template_id)
        overlay.get_by_test_id("ws-template-description").fill("dev workspace v1")
        overlay.get_by_test_id("wt-save").click()
        expect(overlay).not_to_be_visible(timeout=10_000)
        expect(page.get_by_test_id("nv-toasts")).to_contain_text(f"Template created: {template_id}", timeout=10_000)

        card = _find_card(page, template_id)
        expect(card).to_contain_text("dev workspace v1")
        page.get_by_test_id(f"pk-card-open:{template_id}").click()
        expect(overlay).to_be_visible(timeout=10_000)
        expect(overlay.get_by_test_id("ws-template-description")).to_have_value("dev workspace v1")
        overlay.get_by_test_id("ws-template-description").fill("dev workspace v2")
        overlay.get_by_test_id("wt-save").click()
        expect(overlay).not_to_be_visible(timeout=10_000)
        expect(card).to_contain_text("dev workspace v2", timeout=15_000)

        page.get_by_test_id(f"pk-card-menu:{template_id}").click()
        page.get_by_test_id(f"wt-delete:{template_id}").click()
        page.get_by_test_id("dialog-confirm").click()
        expect(page.get_by_test_id(f"pk-card:{template_id}")).to_have_count(0, timeout=15_000)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(f"/v1/workspace_templates/{template_id}").status_code == 404
    finally:
        _cleanup(base_url, [template_id], [provider_id])


def test_an_edit_keeps_every_field_it_did_not_show(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"wt-k8s-{unique_suffix}"
    template_id = f"wt-full-{unique_suffix}"
    _seed_k8s_provider(base_url, provider_id)
    row = {
        "id": template_id, "description": "every field", "provider_id": provider_id,
        "backend": {
            "kind": "kubernetes", "image": "rt:1.4", "entrypoint": ["python", "-m", "primer_runtime.server"], "args": ["--verbose"], "workdir": "/work",
            "pvc_size": "500Mi", "pvc_access_modes": ["ReadWriteMany"], "storage_class": "local-path",
            "extra_volumes": [{"name": "cache", "emptyDir": {}}], "extra_volume_mounts": [{"name": "cache", "mountPath": "/cache"}],
            "pod_overrides": {"dnsPolicy": "Default"}, "node_selector": {"accelerator": "gpu"},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "gpu", "effect": "NoSchedule"}],
            "gpu": {"count": 1}, "service_account_name": "runner", "image_pull_policy": "IfNotPresent",
        },
        "files": [
            {"path": "a.txt", "source": {"kind": "inline", "content": "hello"}, "mode": "0644"},
            {"path": "b.bin", "source": {"kind": "url", "url": "https://reader:s3cret@files.example/x", "sha256": "ab" * 32}},
        ],
        "env": {"TOKEN": "t0ken-value"},
        "init_commands": ["uv sync"], "state_path": ".st", "tmp_path": ".tp", "strict_write_locking": True,
        "resources": {"cpu_cores": 2.5, "memory_bytes": 3 * 2**30 // 2},
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_templates", json=row)
        assert r.status_code == 201, r.text
        before = c.get(f"/v1/workspace_templates/{template_id}").json()
    try:
        _open_page(page, console_url)
        _find_card(page, template_id)
        page.get_by_test_id(f"pk-card-open:{template_id}").click()
        overlay = page.get_by_test_id("wt-overlay")
        expect(overlay.get_by_test_id("ws-template-description")).to_have_value("every field", timeout=10_000)
        overlay.get_by_test_id("ws-template-description").fill("only this changed")
        overlay.get_by_test_id("wt-save").click()
        expect(overlay).not_to_be_visible(timeout=10_000)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            after = c.get(f"/v1/workspace_templates/{template_id}").json()
        assert after["description"] == "only this changed"
        for key in ("description", "updated_at"):
            before.pop(key, None)
            after.pop(key, None)
        assert after == before, "every field the form did not show is stored as it was (the masked secrets restored by the server)"
    finally:
        _cleanup(base_url, [template_id], [provider_id])


def test_the_built_in_template_is_read_only(page: Page, console_url: str) -> None:
    _open_page(page, console_url)
    card = _find_card(page, "local-default")
    expect(card.get_by_test_id("pk-card-badge:local-default")).to_have_text("built in")
    expect(page.get_by_test_id("pk-card-menu:local-default")).to_have_count(0)
    page.get_by_test_id("pk-card-open:local-default").click()
    overlay = page.get_by_test_id("wt-overlay")
    expect(overlay.get_by_test_id("wt-readonly-note")).to_have_text("Built in: this template ships with primer and cannot be changed.", timeout=10_000)
    expect(overlay.get_by_test_id("wt-save")).to_have_count(0)
    overlay.get_by_test_id("wt-close").click()
    expect(overlay).not_to_be_visible(timeout=10_000)


def test_create_a_template_from_the_palette(page: Page, console_url: str) -> None:
    open_shell(page, console_url, default_wid(console_url))
    expect(page.get_by_test_id("wt-templates-page")).to_have_count(0)
    run_verb(page, "Create Workspace Template")
    expect(page).to_have_url(re.compile(r"view=platform(:|%3A)templates"), timeout=15_000)
    overlay = page.get_by_test_id("wt-overlay")
    expect(overlay).to_be_visible(timeout=15_000)
    expect(overlay).to_contain_text("New workspace template")


def test_the_template_page_on_a_phone(page: Page, console_url: str) -> None:
    page.set_viewport_size(PHONE)
    section = open_mobile_platform_nav(page, console_url, "templates")
    expect(section.get_by_test_id("wt-templates-page")).to_be_visible(timeout=20_000)
    widths = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert widths[0] <= widths[1], f"no horizontal overflow: {widths}"
    section.get_by_test_id("wt-new").click()
    overlay = page.get_by_test_id("wt-overlay")
    expect(overlay).to_be_visible(timeout=10_000)
    box = overlay.bounding_box()
    assert box and box["width"] >= PHONE["width"] - 2, f"the overlay is a full-width sheet on a phone: {box}"
