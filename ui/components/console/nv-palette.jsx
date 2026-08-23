/* global React, SH_api, SH_rankVerbs, SH_buildUrl, NV_useConsole */
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
  var sessions = window.primerApi.useResource(
    "nv-palette-sessions",
    function (signal) {
      return open ? SH_api.allSessions(signal) : Promise.resolve({ items: [] });
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
  var files = window.primerApi.useResource(
    "nv-palette-files:" + con.wid,
    function (signal) {
      return open && con.wid
        ? SH_api.filesTree(con.wid, ".", signal)
        : Promise.resolve({ items: [] });
    },
    { pollMs: 0, deps: [open, con.wid] }
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
  var verbRows = SH_rankVerbs(con.registry, q, {
    docKind: con.doc ? con.doc.kind : null,
    frecency: con.frecency,
  }).slice(0, 8).map(function (verb) {
    return {
      key: "v:" + verb.id,
      label: verb.label,
      chord: verb.chord || null,
      tag: null,
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

  if (q.trim()) {
    var sess = NV_matchRows(
      ((sessions.data && sessions.data.items) || []).map(function (s) {
        return {
          key: "s:" + s.session_id,
          label: s.name || s.session_id,
          tag: "session",
          run: runAndClose(function () {
            if (s.workspace_id && s.workspace_id !== con.wid) {
              window.location.hash = SH_buildUrl({
                wid: s.workspace_id,
                doc: { kind: "session", ref: s.session_id },
              });
            } else {
              con.goView("studio");
              con.setDoc({ kind: "session", ref: s.session_id });
            }
          }),
        };
      }), q, "Sessions");
    if (sess) groups.push(sess);

    var fils = NV_matchRows(
      ((files.data && files.data.items) || []).map(function (f) {
        return {
          key: "f:" + f.path,
          label: f.path,
          tag: f.is_dir ? "folder" : "file",
          run: runAndClose(function () {
            con.goView("studio");
            if (!f.is_dir) con.setDoc({ kind: "file", ref: f.path });
          }),
        };
      }), q, "Files");
    if (fils) groups.push(fils);

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
    ents = ents.map(function (e) {
      return {
        key: e.key,
        label: e.label,
        tag: e.tag,
        run: runAndClose(function () { con.goView("platform", e.nav); }),
      };
    });
    var entGroup = NV_matchRows(ents, q, "Entities");
    if (entGroup) groups.push(entGroup);
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
                    <button type="button" key={r.key}
                      className="nv-palette-row"
                      data-testid="nv-palette-row"
                      data-active={idx === selIdx ? "true" : "false"}
                      onClick={r.run}>
                      <span className="nv-palette-label">{r.label}</span>
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
