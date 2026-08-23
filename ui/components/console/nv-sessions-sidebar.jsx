/* global React, SH_api, NV_useConsole, NV_identity, NV_sessionBands,
   SH_statusFromTap */
// Sessions sidebar (wiring plan P2 T6): bands (needs-you first), live
// status chips off the tap, right-click context menu of session verbs,
// "+" creates. Prototype LEFT SIDEBAR sessions region, styles
// extracted to nv- classes.

function NV_SessionsSidebarVerbs() {
  var con = NV_useConsole();
  return (
    <button type="button" className="nv-rail-iconbtn" title="New session"
      data-verb="session.create" data-testid="nv-session-new"
      onClick={function () {
        var verb = con.registry.get("session.create");
        if (verb) verb.run();
      }}>+</button>
  );
}

function NV_SessionContextMenu(props) {
  var con = NV_useConsole();
  var s = props.session;
  var sid = s.session_id;
  function act(label, fn, danger) {
    return { label: label, fn: fn, danger: !!danger };
  }
  var over = s.status === "ended";
  var rows = [
    act("Open", function () { props.onOpen(s); }),
    act("Rename", function () {
      promptDialog({
        title: "Rename session", defaultValue: s.name || "",
      }).then(function (name) {
        if (name == null) return;
        SH_api.renameSession(con.wid, sid, name || null)
          .then(props.onChanged);
      });
    }),
  ];
  if (!over) {
    rows.push(act("Interrupt", function () {
      SH_api.interrupt(con.wid, sid).then(props.onChanged);
    }));
    rows.push(act("Park", function () {
      SH_api.pause(con.wid, sid).then(props.onChanged);
    }));
    rows.push(act("End", function () {
      SH_api.cancel(con.wid, sid).then(props.onChanged);
    }, true));
  }
  rows.push(act("Delete", function () {
    confirmDialog({
      title: "Delete session",
      message: "Permanently delete " + (s.name || sid) + "?",
      danger: true,
    }).then(function (ok) {
      if (!ok) return;
      SH_api.deleteSession(con.wid, sid).then(props.onChanged);
    });
  }, true));

  return (
    <div className="nv-ctx" data-testid={"nv-session-menu:" + sid}
      style={{ left: props.x, top: props.y }}
      onClick={function (ev) { ev.stopPropagation(); }}>
      {rows.map(function (r) {
        return (
          <button type="button" key={r.label} className="nv-menu-row"
            data-danger={r.danger ? "true" : "false"}
            onClick={function () { props.onClose(); r.fn(); }}>
            {r.label}
          </button>
        );
      })}
    </div>
  );
}

function NV_SessionsSidebar() {
  var con = NV_useConsole();
  var tap = window.useWorkspaceTap(con.wid);
  // The TOP-LEVEL sessions route is the robust source (the
  // workspace-scoped one reads on-disk state slots and misses rows
  // whose slot is gone); filter to the selected workspace client-side.
  var sessions = window.primerApi.useResource(
    "nv-sessions:" + con.wid,
    function (signal) {
      return SH_api.allSessions(signal).then(function (out) {
        return {
          items: (out.items || []).filter(function (s) {
            return s.workspace_id === con.wid;
          }),
        };
      });
    },
    { pollMs: 5000, deps: [con.wid] }
  );
  var pending = window.primerApi.useResource(
    SH_api.keys.pending(con.wid),
    function (signal) { return SH_api.pendingYields(con.wid, signal); },
    { pollMs: 10000, deps: [con.wid] }
  );
  window.useWorkspaceTapListener(con.wid, function (ev) {
    var cls = ev && ev["class"];
    if (cls === "yielded" || cls === "resumed" || cls === "done") {
      sessions.refetch();
      pending.refetch();
    }
  });
  var menuState = React.useState(null);
  var menu = menuState[0];
  var setMenu = menuState[1];
  React.useEffect(function () {
    function close() { setMenu(null); }
    document.addEventListener("click", close);
    return function () { document.removeEventListener("click", close); };
  }, []);

  var items = (sessions.data && sessions.data.items) || [];
  var attentionSids = ((pending.data && pending.data.items) || [])
    .map(function (row) { return row.session_id; });
  var bands = NV_sessionBands(items, attentionSids);

  function openDoc(s, promote) {
    con.setDoc({ kind: "session", ref: s.session_id });
    if (promote && con.promoteDoc) con.promoteDoc("session:" + s.session_id);
  }

  function refetch() { sessions.refetch(); pending.refetch(); }

  return (
    <div className="nv-rail-body" data-testid="nv-sessions">
      {!items.length ? (
        <div className="nv-rail-empty">
          <div>No sessions in this workspace yet.</div>
          <button type="button" className="nv-btn-primary"
            data-testid="nv-sessions-empty-create"
            onClick={function () {
              var verb = con.registry.get("session.create");
              if (verb) verb.run();
            }}>Start a session</button>
        </div>
      ) : null}
      {bands.map(function (band) {
        return (
          <div key={band.id}>
            <div className="nv-band-head" data-band={band.id}
              data-testid={"nv-band:" + band.id}>
              <span>{band.label}</span>
              <span className="nv-band-count">{band.rows.length}</span>
            </div>
            {band.rows.map(function (s) {
              var sid = s.session_id;
              var ident = NV_identity(s.binding);
              var live = SH_statusFromTap(tap.events, sid, Date.now());
              var agentName = s.binding
                ? (s.binding.agent_id || s.binding.graph_id || "?")
                : "?";
              var attention = band.id === "attention";
              return (
                <button type="button" key={sid} className="nv-session-row"
                  data-attention={attention ? "true" : "false"}
                  data-testid={"nv-session:" + sid}
                  onClick={function () { openDoc(s, false); }}
                  onDoubleClick={function () { openDoc(s, true); }}
                  onContextMenu={function (ev) {
                    ev.preventDefault();
                    ev.stopPropagation();
                    setMenu({ s: s, x: ev.clientX, y: ev.clientY });
                  }}>
                  <svg width="12" height="12" viewBox="0 0 12 12"
                    style={{ flexShrink: 0, color: ident.color }}>
                    <path d={ident.d} fill="currentColor" />
                  </svg>
                  <div className="nv-session-main">
                    <div className="nv-session-name">{s.name || sid}</div>
                    <div className="nv-session-sub">
                      <span>{agentName}</span>
                      {live ? (
                        <span className="nv-session-live">
                          {live.verb}
                          {live.object ? " " + live.object : ""}
                        </span>
                      ) : null}
                      {s.parked_status ? (
                        <span className="nv-parked">parked</span>
                      ) : null}
                    </div>
                  </div>
                  {band.id === "running" ? (
                    <span className="nv-dot-pulse" />
                  ) : null}
                  {attention ? (
                    <span className="nv-dot-attention" title="needs you" />
                  ) : null}
                </button>
              );
            })}
          </div>
        );
      })}
      {menu ? (
        <NV_SessionContextMenu session={menu.s} x={menu.x} y={menu.y}
          onOpen={function (s) { openDoc(s, true); }}
          onChanged={refetch}
          onClose={function () { setMenu(null); }} />
      ) : null}
    </div>
  );
}

window.NV_SessionsSidebarVerbs = NV_SessionsSidebarVerbs;
window.NV_SessionsSidebar = NV_SessionsSidebar;
