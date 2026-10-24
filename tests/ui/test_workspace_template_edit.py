"""The legacy workspace-template editor must round-trip the complete served row."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.model.workspace import WorkspaceTemplate
from tests.ui._register_harness import COMPONENTS, base_code, context, js, settle, transpiled

TEMPLATES_FILE = COMPONENTS / "workspaces" / "templates.jsx"
SHARED_FILE = COMPONENTS / "workspaces" / "shared.jsx"

STUBS = r"""
window.primerApi.useRouter = function () { return { navigate: function () {}, params: {} }; };
window.primerApi.useMutation = function (fn, opts) {
  var state = React.useState(false);
  return {
    mutate: function (arg) {
      state[1](true);
      return fn(arg).then(function (answer) {
        state[1](false);
        if (opts && opts.onSuccess) opts.onSuccess(answer);
        return answer;
      }, function (err) {
        state[1](false);
        if (opts && opts.onError) opts.onError(err);
        throw err;
      });
    },
    loading: state[0]
  };
};
"""


@pytest.fixture(scope="module")
def code() -> str:
    return base_code() + "\n" + transpiled(SHARED_FILE, TEMPLATES_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    try:
        yield c
    finally:
        c.close()


def _served_template() -> dict:
    return WorkspaceTemplate.model_validate({
        "id": "k8s-complete",
        "description": "before",
        "provider_id": "k8s-provider",
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
    ctx.eval("var SERVED_TEMPLATE = " + json.dumps(row) + ";")
    ctx.eval("""
    CALLS = [];
    serve("ws:templates:providers-picker", { items: [{ id: "k8s-provider", provider: "kubernetes" }] });
    MR.mount(window.WorkspaceTemplateCreateModal, {
      existing: SERVED_TEMPLATE,
      onClose: function () {}
    });
    """)


def _save(ctx) -> dict:
    ctx.eval("""
    ALL("ws-template-modal").filter(function (n) { return n.type === "button" && textOf(n) === "Save"; })[0].props.onClick();
    """)
    settle(ctx)
    calls = [c for c in js(ctx, "CALLS") if c["method"] == "PUT"]
    assert len(calls) == 1
    return calls[0]["body"]


def test_description_edit_keeps_every_unshown_kubernetes_field(ctx) -> None:
    row = _served_template()
    _mount(ctx, row)
    ctx.eval("setValue('ws-template-description', 'after');")
    body = _save(ctx)

    assert WorkspaceTemplate.model_validate(body)
    assert body["description"] == "after"
    assert body["resources"] == row["resources"]
    for field in ("node_selector", "tolerations", "gpu", "service_account_name", "image_pull_policy"):
        assert body["backend"][field] == row["backend"][field]


def test_kubernetes_size_fields_edit_the_live_resources(ctx) -> None:
    _mount(ctx, _served_template())
    ctx.eval("setValue('ws-template-cpu-cores', '6'); setValue('ws-template-memory-bytes', '10737418240');")
    body = _save(ctx)

    assert body["resources"]["cpu_cores"] == 6
    assert body["resources"]["memory_bytes"] == 10 * 2**30
    assert WorkspaceTemplate.model_validate(body)
