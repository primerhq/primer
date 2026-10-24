"""The pure helpers of the new-workspace dialog (platform revamp Plan 08, Task 12; design 14.3).

``ui/components/console/nv-overlays.jsx`` holds a block of plain functions before ``NV_CreateWorkspaceOverlay``, between the markers
``// ---- NV_nw pure helpers ...`` and ``// ---- end NV_nw pure helpers ----``: no JSX and no React, so this file runs the block itself in MiniRacer, after
the real ``ui/foundation/api.js`` (``ApiError``, ``readRefusal``) and the template helpers it builds on (the ``WT_*`` block of
``ui/components/workspaces/templates.jsx``). What they decide:

* the id rule, which is the server's own (``WORKSPACE_ID_PATTERN``);
* which size fields each provider kind offers, and what the dialog pre-fills them with from the template;
* the body ``POST /v1/workspaces`` is sent: a size goes only when the person changed it and the provider can take it, so a pre-filled size nobody
  touched never rewrites the template's value in other units (Review Focus 4), and the body is one the server's own model takes;
* the dialog's own errors, checked only on what the person typed;
* where a refusal is shown: under the field its ``loc`` names when the dialog draws that field, else in the one line above the footer (Review Focus 2),
  read from the real router's envelopes (``tests/ui/_wt_envelopes.py``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from primer.api.routers.workspaces import WorkspaceCreateBody
from primer.model.workspace import WORKSPACE_ID_PATTERN
from tests.ui._wt_envelopes import envelope
from tests.ui.test_wt_helpers import API
from tests.ui.test_wt_helpers import _block as _wt_block

ROOT = Path(__file__).resolve().parents[2]
OVERLAYS = (ROOT / "ui" / "components" / "console" / "nv-overlays.jsx").read_text(encoding="utf-8")
START = "// ---- NV_nw pure helpers (tests/ui/test_nv_new_workspace_helpers.py) ----"
END = "// ---- end NV_nw pure helpers ----"

GIB = 2**30
ID_ERROR = "Use lowercase letters, digits and dashes, starting and ending with a letter or digit (at most 63)."

K8S_T = {
    "id": "tpl-k3s", "description": "Kubernetes", "provider_id": "k3s",
    "backend": {"kind": "kubernetes", "image": "rt:1.4", "pvc_size": "10Gi"},
    "resources": {"cpu_cores": 2, "memory_bytes": 4 * GIB},
}
DOCKER_T = {"id": "tpl-dock", "description": "Docker", "provider_id": "dock", "backend": {"kind": "container", "cpu_cores": 1, "memory_bytes": 2 * GIB}}
LOCAL_T = {"id": "tpl-loc", "description": "Local", "provider_id": "loc", "backend": {"kind": "local"}}

_OPEN: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _block() -> str:
    return OVERLAYS[OVERLAYS.index(START):OVERLAYS.index(END)]


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN.append(ctx)
    ctx.eval("var window = globalThis;")
    ctx.eval(API)
    ctx.eval(_wt_block())
    ctx.eval(_block())
    return ctx


def _js(expr: str, ctx=None):
    ctx = ctx or _ctx()
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _call(name: str, *args):
    return _js(f"{name}({', '.join(json.dumps(a) for a in args)})")


def _state(t: dict, size: dict | None = None, dirty=(), **rest) -> dict:
    """The dialog's state on template ``t``: the sizes it pre-fills, with ``size`` typed over them and ``dirty`` the fields the person changed."""
    sized = {**_call("NV_nwInitialSize", t), **(size or {})}
    state = {
        "templateId": t["id"], "kind": t["backend"]["kind"], "name": "", "id": "", "size": sized, "sizeDirty": {k: True for k in dirty},
        "envRows": [], "files": [], "init": "",
    }
    state.update(rest)
    return state


# ---- the id ---------------------------------------------------------------------------------------------------------------------------------------------


def test_the_id_rule_is_the_servers():
    assert _js("NV_NW_ID_PATTERN") == WORKSPACE_ID_PATTERN
    assert _call("NV_nwIdError", "good-id") is None and _call("NV_nwIdError", "") is None and _call("NV_nwIdError", "   ") is None
    for bad in ("Bad", "-x", "x-", "a" * 64, "a_b", "a.b", "a b"):
        assert _call("NV_nwIdError", bad) == ID_ERROR, bad


@pytest.mark.parametrize("text", ["a", "a1", "a-b", "0", "a" * 63, "a" * 64, "A", "-a", "a-", "a_b", "é", "a\nb"])
def test_the_id_check_agrees_with_the_server_on_every_sample(text):
    """The JavaScript regex and Python's fullmatch read the one pattern alike (a line break is not let through by a `$` that matches before it)."""
    assert (_call("NV_nwIdError", text) is None) == bool(re.fullmatch(WORKSPACE_ID_PATTERN, text)), text


# ---- the size the dialog offers -------------------------------------------------------------------------------------------------------------------------


def test_the_size_fields_by_kind():
    assert _call("NV_nwSizeFields", "kubernetes") == ["cpu", "memoryGiB", "diskGiB"]
    assert _call("NV_nwSizeFields", "container") == ["cpu", "memoryGiB"]
    assert _call("NV_nwSizeFields", "local") == []
    assert _call("NV_nwSizeFields", "something-new") == []


def test_the_dialog_prefills_the_templates_size():
    assert _call("NV_nwInitialSize", K8S_T) == {"cpu": "2", "memoryGiB": "4", "diskGiB": "10"}
    assert _call("NV_nwInitialSize", DOCKER_T) == {"cpu": "1", "memoryGiB": "2", "diskGiB": ""}
    assert _call("NV_nwInitialSize", LOCAL_T) == {"cpu": "", "memoryGiB": "", "diskGiB": ""}
    assert _call("NV_nwInitialSize", {**K8S_T, "resources": {}}) == {"cpu": "", "memoryGiB": "", "diskGiB": "10"}


# ---- the body -------------------------------------------------------------------------------------------------------------------------------------------


def test_untouched_prefilled_sizes_send_no_override():
    """Review Focus 4: a size shown in other units (500Mi as 0.49 GiB, 1.5 GiB) that nobody touched is not sent, so the template's own value stands."""
    t = {"id": "k", "provider_id": "k3s", "backend": {"kind": "kubernetes", "image": "rt", "pvc_size": "500Mi"},
         "resources": {"cpu_cores": 2, "memory_bytes": 1610612736}}
    state = _state(t)
    assert _call("NV_nwInitialSize", t) == {"cpu": "2", "memoryGiB": "1.5", "diskGiB": "0.49"}
    assert _call("NV_nwBody", state) == {"template_id": "k"}


def test_only_edited_sizes_are_sent():
    state = _state(K8S_T, size={"memoryGiB": "8"}, dirty=["memoryGiB"])
    assert _call("NV_nwBody", state)["overrides"] == {"resources": {"memory_bytes": 8 * GIB}}
    state = _state(K8S_T, size={"cpu": "0.5", "diskGiB": "20"}, dirty=["cpu", "diskGiB"])
    assert _call("NV_nwBody", state)["overrides"] == {"resources": {"cpu_cores": 0.5, "disk_bytes": 20 * GIB}}
    state = _state(DOCKER_T, size={"cpu": "3"}, dirty=["cpu"])
    assert _call("NV_nwBody", state) == {"template_id": "tpl-dock", "overrides": {"resources": {"cpu_cores": 3}}}


def test_a_size_the_provider_cannot_take_is_never_sent():
    assert "overrides" not in _call("NV_nwBody", _state(DOCKER_T, size={"diskGiB": "20"}, dirty=["diskGiB"]))
    assert "overrides" not in _call("NV_nwBody", _state(LOCAL_T, size={"cpu": "2"}, dirty=["cpu"]))


def test_a_cleared_size_is_not_sent():
    """Null means "the template's value" on the wire; an empty field sends nothing rather than a zero the server refuses."""
    assert "overrides" not in _call("NV_nwBody", _state(K8S_T, size={"cpu": ""}, dirty=["cpu"]))


def test_env_files_and_commands_map_to_the_overrides():
    files = [
        {"path": "hello.txt", "source": {"kind": "inline", "content": "hi"}, "mode": "0644"},
        {"path": "  ", "source": {"kind": "inline", "content": "no path: dropped"}, "mode": None},
        {"path": "b.bin", "source": {"kind": "url", "url": "https://files.example/b", "sha256": None}, "mode": None},
    ]
    state = _state(
        LOCAL_T, name="  my space ", id=" ws-x ",
        envRows=[{"key": " TOKEN ", "value": "x"}, {"key": "", "value": "dropped"}, {"key": "EMPTY", "value": ""}],
        files=files, init="  echo hi > init.txt \n\n  uv sync  \n",
    )
    assert _call("NV_nwBody", state) == {
        "template_id": "tpl-loc", "name": "my space", "id": "ws-x",
        "overrides": {"env": {"TOKEN": "x", "EMPTY": ""}, "files": [files[0], files[2]], "init_commands": ["echo hi > init.txt", "uv sync"]},
    }


def test_a_blank_name_and_id_are_left_out():
    assert _call("NV_nwBody", _state(LOCAL_T, name="   ", id="")) == {"template_id": "tpl-loc"}


def test_every_body_is_one_the_server_takes():
    """The server's own request model reads each body the helper builds, with nothing dropped on the way."""
    for state in (
        _state(K8S_T),
        _state(K8S_T, size={"memoryGiB": "1.5", "cpu": "0.25", "diskGiB": "0.5"}, dirty=["memoryGiB", "cpu", "diskGiB"], name="n", id="n-1",
               envRows=[{"key": "A", "value": "b"}], files=[{"path": "f", "source": {"kind": "inline", "content": "c"}, "mode": "0600"}], init="x"),
        _state(DOCKER_T, size={"cpu": "2"}, dirty=["cpu"]),
    ):
        body = _call("NV_nwBody", state)
        assert set(body) <= set(WorkspaceCreateBody.model_fields), body
        parsed = WorkspaceCreateBody.model_validate(body)
        assert (parsed.template_id, parsed.name, parsed.id) == (body["template_id"], body.get("name"), body.get("id"))
        sent = body.get("overrides")
        if sent is None:
            assert parsed.overrides is None
            continue
        assert set(sent) <= set(type(parsed.overrides).model_fields), sent
        assert {k: v.get_secret_value() for k, v in parsed.overrides.env.items()} == sent.get("env", {})
        assert [f.path for f in parsed.overrides.files] == [f["path"] for f in sent.get("files", [])]
        assert parsed.overrides.init_commands == sent.get("init_commands", [])
        resources = parsed.overrides.resources.model_dump(exclude_none=True) if parsed.overrides.resources else {}
        assert resources == sent.get("resources", {})


# ---- the dialog's own errors ----------------------------------------------------------------------------------------------------------------------------


def test_errors():
    assert _call("NV_nwErrors", _state(K8S_T)) == {}
    for typed in ("0", "-1", "abc", "1e3", "0x10"):
        assert _call("NV_nwErrors", _state(K8S_T, size={"cpu": typed}, dirty=["cpu"])) == {"cpu": "Enter a positive number"}, typed
        assert _call("NV_nwErrors", _state(K8S_T, size={"memoryGiB": typed}, dirty=["memoryGiB"])) == {"memoryGiB": "Enter a positive number"}, typed
    assert _call("NV_nwErrors", _state(K8S_T, size={"cpu": "2000"}, dirty=["cpu"])) == {"cpu": "At most 1024 CPU cores"}
    assert _call("NV_nwErrors", _state(K8S_T, size={"diskGiB": "70000"}, dirty=["diskGiB"])) == {"diskGiB": "At most 65536 GiB (64 TiB)"}
    rows = [{"key": "A", "value": "1"}, {"key": " A ", "value": "2"}]
    assert _call("NV_nwErrors", _state(LOCAL_T, envRows=rows)) == {"envRows": "Each name once"}
    assert _call("NV_nwErrors", _state(LOCAL_T, id="Bad_Id")) == {"id": ID_ERROR}
    bad_mode = [{"path": "f", "source": {"kind": "inline", "content": ""}, "mode": "rw"}]
    assert _call("NV_nwErrors", _state(LOCAL_T, files=bad_mode)) == {"files": "Use an octal mode such as 0644"}


def test_only_what_the_person_changed_is_checked():
    """A pre-filled size the server took (1e-7 cores, a 1Mi disk shown as 0) does not block a create the person did not size; a cleared field is fine."""
    odd = {**K8S_T, "backend": {**K8S_T["backend"], "pvc_size": "1Mi"}, "resources": {"cpu_cores": 1e-7, "memory_bytes": 4 * GIB}}
    assert _call("NV_nwErrors", _state(odd)) == {}
    assert _call("NV_nwErrors", _state(K8S_T, size={"cpu": ""}, dirty=["cpu"])) == {}
    assert _call("NV_nwErrors", _state(DOCKER_T, size={"diskGiB": "0"}, dirty=["diskGiB"])) == {}, "Docker shows no disk, so it checks none"


# ---- where a refusal is shown ---------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("loc,kind,key", [
    (["body", "id"], "local", "id"),
    (["body", "name"], "local", "name"),
    (["body", "template_id"], "local", "template"),
    (["body", "overrides", "resources", "cpu_cores"], "kubernetes", "cpu"),
    (["body", "overrides", "resources", "memory_bytes"], "container", "memoryGiB"),
    (["body", "overrides", "resources", "disk_bytes"], "kubernetes", "diskGiB"),
    (["body", "overrides", "resources", "disk_bytes"], "container", None),
    (["body", "overrides", "resources", "cpu_cores"], "local", None),
    (["body", "overrides", "env", "PRIMER_RUNTIME_TOKEN"], "kubernetes", "envRows"),
    (["body", "overrides", "files", 0, "mode"], "local", "files"),
    (["body", "overrides", "init_commands", 1], "local", "init"),
    (["body", "overrides"], "local", None),
    (["body", "reply_binding"], "local", None),
    (["body"], "local", None),
    (None, "local", None),
])
def test_a_loc_names_its_field_key(loc, kind, key):
    assert _call("NV_nwFieldKey", loc, kind) == key


def _split(env: dict, kind: str) -> dict:
    return _js("WT_splitErrors(new window.primerApi.ApiError(" + json.dumps(env) + "), function (loc) { return NV_nwFieldKey(loc, " + json.dumps(kind) + "); })")


def test_a_hidden_field_refusal_goes_to_the_line():
    """Review Focus 2, the real envelope of Task 4: the dialog never shows Disk for Docker, so Docker's refusal of a disk is the one line."""
    status, env = envelope("POST", "/v1/workspaces", {"template_id": "tpl-dock", "overrides": {"resources": {"disk_bytes": 10 * GIB}}})
    assert status == 422
    out = _split(env, "container")
    assert out["fields"] == {} and out["general"] == "Docker cannot limit a workspace's disk. Remove this override."
    status, env = envelope("POST", "/v1/workspaces", {"template_id": "tpl-loc", "overrides": {"resources": {"cpu_cores": 2}}})
    assert status == 422
    assert _split(env, "local") == {"fields": {}, "general": "A Local workspace runs on the server itself and cannot be sized. Remove this override."}


def test_a_shown_field_refusal_goes_under_it():
    status, env = envelope("POST", "/v1/workspaces", {"template_id": "tpl-k3s", "overrides": {"resources": {"disk_bytes": 0}}})
    assert status == 422
    out = _split(env, "kubernetes")
    assert list(out["fields"]) == ["diskGiB"] and out["general"] is None
    assert out["fields"]["diskGiB"] == env["extensions"]["errors"][0]["msg"]


def test_a_refused_env_name_goes_under_the_environment_table():
    status, env = envelope("POST", "/v1/workspaces", {"template_id": "tpl-k3s", "overrides": {"env": {"PRIMER_RUNTIME_TOKEN": "x"}}})
    assert status == 422
    out = _split(env, "kubernetes")
    assert out == {"fields": {"envRows": env["extensions"]["errors"][0]["msg"]}, "general": None}


def test_a_refusal_with_no_field_is_said_in_words_not_a_code():
    status, env = envelope("POST", "/v1/workspaces", {"template_id": "no-such-template"})
    assert status == 404
    out = _split(env, "local")
    assert out["fields"] == {} and out["general"] == "WorkspaceTemplate 'no-such-template' does not exist"
    assert out["general"] == _js("window.primerApi.readRefusal(new window.primerApi.ApiError(" + json.dumps(env) + ")).message")
