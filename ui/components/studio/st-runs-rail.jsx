/* global React, Icon, ST2_api, ST2_bucketOf, ST2_BUCKET_ORDER, ST_sessionSort,
   ST_sessionKind, ST_sessionGlyph, ST_dotStyle, ST_onRowKey, NewSessionForm,
   ST_SessionDeleteDialog, ST_SessionRenameDialog */
// Studio revamp - the Runs rail (ui/studio/STUDIO-WIRING.md §5).
//
// Replaces SessionsSection. Same endpoint, same row actions, same create form
// and dialogs (reused, not copied - they are already bundle globals). What
// changes is the ordering axis: a flat status-sorted list becomes four groups in
// the fixed needs -> working -> broken -> done order, so "what needs me" is a
// position on screen instead of a badge you have to notice.
//
// ST_sessionSort stays INSIDE each group, so the within-group ordering rule
// still lives in exactly one place.

var ST2_BUCKET_LABEL = {
  needs: "Needs you",
  working: "Working",
  broken: "Broken",
  done: "Done",
};

// Bucket -> the tone vocabulary ST_dotStyle actually implements
// ("green-pulse" / "amber" / "red" / "gray"; anything else is a dim dot).
var ST2_BUCKET_DOT = {
  needs: "amber",
  working: "green-pulse",
  broken: "red",
  done: "gray",
};

// Only these chips are backed by data. See ST2_RunsRail for why there is no
// "mine".
var ST2_RAIL_FILTERS = [
  { id: "all", label: "All" },
  { id: "needs", label: "Needs you" },
  { id: "open", label: "Open" },
];

function ST2_RailFilterChips({ value, counts, onPick }) {
  return (
    <div className="row" style={{ gap: 4, padding: "6px 8px", flexWrap: "wrap" }}>
      {ST2_RAIL_FILTERS.map(function (f) {
        var active = (value || "all") === f.id;
        var n = counts[f.id];
        return (
          <button
            key={f.id}
            data-testid={"rail-filter-" + f.id}
            onClick={function () { onPick(f.id); }}
            style={{
              padding: "3px 9px", borderRadius: 999, cursor: "pointer",
              border: "1px solid " + (active ? "var(--border-strong)" : "var(--border)"),
              background: active ? "var(--bg-active)" : "transparent",
              color: active ? "var(--text)" : "var(--text-3)",
              fontSize: "var(--fs-11)",
            }}
          >
            {f.label}{n ? " " + n : ""}
          </button>
        );
      })}
    </div>
  );
}

function ST2_RunRow({ session, bucket, isActive, onOpen, onOpenAside, onRename, onDelete }) {
  var sid = session.session_id || session.id;
  var isGraph = ST_sessionKind(session) === "graph";
  var isNeeds = bucket.bucket === "needs";

  return (
    <div
      className="st-session-row"
      data-testid="session-row"
      data-session-id={sid}
      data-bucket={bucket.bucket}
      role="button"
      tabIndex={0}
      onClick={function (e) {
        // Cmd/Ctrl-click opens beside, mirroring the editor convention. Falls
        // through to a normal open until the companion pane exists (Task 6).
        if ((e.metaKey || e.ctrlKey) && onOpenAside) onOpenAside(session);
        else onOpen(session);
      }}
      onKeyDown={ST_onRowKey(function () { onOpen(session); })}
      style={{
        display: "flex", alignItems: "center", gap: 7,
        height: "var(--row-h, 34px)", padding: "0 10px", cursor: "pointer",
        // The amber edge is what makes a needs-you row findable without
        // reading it. An active tab still wins the border.
        borderLeft: "2px solid " + (
          isActive ? "var(--accent)" : isNeeds ? "var(--amber)" : "transparent"
        ),
        background: isActive ? "var(--bg-active)" : "transparent",
      }}
    >
      <span
        className="st-session-dot"
        data-testid="session-status-dot"
        // ST_dotStyle's vocabulary, not the bucket's CSS var: it has no "blue",
        // and an unknown tone falls through to the inert dim dot - which would
        // render a live run as though nothing were happening.
        style={ST_dotStyle(ST2_BUCKET_DOT[bucket.bucket] || "dim")}
      />
      <span
        className="col"
        style={{ flex: 1, minWidth: 0, gap: 0 }}
      >
        <span style={{
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          color: bucket.bucket === "done" ? "var(--text-3)" : "var(--text)",
          fontSize: "var(--fs-12)",
        }}>{session.name || sid}</span>
        {bucket.detail ? (
          <span className="muted" style={{
            fontSize: "var(--fs-11)", overflow: "hidden",
            textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>{bucket.detail}</span>
        ) : null}
      </span>
      <span className="st-row-actions">
        {onOpenAside ? (
          <button
            className="st-row-action"
            data-testid="session-open-aside"
            title="Open beside"
            aria-label="Open beside"
            onClick={function (e) { e.stopPropagation(); onOpenAside(session); }}
          >
            <Icon name="panel-right" size={12} />
          </button>
        ) : null}
        <button
          className="st-row-action"
          data-testid="session-rename"
          title="Rename session"
          aria-label="Rename session"
          onClick={function (e) { e.stopPropagation(); onRename(session); }}
        >
          <Icon name="edit" size={12} />
        </button>
        <button
          className="st-row-action is-danger"
          data-testid="session-delete"
          title="Delete session"
          aria-label="Delete session"
          onClick={function (e) { e.stopPropagation(); onDelete(session); }}
        >
          <Icon name="trash" size={12} />
        </button>
      </span>
      <span style={{
        fontSize: 9, fontWeight: 700, letterSpacing: "0.05em",
        color: "var(--text-4)", border: "1px solid var(--border)",
        borderRadius: 4, padding: "0 4px", flexShrink: 0,
      }}>{isGraph ? "GRAPH" : "AGENT"}</span>
    </div>
  );
}

function ST2_RunsRail({ wid, studio }) {
  var api = window.primerApi;
  var s = studio.state;

  // Same cache key the v1 section and the attention bar use, so all three
  // dedupe onto one poll.
  var sessionsRes = api.useResource(
    ST2_api.keys.sessions(wid),
    function (signal) { return ST2_api.sessions(wid, signal); },
    { pollMs: 3000, deps: [wid] }
  );
  var raw = (sessionsRes.data && Array.isArray(sessionsRes.data.items))
    ? sessionsRes.data.items
    : (Array.isArray(sessionsRes.data) ? sessionsRes.data : []);

  // The pending-yield snapshot promotes a RUNNING session with an unanswered
  // yield into "needs". Shares the attention bar's key, so no extra request.
  var pendingRes = api.useResource(
    ST2_api.keys.pending(wid),
    function (signal) { return ST2_api.pending(wid, signal); },
    { pollMs: 15000, deps: [wid] }
  );
  var pendingBySession = React.useMemo(function () {
    var map = {};
    var items = (pendingRes.data && pendingRes.data.items) || [];
    items.forEach(function (it) { if (it.session_id) map[it.session_id] = true; });
    return map;
  }, [pendingRes.data]);

  var [pendingDelete, setPendingDelete] = React.useState(null);
  var [renaming, setRenaming] = React.useState(null);
  var pushToast = studio.pushToast || (api && api.toastPush) || null;

  var openIds = React.useMemo(function () {
    var map = {};
    (s.openTabs || []).forEach(function (t) {
      if (t.kind === "session" && t.ref) map[t.ref] = true;
    });
    (s.asideTabs || []).forEach(function (t) {
      if (t.kind === "session" && t.ref) map[t.ref] = true;
    });
    return map;
  }, [s.openTabs, s.asideTabs]);

  function openSession(session) {
    var sid = session.session_id || session.id;
    studio.openTab({
      id: "session:" + sid,
      kind: "session",
      ref: sid,
      title: session.name || sid,
      glyph: ST_sessionGlyph(session),
    });
  }

  // Open beside only exists once the companion pane does (Task 6). Guarded
  // rather than stubbed, so the affordance appears when it actually works.
  var openAside = typeof studio.openAside === "function"
    ? function (session) {
        var sid = session.session_id || session.id;
        studio.openAside({
          id: "session:" + sid,
          kind: "session",
          ref: sid,
          title: session.name || sid,
          glyph: ST_sessionGlyph(session),
        });
      }
    : null;

  function onSessionDeleted(sid) {
    setPendingDelete(null);
    // Close BOTH panes' matching tabs - a deleted session left open in the
    // companion pane would keep 404ing.
    if (studio.closeTab) studio.closeTab("session:" + sid);
    if (studio.closeAsideTab) studio.closeAsideTab("session:" + sid);
    if (sessionsRes.refetch) sessionsRes.refetch();
  }

  function onSessionRenamed() {
    setRenaming(null);
    if (sessionsRes.refetch) sessionsRes.refetch();
  }

  var filter = s.railFilter || "all";
  var groups = React.useMemo(function () {
    var out = { needs: [], working: [], broken: [], done: [] };
    raw.forEach(function (item) {
      out[ST2_bucketOf(item, { pendingBySession: pendingBySession }).bucket].push(item);
    });
    // Sort within each group, never across - the group order IS the priority.
    ST2_BUCKET_ORDER.forEach(function (k) { out[k] = ST_sessionSort(out[k]); });
    return out;
  }, [raw, pendingBySession]);

  var counts = {
    all: raw.length,
    needs: groups.needs.length,
    open: raw.filter(function (x) { return openIds[x.session_id || x.id]; }).length,
  };

  function visible(list) {
    if (filter === "needs") return list === groups.needs ? list : [];
    if (filter === "open") return list.filter(function (x) { return openIds[x.session_id || x.id]; });
    return list;
  }

  var total = raw.length;

  return (
    <div className="col" data-testid="rail-runs" style={{ flex: 1, minHeight: 0, gap: 0 }}>
      <div
        className="row"
        style={{
          padding: "0 8px 0 10px", height: 30, flex: "0 0 auto",
          alignItems: "center", gap: 6,
        }}
      >
        <span className="muted" style={{ fontSize: "var(--fs-11)", fontWeight: 600, letterSpacing: "0.04em" }}>
          RUNS
        </span>
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>{total || ""}</span>
        <button
          style={{
            marginLeft: "auto", width: 20, height: 20, display: "grid",
            placeItems: "center", borderRadius: 5, border: "none",
            background: "none", color: "var(--text-3)", fontSize: 14, cursor: "pointer",
          }}
          title="New session"
          data-testid="new-session-btn"
          onClick={function () {
            if (studio.newSessionOpen) studio.closeNewSession();
            else studio.openNewSession();
          }}
        >+</button>
      </div>

      <ST2_RailFilterChips
        value={filter}
        counts={counts}
        onPick={function (id) { studio.setRailFilter(id); }}
      />

      {studio.newSessionOpen && (
        <NewSessionForm
          wid={wid}
          onClose={function () { studio.closeNewSession(); }}
          onCreated={function (session) {
            studio.closeNewSession();
            if (sessionsRes.refetch) sessionsRes.refetch();
            openSession(session);
          }}
        />
      )}

      {pendingDelete && (
        <ST_SessionDeleteDialog
          wid={wid}
          session={pendingDelete}
          pushToast={pushToast}
          onClose={function () { setPendingDelete(null); }}
          onDeleted={onSessionDeleted}
        />
      )}

      {renaming && (
        <ST_SessionRenameDialog
          wid={wid}
          session={renaming}
          pushToast={pushToast}
          onClose={function () { setRenaming(null); }}
          onRenamed={onSessionRenamed}
        />
      )}

      <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
        {sessionsRes.loading && !total ? (
          <div className="muted" style={{ padding: "8px 12px", fontSize: "var(--fs-11)" }}>Loading...</div>
        ) : null}
        {!sessionsRes.loading && !total ? (
          <div className="muted" style={{ padding: "8px 12px", fontSize: "var(--fs-11)" }}>No sessions yet.</div>
        ) : null}

        {ST2_BUCKET_ORDER.map(function (key) {
          var rows = visible(groups[key]);
          if (!rows.length) return null;
          return (
            <div key={key} data-testid={"rail-group-" + key}>
              <div
                className="row"
                style={{
                  padding: "0 10px", height: 24, alignItems: "center", gap: 6,
                  position: "sticky", top: 0, background: "var(--bg-2)",
                  borderBottom: "1px solid var(--bg-active)", zIndex: 1,
                }}
              >
                <span style={{
                  fontSize: "var(--fs-11)", fontWeight: 600,
                  color: key === "needs" ? "var(--amber)" : "var(--text-3)",
                }}>{ST2_BUCKET_LABEL[key]}</span>
                <span className="muted" style={{ fontSize: "var(--fs-11)" }}>{rows.length}</span>
              </div>
              {rows.map(function (session) {
                var sid = session.session_id || session.id;
                return (
                  <ST2_RunRow
                    key={sid}
                    session={session}
                    bucket={ST2_bucketOf(session, { pendingBySession: pendingBySession })}
                    isActive={s.activeTabId === "session:" + sid}
                    onOpen={openSession}
                    onOpenAside={openAside}
                    onRename={setRenaming}
                    onDelete={setPendingDelete}
                  />
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
}

window.ST2_RunsRail = ST2_RunsRail;
window.ST2_RunRow = ST2_RunRow;
window.ST2_RAIL_FILTERS = ST2_RAIL_FILTERS;
