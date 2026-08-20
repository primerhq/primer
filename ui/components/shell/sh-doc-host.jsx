/* global React, SH_api, SH_useShell, SH_statusLine, SH_statusFromTap,
   SH_OVERLAYS */
// Fresh shell center: tab bar, split groups, doc renderers, status bar.
//
// Comparison NEVER goes in an overlay (spec section 8): the trace tab and
// diffs open as tabs in a second group, side by side with the transcript.

var SH_OVERLAY_LABELS = {
  "new-session": "Create New Session",
  providers: "Open Providers Catalog",
  collections: "Open Collections",
  agents: "Open Agents",
  graphs: "Open Graphs",
  triggers: "Open Triggers",
  toolsets: "Open Toolsets",
  tools: "Open Tools",
  workers: "Open Workers",
  approvals: "Open Approvals",
  admin: "Open Admin Settings",
  harnesses: "Open Harnesses",
  services: "Open Services",
  channels: "Open Channels",
  workspaces: "Open Workspaces",
};

function SH_registerCoreVerbs(shell) {
  shell.registry.register({
    id: "workspace.switch", label: "Switch Workspace", chord: "Ctrl+Shift+p",
    surfaces: ["topbar", "rail", "palette"],
    run: function () { shell.openOverlay("workspaces"); },
  });

  // "attention: next / resolve / snooze" as verbs, so triage is
  // keyboard-first (section 8). Each also renders as a rail affordance,
  // satisfying the dual-render rule.
  function attentionState() { return shell.attentionRef.current || null; }
  function firstItem() {
    var state = attentionState();
    return state && state.items.length ? state.items[0] : null;
  }

  shell.registry.register({
    id: "attention.next", label: "Open Attention", chord: "Ctrl+j",
    surfaces: ["rail", "palette"],
    run: function () {
      var item = firstItem();
      if (!item) { shell.toast("Nothing needs you"); return; }
      shell.openDoc({ kind: "session", ref: item.sessionId, preview: true });
    },
  });
  shell.registry.register({
    id: "attention.resolve", label: "Resolve Attention",
    surfaces: ["attention-item", "palette"],
    run: function (arg) {
      var item = (arg && arg.sessionId) ? arg : firstItem();
      if (!item) return;
      shell.openDoc({ kind: "session", ref: item.sessionId, preview: true });
    },
  });
  shell.registry.register({
    id: "attention.snooze", label: "Snooze Attention",
    surfaces: ["attention-item", "palette"],
    run: function (arg) {
      var state = attentionState();
      var item = (arg && arg.id) ? arg : firstItem();
      if (!state || !item) return;
      var next = JSON.parse(JSON.stringify(state.triage));
      next.snoozedUntil[item.id] = Date.now() + 60 * 60 * 1000;
      state.commit(next);
    },
  });
  shell.registry.register({
    id: "attention.mute", label: "Mute Session", destructive: true,
    surfaces: ["attention-item", "palette"],
    run: function (arg) {
      var state = attentionState();
      var item = (arg && arg.sessionId) ? arg : firstItem();
      if (!state || !item) return;
      var next = JSON.parse(JSON.stringify(state.triage));
      next.mutedSessions[item.sessionId] = true;
      state.commit(next);
    },
  });

  shell.registry.register({
    id: "doc.close", label: "Close Tab", chord: "Ctrl+w",
    surfaces: ["tab-menu", "palette"],
    run: function () {
      var group = shell.docs.groups[shell.docs.activeGroup];
      if (group && group.activeId) shell.closeDoc(group.activeId);
    },
  });
  shell.registry.register({
    id: "doc.cycleMru", label: "Switch Tab", chord: "Ctrl+Tab",
    surfaces: ["tab-menu", "palette"],
    run: function () { shell.cycleMru(1); },
  });
  shell.registry.register({
    id: "doc.openQuick", label: "Open File", chord: "Ctrl+p",
    surfaces: ["rail", "palette"],
    run: function () { shell.openOverlay("collections"); },
  });
  // The palette's own chord is a verb like any other: SH_CHORDS names it,
  // so leaving it unregistered would bind two keystrokes to nothing the
  // registry knows about.
  shell.registry.register({
    id: "palette.open", label: "Open Palette", chord: "Ctrl+k",
    surfaces: ["topbar", "palette"],
    run: function () { shell.openPalette(); },
  });
  // diff and wiki are addressable doc kinds, so each needs a verb that
  // opens one: a kind the URL can reach but no verb can is an orphan.
  shell.registry.register({
    id: "doc.openChanges", label: "Open Changes", surfaces: ["rail", "palette"],
    run: function (arg) {
      var sha = arg && arg.sha;
      if (!sha) { shell.openOverlay("workspaces"); return; }
      shell.openDoc({ kind: "diff", ref: sha, title: "Changes " + sha.slice(0, 7),
        preview: true });
    },
  });
  shell.registry.register({
    id: "doc.openWiki", label: "Open Document", surfaces: ["rail", "palette"],
    run: function (arg) {
      var slug = arg && arg.slug;
      if (!slug) { shell.openOverlay("collections"); return; }
      shell.openDoc({ kind: "wiki", ref: slug, title: slug, preview: true });
    },
  });
  shell.registry.register({
    id: "doc.pin", label: "Pin Tab", surfaces: ["tab-menu"],
    run: function () {
      var group = shell.docs.groups[shell.docs.activeGroup];
      if (group && group.activeId) shell.pinDoc(group.activeId, true);
    },
  });
  shell.registry.register({
    id: "layout.splitRight", label: "Split Right", chord: "Ctrl+\\",
    surfaces: ["tab-menu", "palette"],
    run: function () { shell.splitRight(); },
  });

  // Comparison never goes in an overlay: the trace opens as a tab in a
  // SECOND group, side by side with the transcript it explains.
  shell.registry.register({
    id: "trace.split", label: "Split Trace", chord: "Ctrl+Shift+t",
    contexts: ["session"], surfaces: ["tab-menu", "palette"],
    run: function (arg) {
      var group = shell.docs.groups[shell.docs.activeGroup];
      var tab = null;
      for (var i = 0; group && i < group.tabs.length; i++) {
        if (group.tabs[i].id === group.activeId) tab = group.tabs[i];
      }
      if (!tab || tab.kind !== "session") return;
      var turnNo = (arg && arg.turnNo) != null ? arg.turnNo : 0;
      shell.splitRight();
      shell.openDoc({
        kind: "trace",
        ref: window.SH_traceRef(tab.ref, turnNo),
        title: "Trace " + turnNo,
        preview: false,
      });
    },
  });

  shell.registry.register({
    id: "session.create", label: "Create Session", surfaces: ["rail", "palette"],
    run: function () {
      SH_api.createSession(shell.wid, {}).then(function (row) {
        var sid = row && (row.session_id || row.id);
        if (sid) shell.openDoc({ kind: "session", ref: sid, preview: false });
        shell.sessions.refetch();
      });
    },
  });
  shell.registry.register({
    // session.create is the one-click path and takes the workspace
    // default binding. This one is for picking a specific agent or
    // graph, plus a name and opening instructions, before the session
    // exists -- the form is the same one the classic console used.
    id: "session.createAs", label: "Create Session With Binding",
    surfaces: ["rail", "palette"],
    run: function () { shell.openOverlay("new-session"); },
  });
  shell.registry.register({
    id: "session.interrupt", label: "Interrupt Session", destructive: true,
    contexts: ["session"], surfaces: ["tab-menu", "palette"],
    run: function () {
      var group = shell.docs.groups[shell.docs.activeGroup];
      var tab = null;
      for (var i = 0; group && i < group.tabs.length; i++) {
        if (group.tabs[i].id === group.activeId) tab = group.tabs[i];
      }
      if (tab && tab.kind === "session") SH_api.interrupt(shell.wid, tab.ref);
    },
  });
  // The soft pair beside Interrupt. Both endpoints are live, so both
  // need a way in; the active tab supplies the session, same as
  // interrupt does.
  function activeSessionRef() {
    var group = shell.docs.groups[shell.docs.activeGroup];
    for (var j = 0; group && j < group.tabs.length; j++) {
      if (group.tabs[j].id === group.activeId) {
        return group.tabs[j].kind === "session" ? group.tabs[j].ref : null;
      }
    }
    return null;
  }
  shell.registry.register({
    id: "session.pause", label: "Pause Session",
    contexts: ["session"], surfaces: ["tab-menu", "palette"],
    run: function () {
      var ref = activeSessionRef();
      if (ref) SH_api.pause(shell.wid, ref).then(function () {
        shell.sessions.refetch();
      });
    },
  });
  shell.registry.register({
    id: "session.resume", label: "Resume Session",
    contexts: ["session"], surfaces: ["tab-menu", "palette"],
    run: function () {
      var ref = activeSessionRef();
      if (ref) SH_api.resume(shell.wid, ref).then(function () {
        shell.sessions.refetch();
      });
    },
  });

  shell.registry.register({
    id: "session.end", label: "End Session", destructive: true,
    contexts: ["session"], surfaces: ["tab-menu", "palette"],
    run: function () {
      var ref = activeSessionRef();
      if (ref) SH_api.cancel(shell.wid, ref).then(function () {
        shell.toast("Session ended");
        shell.sessions.refetch();
      });
    },
  });
  shell.registry.register({
    id: "session.restart", label: "Restart Session",
    contexts: ["session"], surfaces: ["tab-menu", "palette"],
    run: function () {
      var ref = activeSessionRef();
      if (ref) SH_api.restart(shell.wid, ref).then(function () {
        shell.toast("Session restarted");
        shell.sessions.refetch();
      });
    },
  });

  for (var i = 0; i < SH_OVERLAYS.length; i++) {
    (function (name) {
      shell.registry.register({
        id: "overlay.open." + name,
        label: SH_OVERLAY_LABELS[name],
        surfaces: ["topbar", "palette"],
        run: function () { shell.openOverlay(name); },
      });
    })(SH_OVERLAYS[i]);
  }
}

function SH_TabMenu(props) {
  var shell = SH_useShell();
  return (
    <span className="sh-tab-menu" data-testid={"shell-tab-menu:" + props.tab.id}>
      {shell.registry.forSurface("tab-menu").map(function (verb) {
        if (verb.contexts && verb.contexts.indexOf(props.tab.kind) < 0) return null;
        return (
          <button key={verb.id} type="button" className="sh-verb"
            data-verb={verb.id} onClick={function () { verb.run(props.tab); }}>
            {verb.label}
          </button>
        );
      })}
    </span>
  );
}

function SH_DocBody(props) {
  var tab = props.tab;
  if (tab.kind === "session" && typeof window.SH_SessionDoc === "function") {
    return <window.SH_SessionDoc sid={tab.ref} />;
  }
  if (tab.kind === "file" && typeof window.SH_FileDoc === "function") {
    return <window.SH_FileDoc path={tab.ref} />;
  }
  if (tab.kind === "diff" && typeof window.SH_DiffDoc === "function") {
    return <window.SH_DiffDoc sha={tab.ref} />;
  }
  if (tab.kind === "wiki" && typeof window.SH_WikiDoc === "function") {
    return <window.SH_WikiDoc slug={tab.ref} />;
  }
  if (tab.kind === "trace" && typeof window.SH_TraceTab === "function") {
    return <window.SH_TraceTab docRef={tab.ref} />;
  }
  return <div className="sh-empty">Nothing open. Press Ctrl+K for verbs.</div>;
}

function SH_DocHost() {
  var shell = SH_useShell();
  var tap = window.useWorkspaceTap(shell.wid);
  var registeredRef = React.useRef(false);
  if (!registeredRef.current) {
    registeredRef.current = true;
    SH_registerCoreVerbs(shell);
  }

  return (
    <div className="sh-groups">
      {shell.docs.groups.map(function (group, gi) {
        return (
          <section key={gi} className="sh-group" data-testid={"shell-group:" + gi}>
            <div className="sh-tabbar" role="tablist">
              {group.tabs.map(function (tab) {
                var live = tab.kind === "session"
                  ? SH_statusFromTap(tap.events, tab.ref, Date.now())
                  : null;
                return (
                  <span key={tab.id} className="sh-tab" data-preview={tab.preview}
                    data-pinned={tab.pinned} data-badge={tab.badge}
                    data-testid={"shell-tab:" + tab.id}>
                    <button
                      type="button"
                      onClick={function () {
                        shell.openDoc({
                          kind: tab.kind, ref: tab.ref, preview: tab.preview,
                        });
                      }}
                      onDoubleClick={function () { shell.promoteDoc(tab.id); }}
                    >
                      {tab.title}
                      {live ? (
                        <span className="sh-tab-status">
                          {SH_statusLine({
                            verb: live.verb, object: live.object,
                            elapsedSec: Math.round((Date.now() - live.startedMs) / 1000),
                          })}
                        </span>
                      ) : null}
                    </button>
                    <SH_TabMenu tab={tab} />
                  </span>
                );
              })}
            </div>
            <div className="sh-doc">
              {group.tabs.filter(function (t) { return t.id === group.activeId; })
                .map(function (tab) {
                  return <SH_DocBody key={tab.id} tab={tab} />;
                })}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function SH_StatusBar() {
  var shell = SH_useShell();
  var tap = window.useWorkspaceTap(shell.wid);
  var group = shell.docs.groups[shell.docs.activeGroup];
  var tab = null;
  for (var i = 0; group && i < group.tabs.length; i++) {
    if (group.tabs[i].id === group.activeId) tab = group.tabs[i];
  }
  var live = tab && tab.kind === "session"
    ? SH_statusFromTap(tap.events, tab.ref, Date.now())
    : null;
  return (
    <div className="sh-status" data-testid="shell-status-line">
      {live ? SH_statusLine({
        verb: live.verb, object: live.object,
        elapsedSec: Math.round((Date.now() - live.startedMs) / 1000),
      }) : "idle"}
      {live ? (
        <button type="button" className="sh-verb" data-testid="shell-interrupt"
          onClick={function () { shell.registry.get("session.interrupt").run(); }}>
          Interrupt Session
        </button>
      ) : null}
    </div>
  );
}

function SH_Topbar() {
  var shell = SH_useShell();
  return (
    <div className="sh-topbar-inner">
      {/* The Studio put a gear here that opened the workspace's own
          settings. The overlay that replaced it can address one
          workspace (overlay=workspaces:detail:<wid>) but nothing in the
          shell opened it for the workspace you are actually in, so the
          config, channels, log and destroy tabs were unreachable without
          hand-writing a URL. The name is the affordance. */}
      <button
        type="button"
        className="sh-ws"
        data-testid="shell-workspace"
        title="Workspace settings"
        onClick={function () {
          shell.openOverlay("workspaces", "detail", shell.wid);
        }}
      >{shell.wid}</button>
      <span className="sh-topbar-verbs">
        {shell.registry.forSurface("topbar").map(function (verb) {
          return (
            <button key={verb.id} type="button" className="sh-verb"
              data-verb={verb.id} onClick={function () { verb.run(); }}>
              {verb.label}
            </button>
          );
        })}
      </span>
    </div>
  );
}

window.SH_OVERLAY_LABELS = SH_OVERLAY_LABELS;
window.SH_registerCoreVerbs = SH_registerCoreVerbs;
window.SH_DocHost = SH_DocHost;
window.SH_StatusBar = SH_StatusBar;
window.SH_Topbar = SH_Topbar;
