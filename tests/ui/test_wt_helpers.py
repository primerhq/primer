"""The pure helpers of the Workspace templates page and its overlay (platform revamp Plan 08, Task 9; design 14.2).

``ui/components/workspaces/templates.jsx`` opens with a block of plain functions, between the markers ``// ---- WT pure helpers: ...`` and
``// ---- end WT pure helpers ----``: no JSX and no React, so this file runs the block itself in MiniRacer, next to the real ``ui/foundation/api.js``
(``ApiError`` and ``readRefusal``). What they decide:

* the units: memory and disk are GiB in the console and bytes or a Kubernetes quantity on the wire, and a stored value in other units is never rewritten
  unless the person edited that field (Review Focus 4);
* what a card and a summary line say about a template (its provider, image and size), per backend;
* which admin-only settings a template holds, by the server's own rule (``admin_only_settings_held``), so the console opens such a template read-only
  for a non-admin (Review Focus 5);
* the body a save sends: every field the form shows and the person changed, written onto a copy of the row it loaded, so an edit never drops a field the
  form did not show (Review Focus 1);
* where a refusal is shown: under the field its ``loc`` names, else in one line;
* every template, past the 200 rows of one page (Review Focus 3).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from primer.bootstrap.defaults import RESERVED_WORKSPACE_TEMPLATES
from primer.model.workspace import WorkspaceTemplate
from primer.workspace import template_privilege
from primer.workspace.template_privilege import admin_only_settings_held
from tests.ui._wt_envelopes import envelope

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = (ROOT / "ui" / "components" / "workspaces" / "templates.jsx").read_text(encoding="utf-8")
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")
START = "// ---- WT pure helpers: no JSX, no React (tests/ui/test_wt_helpers.py runs them in MiniRacer) ----"
END = "// ---- end WT pure helpers ----"

GIB = 2**30
MASK = "**********"

# Every model field of a Kubernetes template, the ignored ones included, masked secrets as the server serves them, and a key from the future.
FULL_K8S = {
    "id": "k8s-full", "description": "Every field", "provider_id": "k3s", "x_future": 1,
    "created_at": "2026-10-18T10:00:00Z", "updated_at": "2026-10-19T11:00:00Z",
    "backend": {
        "kind": "kubernetes", "image": "rt:1.4", "entrypoint": ["python", "-m", "primer_runtime.server"], "args": ["--verbose"], "workdir": "/work",
        "cpu_request": "1", "cpu_limit": "2", "memory_request": "1Gi", "memory_limit": "2Gi",
        "pvc_size": "20Gi", "pvc_access_modes": ["ReadWriteMany"], "storage_class": "local-path",
        "extra_volumes": [{"name": "cache", "emptyDir": {}}], "extra_volume_mounts": [{"name": "cache", "mountPath": "/cache", "readOnly": None}],
        "network_policy_name": "np-1", "pod_overrides": {"dnsPolicy": "Default"}, "container_security_context_overrides": None,
        "node_selector": {"accelerator": "gpu"},
        "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "gpu", "effect": "NoSchedule"}],
        "gpu": {"count": 2, "resource_name": "nvidia.com/gpu"}, "service_account_name": "runner", "image_pull_policy": "Always",
        "x_future": 1,
    },
    "files": [
        {"path": "a.txt", "source": {"kind": "inline", "content": "hello"}, "mode": "0644"},
        {"path": "b.bin", "source": {"kind": "url", "url": f"https://u:{MASK}@files.example/x", "sha256": "ab" * 32}, "mode": None},
    ],
    "env": {"TOKEN": MASK, "OTHER": MASK},
    "init_commands": ["uv sync", "git config --global user.name primer"],
    "state_path": ".st", "tmp_path": ".tp", "strict_write_locking": True,
    "resources": {"cpu_cores": 2.5, "memory_bytes": 3 * GIB // 2, "network": "full", "disk_bytes": 123},
}

FULL_DOCKER = {
    "id": "docker-full", "description": "Every Docker field", "provider_id": "dock", "x_future": 1,
    "backend": {
        "kind": "container", "image": "primer/workspace-runtime:1.1", "entrypoint": ["ignored"], "user": "1000:1000", "workdir": "/workspace",
        "cpu_cores": 1.5, "memory_bytes": 2 * GIB, "extra_mounts": [{"host": "/srv/data", "container": "/data", "readonly": True}],
        "network": {"egress": "deny_all"}, "x_future": 1,
    },
    "files": [{"path": "s.txt", "source": {"kind": "secret", "name": "PRIMER_SECRET_X"}, "mode": "0600"}],
    "env": {"A": MASK},
    "init_commands": [], "state_path": ".state", "tmp_path": ".tmp", "strict_write_locking": False,
    "resources": {"cpu_cores": None, "memory_bytes": None, "network": "egress", "disk_bytes": None},
}

LOCAL_ROW = {
    "id": "local-dev", "description": "Local", "provider_id": "local", "backend": {"kind": "local"},
    "files": [{"path": "d.md", "source": {"kind": "document", "collection_id": "c1", "document_id": "d1"}, "mode": None}],
    "env": {}, "init_commands": ["echo hi"], "state_path": ".state", "tmp_path": ".tmp", "strict_write_locking": False,
    "resources": {"cpu_cores": None, "memory_bytes": None, "network": "egress", "disk_bytes": None},
}

CHOICES = {
    "loc": {"id": "loc", "kind": "local", "label": "Local", "default_image": None},
    "dock": {"id": "dock", "kind": "container", "label": "Docker", "default_image": "primer/workspace-runtime:1.1"},
    "k3s": {"id": "k3s", "kind": "kubernetes", "label": "Kubernetes", "default_image": None},
}

# The body paths each form key writes, per kind (what an edit of that key may change).
PATHS = {
    "description": {"kubernetes": [["description"]], "container": [["description"]], "local": [["description"]]},
    "cpu": {"kubernetes": [["resources", "cpu_cores"]], "container": [["backend", "cpu_cores"]], "local": []},
    "envRows": {"kubernetes": [["env"]], "container": [["env"]], "local": [["env"]]},
}

_OPEN: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _block() -> str:
    return TEMPLATES[TEMPLATES.index(START):TEMPLATES.index(END)]


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN.append(ctx)
    ctx.eval("var window = globalThis;")
    ctx.eval(API)
    ctx.eval(_block())
    return ctx


def _js(expr: str, ctx=None):
    ctx = ctx or _ctx()
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _call(name: str, *args):
    return _js(f"{name}({', '.join(json.dumps(a) for a in args)})")


def _edit(row: dict, key: str, value, ctx=None) -> dict:
    """Open ``row`` in the form, change one key, and build the body a save sends."""
    return _js(
        "(function () { var row = " + json.dumps(row) + "; var form = WT_formFromTemplate(row);"
        " var initial = JSON.parse(JSON.stringify(form)); form[" + json.dumps(key) + "] = " + json.dumps(value) + ";"
        " return WT_templateBody(row, form, initial); })()",
        ctx,
    )


def _without(obj: dict, key: str, row: dict) -> dict:
    out = copy.deepcopy(obj)
    for path in PATHS[key][row["backend"]["kind"]]:
        node = out
        for part in path[:-1]:
            node = node.get(part, {})
        node.pop(path[-1], None)
    return out


# ---- units ------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("q,gib", [("10Gi", 10), ("500Mi", 0.48828125), ("1Ti", 1024), ("10G", 9.313225746154785),
                                   ("10737418240", 10), ("1Ei", 2**30), ("bogus", None), ("", None), ("10 Gi", None)])
def test_quantities_parse_to_gib(q, gib):
    assert _js(f"WT_quantityToGiB({json.dumps(q)})") == gib


@pytest.mark.parametrize("gib,text", [(10, "10"), (0.48828125, "0.49"), (1.5, "1.5"), (9.313225746154785, "9.31"), (2.0, "2")])
def test_gib_shows_at_most_two_decimals(gib, text):
    assert _call("WT_formatGiB", gib) == text


@pytest.mark.parametrize("text,size", [("4", 4 * GIB), ("1.5", 3 * GIB // 2), (" 2 ", 2 * GIB), ("0", None), ("-1", None), ("abc", None),
                                       ("", None), ("1e3", None), ("0x10", None), ("0.0000000001", None)])
def test_gib_text_becomes_whole_bytes_or_nothing(text, size):
    assert _call("WT_giBToBytes", text) == size


@pytest.mark.parametrize("text,quantity", [("10", "10Gi"), ("0.5", "512Mi"), ("1.5", "1536Mi"), ("0.3", "322122547"), ("", None), ("0", None)])
def test_a_disk_in_gib_becomes_a_quantity_the_server_takes(text, quantity):
    assert _call("WT_diskQuantity", text) == quantity


# ---- what a card says ---------------------------------------------------------------------------------------------------------------------------------


def test_summary_line_matches_the_mockup():
    t = {"id": "python-dev", "description": "Python 3.12 with uv", "provider_id": "k3s",
         "backend": {"kind": "kubernetes", "image": "rt:1.4", "pvc_size": "10Gi"}, "resources": {"cpu_cores": 2, "memory_bytes": 4294967296}}
    assert _call("WT_summaryLine", t) == "Python 3.12 with uv · Kubernetes on k3s · 2 CPU · 4 GiB · 10 GiB disk"
    assert _call("WT_cardFacts", t) == [["Provider", "k3s · Kubernetes"], ["Image", "rt:1.4"], ["Size", "2 CPU · 4 GiB · 10 GiB disk"]]


def test_docker_and_local_sizes_come_from_their_own_fields():
    docker = {"id": "d", "description": "Docker dev", "provider_id": "dock",
              "backend": {"kind": "container", "image": "img:1", "cpu_cores": 1.5, "memory_bytes": 2147483648},
              "resources": {"cpu_cores": 64, "memory_bytes": 64 * GIB}}
    assert _call("WT_summaryLine", docker, CHOICES["dock"]) == "Docker dev · Docker on dock · 1.5 CPU · 2 GiB"
    assert _call("WT_cardFacts", docker) == [["Provider", "dock · Docker"], ["Image", "img:1"], ["Size", "1.5 CPU · 2 GiB"]]
    local = {"id": "l", "description": "Local dev", "provider_id": "local", "backend": {"kind": "local"}, "resources": {"cpu_cores": 4}}
    assert _call("WT_summaryLine", local).endswith("Local on local")
    assert _call("WT_cardFacts", local) == [["Provider", "local · Local"]]


def test_the_label_comes_from_the_choice_when_there_is_one():
    podman = {"id": "p", "provider_id": "pod", "backend": {"kind": "container", "image": "img"}}
    assert _call("WT_cardFacts", podman, {"id": "pod", "kind": "container", "label": "Podman"})[0] == ["Provider", "pod · Podman"]


def test_a_disk_that_is_not_a_quantity_is_shown_as_stored():
    t = {"id": "old", "provider_id": "k3s", "backend": {"kind": "kubernetes", "image": "i", "pvc_size": "10 Gi"}, "resources": {}}
    assert _call("WT_sizeText", _call("WT_templateSize", t)) == "10 Gi disk"


# ---- the server's rules, mirrored ---------------------------------------------------------------------------------------------------------------------

HELD_ROWS = [
    {"id": "h1", "description": "held", "provider_id": "dock", "backend": {"kind": "container", "extra_mounts": [{"host": "/h", "container": "/c"}]}},
    {"id": "h2", "description": "held", "provider_id": "k3s", "backend": {"kind": "kubernetes", "image": "i", "service_account_name": "runner",
                                                   "tolerations": [{"key": "a", "operator": "Exists"}]}},
    {"id": "h3", "description": "held", "provider_id": "k3s", "backend": {"kind": "kubernetes", "image": "i", "pod_overrides": {"dnsPolicy": "Default"},
                                                   "extra_volumes": [{"name": "v", "emptyDir": {}}], "extra_volume_mounts": [{"name": "v", "mountPath": "/v"}]}},
    {"id": "h4", "description": "held", "provider_id": "loc", "backend": {"kind": "local"}, "files": [{"path": "s", "source": {"kind": "secret", "name": "X"}}]},
    {"id": "h5", "description": "held", "provider_id": "k3s", "backend": {"kind": "kubernetes", "image": "i", "gpu": {"count": 1, "resource_name": "amd.com/gpu"}},
     "files": [{"path": "s", "source": {"kind": "secret", "name": "X"}}]},
    {"id": "h6", "description": "held", "provider_id": "k3s", "backend": {"kind": "kubernetes", "image": "i", "gpu": {"count": 1}, "pod_overrides": {},
                                                   "container_security_context_overrides": {}, "service_account_name": None}},
]


@pytest.mark.parametrize("row", HELD_ROWS, ids=[r["id"] for r in HELD_ROWS])
def test_held_admin_fields_mirror_the_server(row):
    served = WorkspaceTemplate.model_validate(row).model_dump(mode="json")
    assert _call("WT_heldAdminFields", served) == admin_only_settings_held(WorkspaceTemplate.model_validate(row))


def test_held_rows_cover_every_admin_only_field():
    held = set()
    for row in HELD_ROWS:
        held.update(admin_only_settings_held(WorkspaceTemplate.model_validate(row)))
    assert held == {"backend.extra_mounts", "files.secret", "backend.gpu.resource_name"} | {f"backend.{f}" for f in template_privilege._K8S_FIELDS} - {
        "backend.container_security_context_overrides"}


def test_k8s_admin_fields_match_the_server():
    assert _js("WT_K8S_ADMIN_FIELDS") == list(template_privilege._K8S_FIELDS)


def test_reserved_ids_match_the_bootstrap():
    assert _js("WT_RESERVED_IDS") == sorted(RESERVED_WORKSPACE_TEMPLATES)
    assert _call("WT_isReserved", {"id": "local-default"}) is True and _call("WT_isReserved", {"id": "mine"}) is False


def test_the_names_the_page_and_the_dialog_share():
    assert _js("WT_LIST_KEY") == "ws:templates"
    assert _js("WT_TEMPLATE_KEYS") == ["ws:templates", "nv-ov:workspace-templates"]
    assert _js("WT_K8S_DEFAULT_COMMAND") == ["python", "-m", "primer_runtime.server"]
    assert _js("WT_KIND_LABELS") == {"local": "Local", "container": "Docker", "kubernetes": "Kubernetes"}
    assert set(_js("WT_KIND_ICONS")) == {"local", "container", "kubernetes"}
    assert _js("WT_GIB") == GIB and _js("WT_MAX_PAGES") == 100


# ---- the form ---------------------------------------------------------------------------------------------------------------------------------------


def test_an_empty_form_is_prefilled_for_its_provider():
    k8s = _call("WT_emptyForm", CHOICES["k3s"])
    assert (k8s["provider_id"], k8s["kind"], k8s["image"], k8s["command"], k8s["diskGiB"]) == ("k3s", "kubernetes", "", "python\n-m\nprimer_runtime.server", "10")
    docker = _call("WT_emptyForm", CHOICES["dock"])
    assert (docker["kind"], docker["image"], docker["command"]) == ("container", "primer/workspace-runtime:1.1", "")


def test_a_new_provider_replaces_only_the_prefills_nobody_touched():
    out = _js("(function () { var f = WT_emptyForm(" + json.dumps(CHOICES["dock"]) + "); return WT_formForChoice(f, " + json.dumps(CHOICES["k3s"]) + ", "
              + json.dumps(CHOICES["dock"]) + "); })()")
    assert (out["provider_id"], out["kind"], out["image"], out["command"]) == ("k3s", "kubernetes", "", "python\n-m\nprimer_runtime.server")
    typed = _js("(function () { var f = WT_emptyForm(" + json.dumps(CHOICES["dock"]) + "); f.image = 'mine:1'; f.description = 'kept';"
                " return WT_formForChoice(f, " + json.dumps(CHOICES["k3s"]) + ", " + json.dumps(CHOICES["dock"]) + "); })()")
    assert (typed["image"], typed["description"]) == ("mine:1", "kept")


def test_the_form_reads_every_shown_field_from_the_row():
    form = _call("WT_formFromTemplate", FULL_K8S)
    assert form["image"] == "rt:1.4" and form["cpu"] == "2.5" and form["memoryGiB"] == "1.5" and form["diskGiB"] == "20"
    assert form["diskAccess"] == "many" and form["storageClass"] == "local-path"
    assert form["nodeSelectorRows"] == [{"key": "accelerator", "value": "gpu"}]
    assert form["tolerationRows"] == [{"key": "dedicated", "operator": "Equal", "value": "gpu", "effect": "NoSchedule"}]
    assert (form["gpuCount"], form["gpuResource"], form["serviceAccount"], form["pullPolicy"]) == ("2", "nvidia.com/gpu", "runner", "Always")
    assert form["command"] == "python\n-m\nprimer_runtime.server" and form["args"] == "--verbose" and form["workdir"] == "/work"
    assert json.loads(form["extraVolumesJson"]) == FULL_K8S["backend"]["extra_volumes"]
    assert (form["podDns"], form["podSharePid"], form["podRestart"], form["podGrace"]) == ("Default", "", "", "")
    assert form["envRows"] == [{"key": "TOKEN", "value": MASK}, {"key": "OTHER", "value": MASK}]
    assert form["init"] == "uv sync\ngit config --global user.name primer"
    assert (form["statePath"], form["tmpPath"], form["strict"]) == (".st", ".tp", "on")
    docker = _call("WT_formFromTemplate", FULL_DOCKER)
    assert (docker["cpu"], docker["memoryGiB"], docker["user"]) == ("1.5", "2", "1000:1000")
    assert docker["extraMountRows"] == [{"host": "/srv/data", "container": "/data", "readonly": "yes"}]


@pytest.mark.parametrize("row", [FULL_K8S, FULL_DOCKER, LOCAL_ROW], ids=["kubernetes", "docker", "local"])
@pytest.mark.parametrize("key,value", [("description", "changed"), ("cpu", "3"), ("envRows", [{"key": "NEW", "value": "v"}])])
def test_an_edit_of_one_field_keeps_every_other_field(row, key, value):
    """Review Focus 1: the ignored fields, the masked secrets, the unknown key and every unshown value come back as they were served."""
    body = _edit(row, key, value)
    assert _without(body, key, row) == _without(row, key, row)


def test_an_edit_that_changes_nothing_sends_the_row_back():
    for row in (FULL_K8S, FULL_DOCKER, LOCAL_ROW):
        assert _edit(row, "description", row["description"]) == row


def test_an_untouched_disk_keeps_its_stored_quantity():
    row = {**FULL_K8S, "backend": {**FULL_K8S["backend"], "pvc_size": "500Mi"}}
    assert _call("WT_formFromTemplate", row)["diskGiB"] == "0.49"
    assert _edit(row, "cpu", "3")["backend"]["pvc_size"] == "500Mi"


def test_an_untouched_memory_in_other_units_is_kept():
    row = {**FULL_K8S, "resources": {**FULL_K8S["resources"], "memory_bytes": 1000000000}}
    assert _call("WT_formFromTemplate", row)["memoryGiB"] == "0.93"
    assert _edit(row, "description", "x")["resources"]["memory_bytes"] == 1000000000


WRITES = [
    # (row, key, value, path, written)
    (FULL_K8S, "image", "img:2", ["backend", "image"], "img:2"),
    (FULL_K8S, "cpu", "", ["resources", "cpu_cores"], None),
    (FULL_K8S, "memoryGiB", "2", ["resources", "memory_bytes"], 2 * GIB),
    (FULL_K8S, "diskGiB", "0.5", ["backend", "pvc_size"], "512Mi"),
    (FULL_K8S, "storageClass", "", ["backend", "storage_class"], None),
    (FULL_K8S, "diskAccess", "one", ["backend", "pvc_access_modes"], ["ReadWriteOnce"]),
    (FULL_K8S, "nodeSelectorRows", [{"key": " zone ", "value": "a"}, {"key": "", "value": "dropped"}], ["backend", "node_selector"], {"zone": "a"}),
    (FULL_K8S, "tolerationRows", [{"key": "", "operator": "Exists", "value": "", "effect": "Any"}, {"key": "", "operator": "Equal", "value": "", "effect": "Any"}],
     ["backend", "tolerations"], [{"key": None, "operator": "Exists", "value": None, "effect": None}]),
    (FULL_K8S, "gpuCount", "", ["backend", "gpu"], None),
    (FULL_K8S, "gpuCount", "3", ["backend", "gpu"], {"count": 3, "resource_name": "nvidia.com/gpu"}),
    (FULL_K8S, "gpuResource", "", ["backend", "gpu"], {"count": 2, "resource_name": "nvidia.com/gpu"}),
    (FULL_K8S, "serviceAccount", "", ["backend", "service_account_name"], None),
    (FULL_K8S, "pullPolicy", "", ["backend", "image_pull_policy"], None),
    (FULL_K8S, "command", "bash\n -c \n\n", ["backend", "entrypoint"], ["bash", "-c"]),
    (FULL_K8S, "args", "", ["backend", "args"], None),
    (FULL_K8S, "workdir", " ", ["backend", "workdir"], "/workspace"),
    (FULL_K8S, "extraVolumesJson", "", ["backend", "extra_volumes"], []),
    (FULL_K8S, "extraVolumeMountsJson", '[{"name": "v", "mountPath": "/v"}]', ["backend", "extra_volume_mounts"], [{"name": "v", "mountPath": "/v"}]),
    (FULL_K8S, "podDns", "", ["backend", "pod_overrides"], None),
    (FULL_K8S, "podSharePid", "off", ["backend", "pod_overrides"], {"dnsPolicy": "Default", "shareProcessNamespace": False}),
    (FULL_K8S, "podGrace", "30", ["backend", "pod_overrides"], {"dnsPolicy": "Default", "terminationGracePeriodSeconds": 30}),
    (FULL_K8S, "podRestart", "Never", ["backend", "pod_overrides"], {"dnsPolicy": "Default", "restartPolicy": "Never"}),
    (FULL_DOCKER, "cpu", "2", ["backend", "cpu_cores"], 2),
    (FULL_DOCKER, "memoryGiB", "", ["backend", "memory_bytes"], None),
    (FULL_DOCKER, "user", "", ["backend", "user"], None),
    (FULL_DOCKER, "extraMountRows", [{"host": "/h", "container": "/c", "readonly": "no"}, {"host": "", "container": "", "readonly": "no"}],
     ["backend", "extra_mounts"], [{"host": "/h", "container": "/c", "readonly": False}]),
    (LOCAL_ROW, "files", [{"path": "", "source": {"kind": "inline", "content": "x"}}, {"path": "k.txt", "source": {"kind": "inline", "content": "k"}, "mode": None}],
     ["files"], [{"path": "k.txt", "source": {"kind": "inline", "content": "k"}, "mode": None}]),
    (LOCAL_ROW, "envRows", [{"key": "A", "value": MASK}, {"key": " ", "value": "x"}], ["env"], {"A": MASK}),
    (LOCAL_ROW, "init", "a\n\n  b  \n", ["init_commands"], ["a", "b"]),
    (LOCAL_ROW, "statePath", "", ["state_path"], ".state"),
    (LOCAL_ROW, "tmpPath", "t", ["tmp_path"], "t"),
    (LOCAL_ROW, "strict", "on", ["strict_write_locking"], True),
    (LOCAL_ROW, "description", "  spaced  ", ["description"], "spaced"),
]


@pytest.mark.parametrize("row,key,value,path,written", WRITES, ids=[f"{w[0]['backend']['kind']}-{w[1]}-{i}" for i, w in enumerate(WRITES)])
def test_writing_each_field_lands_on_its_path(row, key, value, path, written):
    body = _edit(row, key, value)
    node = body
    for part in path:
        node = node[part]
    assert node == written


def test_a_cleared_disk_leaves_the_servers_default():
    assert "pvc_size" not in _edit(FULL_K8S, "diskGiB", "")["backend"]


def test_a_key_of_another_kind_is_never_written():
    assert _edit(LOCAL_ROW, "cpu", "3") == LOCAL_ROW
    assert _edit(FULL_DOCKER, "diskGiB", "50") == FULL_DOCKER


def test_create_writes_only_the_active_kind():
    body = _js(
        "(function () { var f = WT_emptyForm(" + json.dumps(CHOICES["dock"]) + "); f.description = 'Docker dev'; f.diskGiB = '20'; f.command = 'x';"
        " return WT_templateBody(null, f, null); })()"
    )
    assert body["provider_id"] == "dock" and body["description"] == "Docker dev" and "id" not in body
    assert body["backend"]["kind"] == "container" and body["backend"]["image"] == "primer/workspace-runtime:1.1"
    assert "pvc_size" not in body["backend"] and "entrypoint" not in body["backend"] and "resources" not in body
    WorkspaceTemplate.model_validate({**body, "id": "check"})


def test_create_of_each_kind_is_a_body_the_model_takes():
    for choice in CHOICES.values():
        body = _js(
            "(function () { var f = WT_emptyForm(" + json.dumps(choice) + "); f.id = ' mine '; f.description = 'd'; f.image = f.image || 'rt:1';"
            " return WT_templateBody(null, f, null); })()"
        )
        assert body["id"] == "mine" and body["backend"]["kind"] == choice["kind"]
        WorkspaceTemplate.model_validate(body)
    k8s = _js("(function () { var f = WT_emptyForm(" + json.dumps(CHOICES["k3s"]) + "); f.description = 'd'; f.image = 'rt'; return WT_templateBody(null, f, null); })()")
    assert k8s["backend"]["entrypoint"] == ["python", "-m", "primer_runtime.server"] and k8s["backend"]["pvc_size"] == "10Gi"
    assert set(k8s) == {"provider_id", "backend", "description", "files", "env", "init_commands", "state_path", "tmp_path", "strict_write_locking", "resources"}


# ---- validation -----------------------------------------------------------------------------------------------------------------------------------------


def _errors(form_changes: dict, choice: str = "k3s", is_admin: bool = True) -> dict:
    return _js(
        "(function () { var f = WT_emptyForm(" + json.dumps(CHOICES[choice]) + "); f.description = 'd'; f.image = f.image || 'rt';"
        " Object.assign(f, " + json.dumps(form_changes) + "); return WT_validateForm(f, { isAdmin: " + json.dumps(is_admin) + " }); })()"
    )


def test_a_valid_form_has_no_errors():
    assert _errors({}) == {} and _errors({}, "dock") == {} and _errors({}, "loc") == {}


@pytest.mark.parametrize("changes,errors", [
    ({"description": "  "}, {"description": "Enter a description"}),
    ({"image": ""}, {"image": "Enter an image"}),
    ({"cpu": "0"}, {"cpu": "Enter a positive number"}),
    ({"cpu": "2048"}, {"cpu": "At most 1024 CPU cores"}),
    ({"memoryGiB": "lots"}, {"memoryGiB": "Enter a positive number"}),
    ({"memoryGiB": "70000"}, {"memoryGiB": "At most 65536 GiB (64 TiB)"}),
    ({"diskGiB": "-1"}, {"diskGiB": "Enter a positive number"}),
    ({"diskGiB": "10 Gi"}, {"diskGiB": "Enter a positive number"}),
    ({"gpuCount": "1.5"}, {"gpuCount": "Enter a whole number of at least 1"}),
    ({"gpuCount": "0"}, {"gpuCount": "Enter a whole number of at least 1"}),
    ({"gpuCount": "65"}, {"gpuCount": "At most 64 GPUs"}),
    ({"extraVolumesJson": '{"name": "v"}'}, {"extraVolumesJson": "Enter a JSON list"}),
    ({"extraVolumeMountsJson": "[nope"}, {"extraVolumeMountsJson": "Enter a JSON list"}),
    ({"podGrace": "-3"}, {"podGrace": "Enter a whole number of seconds"}),
    ({"files": [{"path": "a", "source": {"kind": "inline", "content": ""}, "mode": "999"}]}, {"files": "Use an octal mode such as 0644"}),
    ({"envRows": [{"key": "A", "value": "1"}, {"key": " A ", "value": "2"}]}, {"envRows": "Each name once"}),
    ({"nodeSelectorRows": [{"key": "z", "value": "1"}, {"key": "z", "value": "2"}]}, {"nodeSelectorRows": "Each name once"}),
])
def test_the_form_errors(changes, errors):
    assert _errors(changes) == errors


def test_a_docker_image_is_required_too_and_sizes_are_checked_there():
    assert _errors({"image": " ", "cpu": "-2"}, "dock") == {"image": "Enter an image", "cpu": "Enter a positive number"}


def test_a_local_form_checks_no_size():
    assert _errors({"cpu": "nope", "memoryGiB": "nope"}, "loc") == {}


def test_only_an_admin_names_another_gpu_resource():
    assert _errors({"gpuCount": "1", "gpuResource": "amd.com/gpu"}) == {}
    assert _errors({"gpuCount": "1", "gpuResource": "amd.com/gpu"}, is_admin=False) == {"gpuResource": "Only an admin may name another GPU resource"}
    assert _errors({"gpuCount": "1", "gpuResource": "nvidia.com/gpu"}, is_admin=False) == {}


# ---- refusals -----------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("loc,key", [
    (["body", "description"], "description"),
    (["body", "backend", "kubernetes", "node_selector"], "nodeSelectorRows"),
    (["body", "backend", "kubernetes", "tolerations", 0, "key"], "tolerationRows"),
    (["body", "backend", "kubernetes", "gpu", "resource_name"], "gpuResource"),
    (["body", "backend", "kubernetes", "gpu", "count"], "gpuCount"),
    (["body", "backend", "kubernetes", "pvc_size"], "diskGiB"),
    (["body", "backend", "container", "cpu_cores"], "cpu"),
    (["body", "resources", "memory_bytes"], "memoryGiB"),
    (["body", "files", 1, "source", "url", "url"], "files"),
    (["body", "env", "A"], "envRows"),
    (["body", "provider_id"], None),
    (["body"], None),
    (["query", "limit"], None),
])
def test_a_loc_names_its_form_key(loc, key):
    assert _call("WT_fieldKeyForLoc", loc) == key


def _split(env: dict, key_for_loc: str = "WT_fieldKeyForLoc", opts: dict | None = None) -> dict:
    return _js("WT_splitErrors(new window.primerApi.ApiError(" + json.dumps(env) + "), " + key_for_loc + ", " + json.dumps(opts or {}) + ")")


K8S_BODY = {"id": "t-x", "description": "d", "provider_id": "k3s", "backend": {"kind": "kubernetes", "image": "rt"}}


def test_server_errors_split_into_fields_and_a_line():
    status, env = envelope("POST", "/v1/workspace_templates", {**K8S_BODY, "backend": {**K8S_BODY["backend"], "node_selector": {"bad key": "x"}}})
    assert status == 422
    out = _split(env)
    assert list(out["fields"]) == ["nodeSelectorRows"] and out["general"] is None
    assert "'bad key' is not a valid Kubernetes label key" in out["fields"]["nodeSelectorRows"]
    status, env = envelope("POST", "/v1/workspace_templates", {**K8S_BODY, "backend": {**K8S_BODY["backend"], "tolerations": [{"operator": "Exists"}]}}, role="user")
    assert status == 403
    out = _split(env)
    assert out["fields"] == {} and "backend.tolerations" in out["general"]


def test_a_field_error_nobody_shows_goes_to_the_line_with_its_field():
    status, env = envelope("POST", "/v1/workspace_templates", {**K8S_BODY, "backend": {**K8S_BODY["backend"], "node_selector": {"bad key": "x"}}})
    assert status == 422
    message = env["extensions"]["errors"][0]["msg"]
    assert _split(env, "function () { return null; }") == {"fields": {}, "general": message}
    assert _split(env, "function () { return null; }", {"withPath": True}) == {"fields": {}, "general": "backend.node_selector: " + message}


# ---- every template -----------------------------------------------------------------------------------------------------------------------------------


def _fetch_all(total: int | None = None, always_full: bool = False):
    ctx = _ctx()
    ctx.eval(
        "var URLS = []; var OUT = null; var ERR = null;"
        "function fake(method, path, body, opts) { URLS.push(path);"
        "  var offset = Number(/offset=(\\d+)/.exec(path)[1]);"
        f"  var n = {json.dumps(always_full)} ? 200 : Math.max(0, Math.min(200, {total or 0} - offset));"
        "  var items = []; for (var i = 0; i < n; i++) items.push({ id: 't-' + (offset + i) });"
        "  return Promise.resolve({ items: items, total: null }); }"
        "WT_fetchAllTemplates(fake, null).then(function (r) { OUT = r; }, function (e) { ERR = String(e); });"
    )
    return _js("OUT", ctx), _js("URLS", ctx)


def test_fetch_all_templates_pages_past_200():
    out, urls = _fetch_all(total=450)
    assert len(out["items"]) == 450 and out["total"] == 450 and [u.split("offset=")[1].split("&")[0] for u in urls] == ["0", "200", "400"]
    assert all(u.startswith("/workspace_templates?") and "limit=200" in u and "order_by=id:asc" in u for u in urls)
    assert out["items"][-1]["id"] == "t-449"


def test_fetch_all_templates_stops_at_a_short_page():
    out, urls = _fetch_all(total=400)
    assert len(out["items"]) == 400 and len(urls) == 3, "two full pages, then an empty one says the list ended"


def test_fetch_all_templates_stops_at_the_guard():
    out, urls = _fetch_all(always_full=True)
    assert len(urls) == 100 and len(out["items"]) == 100 * 200
