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

// Shared row list: the palette and the composer's "/" both mount this.
function SH_PaletteRows(props) {
  var shell = SH_useShell();
  var active = null;
  var group = shell.docs.groups[shell.docs.activeGroup];
  if (group) {
    for (var i = 0; i < group.tabs.length; i++) {
      if (group.tabs[i].id === group.activeId) active = group.tabs[i];
    }
  }
  var rows = SH_rankVerbs(shell.registry, props.query, {
    docKind: active ? active.kind : null,
    frecency: shell.frecency,
  });
  return (
    <ul className="sh-palette-rows">
      {rows.map(function (verb) {
        var uses = shell.frecency.scoreFor(verb.id);
        return (
          <li key={verb.id}>
            <button
              type="button"
              className="sh-palette-row"
              data-testid="shell-palette-row"
              data-verb={verb.id}
              onClick={function () {
                shell.frecency.record(verb.id);
                shell.frecency.remember(props.query, verb.id);
                verb.run();
                if (props.onRun) props.onRun(verb);
              }}
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
      <button
        type="button"
        className="sh-palette-chip"
        data-testid="shell-palette-chip"
        onClick={function () { setQuery(""); setOpen(true); }}
      >
        <kbd className="sh-kbd">Ctrl+K</kbd> verbs
      </button>
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
window.SH_PaletteRows = SH_PaletteRows;
window.SH_Palette = SH_Palette;
