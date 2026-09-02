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
//   selectedWorkspaceId - the current workspace id (tints its tree row).
//   onSelectWorkspace(wid)
//   onOpenSession(session, wid) - session is the full row from
//                         SH_api.allSessions (session_id, name, binding,
//                         ...); called for BOTH Inbox and tree session
//                         rows, which always open PROMOTED per notes
//                         2.1/2.2 (unlike the doc host's preview-by-
//                         default single click - the rail is never the
//                         preview entry point).
//   onCreateSessionInWorkspace(wid) - the workspace row's own context-
//                         menu "New session" (bound to THAT row's wid,
//                         not necessarily the selected one - a single
//                         combined switch-and-open, F2 follow-up 2026-
//                         08-29 UI review).
//   onCreateSession() - the Inbox header "+" (uiv2 Wave 1, 01a06431):
//                         un-retires US-012b item 4 per the mockup,
//                         bound to the currently selected workspace
//                         (same session.create verb Alt+n/palette run,
//                         which already opens against con.wid) - the
//                         context-menu row above stays for the
//                         targeted, not-necessarily-selected case.
//   onCreateWorkspace() - the workspace-tree header "+".
//   onOpenWorkspaceSettings(wid) - the workspace row's context-menu
//                         "Settings" entry (US-012b item 5a); replaces the
//                         retired topbar dropdown's own settings button.
//
// testids (nv-rail- prefix, matching the CSS prefix):
//   nv-rail-inbox, nv-rail-inbox-row:{sid}
//   nv-rail-tree, nv-rail-ws:{wid}, nv-rail-ws-session:{sid}
//   nv-rail-create-workspace
//   nv-rail-workspace-menu:{wid} (New session / Settings / Delete,
//   right-click on a workspace row - US-012b item 5a), rows are
//   nv-rail-ws-menu-new-session:{wid}, nv-rail-ws-menu-settings:{wid},
//   nv-rail-ws-menu-delete:{wid}
//
// Cross-workspace attention aggregate: notes 2.1 wants "every session
// across ALL workspaces that needs a person" in the Inbox. GET
// /yields/pending (no workspace in the path) has SHIPPED (primer/api/
// routers/workspaces.py list_pending_attention) and is the primary path;
// item shape confirmed workspace_id per item, matching the fallback's
// stamp below. The 404 fallback stays only for a server predating that
// endpoint - fans out SH_api.pendingYields per workspace instead, same
// idea, N calls instead of one. Its item shape is DIFFERENT though (see
// NV_Rail_inboxKindLabel): the fallback's kind comes straight from
// tool_name (ask_user/watch_files/sleep/...), the live aggregate collapses
// to three buckets (approval/ask/parked) - both are normalized there.
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

// Two vocabularies land here: the live aggregate's three buckets
// (approval/ask/parked) and the pre-aggregate fallback's tool_name-derived
// kind (ask_user/watch_files/sleep/...) - review finding #2 (US-007 R2
// phase 1) was that only the fallback's "ask_user" was recognized, so an
// ask_user yield through the (now live, primary) aggregate rendered
// "parked on you" instead of "asking you". Both spellings map to the same
// label; anything else parked on a human falls to "parked on you".
function NV_Rail_inboxKindLabel(kind) {
  if (kind === "approval") return "approval";
  if (kind === "ask" || kind === "ask_user") return "asking you";
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
// explicit prop instead. nv-sessions-sidebar.jsx (and its own copy of
// this menu) retired in US-011a; this is the only one left.
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

  // Bug found closing out R2's BDD pass: this menu positions off raw
  // click coordinates with no viewport clamping (pre-existing, identical
  // in nv-sessions-sidebar.jsx's NV_SessionContextMenu - not new to the
  // rail). The tree can put a row anywhere down a long scrollable list,
  // so a right-click near the bottom rendered later rows (End, Delete)
  // past the viewport edge with position: fixed, permanently unreachable
  // (no amount of page scroll brings a fixed element back in view).
  // Estimates: ~34px/row incl. padding, ~190px menu width from styles.css.
  var menuH = rows.length * 34 + 20;
  var menuW = 190;
  var clampedX = Math.max(4, Math.min(props.x, window.innerWidth - menuW));
  var clampedY = Math.max(4, Math.min(props.y, window.innerHeight - menuH));

  return (
    <div className="nv-ctx" data-testid={"nv-rail-session-menu:" + sid}
      style={{ left: clampedX, top: clampedY }}
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

// US-012b (2026-08-29 dogfood, item 5a): the workspace-tree row's own
// context menu - "New session" targets THIS row's workspace regardless
// of which one is currently selected via con.createSessionInWorkspace
// (one markPush - F2 follow-up, 2026-08-29 UI review: switch-then-act
// through the workspace.switch and session.create verbs separately would
// have carried two markPush calls for one navigation, same risk the
// review flagged in onOpenSession), "Settings" opens the same
// workspaces/detail overlay the retiring topbar dropdown used to (item
// 3), and "Delete" mirrors the session menu's own confirm-then-mutate
// shape (no dedicated SH_api wrapper existed for this route before this
// task - see sh-api.jsx deleteWorkspace).
function NV_Rail_WorkspaceContextMenu(props) {
  var w = props.workspace;
  function act(slug, label, fn, danger, verb) {
    return { slug: slug, label: label, fn: fn, danger: !!danger, verb: verb || null };
  }
  var rows = [
    // data-verb="session.create" here is one of two rendered pointer
    // affordances now - the Inbox header "+" (nv-rail.jsx's own inbox
    // section head) un-retired item 4's 2026-08-29 removal in uiv2 Wave
    // 1 (01a06431); this row stays as the targeted, workspace-specific
    // entry point. The dual-render guard (test_shell_dual_render_guard.py)
    // only requires at least one - either satisfies it on its own.
    act("new-session", "New session", function () {
      if (typeof props.onCreateSessionInWorkspace === "function") {
        props.onCreateSessionInWorkspace(w.id);
      }
    }, false, "session.create"),
    act("settings", "Settings", function () {
      if (typeof props.onOpenSettings === "function") props.onOpenSettings(w.id);
    }),
    act("delete", "Delete", function () {
      window.confirmDialog({
        title: "Delete workspace",
        message: "Permanently delete " + (w.name || w.id) + "? This cannot be undone.",
        danger: true,
      }).then(function (ok) {
        if (!ok) return;
        SH_api.deleteWorkspace(w.id).then(props.onChanged);
      });
    }, true),
  ];

  // Same viewport-clamping fix as NV_Rail_SessionContextMenu above (the
  // tree can put a row anywhere down a long scrollable list).
  var menuH = rows.length * 34 + 20;
  var menuW = 190;
  var clampedX = Math.max(4, Math.min(props.x, window.innerWidth - menuW));
  var clampedY = Math.max(4, Math.min(props.y, window.innerHeight - menuH));

  return (
    <div className="nv-ctx" data-testid={"nv-rail-workspace-menu:" + w.id}
      style={{ left: clampedX, top: clampedY }}
      onClick={function (ev) { ev.stopPropagation(); }}>
      {rows.map(function (r) {
        return (
          <button type="button" key={r.slug} className="nv-menu-row"
            data-testid={"nv-rail-ws-menu-" + r.slug + ":" + w.id}
            data-danger={r.danger ? "true" : "false"}
            data-verb={r.verb || undefined}
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
  // Defect found closing out uiv2 R2 (ui_e2e migration): the shell's own
  // page can already be mounted (hash-only nav does not reload the SPA)
  // when a NEW workspace is created and immediately navigated to - the
  // tree then shows nothing for that workspace for up to one 15s poll,
  // since the list was fetched before it existed. Refetch immediately
  // when the selected workspace is not (yet) in the loaded list, rather
  // than waiting for the poll. Self-limiting: once the list actually
  // contains it (or genuinely never will), wsRes.data stops changing and
  // this stops re-firing (use-resource.js dedupes identical responses).
  React.useEffect(function () {
    if (!props.selectedWorkspaceId) return;
    var items = (wsRes.data && wsRes.data.items) || [];
    if (items.length && !items.some(function (w) {
      return w.id === props.selectedWorkspaceId;
    })) {
      wsRes.refetch();
    }
  }, [props.selectedWorkspaceId, wsRes.data]);
  var sessRes = window.primerApi.useResource(
    "nv-rail-all-sessions",
    function (signal) { return SH_api.allSessions(signal); },
    { pollMs: 5000, deps: [] }
  );
  var inboxRes = window.primerApi.useResource(
    "nv-rail-inbox",
    function (signal) {
      var workspaces = ((wsRes.data && wsRes.data.items) || []);
      return SH_api.pendingAttention(signal)
        .then(function (out) {
          var items = (out && out.items) || [];
          return { items: items.map(function (it) {
            return it.workspace_id ? it : Object.assign({}, it, { workspace_id: it.wid || null });
          }) };
        })
        .catch(function (err) {
          if (err && err.status !== 404) throw err;
          // Compatibility path only: a server predating the aggregate
          // endpoint (list_pending_attention) 404s here, so this fans out
          // the per-workspace endpoint instead - same idea, N calls
          // instead of one, and a different kind vocabulary (see
          // NV_Rail_inboxKindLabel above).
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

  // US-011f: restore the liveness the retired nv-sessions-sidebar.jsx had
  // (git show HEAD -- ui/components/console/nv-sessions-sidebar.jsx) -
  // it refetched on yielded/resumed/done tap frames instead of waiting
  // out its poll. Scoped to the SELECTED workspace's tap, same as that
  // file: the rail's Inbox is cross-workspace, but useWorkspaceTapListener
  // is one EventSource per workspace, and opening one per workspace just
  // for this rail is a bigger change than restoring the property that
  // regressed - attention changes in OTHER workspaces still arrive via
  // inboxRes's own 10s poll, unchanged. Debounced (leading-edge, 500ms
  // suppression window) because the retired code's un-debounced direct
  // refetch was fine for one workspace's frames but a burst here would
  // otherwise hammer the cross-workspace aggregate endpoint once per
  // frame; the first qualifying frame still refetches immediately so the
  // update still feels live.
  var attentionRefetchTimerRef = React.useRef(null);
  window.useWorkspaceTapListener(props.selectedWorkspaceId, function (ev) {
    var cls = ev && ev["class"];
    if (cls !== "yielded" && cls !== "resumed" && cls !== "done") return;
    if (attentionRefetchTimerRef.current) return;
    sessRes.refetch();
    inboxRes.refetch();
    attentionRefetchTimerRef.current = setTimeout(function () {
      attentionRefetchTimerRef.current = null;
    }, 500);
  });

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
          <span>Inbox — needs you</span>
          <span className="nv-rail-count">{inboxItems.length}</span>
          <div style={{ flex: 1 }} />
          <button type="button" className="nv-rail-iconbtn" title="Create session"
            data-verb="session.create"
            data-testid="nv-rail-create-session"
            onClick={function () {
              if (typeof props.onCreateSession === "function") props.onCreateSession();
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
            <button type="button" key={it.session_id} className="nv-rail-inbox-row"
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
            </button>
          );
        })}
      </div>
      <div className="nv-rail-tree" data-testid="nv-rail-tree">
        <div className="nv-rail-section-head">
          <span>Workspaces</span>
          <div style={{ flex: 1 }} />
          {/* data-verb on this button and the ws-row below are load-
              bearing (US-012b item 3): retiring the topbar workspace
              dropdown removed workspace.create/workspace.switch's only
              rendered data-verb attributes, which the dual-render guard
              (test_shell_dual_render_guard.py) requires somewhere. */}
          <button type="button" className="nv-rail-iconbtn" title="New workspace"
            data-verb="workspace.create"
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
              <button type="button" className="nv-rail-ws-row" data-testid={"nv-rail-ws:" + w.id}
                data-selected={isSelected ? "true" : "false"}
                data-verb="workspace.switch"
                onClick={function () {
                  if (typeof props.onSelectWorkspace === "function") props.onSelectWorkspace(w.id);
                  setExpanded(function (prev) {
                    var next = Object.assign({}, prev);
                    next[w.id] = !prev[w.id];
                    return next;
                  });
                }}
                onContextMenu={function (ev) {
                  ev.preventDefault();
                  ev.stopPropagation();
                  setMenu({ kind: "workspace", w: w, x: ev.clientX, y: ev.clientY });
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
                {/* UX reconcile wave 1 (audit A item 17): a bare count read as
                    an unlabeled second number next to the attention count -
                    spell out the unit, same as the reference. */}
                <span className="nv-rail-session-count">
                  {wsSessions.length} {wsSessions.length === 1 ? "session" : "sessions"}
                </span>
              </button>
              {isOpen ? wsSessions.map(function (s) {
                var sid = s.session_id;
                var ident = NV_identity(s.binding);
                var isAttention = !!(attentionSidsByWs[w.id] && attentionSidsByWs[w.id][sid]);
                return (
                  <button type="button" key={sid} className="nv-rail-ws-session"
                    data-testid={"nv-rail-ws-session:" + sid}
                    onClick={function () {
                      if (typeof props.onSelectWorkspace === "function") props.onSelectWorkspace(w.id);
                      openSession(s);
                    }}
                    onContextMenu={function (ev) {
                      ev.preventDefault();
                      ev.stopPropagation();
                      setMenu({ kind: "session", s: s, wid: w.id, x: ev.clientX, y: ev.clientY });
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
                  </button>
                );
              }) : null}
            </div>
          );
        })}
      </div>
      {menu && menu.kind === "session" ? (
        <NV_Rail_SessionContextMenu session={menu.s} wid={menu.wid} x={menu.x} y={menu.y}
          onOpen={function (s) { openSession(s); }}
          onChanged={refetchSessions}
          onClose={function () { setMenu(null); }} />
      ) : null}
      {menu && menu.kind === "workspace" ? (
        <NV_Rail_WorkspaceContextMenu workspace={menu.w} x={menu.x} y={menu.y}
          onCreateSessionInWorkspace={props.onCreateSessionInWorkspace}
          onOpenSettings={props.onOpenWorkspaceSettings}
          onChanged={function () { wsRes.refetch(); refetchSessions(); }}
          onClose={function () { setMenu(null); }} />
      ) : null}
    </div>
  );
}

window.NV_Rail = NV_Rail;
