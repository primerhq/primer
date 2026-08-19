/* global React, SH_api, SH_useShell, SH_statusLine, SH_statusFromTap */
// Fresh shell rail (S8 spec section 8, "Rail discipline").
//
// Exactly three place-y lists: sessions (frecency-ordered, status chips,
// parent nesting), files, attention. Global utilities live in the top bar.
// Order, hiding, badge style and collapse persist per ACCOUNT: keyed by
// the authenticated username from /auth/status, because section 1 forbids
// new backend capability and "per account" cannot mean "per browser".

var SH_RAIL_LISTS = ["sessions", "files", "attention"];

function SH_railPrefsKey(username) {
  return "primer.shell.rail:" + (username || "anon");
}

var SH_RAIL_DEFAULT_PREFS = {
  order: ["sessions", "files", "attention"],
  hidden: [],
  badgeStyle: "count",
  collapsed: {},
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

function SH_SessionsList() {
  var shell = SH_useShell();
  var tap = window.useWorkspaceTap(shell.wid);
  var items = (shell.sessions.data && shell.sessions.data.items) || [];
  var frecency = shell.frecency;
  var ordered = items.slice().sort(function (a, b) {
    var delta = frecency.scoreFor("session:" + b.session_id)
      - frecency.scoreFor("session:" + a.session_id);
    if (delta) return delta;
    return String(b.last_activity_at).localeCompare(String(a.last_activity_at));
  });
  var roots = ordered.filter(function (s) { return !s.parent_session_id; });
  var childrenOf = function (sid) {
    return ordered.filter(function (s) { return s.parent_session_id === sid; });
  };

  function row(session, depth) {
    var live = SH_statusFromTap(tap.events, session.session_id, Date.now());
    return (
      <li key={session.session_id} className="sh-rail-row" data-depth={depth}>
        <button
          type="button"
          data-testid={"rail-session:" + session.session_id}
          onClick={function () {
            shell.frecency.record("session:" + session.session_id);
            shell.openDoc({
              kind: "session", ref: session.session_id,
              title: session.name || session.session_id, preview: true,
            });
          }}
        >
          <span className="sh-rail-title">{session.name || session.session_id}</span>
          <span className="sh-rail-chip" data-status={session.status}>
            {live
              ? SH_statusLine({
                verb: live.verb, object: live.object,
                elapsedSec: Math.round((Date.now() - live.startedMs) / 1000),
              })
              : session.status}
          </span>
        </button>
        <SH_RailVerbs target={{ kind: "session", ref: session.session_id }} />
        {childrenOf(session.session_id).length ? (
          <ul className="sh-rail-children">
            {childrenOf(session.session_id).map(function (child) {
              return row(child, depth + 1);
            })}
          </ul>
        ) : null}
      </li>
    );
  }

  return (
    <ul className="sh-rail-list" data-testid="rail-sessions">
      {roots.map(function (s) { return row(s, 0); })}
      {roots.length ? null : (
        <li className="sh-empty">
          <button type="button" data-testid="rail-sessions-empty"
            onClick={function () { shell.registry.get("session.create").run(); }}>
            Create Session
          </button>
        </li>
      )}
    </ul>
  );
}

function SH_FilesList() {
  var shell = SH_useShell();
  var tree = window.primerApi.useResource(
    SH_api.keys.tree(shell.wid, "."),
    function (signal) { return SH_api.filesTree(shell.wid, ".", signal); },
    { pollMs: 0, deps: [shell.wid] }
  );
  var entries = (tree.data && tree.data.entries) || [];
  return (
    <ul className="sh-rail-list" data-testid="rail-files">
      {entries.map(function (entry) {
        return (
          <li key={entry.path} className="sh-rail-row">
            <button
              type="button"
              data-testid={"rail-file:" + entry.path}
              onClick={function () {
                if (entry.type === "dir") return;
                shell.openDoc({ kind: "file", ref: entry.path, preview: true });
              }}
            >{entry.path}</button>
          </li>
        );
      })}
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

function SH_AttentionList() {
  var shell = SH_useShell();
  var pending = window.primerApi.useResource(
    SH_api.keys.pending(shell.wid),
    function (signal) { return SH_api.pendingYields(shell.wid, signal); },
    { pollMs: 10000, deps: [shell.wid] }
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
  var ambient = visible.filter(function (i) { return i.tier === "ambient"; });
  var digest = visible.filter(function (i) { return i.tier === "digest"; });
  shell.attentionRef.current = { items: visible, triage: triage, commit: commit };

  function triageVerbs(item) {
    return (
      <span className="sh-triage">
        <button type="button" className="sh-verb" data-verb="attention.resolve"
          onClick={function () {
            shell.openDoc({ kind: "session", ref: item.sessionId, preview: true });
          }}>Resolve Attention</button>
        <button type="button" className="sh-verb" data-verb="attention.snooze"
          onClick={function () {
            var next = JSON.parse(JSON.stringify(triage));
            next.snoozedUntil[item.id] = Date.now() + 60 * 60 * 1000;
            commit(next);
          }}>Snooze Attention</button>
        <button type="button" className="sh-verb" data-verb="attention.mute"
          onClick={function () {
            var next = JSON.parse(JSON.stringify(triage));
            next.mutedSessions[item.sessionId] = true;
            commit(next);
          }}>Mute Session</button>
      </span>
    );
  }

  return (
    <section className="sh-rail-list" data-testid="rail-attention">
      <h3>
        Attention
        <span className="sh-rail-badge" data-testid="rail-badge:attention">
          {interrupts.length + ambient.length}
        </span>
      </h3>

      {/* Interrupts, and only interrupts, get an in-shell toast. No
          sound at any tier. */}
      {interrupts.map(function (item) {
        return (
          <div key={item.id} className="sh-attention-toast"
            data-testid={"attention-toast:" + item.toolCallId}>
            <div data-testid={"attention-item:" + item.sessionId}>
              <window.SH_DecisionCard item={item}
                onResolved={function () { pending.refetch(); }} />
              {triageVerbs(item)}
            </div>
          </div>
        );
      })}

      <ul className="sh-attention-ambient">
        {ambient.map(function (item) {
          return (
            <li key={item.id} data-testid={"attention-item:" + item.sessionId}>
              <button type="button"
                onClick={function () {
                  shell.openDoc({
                    kind: "session", ref: item.sessionId, preview: true,
                  });
                }}>{item.title}</button>
              {triageVerbs(item)}
            </li>
          );
        })}
      </ul>

      {digest.length ? (
        <details className="sh-attention-digest">
          <summary>Resolved ({digest.length})</summary>
          <ul>
            {digest.map(function (item) {
              return <li key={item.id}>{item.title}</li>;
            })}
          </ul>
        </details>
      ) : null}
    </section>
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

  var pending = window.primerApi.useResource(
    SH_api.keys.pending(shell.wid),
    function (signal) { return SH_api.pendingYields(shell.wid, signal); },
    { pollMs: 5000, deps: [shell.wid] }
  );
  var attentionCount = ((pending.data && pending.data.items) || []).length;

  function update(next) {
    setPrefs(next);
    SH_saveRailPrefs(username, next);
  }

  var bodies = {
    sessions: <SH_SessionsList />,
    files: <SH_FilesList />,
    attention: <SH_AttentionList />,
  };
  var counts = { sessions: 0, files: 0, attention: attentionCount };

  return (
    <nav className="sh-rail-nav">
      {prefs.order.filter(function (name) {
        return SH_RAIL_LISTS.indexOf(name) >= 0 && prefs.hidden.indexOf(name) < 0;
      }).map(function (name) {
        var collapsed = !!prefs.collapsed[name];
        return (
          <section key={name} className="sh-rail-section" data-collapsed={collapsed}>
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
                <span className="sh-badge" data-testid={"rail-badge:" + name}>
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
window.SH_AttentionList = SH_AttentionList;
