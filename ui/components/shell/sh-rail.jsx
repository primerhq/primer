/* global React, SH_api, SH_useShell, SH_statusLine, SH_statusFromTap */
// Fresh shell rail (S8 spec section 8, "Rail discipline").
//
// Exactly three place-y lists: sessions (frecency-ordered, status chips,
// parent nesting), files, attention. Global utilities live in the top bar.
// Order, hiding, badge style and collapse persist per ACCOUNT: keyed by
// the authenticated username from /auth/status, because section 1 forbids
// new backend capability and "per account" cannot mean "per browser".

// "attention" left the rail lists 2026-08-23: the pinned Inbox row +
// the Inbox doc carry triage now; interrupts toast via the shell-level
// SH_AttentionEngine.
var SH_RAIL_LISTS = ["sessions", "files"];

function SH_railPrefsKey(username) {
  return "primer.shell.rail:" + (username || "anon");
}

var SH_RAIL_DEFAULT_PREFS = {
  order: ["sessions", "files"],
  hidden: [],
  badgeStyle: "count",
  collapsed: {},
  // Collapsed OTHER-workspace groups in the cross-workspace sessions
  // list, keyed by workspace id (revamp section 3).
  groups: {},
  // Persisted drag-resized section heights in px, keyed by list name.
  sizes: {},
};

function SH_loadRailPrefs(username) {
  try {
    var raw = window.localStorage.getItem(SH_railPrefsKey(username));
    if (!raw) return Object.assign({}, SH_RAIL_DEFAULT_PREFS);
    return Object.assign({}, SH_RAIL_DEFAULT_PREFS, JSON.parse(raw));
  } catch (_e) {
    return Object.assign({}, SH_RAIL_DEFAULT_PREFS);
  }
}

function SH_saveRailPrefs(username, prefs) {
  try {
    window.localStorage.setItem(SH_railPrefsKey(username), JSON.stringify(prefs));
  } catch (_e) {
    /* quota or disabled storage: personalization is not worth throwing over */
  }
}

function SH_RailVerbs(props) {
  var shell = SH_useShell();
  var verbs = shell.registry.forSurface("rail");
  return (
    <span className="sh-rail-verbs">
      {verbs.map(function (verb) {
        return (
          <button
            key={verb.id}
            type="button"
            className="sh-verb"
            title={verb.chord ? verb.label + " (" + verb.chord + ")" : verb.label}
            data-testid={"rail-verb:" + verb.id}
            onClick={function () { verb.run(props.target); }}
          >{verb.label}</button>
        );
      })}
    </span>
  );
}

// Cross-workspace sessions (revamp section 3): one list over
// GET /v1/sessions, grouped by workspace. The active workspace's group
// renders first and expanded; other groups collapse, with the state
// persisted per account in the rail prefs (groups.<wid>).
function SH_SessionsList(props) {
  var shell = SH_useShell();
  var tap = window.useWorkspaceTap(shell.wid);
  var prefs = props && props.prefs;
  var update = props && props.update;
  var items = (shell.allSessions.data && shell.allSessions.data.items) || [];
  var frecency = shell.frecency;
  var ordered = items.slice().sort(function (a, b) {
    var delta = frecency.scoreFor("session:" + b.session_id)
      - frecency.scoreFor("session:" + a.session_id);
    if (delta) return delta;
    return String(b.last_activity_at).localeCompare(String(a.last_activity_at));
  });

  function open(session) {
    shell.frecency.record("session:" + session.session_id);
    if (session.workspace_id && session.workspace_id !== shell.wid) {
      // Another workspace: navigate, the URL is the state.
      window.location.hash = window.SH_buildUrl({
        wid: session.workspace_id,
        doc: { kind: "session", ref: session.session_id },
      });
      return;
    }
    shell.openDoc({
      kind: "session", ref: session.session_id,
      title: session.name || session.session_id, preview: true,
    });
  }

  function row(session, depth, inGroup) {
    var live = session.workspace_id === shell.wid
      ? SH_statusFromTap(tap.events, session.session_id, Date.now())
      : null;
    var children = inGroup.filter(function (s) {
      return s.parent_session_id === session.session_id;
    });
    return (
      <li key={session.session_id} className="sh-rail-row" data-depth={depth}>
        <button
          type="button"
          data-testid={"rail-session:" + session.session_id}
          onClick={function () { open(session); }}
        >
          <span className="sh-rail-title">{session.name || session.session_id}</span>
          <span className="sh-rail-chip" data-testid="session-status-dot"
            data-status={session.status}>
            {live
              ? SH_statusLine({
                verb: live.verb, object: live.object,
                elapsedSec: Math.round((Date.now() - live.startedMs) / 1000),
              })
              : session.status}
          </span>
        </button>
        <SH_RailVerbs target={{ kind: "session", ref: session.session_id }} />
        {children.length ? (
          <ul className="sh-rail-children">
            {children.map(function (child) {
              return row(child, depth + 1, inGroup);
            })}
          </ul>
        ) : null}
      </li>
    );
  }

  // Group by workspace; the active group first, others by newest row.
  var groups = [];
  var byWid = {};
  for (var i = 0; i < ordered.length; i++) {
    var wid = ordered[i].workspace_id || "unknown";
    if (!byWid[wid]) {
      byWid[wid] = { wid: wid, items: [] };
      groups.push(byWid[wid]);
    }
    byWid[wid].items.push(ordered[i]);
  }
  groups.sort(function (a, b) {
    if (a.wid === shell.wid) return -1;
    if (b.wid === shell.wid) return 1;
    return 0; // stable: already newest-first via ordered
  });

  function groupBody(group) {
    var roots = group.items.filter(function (s) { return !s.parent_session_id; });
    return roots.map(function (s) { return row(s, 0, group.items); });
  }

  return (
    <ul className="sh-rail-list" data-testid="rail-sessions">
      {groups.map(function (group) {
        if (group.wid === shell.wid) return groupBody(group);
        var collapsed = !!(prefs && prefs.groups && prefs.groups[group.wid]);
        return (
          <li key={"g:" + group.wid} className="sh-rail-group">
            <details open={!collapsed} onToggle={function (ev) {
              if (!prefs || !update) return;
              var next = Object.assign({}, prefs, {
                groups: Object.assign({}, prefs.groups),
              });
              next.groups[group.wid] = !ev.target.open;
              if (next.groups[group.wid] !== collapsed) update(next);
            }}>
              <summary data-testid={"rail-group:" + group.wid}>
                {group.wid}
                <span className="sh-badge">{group.items.length}</span>
              </summary>
              <ul className="sh-rail-list">{groupBody(group)}</ul>
            </details>
          </li>
        );
      })}
      {/* Say what is true, then offer the remedy. A lone button left the
          operator to infer the workspace was empty from the absence of
          rows, which reads the same as a list that has not loaded. */}
      {ordered.length ? null : (
        <li className="sh-empty">
          <span>No sessions yet</span>
          <button type="button" data-testid="rail-sessions-empty"
            onClick={function () { shell.registry.get("session.create").run(); }}>
            Create Session
          </button>
        </li>
      )}
    </ul>
  );
}

// One level below an opened folder. Its own resource, so opening a folder
// costs one request for that folder and closing it stops the polling.
function SH_FilesSubtree(props) {
  var shell = SH_useShell();
  var path = props.path;
  var tree = window.primerApi.useResource(
    SH_api.keys.tree(shell.wid, path),
    function (signal) { return SH_api.filesTree(shell.wid, path, signal); },
    { pollMs: 5000, deps: [shell.wid, path] }
  );
  var entries = (tree.data && tree.data.items) || [];
  if (!entries.length) {
    return (
      <ul className="sh-rail-children">
        <li className="sh-empty">
          <span>{tree.loading ? "Loading…" : "Empty folder"}</span>
        </li>
      </ul>
    );
  }
  return (
    <ul className="sh-rail-children">
      {entries.map(function (entry) {
        return (
          <li key={entry.path} className="sh-rail-row">
            <button
              type="button"
              data-testid={"rail-file:" + entry.path}
              onClick={function () {
                if (entry.is_dir) return;
                shell.openDoc({ kind: "file", ref: entry.path, preview: true });
              }}
            >{entry.name || entry.path}</button>
          </li>
        );
      })}
    </ul>
  );
}

function SH_FilesList() {
  var shell = SH_useShell();
  // The workspace tree changes while an agent works, which is the whole
  // reason to watch a session. Fetched once on mount and never again, a
  // file the agent had just written did not appear in the rail until the
  // page was reloaded. Same cadence as the session list beside it.
  var tree = window.primerApi.useResource(
    SH_api.keys.tree(shell.wid, "."),
    function (signal) { return SH_api.filesTree(shell.wid, ".", signal); },
    { pollMs: 5000, deps: [shell.wid] }
  );
  // The tree route answers {path, items}. This read `entries`, a key it
  // has never sent, so the list was empty every time and the rail showed
  // its empty state for every workspace, however many files were in it.
  var entries = (tree.data && tree.data.items) || [];

  // A folder opens to show what is in it. The route answers one level at
  // a time (recursive=false), so descending means asking for the child
  // path; without that the rail listed the workspace root and nothing
  // under it, which is not a tree.
  var openState = React.useState({});
  var open = openState[0];
  var setOpen = openState[1];
  function toggle(path) {
    setOpen(function (prev) {
      var next = Object.assign({}, prev);
      if (next[path]) delete next[path]; else next[path] = true;
      return next;
    });
  }

  return (
    <ul className="sh-rail-list" data-testid="rail-files">
      {entries.map(function (entry) {
        return (
          <li key={entry.path} className="sh-rail-row">
            <button
              type="button"
              data-testid={"rail-file:" + entry.path}
              aria-expanded={entry.is_dir ? !!open[entry.path] : undefined}
              onClick={function () {
                // is_dir, not type: the same payload, read correctly. A
                // directory opened as a file would have asked the file
                // route to read a folder.
                if (entry.is_dir) { toggle(entry.path); return; }
                shell.openDoc({ kind: "file", ref: entry.path, preview: true });
              }}
            >{entry.path}</button>
            {entry.is_dir && open[entry.path]
              ? <SH_FilesSubtree path={entry.path} />
              : null}
          </li>
        );
      })}
      {/* An empty <ul> has no height, so a workspace with no files (or
          one whose tree has not arrived yet) rendered a list that was
          not merely empty but invisible: nothing on screen said whether
          there were no files or the rail had failed. The sessions list
          beside it has always said. */}
      {/* Every empty state is a prompt WITH an action (spec section 8),
          so this says which of the two situations it is and then offers
          the way on. An empty <ul> has no height, which is how a
          workspace with no files rendered a list that was not merely
          empty but invisible. */}
      {entries.length ? null : (
        <li className="sh-empty">
          <button type="button" data-testid="rail-files-empty"
            onClick={function () { shell.registry.get("doc.openQuick").run(); }}>
            {tree.loading ? "Loading files…" : "Open File"}
          </button>
        </li>
      )}
    </ul>
  );
}

// P2 ships the list as a count and an empty state; P4 Task 22 replaces
// this body with the tiered feed, its triage verbs and its decision
// cards. The empty state is a prompt with an action, per section 8.
// Three tiers routed by consequence (section 8):
//   interrupt -> in-shell toast AND a rail row, spent sparingly
//   ambient   -> a rail badge, no sound, ever
//   digest    -> a per-session collapsed rollup
// Resolved and snoozed items stay queryable: triage filters the live
// view, it never deletes.
function SH_loadTriage(username) {
  try {
    var raw = window.localStorage.getItem(window.SH_triageKey(username));
    return raw ? JSON.parse(raw) : window.SH_emptyTriage();
  } catch (_e) {
    return window.SH_emptyTriage();
  }
}

function SH_saveTriage(username, triage) {
  try {
    window.localStorage.setItem(
      window.SH_triageKey(username), JSON.stringify(triage)
    );
  } catch (_e) { /* private mode: triage is best-effort, never fatal */ }
}

// The attention ENGINE (revamp section 5): headless-ish shell-level
// component. It fans pending yields across EVERY workspace, merges the
// approval records, applies triage, publishes the result on
// shell.attentionRef (the contract the attention.* verbs already use)
// plus a "sh-attention" window event for reactive badges, and renders
// ONLY the interrupt toasts. The Inbox doc renders the triage surface
// from the same published state.
function SH_AttentionEngine() {
  var shell = SH_useShell();
  var pending = window.primerApi.useResource(
    "shell-pending-all",
    function (signal) {
      return SH_api.workspaces(signal).then(function (ws) {
        var wids = ((ws && ws.items) || []).map(function (w) { return w.id; });
        return Promise.all(wids.map(function (wid) {
          return SH_api.pendingYields(wid, signal).then(function (out) {
            var items = (out && out.items) || [];
            for (var i = 0; i < items.length; i++) {
              items[i].workspace_id = items[i].workspace_id || wid;
            }
            return items;
          }, function () { return []; });
        })).then(function (lists) {
          return { items: [].concat.apply([], lists) };
        });
      });
    },
    { pollMs: 10000 }
  );
  var records = window.primerApi.useResource(
    SH_api.keys.records(),
    function (signal) { return SH_api.approvalRecords(signal); },
    { pollMs: 0 }
  );

  // Gate/pending state arrives on the tap, so the feed is live rather
  // than poll-latent (pinned decision 7).
  window.useWorkspaceTapListener(shell.wid, function (ev) {
    var cls = ev && ev["class"];
    if (cls === "yielded" || cls === "resumed" || cls === "done") {
      pending.refetch();
      records.refetch();
    }
  });

  var triageState = React.useState(function () {
    return SH_loadTriage(shell.username);
  });
  var triage = triageState[0];
  var setTriage = triageState[1];

  function commit(next) {
    setTriage(next);
    SH_saveTriage(shell.username, next);
  }

  var all = window.SH_toAttentionItems({
    pending: (pending.data && pending.data.items) || [],
    records: (records.data && records.data.items) || [],
  });
  var visible = window.SH_applyTriage(all, triage);
  var interrupts = visible.filter(function (i) { return i.tier === "interrupt"; });
  shell.attentionRef.current = {
    items: visible, triage: triage, commit: commit,
    refetch: function () { pending.refetch(); records.refetch(); },
  };
  React.useEffect(function () {
    try {
      window.dispatchEvent(new CustomEvent("sh-attention", {
        detail: { count: visible.length - visible.filter(function (i) {
          return i.tier === "digest";
        }).length },
      }));
    } catch (_e) { /* CustomEvent unavailable: badges stay poll-late */ }
  });

  // Interrupts, and only interrupts, get an in-shell toast. No sound at
  // any tier. Ambient and digest render in the Inbox doc.
  return (
    <div className="sh-attention-toasts" data-testid="shell-attention-toasts">
      {interrupts.map(function (item) {
        return (
          <div key={item.id} className="sh-attention-toast"
            data-testid={"attention-toast:" + item.toolCallId}>
            <div data-testid={"attention-item:" + item.sessionId}>
              <window.SH_DecisionCard item={item}
                onResolved={function () { pending.refetch(); }} />
              <window.SH_TriageVerbs item={item} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// The triage strip, rendered from the registry rather than hardcoded
// buttons: a verb that is renamed or dropped must disappear here rather
// than leave a button that looks live and does nothing. Shared by the
// interrupt toasts and the Inbox doc.
function SH_TriageVerbs(props) {
  var shell = SH_useShell();
  var item = props.item;
  var state = shell.attentionRef.current;
  var runners = {
    "attention.resolve": function () {
      shell.openDoc({ kind: "session", ref: item.sessionId, preview: true });
    },
    "attention.snooze": function () {
      if (!state) return;
      var next = JSON.parse(JSON.stringify(state.triage));
      next.snoozedUntil[item.id] = Date.now() + 60 * 60 * 1000;
      state.commit(next);
    },
    "attention.mute": function () {
      if (!state) return;
      var next = JSON.parse(JSON.stringify(state.triage));
      next.muted[item.toolName] = true;
      state.commit(next);
    },
  };
  return (
    <span className="sh-triage">
      {shell.registry.forSurface("attention-item").map(function (verb) {
        var run = runners[verb.id];
        if (!run) return null;
        return (
          <button type="button" key={verb.id} className="sh-verb"
            data-verb={verb.id}
            onClick={function () { run(); }}>{verb.label}</button>
        );
      })}
    </span>
  );
}

function SH_Rail() {
  var shell = SH_useShell();
  var username = shell.username;
  var prefsState = React.useState(function () { return SH_loadRailPrefs(username); });
  var prefs = prefsState[0];
  var setPrefs = prefsState[1];
  React.useEffect(function () {
    setPrefs(SH_loadRailPrefs(username));
  }, [username]);

  // Badge count published by SH_AttentionEngine (shell-level), so the
  // rail needs no data fetch of its own for it.
  var inboxState = React.useState(0);
  var inboxCount = inboxState[0];
  var setInboxCount = inboxState[1];
  React.useEffect(function () {
    function onAttention(ev) {
      setInboxCount((ev && ev.detail && ev.detail.count) || 0);
    }
    window.addEventListener("sh-attention", onAttention);
    return function () { window.removeEventListener("sh-attention", onAttention); };
  }, []);

  function update(next) {
    setPrefs(next);
    SH_saveRailPrefs(username, next);
  }

  var bodies = {
    sessions: <SH_SessionsList prefs={prefs} update={update} />,
    files: <SH_FilesList />,
  };
  var counts = { sessions: 0, files: 0 };

  return (
    <nav className="sh-rail-nav">
      {/* The pinned Inbox row: the one "something needs you" entry point
          (revamp section 3), always above the sections. */}
      <button
        type="button"
        className="sh-rail-inbox"
        data-testid="rail-inbox"
        onClick={function () {
          var verb = shell.registry.get("inbox.open");
          if (verb) verb.run();
        }}
      >
        Inbox
        {inboxCount ? (
          <span className="sh-rail-badge" data-testid="rail-inbox-badge">
            {inboxCount}
          </span>
        ) : null}
      </button>
      {prefs.order.filter(function (name) {
        return SH_RAIL_LISTS.indexOf(name) >= 0 && prefs.hidden.indexOf(name) < 0;
      }).map(function (name) {
        var collapsed = !!prefs.collapsed[name];
        // Native CSS resize + a ResizeObserver for persistence: drag
        // the section's lower-right grip, the height sticks per
        // account (revamp section 3, the unified-rail mitigation).
        var sized = !collapsed && prefs.sizes && prefs.sizes[name];
        return (
          <section key={name} className="sh-rail-section" data-collapsed={collapsed}
            style={sized ? { height: prefs.sizes[name] + "px" } : null}
            ref={function (el) {
              if (!el || collapsed || !window.ResizeObserver) return;
              if (el._shResizeObs) return;
              var obs = new ResizeObserver(function (entries) {
                var h = Math.round(entries[0].contentRect.height);
                if (!h || Math.abs(h - (prefs.sizes[name] || 0)) < 4) return;
                clearTimeout(el._shResizeT);
                el._shResizeT = setTimeout(function () {
                  var next = Object.assign({}, SH_loadRailPrefs(username));
                  next.sizes = Object.assign({}, next.sizes);
                  next.sizes[name] = h;
                  update(next);
                }, 300);
              });
              obs.observe(el);
              el._shResizeObs = obs;
            }}>
            <button
              type="button"
              className="sh-rail-head"
              data-testid={"rail-head:" + name}
              onClick={function () {
                var next = Object.assign({}, prefs, {
                  collapsed: Object.assign({}, prefs.collapsed),
                });
                next.collapsed[name] = !collapsed;
                update(next);
              }}
            >
              {name}
              {counts[name] ? (
                <span className="sh-badge"
                  data-testid={"rail-head-badge:" + name}>
                  {prefs.badgeStyle === "dot" ? "" : counts[name]}
                </span>
              ) : null}
            </button>
            {collapsed ? null : bodies[name]}
          </section>
        );
      })}
    </nav>
  );
}

window.SH_RAIL_LISTS = SH_RAIL_LISTS;
window.SH_railPrefsKey = SH_railPrefsKey;
window.SH_loadRailPrefs = SH_loadRailPrefs;
window.SH_saveRailPrefs = SH_saveRailPrefs;
window.SH_Rail = SH_Rail;
window.SH_loadTriage = SH_loadTriage;
window.SH_saveTriage = SH_saveTriage;
window.SH_AttentionEngine = SH_AttentionEngine;
window.SH_TriageVerbs = SH_TriageVerbs;
