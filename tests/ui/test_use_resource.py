"""ui/foundation/use-resource.js — resourceState().

2026-09-10 audit: 0 of 192 useResource call sites app-wide read `degraded`
at all (see tests/ui/test_console_system.py's own worker-pool history for
the first instance of this class). Every one of them therefore shows its
loading affordance forever on sustained fetch failure, indistinguishable
from a fetch that's merely in flight. resourceState() collapses a
useResource snapshot into the three states a consumer actually needs -
ready / stuck / loading - so a new consumer doesn't have to reinvent (or
omit) the distinction.

Pure logic, no React/timers involved in the function itself, so it is
EXECUTED here via MiniRacer rather than substring-matched, mirroring
tests/ui/test_shell_url.py's own convention for foundation/*.js. The
module references window.React's hooks at load time (never calls them
outside an actual component render), so a minimal stub is enough to load
it without executing any component.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "use-resource.js"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(
        "window.React = { useState: function(){}, useEffect: function(){}, "
        "useRef: function(){}, useCallback: function(f){ return f; } };"
    )
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def _state(snap: dict) -> str:
    ctx = _ctx()
    ctx.eval(f"var __snap = {json.dumps(snap)};")
    return ctx.eval("window.primerApi.resourceState(__snap)")


def test_registered_on_primer_api() -> None:
    src = MODULE.read_text(encoding="utf-8")
    assert "ns.resourceState = resourceState" in src
    assert 'src="foundation/use-resource.js"' in (
        (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    )


def test_data_present_is_ready_even_while_erroring() -> None:
    # Stale-while-error: a later poll failing must not un-render data
    # that already arrived. This is the case that matters for the four
    # nv-system.jsx cards already traced clean (2026-09-10) - their own
    # data sources cannot resolve to a non-null value that isn't real, so
    # "ready" is correct for them whenever data is present at all.
    assert _state({"data": {"total": 3}, "error": None, "degraded": False}) == "ready"
    assert _state({"data": {"total": 3}, "error": {"title": "boom"}, "degraded": True}) == "ready"
    assert _state({"data": 0, "error": None, "degraded": False}) == "ready", (
        "falsy-but-present data (0, empty list/dict) must still be ready, not loading"
    )


def test_no_data_and_degraded_is_stuck() -> None:
    # The permanent-lie case: MAX_ERRORS consecutive failures, backing
    # off, never resolving within human-observable time.
    assert _state({"data": None, "error": {"title": "boom"}, "degraded": True}) == "stuck"


def test_no_data_and_not_degraded_is_loading() -> None:
    # First fetch in flight - the honest case.
    assert _state({"data": None, "error": None, "degraded": False}) == "loading"
    # A single failure (or two) that hasn't crossed MAX_ERRORS yet: still
    # transient by the hook's own design (exponential backoff, not yet
    # the debounced "genuinely stuck" signal).
    assert _state({"data": None, "error": {"title": "boom"}, "degraded": False}) == "loading"


def test_missing_fields_default_to_loading_not_a_crash() -> None:
    # A hand-built snapshot (e.g. a test double) that omits `degraded`
    # entirely must not throw - it reads as loading, the safe default.
    assert _state({"data": None}) == "loading"


# ---------------------------------------------------------------------------
# A null / undefined cache key means "disabled" (ADM-10, ADM-17 of the 2026-10-08 admin review).
#
# useResource(cond ? "key" : null, fetcher) is how two screens (the model-profile form's discovered-models probe
# and the graph builder's run-state poll) say "nothing to fetch yet". The hook used to treat null as just another
# key: it created a cache entry under the key ``null``, ran the fetcher (GET /llm_providers//discovered_models,
# GET /graphs/<id>/runs/undefined/node_states, both doomed 404s), and left the entry in the cache for good. The
# next useMutation with ``invalidates`` then threw ``null.startsWith`` inside findKeys, AFTER the server write had
# succeeded and BEFORE onSuccess ran: the write landed, the toast / refetch / close never happened, and the promise
# rejected unhandled.
#
# These tests RUN the hook (MiniRacer with a React stub whose effects execute immediately), unlike the
# resourceState tests above which only call the pure function.
# ---------------------------------------------------------------------------


def _hook_ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(
        """
        var document = { addEventListener: function () {}, hidden: false };
        var setTimeout = function () { return 1; };
        var clearTimeout = function () {};
        function AbortController() { this.signal = {}; this.abort = function () {}; }
        window.React = {
          useState: function (init) {
            var v = typeof init === 'function' ? init() : init;
            return [v, function (n) { v = n; }];
          },
          useRef: function (v) { return { current: v }; },
          useCallback: function (f) { return f; },
          useEffect: function (f) { f(); },
        };
        var __calls = 0;
        function __fetcher() { __calls += 1; return new Promise(function () {}); }
        """
    )
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def _race_ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(
        """
        var document = { addEventListener: function () {}, hidden: false };
        var setTimeout = function () { return 1; };
        var clearTimeout = function () {};
        var __cleanups = [];
        var __requests = [];
        var __snapshots = [];
        function AbortController() {
          var listeners = [];
          this.signal = {
            aborted: false,
            addEventListener: function (name, fn) {
              if (name === "abort") listeners.push(fn);
            },
          };
          this.abort = function () {
            this.signal.aborted = true;
            listeners.forEach(function (fn) { fn(); });
          };
        }
        window.React = {
          useState: function (init) {
            var value = typeof init === "function" ? init() : init;
            return [value, function (next) {
              value = next;
              __snapshots.push(next);
            }];
          },
          useRef: function (value) { return { current: value }; },
          useCallback: function (fn) { return fn; },
          useEffect: function (fn) {
            var cleanup = fn();
            if (typeof cleanup === "function") __cleanups.push(cleanup);
          },
        };
        function __fetcher(signal) {
          var request = { signal: signal };
          request.promise = new Promise(function (resolve, reject) {
            request.resolve = resolve;
            request.reject = reject;
          });
          __requests.push(request);
          return request.promise;
        }
        """
    )
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def _mount_race_resource(ctx, key: str = "auth-status") -> None:
    ctx.eval(
        "window.primerApi.useResource("
        + json.dumps(key)
        + ", __fetcher, { pollMs: 0 });"
    )


def _install_timer_spy(ctx) -> None:
    ctx.eval(
        """
        var __timers = [];
        var setTimeout = function (fn, ms) {
          __timers.push({ fn: fn, ms: ms, cleared: false });
          return __timers.length;
        };
        var clearTimeout = function (id) {
          if (__timers[id - 1]) __timers[id - 1].cleared = true;
        };
        function __runTimer(index) {
          __timers[index].cleared = true;
          __timers[index].fn();
        }
        function __activeTimerDelays() {
          return __timers.filter(function (timer) { return !timer.cleared; })
            .map(function (timer) { return timer.ms; });
        }
        """
    )


def _resolve_request(ctx, index: int, data: dict) -> None:
    ctx.eval(f"__requests[{index}].resolve({json.dumps(data)});")
    ctx.eval("void 0;")


def _reject_aborted_request(ctx, index: int) -> None:
    ctx.eval(
        f"var error = new Error('aborted'); error.name = 'AbortError'; "
        f"__requests[{index}].reject(error);"
    )
    ctx.eval("void 0;")


def _peek(ctx, key: str = "auth-status"):
    return json.loads(
        ctx.eval(
            "JSON.stringify(window.primerApi._resource.peekData("
            + json.dumps(key)
            + "))"
        )
        or "null"
    )


def test_a_live_key_fetches_once_so_the_null_key_tests_are_not_vacuous() -> None:
    ctx = _hook_ctx()
    ctx.eval("window.primerApi.useResource('live:key', __fetcher, { pollMs: 0 });")
    assert ctx.eval("__calls") == 1


def test_retry_cadence_backs_off_after_failures_and_stops_after_success() -> None:
    ctx = _race_ctx()
    _install_timer_spy(ctx)
    ctx.eval(
        "window.primerApi.useResource("
        "'auth-status', __fetcher, { pollMs: 0, retryMs: 2000 });"
    )

    ctx.eval("__requests[0].reject(new Error('temporary')); void 0;")
    assert json.loads(ctx.eval("JSON.stringify(__activeTimerDelays())")) == [4000]

    ctx.eval("__runTimer(0)")
    ctx.eval("__requests[1].reject(new Error('temporary again')); void 0;")
    assert json.loads(ctx.eval("JSON.stringify(__activeTimerDelays())")) == [8000]

    ctx.eval("__runTimer(1)")
    _resolve_request(ctx, 2, {"authenticated": True, "username": "alice"})
    assert json.loads(ctx.eval("JSON.stringify(__activeTimerDelays())")) == []
    assert _peek(ctx) == {"authenticated": True, "username": "alice"}


def test_a_null_key_never_calls_the_fetcher_and_reports_idle() -> None:
    ctx = _hook_ctx()
    result = json.loads(ctx.eval(
        "JSON.stringify(window.primerApi.useResource(null, __fetcher, { pollMs: 0 }))"
    ))
    assert ctx.eval("__calls") == 0, "a null key must not run the fetcher (it fetched with an undefined id)"
    assert result.get("data") is None
    assert result.get("loading") is False, "disabled is not loading: the consumer must not show a spinner"


def test_an_undefined_key_is_disabled_too() -> None:
    ctx = _hook_ctx()
    ctx.eval("window.primerApi.useResource(undefined, __fetcher, { pollMs: 0 });")
    assert ctx.eval("__calls") == 0


def test_a_null_key_does_not_poison_findkeys_for_a_later_mutation() -> None:
    ctx = _hook_ctx()
    ctx.eval("window.primerApi.useResource(null, __fetcher, { pollMs: 0 });")
    ctx.eval("window.primerApi.useResource('workers:list', __fetcher, { pollMs: 0 });")
    # useMutation calls this after every successful write with a non-empty `invalidates`.
    keys = json.loads(ctx.eval("JSON.stringify(window.primerApi._resource.findKeys('workers:list'))"))
    assert keys == ["workers:list"]


def test_a_superseded_first_success_seeds_an_empty_live_entry() -> None:
    ctx = _race_ctx()
    _mount_race_resource(ctx)
    ctx.eval("window.primerApi._resource.refetchKey('auth-status');")
    assert ctx.eval("__requests.length") == 2
    assert ctx.eval("__requests[0].signal.aborted") is True

    _resolve_request(ctx, 0, {"username": "alice", "source": "first"})

    assert _peek(ctx) == {"username": "alice", "source": "first"}
    assert json.loads(
        ctx.eval("JSON.stringify(__snapshots[__snapshots.length - 1].data)")
    ) == {"username": "alice", "source": "first"}
    ctx.eval("window.primerApi._resource.refetchKey('auth-status');")
    assert ctx.eval("__requests[1].signal.aborted") is True


def test_a_superseded_success_never_overwrites_newer_data() -> None:
    ctx = _race_ctx()
    _mount_race_resource(ctx)
    ctx.eval("window.primerApi._resource.refetchKey('auth-status');")

    _resolve_request(ctx, 1, {"username": "alice", "source": "latest"})
    _resolve_request(ctx, 0, {"username": "stale", "source": "first"})

    assert _peek(ctx) == {"username": "alice", "source": "latest"}


def test_an_aborted_request_leaves_its_replacement_in_charge() -> None:
    ctx = _race_ctx()
    _mount_race_resource(ctx)
    ctx.eval("window.primerApi._resource.refetchKey('auth-status');")
    assert ctx.eval("__requests[0].signal.aborted") is True

    _reject_aborted_request(ctx, 0)
    assert _peek(ctx) is None

    _resolve_request(ctx, 1, {"username": "alice"})
    assert _peek(ctx) == {"username": "alice"}


def test_last_subscriber_churn_does_not_reuse_an_aborted_entry() -> None:
    ctx = _race_ctx()
    _mount_race_resource(ctx)
    ctx.eval("__cleanups[0]();")
    assert ctx.eval("__requests[0].signal.aborted") is True
    assert json.loads(
        ctx.eval("JSON.stringify(window.primerApi._resource.findKeys('auth-status'))")
    ) == []

    _mount_race_resource(ctx)
    assert ctx.eval("__requests.length") == 2
    _resolve_request(ctx, 0, {"username": "stale"})
    assert _peek(ctx) is None

    _resolve_request(ctx, 1, {"username": "alice"})
    assert _peek(ctx) == {"username": "alice"}


# ---------------------------------------------------------------------------
# refetchOnWake: false (audit G1-09). The Setup card reads /setup/state once and again only on Check again, a seed or a reset; each read lists the
# LLM's models and re-attaches the default workspace. Every tab re-show and every wake from idle refetched it anyway, because the page-wide
# visibility listener and refetchAll() re-run every subscribed entry. The opt-out leaves such a resource alone on both paths; an explicit
# refetch() still reads it, and the default stays as it was.
# ---------------------------------------------------------------------------


def _wake_ctx():
    ctx = _race_ctx()
    ctx.eval(
        """
        var __visibility = null;
        document.addEventListener = function (name, fn) {
          if (name === "visibilitychange") __visibility = fn;
        };
        var __now = 1000000000000; Date.now = function () { return __now; };
        function __hide() { document.hidden = true; __visibility(); }
        function __show() { document.hidden = false; __visibility(); }
        """
    )
    return ctx


def test_a_resource_that_opts_out_is_not_read_again_on_a_tab_reshow_or_an_idle_wake() -> None:
    ctx = _wake_ctx()
    ctx.eval("window.primerApi.useResource('setup', __fetcher, { refetchOnWake: false });")
    _resolve_request(ctx, 0, {"ok": True})
    ctx.eval("__hide(); __show();")
    assert ctx.eval("__requests.length") == 1, "a tab re-show read it again"
    ctx.eval("window.primerApi._refetchAll();")
    assert ctx.eval("__requests.length") == 1, "the idle wake-up read it again"
    ctx.eval("window.primerApi._resource.refetchKey('setup');")
    assert ctx.eval("__requests.length") == 2, "an explicit refetch still reads it"


def test_hiding_the_tab_does_not_abort_the_one_read_of_a_resource_that_opts_out() -> None:
    """It is not read again on the re-show, so an aborted first read would leave it loading for good."""
    ctx = _wake_ctx()
    ctx.eval("window.primerApi.useResource('setup', __fetcher, { refetchOnWake: false });")
    ctx.eval("__hide();")
    assert ctx.eval("__requests[0].signal.aborted") is False
    _resolve_request(ctx, 0, {"ok": True})
    ctx.eval("__show();")
    assert _peek(ctx, "setup") == {"ok": True}
    assert ctx.eval("__requests.length") == 1


def test_a_resource_that_does_not_opt_out_is_still_read_again_on_a_reshow_and_a_wake() -> None:
    ctx = _wake_ctx()
    ctx.eval("window.primerApi.useResource('list', __fetcher, {});")
    _resolve_request(ctx, 0, {"items": []})
    ctx.eval("__hide(); __now += 60000;")  # the tab was away a minute: the answer is old
    ctx.eval("__show();")
    assert ctx.eval("__requests.length") == 2
    _resolve_request(ctx, 1, {"items": []})
    ctx.eval("__now += 60000;")
    ctx.eval("window.primerApi._refetchAll();")
    assert ctx.eval("__requests.length") == 3


# Review 8.6: a wake refetched every live entry at once. Now an entry with a young answer keeps it (and resumes its poll), and the rest are spread.


def _clock_ctx():
    ctx = _wake_ctx()
    _install_timer_spy(ctx)
    return ctx


def test_a_wake_leaves_a_young_answer_alone_and_resumes_its_poll() -> None:
    ctx = _clock_ctx()
    ctx.eval("window.primerApi.useResource('young', __fetcher, { pollMs: 15000 });")
    _resolve_request(ctx, 0, {"items": []})
    ctx.eval("__hide();")
    assert json.loads(ctx.eval("JSON.stringify(__activeTimerDelays())")) == [], "hiding stops the poll"
    ctx.eval("__now += 4000; __show();")
    assert ctx.eval("__requests.length") == 1, "an answer 4 s old (poll interval 15 s) is not read again"
    assert json.loads(ctx.eval("JSON.stringify(__activeTimerDelays())")) == [15000], "its poll resumes"
    ctx.eval("__now += 20000; __hide(); __show();")
    assert ctx.eval("__requests.length") == 2, "an answer older than its poll interval is read"


def test_a_wake_spreads_the_stale_entries_instead_of_firing_them_together() -> None:
    ctx = _clock_ctx()
    for n in range(30):
        ctx.eval(f"window.primerApi.useResource('list-{n}', __fetcher, {{}});")
        _resolve_request(ctx, n, {"items": []})
    ctx.eval("__hide(); __now += 600000; __show();")
    assert ctx.eval("__requests.length") == 31, "the first one is read at once, the other 29 wait"
    delays = json.loads(ctx.eval("JSON.stringify(__activeTimerDelays())"))
    # review 2026-11-23, P minor: the stagger is uncapped - the entries
    # beyond the 20th keep staggering 100 ms apart instead of piling at
    # the old 2 s cap (2100 .. 2900 here).
    assert sorted(delays) == delays and delays[0] == 100 and max(delays) == 2900 and len(delays) == 29
    # Hiding the tab again drops the waiting reads (they are the entries' own timers).
    ctx.eval("__hide();")
    assert json.loads(ctx.eval("JSON.stringify(__activeTimerDelays())")) == []


def test_a_session_that_ended_stops_every_poll_and_refetch() -> None:
    ctx = _clock_ctx()
    ctx.eval("window.primerApi.useResource('poll', __fetcher, { pollMs: 5000 });")
    _resolve_request(ctx, 0, {"items": []})
    assert json.loads(ctx.eval("JSON.stringify(__activeTimerDelays())")) == [5000]
    ctx.eval("window.primerApi.sessionExpired = true;")
    ctx.eval("__runTimer(0);")  # the poll timer fires: nothing is requested and no new poll is armed
    assert ctx.eval("__requests.length") == 1
    assert json.loads(ctx.eval("JSON.stringify(__activeTimerDelays())")) == []
    ctx.eval("__now += 600000; __hide(); __show(); window.primerApi._refetchAll();")
    assert ctx.eval("__requests.length") == 1


# Review 8.7: the change check compared two whole JSON.stringify copies per poll. It is now one allocation-free walk with an early exit.


def _eq(ctx, a: str, b: str) -> bool:
    return ctx.eval(f"window.primerApi._resource.eq({a}, {b})")


def test_the_change_check_is_structural_and_finds_a_change_anywhere() -> None:
    ctx = _hook_ctx()
    rows = "[" + ",".join(f'{{id:"s{i}",status:"running",binding:{{agent_id:"a"}}}}' for i in range(2000)) + "]"
    assert _eq(ctx, "{items:" + rows + ",total:2000}", "{items:" + rows + ",total:2000}") is True, "a new but identical payload is not a change"
    changed = rows.replace('id:"s1999",status:"running"', 'id:"s1999",status:"ended"')
    assert _eq(ctx, "{items:" + rows + "}", "{items:" + changed + "}") is False, "a field change in the last row is a change"
    assert _eq(ctx, "{a:1,b:undefined}", "{a:1}") is True, "undefined is absent, as in JSON"
    assert _eq(ctx, "{a:1}", "{a:1,b:2}") is False and _eq(ctx, "{a:1,b:2}", "{a:1}") is False
    assert _eq(ctx, "[1,2]", "[1,2,3]") is False and _eq(ctx, "[1]", "{0:1}") is False and _eq(ctx, "null", "{}") is False
    assert _eq(ctx, "{a:1,b:2}", "{b:2,a:1}") is True, "key order is not a change"


def test_the_change_check_ignores_the_failure_a_cut_short_list_carries() -> None:
    ctx = _hook_ctx()
    ctx.eval("var x = { items: [1], truncated: true }; Object.defineProperty(x, 'failure', { value: new Error('a'), enumerable: false });"
             "var y = { items: [1], truncated: true }; Object.defineProperty(y, 'failure', { value: new Error('b'), enumerable: false });")
    assert ctx.eval("window.primerApi._resource.eq(x, y)") is True
