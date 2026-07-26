/* global React, Icon, ST2_api, ST2_bucketOf, SA_informFromEvent, ST_sessionTranscriptRows */
// Studio revamp - the attention bar (ui/studio/STUDIO-WIRING.md §3, §9).
//
// Replaces the right rail's ActionRequired housing. The bar is ALWAYS mounted
// between the header and the body, including when empty: "Nothing needs you"
// is the feature, not an empty state to be collapsed away.
//
// Handlers and the inform capture are ported from studio-activity.jsx
// unchanged in behaviour; what changes is that the writes now go through
// useMutation with `invalidates` instead of a hand-rolled
// SA_invalidateSessionPending + setTimeout(refetch, 800) pair.

var ST2_PROMPT_CHARS = 80;
var ST2_INFORM_CAP = 30;

function ST2_clip(text, max) {
  var s = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

function ST2_kindCopy(kind) {
  if (kind === "approval" || kind === "ask_approval") return "wants approval";
  if (kind === "ask_user") return "asked a question";
  if (kind === "watch_files") return "waiting on a file";
  if (kind === "sleep") return "sleeping";
  return kind ? String(kind) : "parked";
}

// ---------------------------------------------------------------------------
// ?focus=<tool_call_id> deep links (§9, §2.3)
//
// The point is a URL you can paste into Slack: "this needs you". That makes the
// link's lifetime longer than the yield's, so the id is very often stale by the
// time someone clicks - already approved, denied, or the session ended. A stale
// link must land somewhere useful and SAY it is stale; silently opening an empty
// focus panel, or worse the wrong item, is what makes a feature like this
// untrustworthy.
//
// The Studio is a hash router, so the query lives in the hash fragment.
// ---------------------------------------------------------------------------

// The parse/serialise pair is deliberately pure string work rather than
// URL / URLSearchParams. Those are Web APIs, not ECMAScript, so anything built
// on them is only exercisable in a real browser - which for a query-string
// edge case ("does writing focus drop ?open=") means it effectively is not
// exercised at all. Keeping the logic pure puts it under test; the Web API stays
// at the boundary in ST2_syncFocusUrl / ST2_copyFocusLink.

function ST2_splitHash(hash) {
  var h = String(hash == null ? "" : hash) || "#/";
  var qIdx = h.indexOf("?");
  return {
    path: qIdx >= 0 ? h.slice(0, qIdx) : h,
    query: qIdx >= 0 ? h.slice(qIdx + 1) : "",
  };
}

// [[key, value], ...] preserving order, so rewriting one param cannot reorder
// or drop the others.
function ST2_parseQuery(query) {
  if (!query) return [];
  return String(query).split("&").filter(Boolean).map(function (pair) {
    var eq = pair.indexOf("=");
    var k = eq < 0 ? pair : pair.slice(0, eq);
    var v = eq < 0 ? "" : pair.slice(eq + 1);
    try { return [decodeURIComponent(k), decodeURIComponent(v)]; }
    catch (_e) { return [k, v]; }
  });
}

function ST2_buildQuery(pairs) {
  return pairs.map(function (p) {
    return encodeURIComponent(p[0]) + "=" + encodeURIComponent(p[1]);
  }).join("&");
}

// ST2_focusFromHash(hash) -> the ?focus= value, or null.
function ST2_focusFromHash(hash) {
  var found = null;
  ST2_parseQuery(ST2_splitHash(hash).query).forEach(function (p) {
    if (p[0] === "focus") found = p[1];
  });
  return found || null;
}

// ST2_hashWithFocus(hash, tcid) -> the hash with focus set (or removed when
// tcid is falsy), every other param preserved in place.
function ST2_hashWithFocus(hash, tcid) {
  var parts = ST2_splitHash(hash);
  var pairs = ST2_parseQuery(parts.query).filter(function (p) { return p[0] !== "focus"; });
  if (tcid) pairs.push(["focus", String(tcid)]);
  var qs = ST2_buildQuery(pairs);
  return qs ? parts.path + "?" + qs : parts.path;
}

function ST2_focusFromUrl() {
  try {
    return ST2_focusFromHash(window.location.hash);
  } catch (_e) {
    return null;
  }
}

// Mirror the focused item into the URL with replaceState - no history entry per
// focus change, same idiom as the Studio's ?open= mirroring.
function ST2_syncFocusUrl(tcid) {
  try {
    var next = ST2_hashWithFocus(window.location.hash, tcid);
    window.history.replaceState(null, "", ST2_baseUrl() + next);
    return next;
  } catch (_e) {
    return null;
  }
}

function ST2_baseUrl() {
  var loc = window.location;
  return loc.origin + loc.pathname + (loc.search || "");
}

// Build the shareable URL for a tool_call_id WITHOUT navigating, so "Copy link"
// cannot move the reader off the panel they are answering.
function ST2_focusLinkFor(tcid) {
  try {
    return ST2_baseUrl() + ST2_hashWithFocus(window.location.hash, tcid);
  } catch (_e) {
    return null;
  }
}

function ST2_copyFocusLink(tcid) {
  var link = ST2_focusLinkFor(tcid);
  if (!link) return;
  var toast = window.primerApi && window.primerApi.toastPush;
  var ok = function () {
    if (toast) toast({ kind: "success", title: "Link copied", detail: tcid });
  };
  var fail = function () {
    // Clipboard access is refused outside a secure context, so surface the URL
    // rather than reporting a copy that did not happen.
    if (toast) toast({ kind: "error", title: "Could not copy", detail: link });
  };
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(link).then(ok, fail);
    } else {
      fail();
    }
  } catch (_e) {
    fail();
  }
}

// ST2_resolveFocusTarget(tcid, items, loading) -> {kind, item?}
//   "wait"    - still loading, decide nothing yet
//   "none"    - no ?focus= in the URL
//   "focus"   - the id is live; open it
//   "stale"   - the id is gone; open the queue and say so
function ST2_resolveFocusTarget(tcid, items, loading) {
  if (!tcid) return { kind: "none" };
  if (loading) return { kind: "wait" };
  var match = (items || []).filter(function (i) { return i.tool_call_id === tcid; })[0];
  return match ? { kind: "focus", item: match } : { kind: "stale" };
}

// ---------------------------------------------------------------------------
// Data: one resource, one tap listener, informs in local state
// ---------------------------------------------------------------------------

function ST2_useAttention(wid) {
  var api = window.primerApi;
  var useResource = api.useResource;

  var pending = useResource(
    ST2_api.keys.pending(wid),
    function (signal) { return ST2_api.pendingYields(wid, signal); },
    { pollMs: 15000, deps: [wid] }
  );

  var items = (pending.data && Array.isArray(pending.data.items)) ? pending.data.items : [];

  // Oldest first. Never sort by session name - the queue is a work order.
  var ordered = React.useMemo(function () {
    var copy = items.slice();
    copy.sort(function (a, b) {
      var av = a && a.waiting_since ? String(a.waiting_since) : "";
      var bv = b && b.waiting_since ? String(b.waiting_since) : "";
      if (av && bv) return av < bv ? -1 : (av > bv ? 1 : 0);
      return 0; // fall back to the server's order
    });
    return copy;
  }, [pending.data]); // eslint-disable-line

  // inform_user rows captured live from the tap. They have no pending-yield
  // backing, so they live here, are capped, and "Dismiss" is client-side only.
  var informs = React.useState([]);
  var informList = informs[0];
  var setInformList = informs[1];

  var debounceRef = React.useRef(null);
  window.useWorkspaceTapListener(wid, function (ev) {
    var inform = typeof SA_informFromEvent === "function" ? SA_informFromEvent(ev) : null;
    if (inform) {
      setInformList(function (prev) {
        if (prev.some(function (x) { return x.key === inform.key; })) return prev;
        return prev.concat([inform]).slice(-ST2_INFORM_CAP);
      });
      return;
    }
    var cls = ev && (ev.class || ev.cls);
    if (cls !== "yielded" && cls !== "done") return;
    // Wake the poll rather than replacing it; the 15s pollMs is the backstop.
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(function () { pending.refetch(); }, 300);
  });

  React.useEffect(function () {
    return function () { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, []);

  var dismissInform = function (key) {
    setInformList(function (prev) { return prev.filter(function (x) { return x.key !== key; }); });
  };

  return {
    items: ordered,
    loading: pending.loading,
    refetch: pending.refetch,
    informs: informList,
    dismissInform: dismissInform,
  };
}

// Writes. One mutation per action so each row can show its own error inline
// without losing a typed draft.
function ST2_useYieldActions(wid) {
  var api = window.primerApi;
  var useMutation = api.useMutation;
  var errs = React.useState({});
  var errors = errs[0];
  var setErrors = errs[1];
  var hidden = React.useState({});
  var hiddenMap = hidden[0];
  var setHidden = hidden[1];

  var invalidates = [ST2_api.keys.pending(wid)];

  // Also refetch the per-session pending cache the inline transcript card
  // reads, so answering in one place updates the other.
  var alsoSession = function (sid) {
    var r = window.primerApi._resource;
    if (!r || !sid) return;
    var key = ST2_api.keys.sessionPending(sid);
    (r.findKeys(key) || [key]).forEach(function (k) { r.refetchKey(k); });
  };

  var fail = function (tcid, label) {
    return function (err) {
      setHidden(function (p) { var n = Object.assign({}, p); delete n[tcid]; return n; });
      setErrors(function (p) {
        var n = Object.assign({}, p);
        n[tcid] = (err && (err.detail || err.title || err.message)) || (label + " failed");
        return n;
      });
    };
  };
  var start = function (tcid) {
    setHidden(function (p) { var n = Object.assign({}, p); n[tcid] = true; return n; });
    setErrors(function (p) { var n = Object.assign({}, p); delete n[tcid]; return n; });
  };

  var approve = useMutation(
    function (v) { return ST2_api.approve(v.sid, v.tcid); },
    { invalidates: invalidates }
  );
  var deny = useMutation(
    function (v) { return ST2_api.deny(v.sid, v.tcid, v.reason); },
    { invalidates: invalidates }
  );
  var answer = useMutation(
    function (v) { return ST2_api.answer(v.sid, v.tcid, v.response); },
    { invalidates: invalidates }
  );
  var cancel = useMutation(
    function (v) { return ST2_api.cancelYield(v.sid, v.tcid, v.reason); },
    { invalidates: invalidates }
  );

  var run = function (m, label) {
    return function (item, extra) {
      // Every action guards on tool_call_id: the /yields/pending item shape
      // allows null (malformed or legacy parks), and such a row must render
      // but never POST.
      if (!item || !item.tool_call_id) return;
      var tcid = item.tool_call_id;
      start(tcid);
      m.mutate(Object.assign({ sid: item.session_id, tcid: tcid }, extra || {}))
        .then(function () { alsoSession(item.session_id); }, fail(tcid, label));
    };
  };

  return {
    approve: run(approve, "Approve"),
    deny: run(deny, "Deny"),
    answer: run(answer, "Send"),
    cancel: run(cancel, "Cancel"),
    errors: errors,
    hidden: hiddenMap,
  };
}

// ---------------------------------------------------------------------------
// Per-kind controls
// ---------------------------------------------------------------------------

function ST2_YieldControls({ item, actions, compact }) {
  var actionable = !!(item && item.tool_call_id);
  var draft = React.useState("");
  var text = draft[0];
  var setText = draft[1];
  var kind = item && item.kind;
  var dis = { opacity: actionable ? 1 : 0.45, cursor: actionable ? "pointer" : "not-allowed" };
  var btn = function (extra) {
    return Object.assign({
      padding: compact ? "3px 9px" : "5px 11px", borderRadius: 7,
      fontSize: "var(--fs-12)", border: "1px solid var(--border-strong)",
      background: "var(--bg-active)", color: "var(--text)",
    }, dis, extra || {});
  };

  if (kind === "approval" || kind === "ask_approval") {
    return (
      <div
        className="row"
        data-testid="action-approval-controls"
        style={{ gap: 6, alignItems: "center" }}
      >
        <button
          data-testid="approve" disabled={!actionable}
          title={actionable ? "Approve (Enter)" : "This park has no tool_call_id and cannot be answered"}
          onClick={function () { actions.approve(item); }}
          style={btn({ background: "var(--green-dim)", borderColor: "var(--green)", color: "var(--green)" })}
        >Approve ⏎</button>
        <button
          data-testid="reject" disabled={!actionable}
          onClick={function () { actions.deny(item, { reason: "" }); }}
          style={btn()}
        >Deny</button>
      </div>
    );
  }

  if (kind === "ask_user") {
    var send = function () {
      if (!actionable || !text.trim()) return;
      actions.answer(item, { response: text });
      setText("");
    };
    return (
      <div
        className="row"
        data-testid="action-ask-controls"
        style={{ gap: 6, alignItems: "center", flex: "1 1 auto", minWidth: 0 }}
      >
        <input
          data-testid="respond-input"
          value={text}
          disabled={!actionable}
          placeholder="Type a reply…"
          onChange={function (e) { setText(e.target.value); }}
          onKeyDown={function (e) {
            // Enter sends, Shift+Enter newlines - matches the shipped
            // handleRespondKeyDown behaviour.
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
          }}
          style={{
            flex: "1 1 auto", minWidth: 0, background: "var(--bg-1)",
            border: "1px solid var(--border)", borderRadius: 7,
            padding: "5px 9px", color: "var(--text)", fontSize: "var(--fs-12)",
          }}
        />
        <button data-testid="respond" disabled={!actionable} onClick={send} style={btn()}>Send ⏎</button>
      </div>
    );
  }

  // watch_files / sleep: nothing to answer, only to abandon.
  return (
    <button
      data-testid="cancel-yield" disabled={!actionable}
      onClick={function () { actions.cancel(item, { reason: "operator cancelled" }); }}
      style={btn()}
    >Cancel</button>
  );
}

// ---------------------------------------------------------------------------
// The bar
// ---------------------------------------------------------------------------

function AttentionBar({ wid, studio }) {
  var att = ST2_useAttention(wid);
  var actions = ST2_useYieldActions(wid);
  // The calm state reports how many runs are working. The bar owns this fetch
  // so nothing extra is polled while the revamp is switched off.
  var sessionsRes = window.primerApi.useResource(
    ST2_api.keys.sessions(wid),
    function (signal) { return ST2_api.sessions(wid, signal); },
    { pollMs: 5000, deps: [wid] }
  );
  var sessions = (sessionsRes.data && sessionsRes.data.items) || [];
  var open = React.useState({ queue: false, focus: null });
  var ui = open[0];
  var setUi = open[1];

  var visible = att.items.filter(function (i) { return !actions.hidden[i.tool_call_id]; });
  var count = visible.length;
  var head = visible[0] || null;
  var queueOpen = ui.queue;
  var focusItem = ui.focus;

  // Resolve ?focus= once the pending snapshot has actually landed. Applied at
  // most once per distinct URL value (appliedFocusRef), so the mirroring below
  // cannot feed itself: focusing an item writes the URL, and without this guard
  // that write would be read straight back as a fresh deep-link.
  var appliedFocusRef = React.useRef(null);
  React.useEffect(function () {
    var tcid = ST2_focusFromUrl();
    if (!tcid || appliedFocusRef.current === tcid) return;
    var target = ST2_resolveFocusTarget(tcid, visible, att.loading);
    if (target.kind === "wait") return;
    appliedFocusRef.current = tcid;
    if (target.kind === "focus") {
      setUi({ queue: false, focus: target.item, staleFocus: null });
    } else if (target.kind === "stale") {
      // Land on the queue rather than an empty panel, and name the reason.
      setUi({ queue: true, focus: null, staleFocus: tcid });
      ST2_syncFocusUrl(null);
    }
  }, [att.loading, visible.length]); // eslint-disable-line

  // Keep the URL pointing at whatever is focused, so the address bar is always
  // the shareable link without a separate "copy link" round trip.
  React.useEffect(function () {
    var tcid = focusItem && focusItem.tool_call_id;
    if (tcid) appliedFocusRef.current = tcid;
    ST2_syncFocusUrl(tcid || null);
  }, [focusItem]);

  // Keyboard is registered ONLY while an overlay is open - j/k/d/c would
  // otherwise eat typing anywhere in Studio.
  React.useEffect(function () {
    if (!queueOpen && !focusItem) return undefined;
    var onKey = function (e) {
      var tag = (e.target && e.target.tagName) || "";
      var typing = tag === "INPUT" || tag === "TEXTAREA";
      if (e.key === "Escape") { setUi({ queue: false, focus: null }); return; }
      if (typing) return;
      var item = focusItem || head;
      if (!item) return;
      if (e.key === "Enter" && (item.kind === "approval" || item.kind === "ask_approval")) {
        e.preventDefault(); actions.approve(item);
      } else if (e.key === "d") { e.preventDefault(); actions.deny(item, { reason: "" }); }
      else if (e.key === "c") { e.preventDefault(); actions.cancel(item, {}); }
    };
    window.addEventListener("keydown", onKey);
    return function () { window.removeEventListener("keydown", onKey); };
  }, [queueOpen, focusItem, head]); // eslint-disable-line

  var workingCount = (sessions || []).filter(function (s) {
    return ST2_bucketOf(s, {}).bucket === "working";
  }).length;

  var barBase = {
    height: 44, flex: "0 0 auto", gap: 10, alignItems: "center",
    padding: "0 14px", borderBottom: "1px solid var(--border)", position: "relative",
  };

  // First paint: hold the calm height rather than reflowing.
  if (att.loading && !att.items.length) {
    return (
      <div className="row" data-testid="attention-bar" style={Object.assign({ background: "var(--bg-2)" }, barBase)}>
        <span className="muted" style={{ fontSize: "var(--fs-12)" }}>…</span>
      </div>
    );
  }

  if (count === 0) {
    return (
      <div
        className="row" data-testid="attention-bar"
        style={Object.assign({ background: "var(--bg-2)" }, barBase)}
      >
        <span data-testid="attention-bar-calm" className="row" style={{ gap: 8, alignItems: "center" }}>
          <span style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--green)" }} />
          <span style={{ fontSize: "var(--fs-12)", color: "var(--text)" }}>Nothing needs you.</span>
        </span>
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
          {workingCount ? workingCount + (workingCount === 1 ? " run working" : " runs working") : "no runs working"}
        </span>
        {att.informs.length ? (
          <span
            className="row" style={{ marginLeft: "auto", gap: 6, cursor: "pointer" }}
            onClick={function () { setUi({ queue: true, focus: null }); }}
          >
            <span style={{ fontSize: "var(--fs-11)", color: "var(--violet)" }}>
              {att.informs.length} update{att.informs.length === 1 ? "" : "s"}
            </span>
          </span>
        ) : null}
        {queueOpen ? (
          <AttentionQueue
            items={visible} informs={att.informs} actions={actions}
            staleFocus={ui.staleFocus}
            onDismissInform={att.dismissInform}
            onFocus={function (it) { setUi({ queue: false, focus: it }); }}
            onClose={function () { setUi({ queue: false, focus: null, staleFocus: null }); }}
          />
        ) : null}
      </div>
    );
  }

  return (
    <div
      className="row" data-testid="attention-bar"
      style={Object.assign({
        background: "var(--amber-dim)", borderBottom: "1px solid var(--amber)",
      }, barBase)}
    >
      {/* action-required kept so existing journeys still resolve (§13). */}
      <span
        data-testid="action-required-count"
        style={{
          padding: "2px 9px", borderRadius: 999, background: "var(--amber)",
          color: "var(--bg-1)", fontSize: "var(--fs-11)", fontWeight: 600,
        }}
      >
        <span data-testid="attention-count">{count}</span>
      </span>
      <span data-testid="action-required" className="row" style={{ gap: 8, alignItems: "center", minWidth: 0, flex: "1 1 auto" }}>
        <span data-testid="attention-head-item" className="row" style={{ gap: 8, alignItems: "center", minWidth: 0 }}>
          <span data-testid="action-session-link" style={{ fontSize: "var(--fs-12)", fontWeight: 600, color: "var(--text)", whiteSpace: "nowrap" }}>
            {head.session_name || head.session_id}
          </span>
          <span data-testid="user-interaction-label" style={{ fontSize: "var(--fs-11)", color: "var(--amber)" }}>
            {ST2_kindCopy(head.kind)}
          </span>
          <span className="muted" style={{ fontSize: "var(--fs-11)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>
            {ST2_clip(head.prompt || head.command || "", ST2_PROMPT_CHARS)}
          </span>
        </span>
      </span>
      <div data-testid="action-item" className="row" style={{ gap: 6, alignItems: "center", flex: "0 1 auto" }}>
        <ST2_YieldControls item={head} actions={actions} compact />
        {actions.errors[head.tool_call_id] ? (
          <span style={{ fontSize: "var(--fs-11)", color: "var(--red)" }}>{actions.errors[head.tool_call_id]}</span>
        ) : null}
        <button
          onClick={function () { setUi({ queue: false, focus: head }); }}
          style={{
            padding: "3px 9px", borderRadius: 7, background: "var(--bg-active)",
            border: "1px solid var(--border-strong)", color: "var(--text)",
            fontSize: "var(--fs-12)", cursor: "pointer",
          }}
        >See context</button>
        <button
          onClick={function () { setUi({ queue: !queueOpen, focus: null }); }}
          style={{
            padding: "3px 9px", borderRadius: 7, background: "transparent",
            border: "1px solid var(--border)", color: "var(--text-2)",
            fontSize: "var(--fs-12)", cursor: "pointer",
          }}
        >Inbox ▾</button>
      </div>

      {queueOpen ? (
        <AttentionQueue
          items={visible} informs={att.informs} actions={actions}
          staleFocus={ui.staleFocus}
          onDismissInform={att.dismissInform}
          onFocus={function (it) { setUi({ queue: false, focus: it }); }}
          onClose={function () { setUi({ queue: false, focus: null, staleFocus: null }); }}
        />
      ) : null}
      {focusItem ? (
        <UnblockFocus
          wid={wid} item={focusItem} queue={visible} actions={actions}
          onAdvance={function () {
            var next = visible.filter(function (i) { return i.tool_call_id !== focusItem.tool_call_id; })[0] || null;
            setUi({ queue: false, focus: next });
          }}
          onClose={function () { setUi({ queue: false, focus: null }); }}
        />
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Queue popover
// ---------------------------------------------------------------------------

function AttentionQueue({ items, informs, actions, staleFocus, onDismissInform, onFocus, onClose }) {
  return (
    <div
      data-testid="attention-queue"
      style={{
        position: "absolute", top: 44, right: 12, zIndex: 40, width: 460,
        background: "var(--bg-elev)", border: "1px solid var(--border-strong)",
        borderRadius: 12, boxShadow: "0 30px 60px -20px rgba(0,0,0,.75)", overflow: "hidden",
      }}
    >
      <div className="row" style={{ padding: "10px 13px", borderBottom: "1px solid var(--border)", alignItems: "center" }}>
        <span style={{ fontSize: "var(--fs-12)", fontWeight: 600 }}>Needs you</span>
        <span className="muted" style={{ marginLeft: "auto", fontSize: "var(--fs-11)", cursor: "pointer" }} onClick={onClose}>esc</span>
      </div>
      {/* A shared ?focus= link outlives the yield it points at, so say plainly
          that this one is already handled rather than showing an empty panel
          and letting the reader assume the link was broken (§9). */}
      {staleFocus ? (
        <div
          data-testid="focus-resolved-note"
          style={{
            padding: "8px 13px", fontSize: "var(--fs-11)",
            background: "var(--bg-1)", color: "var(--text-2)",
            borderBottom: "1px solid var(--border)",
          }}
        >
          That request was already handled. Here is what still needs you.
        </div>
      ) : null}
      <div data-testid="action-required-list" style={{ maxHeight: 320, overflow: "auto" }}>
        {!items.length ? (
          <div data-testid="action-required-empty" className="muted" style={{ padding: 14, fontSize: "var(--fs-12)" }}>
            Nothing needs you.
          </div>
        ) : null}
        {items.map(function (it) {
          return (
            <div
              key={it.tool_call_id || it.session_id}
              data-testid="attention-queue-item"
              className="col"
              style={{ gap: 7, padding: "11px 13px", borderBottom: "1px solid var(--bg-active)" }}
            >
              <div className="row" style={{ gap: 8, alignItems: "center", minWidth: 0 }}>
                <span
                  data-testid="action-session-link"
                  style={{ fontSize: "var(--fs-12)", fontWeight: 600, cursor: "pointer" }}
                  onClick={function () { onFocus(it); }}
                >{it.session_name || it.session_id}</span>
                <span style={{ fontSize: "var(--fs-11)", color: "var(--amber)" }}>{ST2_kindCopy(it.kind)}</span>
                <span
                  className="muted" style={{ marginLeft: "auto", fontSize: "var(--fs-11)", cursor: "pointer" }}
                  onClick={function () { onFocus(it); }}
                >See context</span>
              </div>
              {it.prompt ? (
                <div className="muted" style={{ fontSize: "var(--fs-11)", lineHeight: 1.5 }}>{ST2_clip(it.prompt, 160)}</div>
              ) : null}
              <ST2_YieldControls item={it} actions={actions} />
              {actions.errors[it.tool_call_id] ? (
                <div style={{ fontSize: "var(--fs-11)", color: "var(--red)" }}>{actions.errors[it.tool_call_id]}</div>
              ) : null}
            </div>
          );
        })}

        {/* Informs are one-way: no pending-yield backing, never counted as
            "needs you", dismissal is client-side only. */}
        {informs.map(function (inf) {
          return (
            <div
              key={inf.key} data-testid="inform-item" className="row"
              style={{ gap: 8, padding: "9px 13px", alignItems: "flex-start", background: "var(--bg-2)" }}
            >
              <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--violet)", marginTop: 5, flex: "0 0 auto" }} />
              <span data-testid="inform-message" className="muted" style={{ fontSize: "var(--fs-11)", lineHeight: 1.5, minWidth: 0 }}>
                {inf.message}
              </span>
              <span
                data-testid="inform-dismiss"
                onClick={function () { onDismissInform(inf.key); }}
                style={{ marginLeft: "auto", fontSize: "var(--fs-11)", color: "var(--text-3)", cursor: "pointer", flex: "0 0 auto" }}
              >Dismiss</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Unblock focus (§9)
// ---------------------------------------------------------------------------

function UnblockFocus({ wid, item, queue, actions, onAdvance, onClose }) {
  var actionable = !!(item && item.tool_call_id);
  var nextUp = (queue || []).filter(function (i) { return i.tool_call_id !== item.tool_call_id; });

  // Consequences, derived honestly: for a shell/file tool show the command and
  // its arguments; for anything else show the raw arguments JSON. There is no
  // backend preview API and this must not pretend otherwise.
  var args = item && (item.arguments || item.args) || null;
  var argsText = null;
  if (args) {
    try { argsText = typeof args === "string" ? args : JSON.stringify(args, null, 2); }
    catch (_e) { argsText = String(args); }
  }

  return (
    <div
      className="modal-overlay" data-testid="unblock-focus"
      onClick={function (e) { if (e.target === e.currentTarget) onClose(); }}
      style={{ alignItems: "flex-start", paddingTop: 80 }}
    >
      <div
        className="modal col"
        style={{ width: "min(720px, 94vw)", gap: 0, background: "var(--bg-elev)", border: "1px solid var(--border-strong)", borderRadius: 12, overflow: "hidden" }}
      >
        <div className="row" style={{ padding: 14, borderBottom: "1px solid var(--border)", alignItems: "center", gap: 9 }}>
          <span style={{ fontSize: "var(--fs-13)", fontWeight: 600 }}>{item.session_name || item.session_id}</span>
          <span style={{ fontSize: "var(--fs-11)", color: "var(--amber)" }}>{ST2_kindCopy(item.kind)}</span>
          {/* The address bar already carries ?focus= for whatever is open here,
              so this is a convenience over the same URL, not a second source. */}
          {actionable ? (
            <span
              data-testid="focus-copy-link"
              title="Copy a link to this request"
              onClick={function () { ST2_copyFocusLink(item.tool_call_id); }}
              style={{ marginLeft: "auto", fontSize: "var(--fs-11)", color: "var(--text-3)", cursor: "pointer" }}
            >Copy link</span>
          ) : null}
          <span
            className="muted mono"
            style={{ marginLeft: actionable ? 0 : "auto", fontSize: "var(--fs-11)" }}
          >
            {item.tool_call_id || "no tool_call_id"}
          </span>
        </div>

        <div className="col" style={{ gap: 14, padding: 14, maxHeight: "58vh", overflow: "auto" }}>
          {item.prompt ? (
            <div style={{ fontSize: "var(--fs-13)", lineHeight: 1.6, color: "var(--text)" }}>{item.prompt}</div>
          ) : null}

          <div className="col" data-testid="unblock-consequences" style={{ gap: 6 }}>
            <div style={{ fontSize: "var(--fs-11)", textTransform: "uppercase", letterSpacing: ".07em", color: "var(--text-3)", fontWeight: 600 }}>
              What it will do
            </div>
            {item.command ? (
              <div className="mono" style={{ fontSize: "var(--fs-12)", background: "var(--bg-1)", border: "1px solid var(--border)", borderRadius: 8, padding: "9px 11px", color: "var(--text)" }}>
                $ {item.command}
              </div>
            ) : argsText ? (
              <pre className="mono" style={{ margin: 0, fontSize: "var(--fs-11)", background: "var(--bg-1)", border: "1px solid var(--border)", borderRadius: 8, padding: "9px 11px", color: "var(--text-2)", overflow: "auto", maxHeight: 200 }}>
                {argsText}
              </pre>
            ) : (
              <div className="muted" style={{ fontSize: "var(--fs-11)" }}>
                This park carries no arguments to preview.
              </div>
            )}
          </div>

          {nextUp.length ? (
            <div className="col" data-testid="unblock-next" style={{ gap: 6 }}>
              <div style={{ fontSize: "var(--fs-11)", textTransform: "uppercase", letterSpacing: ".07em", color: "var(--text-3)", fontWeight: 600 }}>
                Next in queue · {nextUp.length}
              </div>
              {nextUp.slice(0, 3).map(function (n) {
                return (
                  <div key={n.tool_call_id || n.session_id} className="row muted" style={{ gap: 8, fontSize: "var(--fs-11)" }}>
                    <span>{n.session_name || n.session_id}</span>
                    <span style={{ color: "var(--amber)" }}>{ST2_kindCopy(n.kind)}</span>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>

        <div className="row" style={{ padding: 13, borderTop: "1px solid var(--border)", gap: 8, alignItems: "center", background: "var(--bg-2)" }}>
          <ST2_YieldControls item={item} actions={actions} />
          {!actionable ? (
            <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
              This park has no tool_call_id, so it cannot be answered from here.
            </span>
          ) : null}
          <span
            className="muted" style={{ marginLeft: "auto", fontSize: "var(--fs-11)", cursor: "pointer" }}
            onClick={onAdvance}
          >Skip</span>
        </div>
      </div>
    </div>
  );
}

window.AttentionBar = AttentionBar;
window.AttentionQueue = AttentionQueue;
window.UnblockFocus = UnblockFocus;
window.ST2_useAttention = ST2_useAttention;
window.ST2_useYieldActions = ST2_useYieldActions;
window.ST2_kindCopy = ST2_kindCopy;
window.ST2_focusFromUrl = ST2_focusFromUrl;
window.ST2_syncFocusUrl = ST2_syncFocusUrl;
window.ST2_resolveFocusTarget = ST2_resolveFocusTarget;
window.ST2_focusLinkFor = ST2_focusLinkFor;
window.ST2_focusFromHash = ST2_focusFromHash;
window.ST2_hashWithFocus = ST2_hashWithFocus;
