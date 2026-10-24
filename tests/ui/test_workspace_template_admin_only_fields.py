"""The workspace template overlay marks the admin-only fields (security review 2026-10-08: AUTHZ-02, INJ-02, AUTHZ-05; design 14.2).

The server refuses a non-admin write of container ``extra_mounts``, the Kubernetes overlays, the service account, the tolerations and secret file sources
with 403 ``forbidden_role``, and refuses a Kubernetes overlay outside its allowlist for everyone. The overlay says so up front: each admin-only control
carries the ``Admin only`` help, the volume hints name the allowlist, the four pod settings are the four the allowlist takes, an admin reads
``WT_ADMIN_ONLY_NOTE`` at the top of each More fold, and a non-admin opens a template that holds any of them read-only with the reason.

These pins moved from the old JSON and table form (``WorkspaceTemplateCreateModal``, removed by Plan 08 Task 15) to ``WT_TemplateOverlay``, which they
render on the overlay's own harness (``tests/ui/test_wt_template_overlay.py``: the real kit, form field, shared components and ``templates.jsx``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.workspace.k8s.backend import _POD_OVERRIDES_ALLOWED, _VOLUME_MOUNT_ALLOWED
from tests.ui._register_harness import base_code, context, js, settle, transpiled
from tests.ui.test_wt_helpers import CHOICES, FULL_K8S
from tests.ui.test_wt_template_overlay import SHARED_FILE, STUBS, TEMPLATES_FILE

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = TEMPLATES_FILE.read_text("utf-8")

# The help line of a control: the muted line in the kit field (or the row table's own block) that holds the control, by its test id or its kit testid prop.
HELP = r"""
function __holder(testid, part) {
  var hit = null;
  ALL("wt-overlay").forEach(function (n) {
    if (hit !== null || n.type !== "div") return;
    var cls = String(n.props.className || "");
    if (cls.indexOf("pk-field") < 0 && cls.indexOf("pk-span-full") < 0) return;
    var kids = __flat(n.children);
    if (!kids.some(function (c) { return c.props && (c.props["data-testid"] === testid || c.props.testid === testid); })) return;
    var found = kids.filter(part)[0];
    hit = found ? textOf(found) : "";
  });
  return hit;
}
function helpOf(testid) { return __holder(testid, function (c) { return c.props.className === "field-help" && c.props.role !== "alert"; }); }
function labelOf(testid) { return __holder(testid, function (c) { return c.type === "label"; }); }
function sourceLabels(testid) { return MR.findProp("testid", testid).props.options.map(function (o) { return o.label; }); }
"""

HELD = {"id": "gpu-held", "description": "Tolerates the GPU nodes", "provider_id": "k3s",
        "backend": {"kind": "kubernetes", "image": "rt:1.4", "tolerations": [{"key": "gpu", "operator": "Exists"}]}}


@pytest.fixture(scope="module")
def code() -> str:
    return base_code() + "\n" + transpiled(SHARED_FILE, TEMPLATES_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    c.eval(HELP)
    c.eval("serve('ws:provider-choices', { items: " + json.dumps([CHOICES["dock"], CHOICES["k3s"], CHOICES["loc"]]) + " });")
    try:
        yield c
    finally:
        c.close()


def _create(ctx, provider: str, role: str) -> None:
    ctx.eval("MR.mount(function Nothing() { return null; }, {});")
    ctx.eval(f"ROLE = {json.dumps(role)}; MR.mount(window.WT_TemplateOverlay, {{ create: true, onClose: onClose, onSaved: onSaved }});")
    ctx.eval(f"overlayProps().selector.onChange({json.dumps(provider)}); MR.rerender();")


def _open(ctx, fold: str) -> None:
    ctx.eval(f"MR.click({json.dumps(fold + ':toggle')});")


def test_the_admin_only_note_keeps_its_two_warnings() -> None:
    """Lead review of #473: a template that holds any of them is admin-only to edit at all. Lead re-review: the held rule does not stop instantiation, so the
    note warns the admin; lead ruling 16 of plan 08 (from the review of #782): the pod runs as the template's service account, so the warning names it too."""
    assert SRC.count("const WT_ADMIN_ONLY_NOTE =") == 1
    note = SRC[SRC.index("const WT_ADMIN_ONLY_NOTE ="):]
    note = note[:note.index(";\n")]
    assert "only an admin can edit or delete a template that has any of them set" in note
    assert "do not put host-privileged mounts, secrets or a service account with real permissions in a template ordinary users may use" in note
    # One element draws it, the overlay's, which puts it at the top of each More fold (the old form drew two of its own).
    assert SRC.count('data-testid="ws-template-admin-only-note"') == 1 and SRC.count("{WT_ADMIN_ONLY_NOTE}") == 1
    assert SRC.count("{adminNote}") == 2


@pytest.mark.parametrize(("provider", "fold"), [("k3s", "wt-k8s-more"), ("dock", "wt-docker-more")])
def test_an_admin_reads_the_note_at_the_top_of_each_more_fold(ctx, provider: str, fold: str) -> None:
    _create(ctx, provider, "admin")
    _open(ctx, fold)
    assert js(ctx, "testids(" + json.dumps(fold) + ").slice(0, 3)") == [fold, fold + ":toggle", "ws-template-admin-only-note"]
    assert ctx.eval("textOf(MR.find('ws-template-admin-only-note'))") == js(ctx, "WT_ADMIN_ONLY_NOTE")
    assert js(ctx, "MR.findAll('ws-template-admin-only-note').length") == 1
    _create(ctx, provider, "user")
    _open(ctx, fold)
    assert ctx.eval("MR.find('ws-template-admin-only-note') === null"), "a user, who cannot set them, reads Admin only on each control instead"


def test_each_kubernetes_admin_only_control_says_admin_only(ctx) -> None:
    _create(ctx, "k3s", "user")
    assert js(ctx, "helpOf('wt-service-account')") == "Admin only"
    assert js(ctx, "helpOf('wt-gpu-resource')") == "Admin only", "a GPU resource other than nvidia.com/gpu is the admin's (lead ruling 15)"
    assert js(ctx, "helpOf('wt-tolerations')") == "Admin only"
    _open(ctx, "wt-k8s-more")
    assert js(ctx, "helpOf('wt-extra-volumes')") == "Admin only. A JSON list of emptyDir or configMap volumes only."
    assert js(ctx, "helpOf('wt-extra-volume-mounts')") == "Admin only. A JSON list of name, mountPath, readOnly, subPath."
    for testid in ("wt-pod-share-pid", "wt-pod-dns", "wt-pod-restart", "wt-pod-grace"):
        assert js(ctx, f"helpOf('{testid}')") == "Admin only", testid
    assert js(ctx, "helpOf('wt-command')") != "Admin only", "the command is the user's own"


def test_the_docker_extra_mounts_say_admin_only(ctx) -> None:
    _create(ctx, "dock", "user")
    _open(ctx, "wt-docker-more")
    assert js(ctx, "helpOf('wt-extra-mounts')") == "Admin only"
    assert js(ctx, "helpOf('wt-user')") == "", "the run-as user is not an admin-only setting"


def test_a_secret_file_source_is_offered_to_admins_only(ctx) -> None:
    """The old form's files hint said "secret sources are admin only"; the overlay offers the Secret source to an admin only (and shows it on a row that
    already holds one), so a user is never offered a source the server would refuse."""
    _create(ctx, "loc", "user")
    ctx.eval("MR.click('wt-files-add');")
    assert js(ctx, "sourceLabels('wt-files-source:0')") == ["Text", "URL", "Document"]
    _create(ctx, "loc", "admin")
    ctx.eval("MR.click('wt-files-add');")
    assert js(ctx, "sourceLabels('wt-files-source:0')") == ["Text", "URL", "Document", "Secret"]


def test_the_kubernetes_hints_name_the_allowlist(ctx) -> None:
    """The volume hints name what the server's allowlist takes, and the four pod settings are the four keys of ``_POD_OVERRIDES_ALLOWED``, by name."""
    assert "emptyDir or configMap volumes only" in SRC
    assert "A JSON list of " + ", ".join(_VOLUME_MOUNT_ALLOWED) + "." in SRC
    _create(ctx, "k3s", "admin")
    _open(ctx, "wt-k8s-more")
    labels = [js(ctx, f"labelOf('{t}')") for t in ("wt-pod-share-pid", "wt-pod-dns", "wt-pod-restart", "wt-pod-grace")]
    assert labels == ["Share process namespace", "DNS policy", "Restart policy", "Termination grace period (s)"]
    ctx.eval("MR.click('wt-pod-share-pid:off'); setValue('wt-pod-dns', 'ClusterFirst'); MR.click('wt-pod-restart:Never'); setValue('wt-pod-grace', '30');")
    ctx.eval("setValue('ws-template-description', 'd'); setValue('ws-template-image', 'rt:1.4'); press('wt-save');")
    settle(ctx)
    post = [c for c in js(ctx, "CALLS") if c["method"] == "POST"]
    assert post and post[0]["body"]["backend"]["pod_overrides"] == {
        "shareProcessNamespace": False, "dnsPolicy": "ClusterFirst", "restartPolicy": "Never", "terminationGracePeriodSeconds": 30,
    }
    assert set(post[0]["body"]["backend"]["pod_overrides"]) == set(_POD_OVERRIDES_ALLOWED)


def test_the_never_applied_security_context_is_not_shown_and_kept_as_stored(ctx) -> None:
    """The old form showed ``container_security_context_overrides`` with "any non-empty value is refused". The overlay never shows a field the backend
    ignores (plan 08 decision 3): an edit carries it as stored."""
    assert "container_security_context_overrides" not in SRC[SRC.index("function WT_TemplateOverlay("):SRC.index("window.WT_TemplateOverlay = WT_TemplateOverlay;")]
    row = {**FULL_K8S, "backend": {**FULL_K8S["backend"], "container_security_context_overrides": {}}}
    ctx.eval(f"ROWS[{json.dumps('/workspace_templates/' + row['id'])}] = {json.dumps(row)};")
    ctx.eval("ROLE = 'admin'; MR.mount(window.WT_TemplateOverlay, { row: " + json.dumps(row) + ", onClose: onClose, onSaved: onSaved });")
    ctx.eval("setValue('ws-template-description', 'changed'); press('wt-save');")
    settle(ctx)
    put = [c for c in js(ctx, "CALLS") if c["method"] == "PUT"]
    assert put[0]["body"]["backend"]["container_security_context_overrides"] == {}


def test_a_user_opens_a_held_template_read_only_with_the_reason(ctx) -> None:
    ctx.eval("ROLE = 'user'; MR.mount(window.WT_TemplateOverlay, { row: " + json.dumps(HELD) + ", onClose: onClose, onSaved: onSaved });")
    assert ctx.eval("textOf(MR.find('wt-readonly-note'))") == (
        "This template holds admin-only settings (backend.tolerations). Only an admin can change or delete it."
    )
    assert ctx.eval("MR.find('wt-save') === null")


def test_bundle_transpiles_with_the_notes() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    _etag, body = build_jsx_bundle(UI)
    assert "WT_ADMIN_ONLY_NOTE" in body.decode("utf-8")
