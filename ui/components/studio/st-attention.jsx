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
      <div className="row" style={{ gap: 6, alignItems: "center" }}>
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
      <div className="row" style={{ gap: 6, alignItems: "center", flex: "1 1 auto", minWidth: 0 }}>
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
            onDismissInform={att.dismissInform}
            onFocus={function (it) { setUi({ queue: false, focus: it }); }}
            onClose={function () { setUi({ queue: false, focus: null }); }}
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
          onDismissInform={att.dismissInform}
          onFocus={function (it) { setUi({ queue: false, focus: it }); }}
          onClose={function () { setUi({ queue: false, focus: null }); }}
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

function AttentionQueue({ items, informs, actions, onDismissInform, onFocus, onClose }) {
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
                <span style={{ fontSize: "var(--fs-12)", fontWeight: 600 }}>{it.session_name || it.session_id}</span>
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
          <span className="muted mono" style={{ marginLeft: "auto", fontSize: "var(--fs-11)" }}>
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
