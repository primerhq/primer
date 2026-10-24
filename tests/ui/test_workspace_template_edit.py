"""A workspace template edit must round-trip the complete served row, and show the server's refusals where the person can see them.

These pins were written for the old form (``WorkspaceTemplateCreateModal``, the review of #869), which Plan 08 Task 15 removed; they hold the template
overlay that replaced it (``WT_TemplateOverlay``) to the same contract, on the overlay's own harness (``tests/ui/test_wt_template_overlay.py``). The row is
one the real model serves (``WorkspaceTemplate.model_validate(...).model_dump(mode="json")``), the body a save sends must validate against the same model,
and every refusal is the real router's (``tests/ui/_wt_envelopes.py``).
"""

from __future__ import annotations

import json

import pytest

from primer.model.workspace import WorkspaceTemplate
from tests.ui._register_harness import base_code, context, js, settle, transpiled
from tests.ui._wt_envelopes import envelope
from tests.ui.test_wt_helpers import CHOICES
from tests.ui.test_wt_template_overlay import SHARED_FILE, STUBS, TEMPLATES_FILE

ERRORS = r"""
function errorUnder(testid) {
  var row = fieldRow("wt-overlay", testid);
  if (!row) return null;
  var hit = row.filter(function (c) { return c.role === "alert"; })[0];
  return hit ? hit.text : null;
}
"""


@pytest.fixture(scope="module")
def code() -> str:
    return base_code() + "\n" + transpiled(SHARED_FILE, TEMPLATES_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    c.eval(ERRORS)
    c.eval("serve('ws:provider-choices', { items: " + json.dumps([CHOICES["k3s"]]) + " });")
    try:
        yield c
    finally:
        c.close()


def _served_template() -> dict:
    return WorkspaceTemplate.model_validate({
        "id": "k8s-complete",
        "description": "before",
        "provider_id": "k3s",
        "backend": {
            "kind": "kubernetes",
            "image": "registry.example/runtime:1",
            "workdir": "/workspace",
            "pvc_size": "20Gi",
            "node_selector": {"accelerator": "gpu"},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "gpu", "effect": "NoSchedule"}],
            "gpu": {"count": 2, "resource_name": "nvidia.com/gpu"},
            "service_account_name": "workspace-runner",
            "image_pull_policy": "Always",
        },
        "resources": {"cpu_cores": 4, "memory_bytes": 8 * 2**30, "network": "full"},
    }).model_dump(mode="json")


def _mount(ctx, row: dict) -> None:
    """The overlay opened on ``row``, which the server also holds (a save reads it again before it writes)."""
    ctx.eval(f"ROWS[{json.dumps('/workspace_templates/' + row['id'])}] = {json.dumps(row)};")
    ctx.eval("ROLE = 'admin'; MR.mount(window.WT_TemplateOverlay, { row: " + json.dumps(row) + ", onClose: onClose, onSaved: onSaved });")


def _save(ctx) -> dict:
    ctx.eval("press('wt-save');")
    settle(ctx)
    puts = [c for c in js(ctx, "CALLS") if c["method"] == "PUT"]
    assert len(puts) == 1
    return puts[0]["body"]


def test_description_edit_keeps_every_unshown_kubernetes_field(ctx) -> None:
    row = _served_template()
    _mount(ctx, row)
    ctx.eval("setValue('ws-template-description', 'after');")
    body = _save(ctx)

    assert WorkspaceTemplate.model_validate(body)
    assert body == {**row, "description": "after"}
    for field in ("node_selector", "tolerations", "gpu", "service_account_name", "image_pull_policy"):
        assert body["backend"][field] == row["backend"][field]


def test_kubernetes_size_fields_edit_the_live_resources(ctx) -> None:
    """A Kubernetes template's size is ``resources.cpu_cores`` and ``resources.memory_bytes`` (requests = limits); the overlay takes memory in GiB."""
    _mount(ctx, _served_template())
    ctx.eval("setValue('wt-cpu', '6'); setValue('wt-memory', '10');")
    body = _save(ctx)

    assert body["resources"]["cpu_cores"] == 6
    assert body["resources"]["memory_bytes"] == 10 * 2**30
    assert body["resources"]["network"] == "full", "the size fields write their two keys and keep the rest"
    assert WorkspaceTemplate.model_validate(body)


def _refused_body(**changes) -> dict:
    row = _served_template()
    row["backend"].update(changes.pop("backend", {}))
    row["resources"].update(changes.pop("resources", {}))
    return row


def _save_refused(ctx, refusal: tuple[int, dict]) -> None:
    assert refusal[0] == 422, refusal
    ctx.eval("var ENVELOPE = " + json.dumps(refusal[1]) + "; HANDLER = function (m) { return m === 'PUT' ? refuse(ENVELOPE) : Promise.resolve(JSON.parse(JSON.stringify(ROWS['/workspace_templates/k8s-complete']))); };")
    ctx.eval("press('wt-save');")
    settle(ctx)


@pytest.mark.parametrize(("change", "testid", "typed"), [
    ({"backend": {"pvc_size": "10 Gi"}}, "wt-disk", "12"),
    ({"resources": {"cpu_cores": 0}}, "wt-cpu", "3"),
    ({"resources": {"memory_bytes": -1}}, "wt-memory", "6"),
])
def test_a_size_the_server_refuses_is_shown_under_its_field(ctx, change, testid, typed) -> None:
    """The overlay checks a size before it sends it (the next test), so the server's refusal is the one for the stored row it writes onto: the real
    router's envelope for that size lands under the size's own field, and the overlay stays open."""
    status, answer = envelope("PUT", "/v1/workspace_templates/k8s-complete", _refused_body(**change))
    message = answer["extensions"]["errors"][0]["msg"].removeprefix("Value error, ")
    _mount(ctx, _served_template())
    ctx.eval(f"setValue({json.dumps(testid)}, {json.dumps(typed)});")

    _save_refused(ctx, (status, answer))

    assert js(ctx, f"errorUnder({json.dumps(testid)})") == message
    assert ctx.eval("MR.find('wt-error') === null"), "a refusal its field shows is not said again in the line"
    assert js(ctx, "CLOSED") == 0


def test_a_field_error_no_field_shows_is_the_line_above_the_footer(ctx) -> None:
    """``resources.network`` is kept on the model and never shown (plan 08 decision 3): its refusal is the one line, with its field."""
    status, answer = envelope("PUT", "/v1/workspace_templates/k8s-complete", _refused_body(resources={"network": "bogus"}))
    message = answer["extensions"]["errors"][0]["msg"].removeprefix("Value error, ")
    _mount(ctx, _served_template())
    ctx.eval("setValue('ws-template-description', 'after');")

    _save_refused(ctx, (status, answer))

    line = ctx.eval("textOf(MR.find('wt-error'))")
    assert line == "resources.network: " + message
    assert js(ctx, "MR.find('wt-error').props.role") == "alert"


@pytest.mark.parametrize(("testid", "typed"), [("wt-cpu", "0"), ("wt-memory", "-1"), ("wt-disk", "0")])
def test_the_size_fields_ask_for_more_than_zero(ctx, testid, typed) -> None:
    """The old form's inputs carried ``min``; the overlay checks a changed size before it sends anything and says so under the field."""
    _mount(ctx, _served_template())
    ctx.eval(f"setValue({json.dumps(testid)}, {json.dumps(typed)}); press('wt-save');")
    settle(ctx)
    assert [c for c in js(ctx, "CALLS") if c["method"] in ("PUT", "GET")] == []
    assert js(ctx, f"errorUnder({json.dumps(testid)})") == "Enter a positive number"
