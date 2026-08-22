/* global React, SH_useShell, SH_rankVerbs */
// Fresh shell palette (S8 spec section 8, "Palette").
//
// The palette is the router, but never the only door: rows are rendered
// from the same registry the rail, tab menus, overlay buttons and
// attention items render from. The composer's "/" affordance mounts
// SH_PaletteRows directly, so there is one ranking, not two.

var SH_CHORDS = {
  "Ctrl+k": "palette.open",
  "Ctrl+p": "doc.openQuick",
  "Ctrl+Shift+p": "palette.open",
  "Ctrl+\\": "layout.splitRight",
  "Ctrl+Tab": "doc.cycleMru",
  "Ctrl+w": "doc.close",
  "Ctrl+Shift+a": "attention.next",
};

function SH_chordFor(event) {
  if (!event) return null;
  var parts = [];
  if (event.ctrlKey || event.metaKey) parts.push("Ctrl");
  if (event.shiftKey) parts.push("Shift");
  var key = event.key === " " ? "Space" : event.key;
  if (!key || key === "Control" || key === "Shift" || key === "Meta") return null;
  parts.push(key.length === 1 ? key.toLowerCase() : key);
  var chord = parts.join("+");
  return SH_CHORDS[chord] ? chord : null;
}

// Search-first (revamp section 8): sessions match beside verbs, so the
// palette is the one field that reaches both actions and places.
function SH_matchSessions(sessions, query) {
  var q = String(query || "").toLowerCase().trim();
  if (!q) return [];
  var items = (sessions && sessions.data && sessions.data.items) || [];
  var out = [];
  for (var i = 0; i < items.length && out.length < 5; i++) {
    var s = items[i];
    var label = s.name || s.session_id;
    if (String(label).toLowerCase().indexOf(q) >= 0
      || String(s.session_id).toLowerCase().indexOf(q) >= 0) {
      out.push({ id: s.session_id, label: label });
    }
  }
  return out;
}

// Shared row list: the palette and the composer's "/" both mount this.
// Arrow keys move a selection over the COMBINED list (sessions first);
// Enter runs it. Click always works too (dual-render in miniature).
function SH_PaletteRows(props) {
  var shell = SH_useShell();
  var selState = React.useState(0);
  var sel = selState[0];
  var setSel = selState[1];
  var active = null;
  var group = shell.docs.groups[shell.docs.activeGroup];
  if (group) {
    for (var i = 0; i < group.tabs.length; i++) {
      if (group.tabs[i].id === group.activeId) active = group.tabs[i];
    }
  }
  var sessionRows = SH_matchSessions(shell.sessions, props.query);
  var verbRows = SH_rankVerbs(shell.registry, props.query, {
    docKind: active ? active.kind : null,
    frecency: shell.frecency,
  });
  var total = sessionRows.length + verbRows.length;

  function runSession(row) {
    shell.openDoc({
      kind: "session", ref: row.id, title: row.label, preview: true,
    });
    if (props.onRun) props.onRun(null);
  }
  function runVerb(verb) {
    shell.frecency.record(verb.id);
    shell.frecency.remember(props.query, verb.id);
    verb.run();
    if (props.onRun) props.onRun(verb);
  }

  React.useEffect(function () { setSel(0); }, [props.query]);
  React.useEffect(function () {
    function onKey(ev) {
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        setSel(function (v) { return total ? (v + 1) % total : 0; });
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        setSel(function (v) { return total ? (v - 1 + total) % total : 0; });
      } else if (ev.key === "Enter") {
        if (!total) return;
        ev.preventDefault();
        var idx = Math.min(sel, total - 1);
        if (idx < sessionRows.length) runSession(sessionRows[idx]);
        else runVerb(verbRows[idx - sessionRows.length]);
      }
    }
    window.addEventListener("keydown", onKey);
    return function () { window.removeEventListener("keydown", onKey); };
  });

  return (
    <ul className="sh-palette-rows">
      {sessionRows.map(function (row, i) {
        return (
          <li key={"s:" + row.id}>
            <button
              type="button"
              className="sh-palette-row"
              data-testid="shell-palette-session-row"
              data-active={i === sel ? "true" : "false"}
              onClick={function () { runSession(row); }}
            >
              <span className="sh-chip">session</span>
              <span className="sh-palette-label">{row.label}</span>
            </button>
          </li>
        );
      })}
      {verbRows.map(function (verb, i) {
        var uses = shell.frecency.scoreFor(verb.id);
        var idx = sessionRows.length + i;
        return (
          <li key={verb.id}>
            <button
              type="button"
              className="sh-palette-row"
              data-testid="shell-palette-row"
              data-verb={verb.id}
              data-active={idx === sel ? "true" : "false"}
              onClick={function () { runVerb(verb); }}
            >
              <span className="sh-palette-label">{verb.label}</span>
              {verb.chord ? <kbd className="sh-kbd">{verb.chord}</kbd> : null}
              {!verb.chord && uses >= 3 ? (
                // graduation hint: repeated use earns a chord suggestion
                <span className="sh-hint" data-testid="shell-palette-hint">
                  bind a chord
                </span>
              ) : null}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function SH_Palette() {
  var shell = SH_useShell();
  var openState = React.useState(false);
  var open = openState[0];
  var setOpen = openState[1];
  var queryState = React.useState("");
  var query = queryState[0];
  var setQuery = queryState[1];

  // Publish the opener so the palette.open verb and the chord run the
  // same code path.
  shell.paletteRef.current.open = function () { setQuery(""); setOpen(true); };

  React.useEffect(function () {
    function onKey(ev) {
      var chord = SH_chordFor(ev);
      if (!chord) {
        if (ev.key === "Escape" && open) setOpen(false);
        return;
      }
      var verbId = SH_CHORDS[chord];
      if (verbId === "palette.open") {
        ev.preventDefault();
        setQuery("");
        setOpen(true);
        return;
      }
      var verb = shell.registry.get(verbId);
      if (verb) {
        ev.preventDefault();
        verb.run();
      }
    }
    window.addEventListener("keydown", onKey);
    return function () { window.removeEventListener("keydown", onKey); };
  }, [open, shell.registry]);

  return (
    <React.Fragment>
      {/* The floating Ctrl+K chip retired 2026-08-23: the topbar search
          field is the persistent advertisement now (same kbd, same
          opener), and the chip collided with the composer's Send. */}
      {open ? (
        <div className="sh-palette" data-testid="shell-palette" role="dialog">
          <input
            className="sh-palette-input"
            data-testid="shell-palette-input"
            autoFocus
            value={query}
            placeholder="Type a verb"
            onChange={function (ev) { setQuery(ev.target.value); }}
          />
          <SH_PaletteRows query={query} onRun={function () { setOpen(false); }} />
        </div>
      ) : null}
    </React.Fragment>
  );
}

window.SH_CHORDS = SH_CHORDS;
window.SH_chordFor = SH_chordFor;
window.SH_matchSessions = SH_matchSessions;
window.SH_PaletteRows = SH_PaletteRows;
window.SH_Palette = SH_Palette;
