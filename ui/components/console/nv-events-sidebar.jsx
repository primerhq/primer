/* global React, SH_api, NV_useConsole */
// The workspace event stream (wiring plan P2 T8): right sidebar,
// auto-scrolling tail of the platform event log filtered to the
// selected workspace, plus the per-workspace streaming opt-in config.
//
// SECURITY: until P6 T14 lands (redaction + user-tier reads) the
// /v1/events window is ADMIN-gated; non-admins see the locked state
// rather than a broken fetch. P6 flips this to the redacted user feed
// and deletes the lock.

var NV_EVENT_COLORS = [
  ["approval.", "var(--attention)"],
  ["workspace.file", "var(--teal)"],
  ["workspace.exec", "var(--teal)"],
  ["session.", "var(--blue)"],
  ["trigger.", "var(--pink)"],
  ["tool.", "var(--amber)"],
  ["llm.", "var(--amber)"],
];

function NV_eventColor(type) {
  for (var i = 0; i < NV_EVENT_COLORS.length; i++) {
    if (String(type).indexOf(NV_EVENT_COLORS[i][0]) === 0) {
      return NV_EVENT_COLORS[i][1];
    }
  }
  return "var(--violet)";
}

function NV_EventsSidebar() {
  var con = NV_useConsole();
  var rowsState = React.useState([]);
  var rows = rowsState[0];
  var setRows = rowsState[1];
  var cursorRef = React.useRef(null);
  var lockedState = React.useState(false);
  var locked = lockedState[0];
  var setLocked = lockedState[1];
  var bodyRef = React.useRef(null);

  // Streaming config on the workspace row.
  var ws = (con.workspaces || []).find(function (w) { return w.id === con.wid; });
  var streaming = !!(ws && ws.events && ws.events.enabled);

  React.useEffect(function () {
    var stop = false;
    function tick() {
      if (stop) return;
      var after = cursorRef.current;
      var call = after == null
        ? SH_api.events({ limit: 1 }).then(function (head) {
          cursorRef.current = Math.max(0, (head.max_id || 0) - 60);
          return SH_api.events({
            afterId: cursorRef.current, limit: 60, workspaceId: con.wid,
          });
        })
        : SH_api.events({ afterId: after, limit: 100, workspaceId: con.wid });
      call.then(function (out) {
        if (stop) return;
        var items = (out && out.items) || [];
        if (items.length) {
          cursorRef.current = items[items.length - 1].id;
          setRows(function (prev) { return prev.concat(items).slice(-300); });
        }
        setTimeout(tick, 3000);
      }, function (err) {
        if (err && (err.status === 403 || err.status === 401)) {
          setLocked(true);
          return;
        }
        if (!stop) setTimeout(tick, 6000);
      });
    }
    tick();
    return function () { stop = true; };
  }, [con.wid]);

  React.useEffect(function () {
    if (bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [rows.length]);

  return (
    <div className="nv-events" data-testid="nv-events">
      <div className="nv-trace-head">
        <span>workspace events</span>
        <span style={{ flex: 1 }} />
        <label className="nv-events-optin" title="Stream file/exec lifecycle
          events from the workspace runtime onto the platform log">
          <input type="checkbox" checked={streaming}
            data-testid="nv-events-optin"
            onChange={function (ev) {
              SH_api.setWorkspaceEvents(con.wid, ev.target.checked).then(
                function () { con.toast(ev.target.checked
                  ? "Workspace event streaming on"
                  : "Workspace event streaming off"); },
                function (err) { con.toast("Toggle failed: " + err.message); }
              );
            }} />
          stream
        </label>
        <button type="button" className="nv-rail-iconbtn"
          data-testid="nv-events-close"
          onClick={function () {
            var verb = con.registry.get("events.toggle");
            if (verb) verb.run();
          }}>×</button>
      </div>
      {locked ? (
        <div className="nv-rail-empty" data-testid="nv-events-locked">
          <div>
            The event window is admin-gated today. It opens to every
            user with the redacted workspace feed (wiring plan P6).
          </div>
        </div>
      ) : (
        <div className="nv-events-body" ref={bodyRef}>
          {rows.map(function (ev) {
            return (
              <div key={ev.id} className="nv-event-row"
                data-testid={"nv-event:" + ev.id}>
                <span className="nv-event-dot"
                  style={{ background: NV_eventColor(ev.event_type) }} />
                <span className="nv-event-type">{ev.event_type}</span>
                <span className="nv-event-detail">
                  {ev.entity_id || ev.session_id || ""}
                </span>
              </div>
            );
          })}
          {!rows.length ? (
            <div className="nv-rail-empty">
              <div>No events in this window yet.</div>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

window.NV_EventsSidebar = NV_EventsSidebar;
