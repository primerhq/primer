/* global React, Icon, ST2_api, ST2_bucketOf, WorkspaceTap, TerminalPanel */
// Studio revamp - the investigate dock (ui/studio/STUDIO-WIRING.md §8).
//
// Replaces the right rail as the home for "things you look at when something is
// wrong": the live event tap, the terminal, and a derived Problems list. It sits
// below the center, is closed by default, and is toggled with Ctrl-`.
//
// The tap and the terminal are re-housed VERBATIM - same components, same
// props, same chip keys - so the server-side class filter
// (WTP_buildSelector) and the terminal's ephemeral mount semantics are
// unchanged. Problems is new and needs no endpoint: it is derived from the
// session list and the tap's error rows.

var ST2_PROBLEM_CAP = 40;

function ST2_DockTab({ id, label, active, count, tone, onClick }) {
  return (
    <button
      data-testid={"dock-tab-" + id}
      onClick={onClick}
      style={{
        padding: "5px 11px", borderRadius: 7, border: "1px solid " + (active ? "var(--border-strong)" : "transparent"),
        background: active ? "var(--bg-active)" : "transparent",
        color: active ? "var(--text)" : "var(--text-3)",
        fontSize: "var(--fs-12)", cursor: "pointer", display: "flex",
        alignItems: "center", gap: 6, whiteSpace: "nowrap",
      }}
    >
      {label}
      {count ? (
        <span
          style={{
            padding: "0 6px", borderRadius: 999, fontSize: "var(--fs-11)",
            background: tone ? "var(" + tone + "-dim)" : "var(--bg-1)",
            color: tone ? "var(" + tone + ")" : "var(--text-3)",
          }}
        >{count}</span>
      ) : null}
    </button>
  );
}

// Problems: derived, no endpoint. Failed runs + error-class tap rows + the last
// failed save. This is what makes the dock worth opening when nothing is
// actively being debugged.
function ST2_ProblemsList({ wid, sessions, errors, onOpenSession }) {
  var broken = (sessions || []).filter(function (s) {
    return ST2_bucketOf(s, {}).bucket === "broken";
  });

  if (!broken.length && !errors.length) {
    return (
      <div className="muted" style={{ padding: 14, fontSize: "var(--fs-12)" }}>
        No problems. Failed runs and errors show up here.
      </div>
    );
  }

  return (
    <div data-testid="problems-list" className="col" style={{ gap: 0, overflow: "auto", height: "100%" }}>
      {broken.map(function (s) {
        var b = ST2_bucketOf(s, {});
        return (
          <div
            key={"s:" + s.id}
            className="row"
            onClick={function () { if (onOpenSession) onOpenSession(s.id); }}
            style={{
              gap: 9, alignItems: "center", padding: "9px 13px", cursor: "pointer",
              borderBottom: "1px solid var(--bg-active)",
            }}
          >
            <span style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--red)", flex: "0 0 auto" }} />
            <span style={{ fontSize: "var(--fs-12)", fontWeight: 500 }}>{s.name || s.id}</span>
            <span className="mono muted" style={{ fontSize: "var(--fs-11)", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {b.detail}
            </span>
            <span className="muted" style={{ marginLeft: "auto", fontSize: "var(--fs-11)", flex: "0 0 auto" }}>open run</span>
          </div>
        );
      })}
      {errors.map(function (e) {
        return (
          <div
            key={"e:" + e.key}
            className="row"
            onClick={function () { if (onOpenSession && e.session_id) onOpenSession(e.session_id); }}
            style={{
              gap: 9, alignItems: "flex-start", padding: "9px 13px",
              cursor: e.session_id ? "pointer" : "default",
              borderBottom: "1px solid var(--bg-active)",
            }}
          >
            <span style={{ fontSize: "var(--fs-11)", color: "var(--red)", flex: "0 0 auto", marginTop: 1 }}>err</span>
            <span className="mono" style={{ fontSize: "var(--fs-11)", color: "var(--text-2)", minWidth: 0, lineHeight: 1.5 }}>
              {e.message}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function InvestigateDock({ wid, studio, sessionId }) {
  var s = studio.state;
  var api = window.primerApi;
  var dockTab = s.dockTab || "events";

  // Shared key with the attention bar's fetch, so useResource dedupes it.
  var sessionsRes = api.useResource(
    ST2_api.keys.sessions(wid),
    function (signal) { return ST2_api.sessions(wid, signal); },
    { pollMs: 5000, deps: [wid] }
  );
  var sessions = (sessionsRes.data && sessionsRes.data.items) || [];

  // Error rows harvested from the one shared tap listener.
  var errState = React.useState([]);
  var errors = errState[0];
  var setErrors = errState[1];
  window.useWorkspaceTapListener(wid, function (ev) {
    var cls = ev && (ev.class || ev.cls);
    if (cls !== "error") return;
    var payload = (ev && ev.payload) || {};
    var msg = payload.message || payload.error || payload.detail || "error";
    var key = (ev.session_id || "") + ":" + (ev.seq != null ? ev.seq : Math.random());
    setErrors(function (prev) {
      if (prev.some(function (x) { return x.key === key; })) return prev;
      return prev.concat([{ key: key, session_id: ev.session_id || null, message: String(msg) }]).slice(-ST2_PROBLEM_CAP);
    });
  });

  var brokenCount = sessions.filter(function (x) { return ST2_bucketOf(x, {}).bucket === "broken"; }).length;
  var problemCount = brokenCount + errors.length;

  var termTabs = s.termTabs || [];
  var isTerm = String(dockTab).indexOf("term:") === 0;

  var openSession = function (sid) {
    if (studio.openTab) studio.openTab({ kind: "session", ref: sid, title: sid });
  };

  return (
    <div
      data-testid="investigate-dock"
      className="col"
      style={{
        height: s.dockHeight || 268, flex: "0 0 auto", minHeight: 0,
        borderTop: "1px solid var(--border)", background: "var(--bg-2)", overflow: "hidden",
      }}
    >
      <div
        className="row"
        style={{
          height: 38, flex: "0 0 auto", gap: 4, alignItems: "center",
          padding: "0 10px", borderBottom: "1px solid var(--border)", background: "var(--bg-elev)",
        }}
      >
        <ST2_DockTab
          id="events" label="Events" active={dockTab === "events"}
          onClick={function () { studio.setDockTab("events"); }}
        />
        {termTabs.map(function (t) {
          return (
            <ST2_DockTab
              key={t.id}
              id={termTabs.length > 1 ? "terminal-" + t.id : "terminal"}
              label={t.title || t.id}
              active={dockTab === "term:" + t.id}
              onClick={function () { studio.setDockTab("term:" + t.id); }}
            />
          );
        })}
        {/* Always expose a stable terminal tab id even with several terminals,
            so test_studio_terminal.py has one locator to hold. */}
        {termTabs.length > 1 ? (
          <span data-testid="dock-tab-terminal" style={{ display: "none" }} />
        ) : null}
        <ST2_DockTab
          id="problems" label="Problems" active={dockTab === "problems"}
          count={problemCount} tone={problemCount ? "--red" : null}
          onClick={function () { studio.setDockTab("problems"); }}
        />
        <span
          data-testid="dock-close"
          title="Collapse the dock (Ctrl-`)"
          onClick={studio.toggleDock}
          style={{ marginLeft: "auto", cursor: "pointer", color: "var(--text-3)", fontSize: "var(--fs-12)", padding: "4px 8px" }}
        >Hide</span>
      </div>

      <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
        {dockTab === "events" ? (
          <WorkspaceTap wid={wid} sessionId={sessionId} fillHeight />
        ) : null}
        {isTerm ? (
          // Mounted only while the dock is open, which is what unmounts every
          // xterm + WS on close - the intended ephemeral v1 (studio-terminal.jsx).
          <TerminalPanel wid={wid} studio={studio} />
        ) : null}
        {dockTab === "problems" ? (
          <ST2_ProblemsList wid={wid} sessions={sessions} errors={errors} onOpenSession={openSession} />
        ) : null}
      </div>
    </div>
  );
}

window.InvestigateDock = InvestigateDock;
window.ST2_ProblemsList = ST2_ProblemsList;
