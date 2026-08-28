/* global React, SH_api, NV_identity */
// The left rail (uiv2 R2, US-007 phase 1): Inbox (cross-workspace
// attention feed) stacked over the workspace tree, per
// uiv2/implementer-notes.md sections 2.1-2.2. Replaces nv-sessions-
// sidebar.jsx's bands + Sessions|Files tab toggle - notes 2.5 puts Files
// in its own always-visible sidebar, so this file is Sessions-only.
//
// STANDALONE for now: nothing mounts this component yet. Shell
// integration is phase 2, after the R1 owner releases nv-shell.jsx.
//
// Data: fetched internally (SH_api + window.primerApi.useResource),
// matching the sibling sidebars' existing self-fetching pattern - this is
// remote data the component owns fetching for, not caller-supplied state
// like the tab-group model. INTERACTIONS are props-driven instead: every
// row click calls a callback prop rather than writing con.setDoc/the URL
// directly, so the caller (the real shell, in phase 2) decides what
// selecting a workspace or opening a session actually does.
//
// Props:
//   selectedWorkspaceId - the current workspace id (tints its tree row,
//                         and is who onCreateSession binds to).
//   onSelectWorkspace(wid)
//   onOpenSession(session, wid) - session is the full row from
//                         SH_api.allSessions (session_id, name, binding,
//                         ...); called for BOTH Inbox and tree session
//                         rows, which always open PROMOTED per notes
//                         2.1/2.2 (unlike the doc host's preview-by-
//                         default single click - the rail is never the
//                         preview entry point).
//   onCreateSession(selectedWorkspaceId) - the Inbox header "+".
//   onCreateWorkspace() - the workspace-tree header "+".
//
// testids (nv-rail- prefix, matching the CSS prefix):
//   nv-rail-inbox, nv-rail-inbox-row:{sid}
//   nv-rail-tree, nv-rail-ws:{wid}, nv-rail-ws-session:{sid}
//   nv-rail-create-session, nv-rail-create-workspace
//
// Cross-workspace attention aggregate: notes 2.1 wants "every session
// across ALL workspaces that needs a person" in the Inbox. A teammate is
// building GET /yields/pending (no workspace in the path) in parallel;
// until it lands this codes defensively against a 404 and fans out
// SH_api.pendingYields per workspace instead - same item shape, just N
// calls instead of one. TODO(uiv2-R2): once the aggregate endpoint ships,
// confirm its item shape includes workspace_id per item (my own gap-map
// sketch assumed so) and drop the fallback fan-out.
//
// Session context menu: NOT reused from nv-sessions-sidebar.jsx's
// NV_SessionContextMenu, deliberately - see NV_Rail_SessionContextMenu's
// own comment below for why (it reads con.wid from context, which is
// wrong for a tree that shows every workspace's sessions at once, not
// just the selected one; this is a real correctness bug, not a style
// call, so it is a minimal duplicate instead).

function NV_Rail_SessionPulse(props) {
  // One hook call per mounted row (React's rules of hooks) - see the
  // identical pattern in nv-tab-groups.jsx's NV_TG_SessionPulse; this
  // copy is small and kept local so this file has no load-order
  // dependency on that one.
  var statusSnap = typeof window.useSessionStore === "function"
    ? window.useSessionStore(props.wid, props.sid, "status")
    : null;
  if (!statusSnap || !statusSnap.verb) return null;
  return <span className="nv-dot-pulse" title="running" />;
}

function NV_Rail_inboxKindLabel(kind) {
  if (kind === "approval") return "approval";
  if (kind === "ask_user") return "asking you";
  return "parked on you";
}

// A minimal duplicate of nv-sessions-sidebar.jsx's NV_SessionContextMenu,
// NOT a reuse - deliberately, not out of laziness. That component reads
// con.wid from NV_useConsole() for every SH_api call (rename/interrupt/
// park/end/delete), which is correct there because it only ever lists
// the SELECTED workspace's own sessions. The rail's tree shows sessions
// from EVERY workspace, expanded or not, independent of which one is
// currently selected - a right-click on a session in a non-selected
// workspace's expanded row would silently act against the WRONG
// workspace if it read con.wid instead of the row's own wid. That is a
// real correctness bug, not a style preference, so this takes wid as an
// explicit prop instead. Keep the two in sync by hand if either changes;
// nv-sessions-sidebar.jsx stays untouched (owned elsewhere until cutover).
function NV_Rail_SessionContextMenu(props) {
  var s = props.session;
  var wid = props.wid;
  var sid = s.session_id;
  function act(label, fn, danger) {
    return { label: label, fn: fn, danger: !!danger };
  }
  var over = s.status === "ended";
  var rows = [
    act("Open", function () { props.onOpen(s); }),
    act("Rename", function () {
      window.promptDialog({
        title: "Rename session", defaultValue: s.name || "",
      }).then(function (name) {
        if (name == null) return;
        SH_api.renameSession(wid, sid, name || null).then(props.onChanged);
      });
    }),
  ];
  if (!over) {
    rows.push(act("Interrupt", function () {
      SH_api.interrupt(wid, sid).then(props.onChanged);
    }));
    rows.push(act("Park", function () {
      SH_api.pause(wid, sid).then(props.onChanged);
    }));
    rows.push(act("End", function () {
      SH_api.cancel(wid, sid).then(props.onChanged);
    }, true));
  }
  rows.push(act("Delete", function () {
    window.confirmDialog({
      title: "Delete session",
      message: "Permanently delete " + (s.name || sid) + "?",
      danger: true,
    }).then(function (ok) {
      if (!ok) return;
      SH_api.deleteSession(wid, sid).then(props.onChanged);
    });
  }, true));

  return (
    <div className="nv-ctx" data-testid={"nv-rail-session-menu:" + sid}
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

function NV_Rail(props) {
  var wsRes = window.primerApi.useResource(
    "nv-rail-workspaces",
    function (signal) { return SH_api.workspaces(signal); },
    { pollMs: 15000, deps: [] }
  );
  var sessRes = window.primerApi.useResource(
    "nv-rail-all-sessions",
    function (signal) { return SH_api.allSessions(signal); },
    { pollMs: 5000, deps: [] }
  );
  var inboxRes = window.primerApi.useResource(
    "nv-rail-inbox",
    function (signal) {
      var workspaces = ((wsRes.data && wsRes.data.items) || []);
      return window.primerApi.apiFetch("GET", "/yields/pending", null, { signal: signal })
        .then(function (out) {
          var items = (out && out.items) || [];
          return { items: items.map(function (it) {
            return it.workspace_id ? it : Object.assign({}, it, { workspace_id: it.wid || null });
          }) };
        })
        .catch(function (err) {
          if (err && err.status !== 404) throw err;
          // TODO(uiv2-R2): drop this fan-out once GET /yields/pending ships.
          return Promise.all(workspaces.map(function (w) {
            return SH_api.pendingYields(w.id, signal).then(function (out) {
              return ((out && out.items) || []).map(function (it) {
                return Object.assign({}, it, { workspace_id: w.id });
              });
            });
          })).then(function (perWs) {
            return { items: perWs.reduce(function (a, b) { return a.concat(b); }, []) };
          });
        });
    },
    { pollMs: 10000, deps: [(wsRes.data && wsRes.data.items || []).length] }
  );

  var expandedState = React.useState({});
  var expanded = expandedState[0];
  var setExpanded = expandedState[1];
  var menuState = React.useState(null);
  var menu = menuState[0];
  var setMenu = menuState[1];
  React.useEffect(function () {
    function close() { setMenu(null); }
    document.addEventListener("click", close);
    return function () { document.removeEventListener("click", close); };
  }, []);

  function refetchSessions() { sessRes.refetch(); inboxRes.refetch(); }

  var workspaces = (wsRes.data && wsRes.data.items) || [];
  var sessions = (sessRes.data && sessRes.data.items) || [];
  var inboxItems = (inboxRes.data && inboxRes.data.items) || [];

  var sessionsById = {};
  sessions.forEach(function (s) { sessionsById[s.session_id] = s; });

  var sessionsByWs = {};
  sessions.forEach(function (s) {
    var wid = s.workspace_id;
    if (!sessionsByWs[wid]) sessionsByWs[wid] = [];
    sessionsByWs[wid].push(s);
  });

  var attentionSidsByWs = {};
  inboxItems.forEach(function (it) {
    var wid = it.workspace_id;
    if (!attentionSidsByWs[wid]) attentionSidsByWs[wid] = {};
    attentionSidsByWs[wid][it.session_id] = true;
  });

  function openSession(session) {
    // Every rail-triggered open is PROMOTED (notes 2.1/2.2) - the rail is
    // never the preview entry point, so there is no promote param here.
    if (typeof props.onOpenSession === "function") props.onOpenSession(session, session.workspace_id);
  }

  return (
    <div className="nv-rail-sections" data-testid="nv-rail-sections">
      <div className="nv-rail-inbox" data-testid="nv-rail-inbox">
        <div className="nv-rail-section-head">
          <span>Inbox - needs you</span>
          <span className="nv-rail-count">{inboxItems.length}</span>
          <div style={{ flex: 1 }} />
          <button type="button" className="nv-rail-iconbtn" title="New session"
            data-testid="nv-rail-create-session"
            onClick={function () {
              if (typeof props.onCreateSession === "function") {
                props.onCreateSession(props.selectedWorkspaceId);
              }
            }}>+</button>
        </div>
        {!inboxItems.length ? (
          <div className="nv-rail-empty">Nothing needs you right now.</div>
        ) : null}
        {inboxItems.map(function (it) {
          var s = sessionsById[it.session_id];
          var ws = workspaces.filter(function (w) { return w.id === it.workspace_id; })[0];
          var ident = s ? NV_identity(s.binding) : null;
          return (
            <div key={it.session_id} className="nv-rail-inbox-row"
              data-testid={"nv-rail-inbox-row:" + it.session_id}
              onClick={function () {
                if (typeof props.onSelectWorkspace === "function") {
                  props.onSelectWorkspace(it.workspace_id);
                }
                openSession(s || { session_id: it.session_id, workspace_id: it.workspace_id });
              }}>
              <span className="nv-dot-attention" />
              <div className="nv-rail-main">
                <div className="nv-rail-name">{(s && (s.name || s.session_id)) || it.session_id}</div>
                <div className="nv-rail-sub">
                  <span className="nv-rail-kind">{NV_Rail_inboxKindLabel(it.kind)}</span>
                  <span>{ws ? ws.name : it.workspace_id}</span>
                </div>
              </div>
              {ident ? (
                <svg width="11" height="11" viewBox="0 0 12 12" style={{ flexShrink: 0, color: ident.color }}>
                  <path d={ident.d} fill="currentColor" />
                </svg>
              ) : null}
            </div>
          );
        })}
      </div>
      <div className="nv-rail-tree" data-testid="nv-rail-tree">
        <div className="nv-rail-section-head">
          <span>Workspaces</span>
          <div style={{ flex: 1 }} />
          <button type="button" className="nv-rail-iconbtn" title="New workspace"
            data-testid="nv-rail-create-workspace"
            onClick={function () {
              if (typeof props.onCreateWorkspace === "function") props.onCreateWorkspace();
            }}>+</button>
        </div>
        {workspaces.map(function (w) {
          var isSelected = w.id === props.selectedWorkspaceId;
          var isOpen = !!expanded[w.id];
          var wsSessions = sessionsByWs[w.id] || [];
          var attnCount = Object.keys(attentionSidsByWs[w.id] || {}).length;
          return (
            <div key={w.id}>
              <div className="nv-rail-ws-row" data-testid={"nv-rail-ws:" + w.id}
                data-selected={isSelected ? "true" : "false"}
                onClick={function () {
                  if (typeof props.onSelectWorkspace === "function") props.onSelectWorkspace(w.id);
                  setExpanded(function (prev) {
                    var next = Object.assign({}, prev);
                    next[w.id] = !prev[w.id];
                    return next;
                  });
                }}>
                <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5"
                  style={{ flexShrink: 0, transform: "rotate(" + (isOpen ? 90 : 0) + "deg)" }}>
                  <path d="M3.5 2 6.5 5 3.5 8" />
                </svg>
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.3" style={{ flexShrink: 0 }}>
                  <path d="M1.5 3.5 6 1l4.5 2.5v5L6 11 1.5 8.5Z M6 6v5M1.5 3.5 6 6l4.5-2.5" />
                </svg>
                <span className="nv-rail-name">{w.name || w.id}</span>
                {attnCount > 0 ? <span className="nv-rail-attn-count">{attnCount}</span> : null}
                <span className="nv-rail-session-count">{wsSessions.length}</span>
              </div>
              {isOpen ? wsSessions.map(function (s) {
                var sid = s.session_id;
                var ident = NV_identity(s.binding);
                var isAttention = !!(attentionSidsByWs[w.id] && attentionSidsByWs[w.id][sid]);
                return (
                  <div key={sid} className="nv-rail-ws-session"
                    data-testid={"nv-rail-ws-session:" + sid}
                    onClick={function () {
                      if (typeof props.onSelectWorkspace === "function") props.onSelectWorkspace(w.id);
                      openSession(s);
                    }}
                    onContextMenu={function (ev) {
                      ev.preventDefault();
                      ev.stopPropagation();
                      setMenu({ s: s, wid: w.id, x: ev.clientX, y: ev.clientY });
                    }}>
                    <svg width="10" height="10" viewBox="0 0 12 12" style={{ flexShrink: 0, color: ident.color }}>
                      <path d={ident.d} fill="currentColor" />
                    </svg>
                    <span className="nv-rail-name">{s.name || sid}</span>
                    {isAttention ? (
                      <span className="nv-dot-attention" title="needs you" />
                    ) : (
                      <NV_Rail_SessionPulse wid={w.id} sid={sid} />
                    )}
                  </div>
                );
              }) : null}
            </div>
          );
        })}
      </div>
      {menu ? (
        <NV_Rail_SessionContextMenu session={menu.s} wid={menu.wid} x={menu.x} y={menu.y}
          onOpen={function (s) { openSession(s); }}
          onChanged={refetchSessions}
          onClose={function () { setMenu(null); }} />
      ) : null}
    </div>
  );
}

window.NV_Rail = NV_Rail;
