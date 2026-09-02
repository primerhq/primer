/* global React, SH_api, SH_rankVerbs, NV_useConsole, NV_identity */
// The universal search bar (wiring plan P1 T5): one field over verbs,
// sessions (every workspace), files (current workspace), and platform
// entities. Grouped rows, arrow keys spanning every group, Enter runs.
// The prototype's PALETTE region, inline styles extracted to classes.

var NV_PALETTE_CAP = 5;

function NV_matchRows(items, q, label) {
  var out = [];
  var needle = q.toLowerCase();
  for (var i = 0; i < items.length && out.length < NV_PALETTE_CAP; i++) {
    var it = items[i];
    if (String(it.label).toLowerCase().indexOf(needle) >= 0) out.push(it);
  }
  return out.length ? { label: label, rows: out } : null;
}

function NV_Palette() {
  var con = NV_useConsole();
  var openState = React.useState(false);
  var open = openState[0];
  var setOpen = openState[1];
  var qState = React.useState("");
  var q = qState[0];
  var setQ = qState[1];
  var selState = React.useState(0);
  var sel = selState[0];
  var setSel = selState[1];

  con.paletteRef.current.open = function () {
    setQ("");
    setSel(0);
    setOpen(true);
  };

  // Lazy entity/file resources: fetched once the palette first opens.
  // F9 (2026-08-29 UI review) empty-query recents: "nv-rail-all-sessions"
  // is nv-shell.jsx's OWN cache key for the rail's session list (also
  // reused by NV_TabGroups' resolveSessionMeta) - use-resource.js keys
  // its cache by string across components, so sharing the key here
  // costs one fetch total, not a second "nv-palette-sessions" one for
  // the same underlying data.
  var sessions = window.primerApi.useResource(
    "nv-rail-all-sessions",
    function (signal) { return SH_api.allSessions(signal); },
    { pollMs: 0 }
  );
  // uiv2 Wave 1: the approved recents endpoint (sh-api.jsx's own
  // comment has the full row shape) - a 404 means it is not deployed on
  // this server yet (its own branch may land after this one), so that
  // specific status degrades to null rather than throwing; every other
  // error still surfaces through useResource's normal error state. null
  // data is exactly what the empty-query Sessions group below already
  // treats as "fall back to deriving it from allSessions/con.workspaces".
  var recents = window.primerApi.useResource(
    "nv-palette-recents",
    function (signal) {
      return open
        ? SH_api.recentSessions(signal).catch(function (err) {
            if (err && err.status === 404) return null;
            throw err;
          })
        : Promise.resolve(null);
    },
    { pollMs: 0, deps: [open] }
  );
  var agents = window.primerApi.useResource(
    "nv-palette-agents",
    function (signal) {
      return open ? SH_api.agents(signal) : Promise.resolve({ items: [] });
    },
    { pollMs: 0, deps: [open] }
  );
  var graphs = window.primerApi.useResource(
    "nv-palette-graphs",
    function (signal) {
      return open ? SH_api.graphs(signal) : Promise.resolve({ items: [] });
    },
    { pollMs: 0, deps: [open] }
  );
  // F9: the two missing entity sources - same lazy-list-then-filter
  // shape as agents/graphs above, feeding the same "Platform" group.
  var triggers = window.primerApi.useResource(
    "nv-palette-triggers",
    function (signal) {
      return open ? SH_api.triggers(signal) : Promise.resolve({ items: [] });
    },
    { pollMs: 0, deps: [open] }
  );
  var toolsets = window.primerApi.useResource(
    "nv-palette-toolsets",
    function (signal) {
      return open ? SH_api.toolsets(signal) : Promise.resolve({ items: [] });
    },
    { pollMs: 0, deps: [open] }
  );
  var files = window.primerApi.useResource(
    "nv-palette-files:" + con.wid,
    function (signal) {
      return open && con.wid
        ? SH_api.filesTree(con.wid, ".", signal)
        : Promise.resolve({ items: [] });
    },
    { pollMs: 0, deps: [open, con.wid] }
  );
  // F9: the wiki source - every document path in the system collection,
  // searched the same client-side way sessions/files/entities are.
  var wikiDocs = window.primerApi.useResource(
    "nv-palette-wiki",
    function (signal) {
      return open
        ? SH_api.collectionDocuments("system", signal)
        : Promise.resolve({ documents: [] });
    },
    { pollMs: 0, deps: [open] }
  );

  // Ctrl+K opens; Esc closes. Registered while mounted.
  React.useEffect(function () {
    function onKey(ev) {
      if ((ev.ctrlKey || ev.metaKey) && String(ev.key).toLowerCase() === "k") {
        ev.preventDefault();
        con.paletteRef.current.open();
        return;
      }
      if (ev.key === "Escape" && open) setOpen(false);
    }
    window.addEventListener("keydown", onKey);
    return function () { window.removeEventListener("keydown", onKey); };
  }, [open]);

  if (!open) return null;

  function runAndClose(fn) {
    return function () { setOpen(false); fn(); };
  }

  // --- Build the grouped result list -------------------------------------
  var groups = [];
  var rankedVerbs = SH_rankVerbs(con.registry, q, {
    docKind: con.doc ? con.doc.kind : null,
    frecency: con.frecency,
  });
  // Notes 1.3: a focused session tab prepends its session verbs
  // (Interrupt/Park/End/Rename/Split Right/Compact/Rewind) ahead of the
  // rest. Ranking already context-gates on docKind (shell-verbs.js); this
  // reorders within that gated set rather than re-ranking by weight, so a
  // verb declaring contexts: ["session"] always leads regardless of score.
  if (con.doc && con.doc.kind === "session") {
    var sessionVerbs = [];
    var otherVerbs = [];
    rankedVerbs.forEach(function (verb) {
      if (verb.contexts && verb.contexts.indexOf("session") >= 0) {
        sessionVerbs.push(verb);
      } else {
        otherVerbs.push(verb);
      }
    });
    rankedVerbs = sessionVerbs.concat(otherVerbs);
  }
  // uiv2 Wave 1: "Open Palette" inside the already-open palette is a
  // self-referential registry entry the mockup never surfaces in this
  // state - filtered before the top-8 slice so it never displaces a
  // real result either.
  var rankedVerbsVisible = rankedVerbs.filter(function (verb) {
    return verb.id !== "palette.open";
  });
  var verbRows = rankedVerbsVisible.slice(0, 8).map(function (verb) {
    return {
      key: "v:" + verb.id,
      label: verb.label,
      chord: verb.chord || null,
      dot: true,
      // FREQUENT (mockup): at minimum render what in-session frecency
      // already tracks (record()/remember() below feed it) rather than
      // overpromise a footer that says "frecency-ranked" with nothing to
      // show for it - per-user persistence across sessions is the
      // backend half (c-2), unimplemented here.
      tag: con.frecency && con.frecency.scoreFor(verb.id) > 0 ? "frequent" : null,
      run: runAndClose(function () {
        if (con.frecency) {
          con.frecency.record(verb.id);
          con.frecency.remember(q, verb.id);
        }
        verb.run();
      }),
    };
  });
  if (verbRows.length) groups.push({ label: "Verbs", rows: verbRows });

  // F3/F2 (2026-08-29 UI review, unchanged): rail rows promote on open
  // AND route a cross-workspace target through the combined
  // con.openInWorkspace nav, one history entry either way. Shared by
  // both the searched "Sessions" group below and the empty-query
  // "Sessions" group (F9) so the two never drift.
  //
  // uiv2 Wave 1: the trailing "SESSION" text badge is gone - a session
  // row now leads with the same colored agent glyph the rail/tab-groups
  // already use (NV_identity(s.binding)), matching the mockup, and
  // reserving the trailing tag slot for the Platform group where
  // multiple entity kinds share one heading. The sub label ("agent @
  // workspace") is a client-computable disambiguator for the live
  // triplicate-"main"-rows bug (workspace/agent already ride on every
  // session row - s.workspace_id, s.binding) - it does not wait on the
  // backend dedupe/qualifier batch (01a06431 item 1), which fixes the
  // actual data-scoping bug (stale/cross-scope rows) a display label
  // cannot: two rows that are genuinely different sessions both named
  // "main" stay two rows, just no longer indistinguishable ones.
  function sessionRow(s) {
    var ws = (con.workspaces || []).filter(function (w) { return w.id === s.workspace_id; })[0];
    var ident = NV_identity(s.binding);
    var agentLabel = s.binding && s.binding.kind === "graph"
      ? "graph"
      : (s.binding && s.binding.agent_id) || "operator";
    return {
      key: "s:" + s.session_id,
      label: s.name || s.session_id,
      sub: agentLabel + " @ " + ((ws && (ws.name || ws.id)) || s.workspace_id || "?"),
      glyph: ident,
      tag: null,
      run: runAndClose(function () {
        con.goView("studio");
        if (s.workspace_id && s.workspace_id !== con.wid && con.openInWorkspace) {
          con.openInWorkspace(s.workspace_id, { kind: "session", ref: s.session_id });
        } else {
          con.setDoc({ kind: "session", ref: s.session_id });
        }
        if (con.promoteDoc) con.promoteDoc("session:" + s.session_id);
      }),
    };
  }

  // uiv2 Wave 1: the recents-endpoint row shape, when it's actually
  // deployed (see the `recents` resource above) - pre-composed
  // workspace_name/agent fields replace the client-side lookups
  // sessionRow() above does, so this only feeds the empty-query group,
  // never the searched one (which still needs the FULL session list to
  // search over, not a capped "recent 20"). Field names beyond
  // workspace_name are read defensively (a few plausible candidates,
  // then a plain fallback) since this endpoint's own branch had not
  // landed as of this wave - if the real names differ once it does,
  // this degrades to the same "operator" default sessionRow() uses
  // rather than showing an unresolved value.
  function sessionRowFromRecent(r) {
    var agentLabel = r.graph_ref ? "graph"
      : r.agent_display_name || r.agent_name || r.agent_id || "operator";
    var syntheticBinding = r.graph_ref ? { kind: "graph" }
      : r.agent_id ? { agent_id: r.agent_id } : null;
    return {
      key: "s:" + r.session_id,
      label: r.name || r.session_id,
      sub: agentLabel + " @ " + (r.workspace_name || r.workspace_id || "?"),
      glyph: NV_identity(syntheticBinding),
      tag: null,
      run: runAndClose(function () {
        con.goView("studio");
        if (r.workspace_id && r.workspace_id !== con.wid && con.openInWorkspace) {
          con.openInWorkspace(r.workspace_id, { kind: "session", ref: r.session_id });
        } else {
          con.setDoc({ kind: "session", ref: r.session_id });
        }
        if (con.promoteDoc) con.promoteDoc("session:" + r.session_id);
      }),
    };
  }

  // Shared by both the searched "Files" group below and the empty-query
  // recent-files group (F9), same reason sessionRow is shared above.
  // uiv2 Wave 1: folder/file text badge dropped for the same reason -
  // the group heading already says "Files"; a leading dot (dot: true)
  // takes its place per the mockup's row iconography.
  function fileRow(f) {
    return {
      key: "f:" + f.path,
      label: f.path,
      tag: null,
      dot: true,
      run: runAndClose(function () {
        con.goView("studio");
        if (!f.is_dir) con.setDoc({ kind: "file", ref: f.path });
      }),
    };
  }

  if (q.trim()) {
    var sess = NV_matchRows(
      ((sessions.data && sessions.data.items) || []).map(sessionRow),
      q, "Sessions");
    if (sess) groups.push(sess);

    var fils = NV_matchRows(
      ((files.data && files.data.items) || []).map(fileRow), q, "Files");
    if (fils) groups.push(fils);

    // F9: the wiki source - every document path in the system
    // collection. "doc: <slug>" opens a wiki tab (con.setDoc kind
    // "wiki"), same as a session/file row opens its own doc kind -
    // NV_WikiDoc's own slug format is "{collection_id}/{path}", split
    // on the first "/" (nv-file-docs.jsx), so the ref must match that
    // exactly.
    var wiki = NV_matchRows(
      ((wikiDocs.data && wikiDocs.data.documents) || []).map(function (d) {
        return {
          key: "w:" + d.path,
          label: d.path,
          tag: "wiki",
          run: runAndClose(function () {
            con.goView("studio");
            con.setDoc({ kind: "wiki", ref: "system/" + d.path });
          }),
        };
      }), q, "Wiki");
    if (wiki) groups.push(wiki);

    var ents = [];
    ((agents.data && agents.data.items) || []).forEach(function (a) {
      ents.push({
        key: "a:" + a.id, label: a.id, tag: "agent", nav: "agents",
      });
    });
    ((graphs.data && graphs.data.items) || []).forEach(function (g) {
      ents.push({
        key: "g:" + g.id, label: g.id, tag: "graph", nav: "graphs",
      });
    });
    // F9: the two missing entity sources, same platform overlay pattern
    // as agents/graphs above (Platform grammar names them "triggers"/
    // "toolsets" - ui/foundation/shell-url.js).
    ((triggers.data && triggers.data.items) || []).forEach(function (t) {
      ents.push({
        key: "tr:" + t.id, label: t.id, tag: "trigger", nav: "triggers",
      });
    });
    ((toolsets.data && toolsets.data.items) || []).forEach(function (t) {
      ents.push({
        key: "ts:" + t.id, label: t.id, tag: "toolset", nav: "toolsets",
      });
    });
    ents = ents.map(function (e) {
      return {
        key: e.key,
        label: e.label,
        tag: e.tag,
        // The RECORD, not just its list page: session rows open the
        // session and file rows open the file, so an entity row
        // landing on a bare list was the one inconsistent pick (BDD
        // round 2). Same page+overlay pair a platform card click uses.
        run: runAndClose(function () {
          con.goView("platform", e.nav);
          con.openOverlay(e.nav, null, e.label);
        }),
      };
    });
    // Notes 1.3 group order: Verbs, Sessions, Files, Wiki, Platform.
    var entGroup = NV_matchRows(ents, q, "Platform");
    if (entGroup) groups.push(entGroup);
  } else {
    // F9: an empty query showed verbs only - a few recent sessions (and
    // files, since that resource is already sitting in cache the moment
    // the workspace is open - no new fetch) make the palette useful the
    // instant it opens, not just once you start typing (design: "a few
    // recent sessions + files under the verbs"). last_activity_at is
    // stamped onto every row by SH_api.allSessions' own normalisation,
    // independent of the list's server-side order.
    // uiv2 Wave 1: prefer the recents endpoint's own pre-composed rows
    // (already ordered last_activity_at desc, live-workspaces only -
    // no client-side sort/dedupe needed) when it is actually deployed;
    // recents.data is null on a 404 (see the resource above), so this
    // degrades to the exact same client-derived computation the wave
    // shipped with before that endpoint existed.
    var recent = recents.data && recents.data.items
      ? recents.data.items.slice(0, NV_PALETTE_CAP).map(sessionRowFromRecent)
      : ((sessions.data && sessions.data.items) || [])
        .slice()
        .sort(function (a, b) {
          return String(b.last_activity_at || "").localeCompare(
            String(a.last_activity_at || ""));
        })
        .slice(0, NV_PALETTE_CAP)
        .map(sessionRow);
    // uiv2 Wave 1: the empty-query group label matches the mockup's own
    // taxonomy now (Verbs, Sessions, Files) - it already only ever held
    // sessions, the old label just said otherwise.
    if (recent.length) groups.push({ label: "Sessions", rows: recent });

    // files/tree stamps a real mtime per entry (workspaces.py file_tree);
    // only leaf files carry an "open this" action, so directories (no
    // recency signal worth surfacing here) are left out.
    //
    // KNOWN GAP, flagged not built: /files/tree is a documented
    // one-level listing (recursive=False, workspaces.py file_tree) - a
    // workspace whose root holds only folders (this dogfood workspace's
    // does: .tmp, artifacts) legitimately produces zero leaf files here,
    // same as the mockup's own src/api.ts example would if its root
    // were listed one level deep. Matching the mockup for a real nested
    // repo needs either a capped recursive walk (N extra calls, no
    // natural depth bound) or a backend "recent files" endpoint -
    // either is out of this UI-only wave's scope, so this stays a
    // one-level fetch rather than growing ad hoc recursion here.
    var recentFiles = ((files.data && files.data.items) || [])
      .filter(function (f) { return !f.is_dir; })
      .slice()
      .sort(function (a, b) { return (b.mtime || 0) - (a.mtime || 0); })
      .slice(0, 3)
      .map(fileRow);
    if (recentFiles.length) groups.push({ label: "Files", rows: recentFiles });
  }

  var flat = [];
  groups.forEach(function (g) {
    g.rows.forEach(function (r) { flat.push(r); });
  });
  var selIdx = flat.length ? Math.min(sel, flat.length - 1) : 0;

  function onInputKey(ev) {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      setSel(function (v) { return flat.length ? (v + 1) % flat.length : 0; });
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      setSel(function (v) {
        return flat.length ? (v - 1 + flat.length) % flat.length : 0;
      });
    } else if (ev.key === "Enter" && flat.length) {
      ev.preventDefault();
      flat[selIdx].run();
    }
  }

  var flatPos = 0;
  return (
    <div className="nv-scrim" data-testid="nv-palette-scrim"
      onClick={function () { setOpen(false); }}>
      <div className="nv-palette" data-testid="nv-palette"
        role="dialog" aria-label="Command palette"
        onClick={function (ev) { ev.stopPropagation(); }}>
        <div className="nv-palette-head">
          <svg width="14" height="14" viewBox="0 0 12 12" fill="none"
            stroke="var(--text-3)" strokeWidth="1.4">
            <circle cx="5.2" cy="5.2" r="3.6" />
            <path d="M8 8 11 11" />
          </svg>
          <input className="nv-palette-input" data-testid="nv-palette-input"
            autoFocus value={q}
            placeholder="Type a verb, a session, a file, an entity…"
            onChange={function (ev) { setQ(ev.target.value); setSel(0); }}
            onKeyDown={onInputKey} />
          <kbd className="nv-kbd">esc</kbd>
        </div>
        <div className="nv-palette-body">
          {!flat.length ? (
            <div className="nv-palette-empty">
              Nothing matches {JSON.stringify(q)}.
            </div>
          ) : null}
          {groups.map(function (g) {
            return (
              <div key={g.label}>
                <div className="nv-palette-group">{g.label}</div>
                {g.rows.map(function (r) {
                  var idx = flatPos;
                  flatPos += 1;
                  return (
                    // Round-two gate flake (palette2, suite-context-dependent):
                    // every row shares the SAME testid (an established,
                    // still-relied-on convention - run_verb() in
                    // _shell_helpers.py fills the input then clicks
                    // get_by_test_id("nv-palette-row").first on the narrowed
                    // set) so it cannot be repointed to a per-row value
                    // without breaking every other caller of that generic
                    // "any row" selector. data-row-key adds a second, exact,
                    // content-independent handle - r.key is already unique
                    // per row (the "a:"/"s:"/"f:"/etc builders above) - for
                    // scenarios that need ONE specific row, not "the first
                    // match".
                    <button type="button" key={r.key}
                      className="nv-palette-row"
                      data-testid="nv-palette-row"
                      data-row-key={r.key}
                      data-active={idx === selIdx ? "true" : "false"}
                      onClick={r.run}>
                      {r.glyph ? (
                        <svg width="11" height="11" viewBox="0 0 12 12"
                          style={{ flexShrink: 0, color: r.glyph.color }}>
                          <path d={r.glyph.d} fill="currentColor" />
                        </svg>
                      ) : r.dot ? (
                        <span className="nv-palette-dot" />
                      ) : null}
                      <span className="nv-palette-main">
                        <span className="nv-palette-label">{r.label}</span>
                        {r.sub ? <span className="nv-palette-sub">{r.sub}</span> : null}
                      </span>
                      {r.tag ? (
                        <span className="nv-palette-tag">{r.tag}</span>
                      ) : null}
                      <span style={{ flex: 1 }} />
                      {r.chord ? <kbd className="nv-kbd">{r.chord}</kbd> : null}
                    </button>
                  );
                })}
              </div>
            );
          })}
        </div>
        <div className="nv-palette-foot">
          ↑↓ select · ↵ run · frecency-ranked · one verb registry —
          everything here is also a button somewhere
        </div>
      </div>
    </div>
  );
}

window.NV_Palette = NV_Palette;
