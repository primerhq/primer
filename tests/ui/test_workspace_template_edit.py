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


# ---- the review of #869, F4: the form shows the size rules' 422s -------------------------------------------------------------------------------

ROW_ERROR = r"""
function rowError(testid) {
  var hit = null;
  ALL("ws-template-modal").forEach(function (n) {
    if (hit !== null || n.type !== "div" || n.props.className !== "field") return;
    var kids = __flat(n.children);
    if (!kids.some(function (c) { return c.props && c.props["data-testid"] === testid; })) return;
    var alert = kids.filter(function (c) { return c.props && c.props.role === "alert"; })[0];
    hit = alert ? textOf(alert) : "";
  });
  return hit;
}
"""


def _real_422(body: dict) -> dict:
    """The 422 envelope the real router answers for this template body (a PUT validates the body before anything is read)."""
    import asyncio

    import httpx
    from httpx import ASGITransport

    from primer.api.app import create_test_app
    from primer.api.registries import ProviderRegistry, WorkspaceRegistry
    from tests.api.test_workspaces import _SP

    async def run() -> dict:
        sp = _SP()
        app = create_test_app(
            storage_provider=sp,  # type: ignore[arg-type]
            provider_registry=ProviderRegistry(
                sp,  # type: ignore[arg-type]
                llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
                cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
            ),
            workspace_registry=WorkspaceRegistry(sp),  # type: ignore[arg-type]
        )
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/v1/auth/register", json={"username": "formadmin", "password": "formadminpass1"})
            assert r.status_code == 200, r.text
            r = await client.put(f"/v1/workspace_templates/{body['id']}", json=body)
        assert r.status_code == 422, r.text
        return r.json()

    return asyncio.run(run())


def _save_refused(ctx, envelope: dict) -> None:
    ctx.eval(ROW_ERROR)
    ctx.eval("var ENVELOPE = " + json.dumps(envelope) + "; HANDLER = function () { return refuse(ENVELOPE); };")
    ctx.eval("""
    ALL("ws-template-modal").filter(function (n) { return n.type === "button" && textOf(n) === "Save"; })[0].props.onClick();
    """)
    settle(ctx)


def _refused_body(**changes) -> dict:
    row = _served_template()
    row["backend"].update(changes.pop("backend", {}))
    row["resources"].update(changes.pop("resources", {}))
    return row


@pytest.mark.parametrize(("change", "testid", "control"), [
    ({"backend": {"pvc_size": "10 Gi"}}, "ws-template-pvc-size", "10 Gi"),
    ({"resources": {"cpu_cores": 0}}, "ws-template-cpu-cores", "0"),
    ({"resources": {"memory_bytes": -1}}, "ws-template-memory-bytes", "-1"),
])
def test_a_size_the_server_refuses_is_shown_under_its_field(ctx, change, testid, control) -> None:
    envelope = _real_422(_refused_body(**change))
    message = envelope["extensions"]["errors"][0]["msg"]
    _mount(ctx, _served_template())
    ctx.eval(f"setValue({json.dumps(testid)}, {json.dumps(control)});")

    _save_refused(ctx, envelope)

    assert message in js(ctx, f"rowError({json.dumps(testid)})")
    assert js(ctx, "MR.find('ws-template-form-errors')") is None, "a refusal its row shows is not listed again"


def test_a_field_error_no_row_shows_is_listed_above_the_fields(ctx) -> None:
    envelope = _real_422(_refused_body(backend={"node_selector": {"not a key!": "x"}}))
    message = envelope["extensions"]["errors"][0]["msg"]
    _mount(ctx, _served_template())

    _save_refused(ctx, envelope)

    text = js(ctx, "textOf(MR.find('ws-template-form-errors'))")
    assert "backend.node_selector" in text and message in text


def test_the_size_inputs_ask_for_more_than_zero(ctx) -> None:
    _mount(ctx, _served_template())
    assert js(ctx, "MR.find('ws-template-cpu-cores').props.min") == "0.1"
    assert js(ctx, "MR.find('ws-template-memory-bytes').props.min") == "1"
