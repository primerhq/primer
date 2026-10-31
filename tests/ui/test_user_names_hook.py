"""ACL-P6-T3a: the console side of the username lookup by id.

The console shows owners and grantees by USERNAME everywhere it shows access,
but a list's rows only carry ids (``row.access.owners`` / ``grants``; the
access VIEW carries usernames, a row's access block does not). The shared hook
``SHR_useUserNames(ids)`` (``ui/components/shared/share-dialog.jsx``) answers
id -> username for any list: one batched ``GET /v1/users/lookup?ids=`` per set
of not-yet-resolved ids (the route's one query over at most 200 ids, unknown
ids absent), a module cache so an id resolved once is never fetched again for
the page, and the id itself as the answer while it is loading or when it names
no user. The fetch goes through ``SH_api`` (the one file that names a URL);
this file proves it with the real source in MiniRacer and pins where the hook
is wired in: the agents list's ownership markers (ACL-P6-T3).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

SHARE_SRC = UI / "components" / "shared" / "share-dialog.jsx"
SH_API_SRC = (UI / "components" / "shell" / "sh-api.jsx").read_text(encoding="utf-8")
AGENTS = (UI / "components" / "agents.jsx").read_text(encoding="utf-8")


def _transpiled(path: Path) -> str:
    from tests.ui._mini_react import transpile

    return transpile(path)


# ---- the wiring (the real lines) ---------------------------------------------


def test_the_ids_fetch_lives_in_sh_api() -> None:
    """The URL is named by SH_api (the one file that names a URL); the hook
    fetches through it and is exported for the lists to call."""
    assert '"/users/lookup?ids="' in SH_API_SRC
    assert "userNames: function (ids, signal)" in SH_API_SRC
    share = SHARE_SRC.read_text(encoding="utf-8")
    assert "SH_api.userNames" in share
    assert "window.SHR_useUserNames = SHR_useUserNames" in share


def test_the_agents_list_says_who_owns_each_row() -> None:
    """ACL-P6-T3: the one place the hook is wired in: the agents list's
    owner badge is the shared marker row (the owner's names from this
    lookup, "Owned by you" when it is ours)."""
    assert "window.SHR_useUserNames(ownerIds)" in AGENTS
    assert "SHR_AccessMarkers access={a.access}" in AGENTS
    assert "names={ownerNames}" in AGENTS


# ---- the hook's behavior (the real file in MiniRacer) -------------------------

# The hook's two collaborators, stubbed: useResource records each (key, fetcher)
# and answers from a map the test fills; SH_api.userNames records the ids it is
# asked to name (the batched-read pin).
PRELUDE = r"""
var __res = [];
var __resData = {};
var __namesCalls = [];
window.primerApi = {
  useResource: function (key, fetcher, opts) {
    __res.push({ key: key, fetcher: fetcher });
    return {
      data: (key != null && __resData[key]) ? __resData[key] : undefined,
      error: null,
      loading: false,
      degraded: false,
      refetch: function () {}
    };
  }
};
var SH_api = {
  userNames: function (ids, signal) {
    __namesCalls.push(ids.slice());
    return Promise.resolve([]);
  }
};
function T(props) {
  var names = SHR_useUserNames(props.ids);
  var parts = [];
  Object.keys(names).forEach(function (k) { parts.push(k + ":" + names[k]); });
  return React.createElement("div", { "data-testid": "t-names" }, parts.join(" "));
}
"""


def _ctx():
    from tests.ui._mini_react import mini_react_context

    return mini_react_context(_transpiled(SHARE_SRC), prelude=PRELUDE)


def _texts(ctx) -> str:
    return " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


def test_the_hook_falls_back_to_the_id_while_loading_and_when_unknown() -> None:
    """Every requested id answers the username, or the id itself: while the
    lookup is out, and for an id that names no user (the route simply omits
    it). The fetch is one batched read of the missing ids."""
    ctx = _ctx()
    try:
        ctx.eval("MR.mount(T, { ids: ['u1', 'u2'] })")
        assert _texts(ctx) == "u1:u1 u2:u2", "the id stands in while the lookup is out"
        assert json.loads(ctx.eval("JSON.stringify(__res[0].key)")) == "shr-user-names:u1,u2"
        ctx.eval("__res[0].fetcher(null)")
        assert json.loads(ctx.eval("JSON.stringify(__namesCalls)")) == [["u1", "u2"]], (
            "one batched read of the missing ids"
        )
        ctx.eval("__resData['shr-user-names:u1,u2'] = " + json.dumps([{"id": "u1", "username": "alice"}]))
        ctx.eval("MR.rerender()")
        assert _texts(ctx) == "u1:alice u2:u2", "the resolved id says its name; the unknown keeps the id"
    finally:
        ctx.close()


def test_a_resolved_id_is_not_fetched_again() -> None:
    """The module cache: an id the page already resolved is out of the next
    lookup's missing set, so the badge for a wider list only fetches what it
    has not seen."""
    ctx = _ctx()
    try:
        ctx.eval("__resData['shr-user-names:u1'] = " + json.dumps([{"id": "u1", "username": "alice"}]))
        ctx.eval("MR.mount(T, { ids: ['u1'] })")
        assert json.loads(ctx.eval('JSON.stringify(SHR_NAME_CACHE["u1"])')) == "alice", (
            "the resolution is cached for the page"
        )
        ctx.eval("MR.mount(T, { ids: ['u1', 'u3'] })")
        assert json.loads(ctx.eval("JSON.stringify(__res[1].key)")) == "shr-user-names:u3", (
            "u1 is already resolved, so the next lookup names only u3"
        )
        ctx.eval("__res[1].fetcher(null)")
        assert json.loads(ctx.eval("JSON.stringify(__namesCalls)")) == [["u3"]]
    finally:
        ctx.close()


def test_the_hook_makes_no_fetch_when_every_id_is_resolved() -> None:
    """No ids, or every id already in the cache: the resource key is null
    (disabled) and nothing is sent."""
    ctx = _ctx()
    try:
        ctx.eval("MR.mount(T, { ids: [] })")
        assert json.loads(ctx.eval("JSON.stringify(__res[0].key)")) is None
        ctx.eval('SHR_NAME_CACHE["u1"] = "alice"')
        ctx.eval("MR.mount(T, { ids: ['u1'] })")
        assert json.loads(ctx.eval("JSON.stringify(__res[1].key)")) is None
        assert json.loads(ctx.eval("JSON.stringify(__namesCalls)")) == []
    finally:
        ctx.close()


def test_the_hook_never_sends_more_than_the_route_caps() -> None:
    """The route refuses more than 200 ids; a badge set past that fetches the
    first 200 (sorted, so the key is stable across render orders) and says
    the raw id for the rest."""
    ctx = _ctx()
    try:
        ctx.eval("var __big = []; for (var i = 0; i < 201; i++) __big.push('x' + i);")
        ctx.eval("MR.mount(T, { ids: __big })")
        expected = "shr-user-names:" + ",".join(sorted(f"x{i}" for i in range(201))[:200])
        assert json.loads(ctx.eval("JSON.stringify(__res[0].key)")) == expected
        ctx.eval("__res[0].fetcher(null)")
        assert len(json.loads(ctx.eval("JSON.stringify(__namesCalls[0])"))) == 200
    finally:
        ctx.close()
