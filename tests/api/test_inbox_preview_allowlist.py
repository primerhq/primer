"""The Inbox approval preview draws only what the park's allowlist says (design note 01a11cd3-66b0, rulings D1 to D5, slice 1).

Until now the preview ran a heuristic scrubber over EVERY argument value. A heuristic cannot catch a secret with no shape and no telling name (a random password under
``note``, a 40 character base64 key split by ``/``), which is why ``tests/api/test_inbox_scrubber_known_misses.py`` keeps them as documented misses. The park now stamps the
effective allowlist (``resume_metadata["preview"] = {"paths": [...], "source": ...}``, ``tests/agent/test_approval_preview_stamp.py``) and ``_approval_preview(call, stamp)``
applies it BEFORE any value is looked at:

* an argument not allowed is drawn as its name and ``<hidden>`` (``<redacted>`` stays the word for a secret the scrubber FOUND in an allowed value: the two differ on purpose);
* a hidden value is never read: not stringified, not measured, not iterated (``content=<hidden>``, not ``<N chars>``);
* the scrubber stays as defense in depth on every allowed value;
* a row with NO stamp (parked before the field existed, or by a site that does not stamp) takes the default rule by VALUE type (ruling D5): a boolean, a number or ``null``
  is shown, a string, an object or a list is not;
* ``hidden_keys`` names what was withheld (dotted paths, at most 12) and ``preview`` says who decided, so the card can say so.

The session's own pending-yields route still returns the whole call: this is a rule about the CARD, not about who may read the call.
"""

from __future__ import annotations

import ast
import base64
import random
import string
from pathlib import Path

import pytest

from primer.api.routers.workspaces import _allow_only, _approval_preview, _preview_stamp, _value_rule
from tests.api.test_workspace_yields_pending import (  # noqa: F401  (fixtures are used by name)
    _approval_state,
    _create_workspace,
    _make_session,
    app,
    client,
    pr,
    sp,
    wsr,
)
from primer.model.workspace_session import WorkspaceSession

ROOT = Path(__file__).resolve().parents[2]


def _stamp(*paths: str, source: str = "tool") -> dict:
    return {"paths": list(paths), "source": source}


def _preview(arguments, stamp, **extra):
    return _approval_preview({"name": "t", "arguments": arguments, **extra}, stamp)


# ---- the filter: nested paths, transparent lists ------------------------------------------------------------------------------------------------------------


def test_an_allowed_argument_is_kept_and_every_other_is_withheld_by_name() -> None:
    filtered, hidden = _allow_only({"path": "/a", "mode": "read", "note": "x"}, ["path", "mode"])

    assert filtered == {"path": "/a", "mode": "read", "note": "<hidden>"}
    assert hidden == ["note"]


def test_a_path_allows_its_whole_subtree() -> None:
    args = {"entity": {"id": "a", "model": {"profile_id": "p", "temperature": 0.2}}}

    filtered, hidden = _allow_only(args, ["entity"])

    assert filtered == args and hidden == []


def test_a_deeper_path_shows_part_of_an_object_and_withholds_its_siblings() -> None:
    args = {"entity": {"id": "a", "model": {"profile_id": "p", "temperature": 0.2}, "system_prompt": ["s3cr3t"], "config": {"token": "t"}}}

    filtered, hidden = _allow_only(args, ["entity.id", "entity.model.profile_id"])

    assert filtered == {"entity": {"id": "a", "model": {"profile_id": "p", "temperature": "<hidden>"}, "system_prompt": "<hidden>", "config": "<hidden>"}}
    assert hidden == ["entity.model.temperature", "entity.system_prompt", "entity.config"]


def test_a_list_is_transparent_in_a_path() -> None:
    args = {"entity": {"nodes": [{"id": "n1", "agent_id": "a1", "input_template": "SECRET one"}, {"id": "n2", "agent_id": "a2", "input_template": "SECRET two"}]}}

    filtered, hidden = _allow_only(args, ["entity.nodes.agent_id", "entity.nodes.id"])

    assert filtered["entity"]["nodes"] == [
        {"id": "n1", "agent_id": "a1", "input_template": "<hidden>"},
        {"id": "n2", "agent_id": "a2", "input_template": "<hidden>"},
    ]
    assert hidden == ["entity.nodes.input_template", "entity.nodes.input_template"]


def test_a_deeper_path_where_the_value_is_not_a_container_withholds_it() -> None:
    """The tool was told ``entity.id`` is shown but was sent a bare string for ``entity``: there is no part of it to show."""
    filtered, hidden = _allow_only({"entity": "just text"}, ["entity.id"])

    assert filtered == {"entity": "<hidden>"} and hidden == ["entity"]


def test_an_empty_allowlist_withholds_every_value() -> None:
    filtered, hidden = _allow_only({"a": 1, "b": "x"}, [])

    assert filtered == {"a": "<hidden>", "b": "<hidden>"} and hidden == ["a", "b"]


# ---- the line ---------------------------------------------------------------------------------------------------------------------------------------------


def test_the_line_names_a_withheld_argument_and_marks_the_preview_truncated() -> None:
    got = _preview({"path": "/a", "mode": "read", "note": "free text"}, _stamp("path", "mode"))

    assert got["arguments"] == "path=/a, mode=read, note=<hidden>"
    assert got["truncated"] is True
    assert got["hidden_keys"] == ["note"] and got["preview"] == "tool"
    assert got["argument_keys"] == ["path", "mode", "note"], "the rail keeps listing every argument NAME"


def test_a_call_with_nothing_withheld_is_not_truncated() -> None:
    got = _preview({"path": "/a", "mode": "read"}, _stamp("path", "mode"))

    assert got["arguments"] == "path=/a, mode=read" and got["truncated"] is False and got["hidden_keys"] == []


@pytest.mark.parametrize("source", ["policy", "tool", "default"])
def test_the_card_is_told_who_decided(source: str) -> None:
    assert _preview({"path": "/a"}, _stamp("path", source=source))["preview"] == source


def test_a_withheld_bulky_argument_is_not_measured() -> None:
    """``content=<N chars>`` reads the value's size; a withheld one says only that it is withheld."""
    got = _preview({"path": "/a", "content": "x" * 5000}, _stamp("path"))

    assert got["arguments"] == "path=/a, content=<hidden>"


def test_an_allowed_bulky_argument_is_still_counted_not_shipped() -> None:
    got = _preview({"path": "/a", "content": "x" * 5000}, _stamp("path", "content"))

    assert got["arguments"] == "path=/a, content=<5000 chars>"


def test_the_scrubber_still_runs_on_an_allowed_value() -> None:
    """Defense in depth: ``<redacted>`` is the word for a secret FOUND in a value the allowlist let through; ``<hidden>`` is the word for one never read."""
    got = _preview({"url": "https://deploy:hunter2@host/x", "description": "key ghp_abcdefghijklmnopqrstuvwxyz0123456789"}, _stamp("url", "description"))

    assert "hunter2" not in got["arguments"] and "ghp_abcdefghijkl" not in got["arguments"]
    assert "<redacted>" in got["arguments"] and "<hidden>" not in got["arguments"]
    assert got["truncated"] is True and got["hidden_keys"] == []


def test_arguments_sent_as_json_text_are_parsed_and_then_filtered() -> None:
    got = _approval_preview({"name": "t", "arguments": '{"path": "/a", "note": "free text"}'}, _stamp("path"))

    assert got["arguments"] == "path=/a, note=<hidden>"


@pytest.mark.parametrize("arguments", ["just text", ["a", "b"], 12])
def test_arguments_that_are_not_an_object_are_withheld_when_nothing_can_name_them(arguments) -> None:
    got = _preview(arguments, _stamp("path"))

    assert got["arguments"] == "<hidden>" and got["truncated"] is True


@pytest.mark.parametrize("arguments", [None, "", {}])
def test_a_call_with_no_arguments_has_an_empty_line(arguments) -> None:
    got = _preview(arguments, _stamp("path"))

    assert got["arguments"] == "" and got["truncated"] is False and got["hidden_keys"] == []


def test_the_withheld_names_are_capped_and_scrubbed_like_every_other_name() -> None:
    args = {f"k{i:02d}": "v" for i in range(30)}

    got = _preview(args, _stamp())

    assert len(got["hidden_keys"]) == 12
    got = _preview({"sk-abcdefghijklmnopqrstu": "v"}, _stamp())
    assert "abcdefghijklmnopqrstu" not in str(got["hidden_keys"]) + got["arguments"], "a credential used as a NAME is not drawn"


# ---- what the heuristic cannot do: shapeless secrets under harmless names -------------------------------------------------------------------------------------


def _shapeless_secrets() -> dict[str, str]:
    rng = random.Random(20261009)
    alphabet = string.ascii_letters + string.digits
    return {
        "a random password": "".join(rng.choice(alphabet + "!@#%^&*") for _ in range(18)),
        "a base64 key with slashes": "/".join(base64.b64encode(rng.randbytes(15)).decode() for _ in range(2)),
        "a dotted 40 character key": ".".join(["".join(rng.choice(alphabet) for _ in range(13)) for _ in range(3)]),
        "a diceware passphrase": "correct horse battery staple",
        "a hex token under a path": "/srv/" + "".join(rng.choice("0123456789abcdef") for _ in range(26)) + "/data",
    }


@pytest.mark.parametrize("name", ["note", "label", "comment", "description", "title", "message"])
def test_a_shapeless_secret_under_a_harmless_name_is_not_drawn_unless_the_tool_allowed_that_name(name: str) -> None:
    for shape, secret in _shapeless_secrets().items():
        got = _preview({"path": "/work", name: secret}, _stamp("path"))
        shown = got["arguments"] + " | " + " ".join(got["argument_keys"]) + " | " + " ".join(got["hidden_keys"])
        leaked = [piece for piece in [secret, *secret.replace("/", " ").replace(".", " ").split()] if len(piece) > 5 and piece in shown]
        assert not leaked, f"{shape} under {name!r} leaked {leaked}: {shown}"


def test_the_scrubber_alone_does_miss_these_which_is_why_the_allowlist_exists() -> None:
    """The premise of the test above: with no allowlist (the old behaviour) these shapes DO show, so the test above can fail."""
    shown = [secret for secret in _shapeless_secrets().values() if secret in _approval_preview({"name": "t", "arguments": {"note": secret}})["arguments"]]

    assert len(shown) >= 2, shown


# ---- a withheld value is never read -------------------------------------------------------------------------------------------------------------------------


class _Forbidden:
    """A value nobody may look at: any attempt to turn it into text, measure it, iterate it, compare it or hash it raises."""

    def _no(self, *args, **kwargs):
        raise AssertionError("a withheld value was read")

    __str__ = __repr__ = __len__ = __iter__ = __bool__ = __eq__ = __hash__ = __format__ = __getitem__ = __contains__ = _no
    __int__ = __float__ = __index__ = __reduce__ = __reduce_ex__ = __copy__ = __deepcopy__ = __sizeof__ = _no


def test_a_withheld_value_is_never_read() -> None:
    forbidden = _Forbidden()
    args = {"path": "/a", "note": forbidden, "content": forbidden, "entity": {"id": "e", "config": forbidden, "list": [forbidden, forbidden]}}

    got = _preview(args, _stamp("path", "entity.id"))

    assert got["arguments"] == 'path=/a, entity={"id": "e", "config": "<hidden>", "list": "<hidden>"}, note=<hidden>, content=<hidden>'
    assert got["hidden_keys"] == ["note", "content", "entity.config", "entity.list"]


def test_a_withheld_value_is_never_read_by_the_default_rule_either() -> None:
    forbidden = _Forbidden()

    got = _approval_preview({"name": "t", "arguments": {"path": forbidden, "force": True, "blob": [forbidden]}}, None)

    assert "path=<hidden>" in got["arguments"] and "force=true" in got["arguments"] and "blob=<hidden>" in got["arguments"]


def test_an_empty_allowlist_reads_nothing() -> None:
    forbidden = _Forbidden()

    got = _preview({"a": forbidden, "b": forbidden}, _stamp(source="policy"))

    assert got["arguments"] == "a=<hidden>, b=<hidden>"


# ---- a row with no stamp takes the default rule by value type (ruling D5) ---------------------------------------------------------------------------------------


def test_the_default_rule_shows_a_boolean_a_number_and_null_and_withholds_text_and_containers() -> None:
    filtered, hidden = _value_rule({"path": "/a", "force": True, "limit": 3, "ratio": 0.5, "nothing": None, "mode": "read", "obj": {"a": 1}, "xs": [1, 2]})

    assert filtered == {"path": "<hidden>", "force": True, "limit": 3, "ratio": 0.5, "nothing": None, "mode": "<hidden>", "obj": "<hidden>", "xs": "<hidden>"}
    assert hidden == ["path", "mode", "obj", "xs"]


def test_an_unstamped_row_draws_by_the_default_rule_and_says_so() -> None:
    got = _approval_preview({"name": "t", "arguments": {"path": "/a", "force": True, "limit": 3}}, None)

    assert got["arguments"] == "path=<hidden>, force=true, limit=3"
    assert got["preview"] == "unstamped" and got["hidden_keys"] == ["path"] and got["truncated"] is True


def test_a_boolean_is_shown_as_json_not_as_python() -> None:
    assert _approval_preview({"name": "t", "arguments": {"force": False}}, None)["arguments"] == "force=false"


# ---- the stamp is read defensively ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stamp",
    [None, "tool", 3, [], {}, {"paths": "path", "source": "tool"}, {"paths": ["path"], "source": "magic"}, {"paths": ["path", 3], "source": "tool"},
     {"paths": ["a b"], "source": "tool"}, {"source": "tool"}, {"paths": ["path"]}],
)
def test_a_stamp_that_is_not_one_is_no_stamp(stamp) -> None:
    assert _preview_stamp({"preview": stamp}) is None


def test_a_good_stamp_is_returned_as_it_is() -> None:
    assert _preview_stamp({"preview": {"paths": ["path", "entity.id"], "source": "policy"}}) == {"paths": ["path", "entity.id"], "source": "policy"}
    assert _preview_stamp({"preview": {"paths": [], "source": "default"}}) == {"paths": [], "source": "default"}
    assert _preview_stamp({}) is None and _preview_stamp(None) is None


def test_a_stamp_with_more_than_the_cap_of_paths_is_no_stamp() -> None:
    assert _preview_stamp({"preview": {"paths": [f"a{i}" for i in range(65)], "source": "tool"}}) is None


# ---- the route --------------------------------------------------------------------------------------------------------------------------------------------


async def _row(client, sp, sid: str, state: dict) -> dict:
    wid = await _create_workspace(client)
    await sp.get_storage(WorkspaceSession).create(_make_session(sid, wid, parked_status="parked", parked_state=state))
    resp = await client.get("/v1/yields/pending")
    assert resp.status_code == 200, resp.text
    return {i["session_id"]: i for i in resp.json()["items"]}[sid]


def _state(call: dict, stamp: dict | None) -> dict:
    state = _approval_state(call["id"], call)
    if stamp is not None:
        state["yielded"]["resume_metadata"]["preview"] = stamp
    return state


@pytest.mark.asyncio
async def test_a_stamped_row_is_drawn_by_its_allowlist_and_carries_what_the_card_needs(client, sp) -> None:
    call = {"id": "tc-1", "name": "ts__write", "arguments": {"path": "/a", "note": "correct horse battery staple", "force": True}}

    row = await _row(client, sp, "sess-al-1", _state(call, _stamp("path", "force", source="policy")))

    approval = row["approval"]
    assert approval["arguments"] == "path=/a, force=true, note=<hidden>"
    assert approval["hidden_keys"] == ["note"] and approval["preview"] == "policy" and approval["truncated"] is True
    assert approval["tool_name"] == "ts__write" and approval["argument_keys"] == ["path", "force", "note"]


@pytest.mark.asyncio
async def test_an_unstamped_row_takes_the_default_rule_in_the_real_route(client, sp) -> None:
    call = {"id": "tc-2", "name": "ts__write", "arguments": {"path": "/a", "force": True}}

    row = await _row(client, sp, "sess-al-2", _state(call, None))

    assert row["approval"]["arguments"] == "path=<hidden>, force=true" and row["approval"]["preview"] == "unstamped"


@pytest.mark.asyncio
async def test_the_whole_call_is_still_one_show_all_away_for_the_session_route(client, sp) -> None:
    """The allowlist is a rule about the CARD. The session's own pending-yields route is the access-controlled one and returns the call whole."""
    call = {"id": "tc-3", "name": "ts__write", "arguments": {"path": "/a", "note": "correct horse battery staple"}}
    wid = await _create_workspace(client)
    await sp.get_storage(WorkspaceSession).create(_make_session("sess-al-3", wid, parked_status="parked", parked_state=_state(call, _stamp("path"))))

    resp = await client.get(f"/v1/workspaces/{wid}/yields/pending")

    assert resp.status_code == 200, resp.text
    assert "correct horse battery staple" in resp.text, "the workspace route returns the original_call whole"


# ---- no caller can forget the stamp -------------------------------------------------------------------------------------------------------------------------


def test_every_call_of_approval_preview_in_the_api_passes_the_stamp() -> None:
    """``_approval_preview(call)`` with one argument is the scrubber alone (what the unit tests of the scrubber call); the API must always pass the second."""
    offenders = []
    for path in sorted((ROOT / "primer").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "_approval_preview":
                if len(node.args) + len(node.keywords) < 2:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, "a caller draws a card from the scrubber alone: " + ", ".join(offenders)
