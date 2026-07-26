/* global React, Icon, ST2_api, ST2_bucketOf, ST2_kindCopy, ST2_useAttention,
   ST2_useYieldActions, ST2_YieldControls, ST_sessionSort */
// Studio revamp - the phone layout (ui/studio/STUDIO-WIRING.md §11).
//
// Triage and unblock, NOT a three-column shrink. The things a three-column
// shrink would give you on a phone - a file editor, a terminal, a live event
// feed - are all unusable at that width, and shipping them cramped is worse
// than saying they are desktop-only, because a cramped editor invites an edit
// you cannot review.
//
// So: answer what is blocking, read what happened, and nothing else.
//
// The bottom bar keeps the studio-left-toggle / studio-right-toggle testids on
// its items (§13). The drawers they used to open are gone, but the shipped
// mobile journeys locate the bar by them, and a testid is a contract with those
// journeys, not a description of the drawer that once existed.

var ST2_MOBILE_TABS = [
  { id: "needs", label: "Needs you", icon: "bell", testId: "studio-left-toggle" },
  { id: "runs", label: "Runs", icon: "agent", testId: "studio-right-toggle" },
  { id: "changes", label: "Changes", icon: "git-commit", testId: "studio-changes-toggle" },
];

// Suggestion chips so answering an ask_user is one tap. Only for ask_user:
// offering "Yes" on an approval would put a one-tap authorisation of an
// arbitrary command behind a chip, which is the last thing this should do.
var ST2_ASK_SUGGESTIONS = ["Yes", "No", "Go ahead"];

function ST2_MobileBottomBar({ tab, counts, onPick }) {
  return (
    <div
      data-testid="studio-mobile-bar"
      className="row mobile-only"
      style={{
        flex: "0 0 auto", height: 56, alignItems: "stretch",
        borderTop: "1px solid var(--border)", background: "var(--bg-elev)",
      }}
    >
      {ST2_MOBILE_TABS.map(function (t) {
        var active = tab === t.id;
        var n = counts[t.id];
        return (
          <button
            key={t.id}
            data-testid={t.testId}
            aria-pressed={active}
            onClick={function () { onPick(t.id); }}
            style={{
              // 44px minimum target (§11); the bar is 56 tall and each item
              // takes an equal share of the width.
              flex: 1, minHeight: 44, border: "none", background: "transparent",
              display: "flex", flexDirection: "column", alignItems: "center",
              justifyContent: "center", gap: 2, cursor: "pointer",
              color: active ? "var(--accent)" : "var(--text-3)",
            }}
          >
            <span style={{ position: "relative" }}>
              <Icon name={t.icon} size={17} />
              {n ? (
                <span
                  data-testid={"mobile-badge-" + t.id}
                  style={{
                    position: "absolute", top: -4, right: -9, minWidth: 15,
                    padding: "0 3px", borderRadius: 999, background: "var(--amber)",
                    color: "var(--bg-1)", fontSize: 9, fontWeight: 700, lineHeight: "15px",
                  }}
                >{n}</span>
              ) : null}
            </span>
            <span style={{ fontSize: "var(--fs-11)" }}>{t.label}</span>
          </button>
        );
      })}
    </div>
  );
}

// Full-bleed cards, one per blocking request - the home view on a phone.
function ST2_MobileNeeds({ items, actions }) {
  if (!items.length) {
    return (
      <div data-testid="mobile-needs-empty" className="col" style={{ padding: 24, gap: 6, alignItems: "center" }}>
        <span style={{ fontSize: "var(--fs-13)", fontWeight: 600 }}>Nothing needs you.</span>
        <span className="muted" style={{ fontSize: "var(--fs-12)", textAlign: "center" }}>
          Anything waiting on an answer shows up here.
        </span>
      </div>
    );
  }
  return (
    <div data-testid="mobile-needs" className="col" style={{ gap: 10, padding: 10 }}>
      {items.map(function (item) {
        return (
          <div
            key={item.tool_call_id || item.session_id}
            data-testid="mobile-needs-card"
            className="col"
            style={{
              gap: 9, padding: 13, borderRadius: 11,
              background: "var(--bg-elev)", border: "1px solid var(--amber)",
            }}
          >
            <div className="row" style={{ gap: 7, alignItems: "baseline", flexWrap: "wrap" }}>
              <span style={{ fontSize: "var(--fs-13)", fontWeight: 600 }}>
                {item.session_name || item.session_id}
              </span>
              <span style={{ fontSize: "var(--fs-11)", color: "var(--amber)" }}>
                {ST2_kindCopy(item.kind)}
              </span>
            </div>
            {item.prompt ? (
              <div style={{ fontSize: "var(--fs-12)", lineHeight: 1.5, color: "var(--text-2)" }}>
                {item.prompt}
              </div>
            ) : null}
            {item.kind === "ask_user" ? (
              <div className="row" data-testid="mobile-suggestions" style={{ gap: 6, flexWrap: "wrap" }}>
                {ST2_ASK_SUGGESTIONS.map(function (s) {
                  return (
                    <button
                      key={s}
                      onClick={function () { actions.answer(item, { response: s }); }}
                      style={{
                        minHeight: 44, padding: "0 15px", borderRadius: 9,
                        border: "1px solid var(--border-strong)", background: "var(--bg-1)",
                        color: "var(--text)", fontSize: "var(--fs-12)", cursor: "pointer",
                      }}
                    >{s}</button>
                  );
                })}
              </div>
            ) : null}
            <ST2_YieldControls item={item} actions={actions} />
          </div>
        );
      })}
    </div>
  );
}

// Read-only list; tapping a run opens its transcript. No editing on a phone.
function ST2_MobileRuns({ sessions, pendingBySession, onOpen }) {
  var rows = ST_sessionSort(sessions || []);
  if (!rows.length) {
    return (
      <div data-testid="mobile-runs-empty" className="muted" style={{ padding: 24, fontSize: "var(--fs-12)", textAlign: "center" }}>
        No runs yet.
      </div>
    );
  }
  return (
    <div data-testid="mobile-runs" className="col" style={{ gap: 0 }}>
      {rows.map(function (s) {
        var sid = s.session_id || s.id;
        var b = ST2_bucketOf(s, { pendingBySession: pendingBySession });
        return (
          <div
            key={sid}
            data-testid="mobile-run-row"
            onClick={function () { if (onOpen) onOpen(sid); }}
            className="row"
            style={{
              gap: 10, minHeight: 56, alignItems: "center", padding: "0 14px",
              borderBottom: "1px solid var(--bg-active)", cursor: "pointer",
            }}
          >
            <span style={{
              width: 8, height: 8, borderRadius: "50%", flex: "0 0 auto",
              background: "var(" + b.tone + ")",
            }} />
            <span className="col" style={{ gap: 1, flex: 1, minWidth: 0 }}>
              <span style={{ fontSize: "var(--fs-13)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {s.name || sid}
              </span>
              <span className="muted" style={{ fontSize: "var(--fs-11)" }}>{b.label}{b.detail ? " - " + b.detail : ""}</span>
            </span>
            <Icon name="chevron-right" size={14} />
          </div>
        );
      })}
    </div>
  );
}

// A one-line note beats a cramped implementation (§11).
function ST2_MobileDesktopOnly({ what }) {
  return (
    <div
      data-testid="mobile-desktop-only"
      className="muted"
      style={{ padding: 24, fontSize: "var(--fs-12)", textAlign: "center", lineHeight: 1.6 }}
    >
      {what} is desktop only.
    </div>
  );
}

function StudioMobile({ wid, studio }) {
  var att = ST2_useAttention(wid);
  var actions = ST2_useYieldActions(wid);
  var api = window.primerApi;

  var sessionsRes = api.useResource(
    ST2_api.keys.sessions(wid),
    function (signal) { return ST2_api.sessions(wid, signal); },
    { pollMs: 5000, deps: [wid] }
  );
  var sessions = (sessionsRes.data && sessionsRes.data.items) || [];

  var visible = att.items.filter(function (i) { return !actions.hidden[i.tool_call_id]; });
  var pendingBySession = React.useMemo(function () {
    var map = {};
    visible.forEach(function (i) { if (i.session_id) map[i.session_id] = true; });
    return map;
  }, [att.items, actions.hidden]); // eslint-disable-line

  var tabState = React.useState("needs");
  var tab = tabState[0];
  var setTab = tabState[1];

  var counts = { needs: visible.length, runs: 0, changes: 0 };

  return (
    <div className="col" data-testid="studio-mobile" style={{ height: "100%", minHeight: 0, gap: 0 }}>
      <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
        {tab === "needs" ? <ST2_MobileNeeds items={visible} actions={actions} /> : null}
        {tab === "runs" ? (
          <ST2_MobileRuns
            sessions={sessions}
            pendingBySession={pendingBySession}
            onOpen={function (sid) {
              studio.openTab({ id: "session:" + sid, kind: "session", ref: sid, title: sid });
              setTab("runs");
            }}
          />
        ) : null}
        {/* Changes needs the per-turn file trail, which has no endpoint yet;
            saying so is better than an empty tab that reads as broken. */}
        {tab === "changes" ? <ST2_MobileDesktopOnly what="Changes" /> : null}
      </div>
      <ST2_MobileBottomBar tab={tab} counts={counts} onPick={setTab} />
    </div>
  );
}

window.StudioMobile = StudioMobile;
window.ST2_MobileBottomBar = ST2_MobileBottomBar;
window.ST2_MobileNeeds = ST2_MobileNeeds;
window.ST2_MobileRuns = ST2_MobileRuns;
window.ST2_MobileDesktopOnly = ST2_MobileDesktopOnly;
window.ST2_MOBILE_TABS = ST2_MOBILE_TABS;
window.ST2_ASK_SUGGESTIONS = ST2_ASK_SUGGESTIONS;
