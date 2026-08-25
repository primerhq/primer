/* global React, SH_api, NV_useConsole */
// The workspace event stream (wiring plan P2 T8): right sidebar,
// auto-scrolling tail of the platform event log filtered to the
// selected workspace, plus the per-workspace streaming opt-in config.
//
// Open to every user since P6 T14: reads are redacted server-side
// (primer/events/redaction.py) and non-admins get the
// workspace-scoped safe-kind feed, so no lock lives here. A 403 can
// still reach a restricted-role session; it renders as the denied
// note, not a broken fetch.

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
  // id -> expanded. A row opens to its full envelope (actor, entity,
  // payload) in place.
  var openState = React.useState({});
  var open = openState[0];
  var setOpen = openState[1];
  var bodyRef = React.useRef(null);

  // Streaming config on the workspace row. The row rides the 15s
  // workspace poll, so an optimistic override keeps the checkbox honest
  // between the click and the next poll (BDD pass 2026-08-24: without
  // it the box visually snapped back for up to 15s after toggling).
  var ws = (con.workspaces || []).find(function (w) { return w.id === con.wid; });
  var optimisticState = React.useState(null);
  var optimistic = optimisticState[0];
  var setOptimistic = optimisticState[1];
  var streaming = optimistic != null
    ? optimistic
    : !!(ws && ws.events && ws.events.enabled);

  React.useEffect(function () {
    var stop = false;
    function tick() {
      if (stop) return;
      var after = cursorRef.current;
      // The head probe carries the workspace filter too: a non-admin
      // read without one is refused by the P6 route.
      var call = after == null
        ? SH_api.events({ limit: 1, workspaceId: con.wid }).then(function (head) {
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
              // Read the target SYNCHRONOUSLY: inside the .then the
              // pooled synthetic event is recycled and the toast said
              // "on" while actually disabling (BDD pass 2026-08-24).
              var next = ev.target.checked;
              setOptimistic(next);
              SH_api.setWorkspaceEvents(con.wid, next).then(
                function () { con.toast(next
                  ? "Workspace event streaming on"
                  : "Workspace event streaming off"); },
                function (err) {
                  setOptimistic(null);
                  con.toast("Toggle failed: " + err.message);
                }
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
            This account's role cannot read the event feed.
          </div>
        </div>
      ) : (
        <div className="nv-events-body" ref={bodyRef}>
          {rows.map(function (ev) {
            var isOpen = !!open[ev.id];
            return (
              <React.Fragment key={ev.id}>
                <div className="nv-event-row"
                  data-testid={"nv-event:" + ev.id}
                  onClick={function () {
                    setOpen(function (prev) {
                      var next = Object.assign({}, prev);
                      if (next[ev.id]) delete next[ev.id];
                      else next[ev.id] = true;
                      return next;
                    });
                  }}>
                  <span className="nv-event-caret">
                    {isOpen ? "▾" : "▸"}
                  </span>
                  <span className="nv-event-dot"
                    style={{ background: NV_eventColor(ev.event_type) }} />
                  <span className="nv-event-type">{ev.event_type}</span>
                  <span className="nv-event-detail">
                    {ev.entity_id || ev.session_id || ""}
                  </span>
                </div>
                {isOpen ? (
                  <div className="nv-event-detail-block"
                    data-testid={"nv-event-open:" + ev.id}>
                    <div className="nv-event-kv">
                      <span className="k">at</span>
                      <span className="v">{ev.occurred_at || ""}</span>
                    </div>
                    <div className="nv-event-kv">
                      <span className="k">actor</span>
                      <span className="v">{ev.actor || ""}</span>
                    </div>
                    <div className="nv-event-kv">
                      <span className="k">entity</span>
                      <span className="v">
                        {[ev.entity_kind, ev.entity_id]
                          .filter(Boolean).join(" · ")}
                      </span>
                    </div>
                    {ev.session_id ? (
                      <div className="nv-event-kv">
                        <span className="k">session</span>
                        <span className="v">{ev.session_id}</span>
                      </div>
                    ) : null}
                    {ev.correlation_id ? (
                      <div className="nv-event-kv">
                        <span className="k">correlation</span>
                        <span className="v">{ev.correlation_id}</span>
                      </div>
                    ) : null}
                    <pre className="nv-event-payload">
                      {ev.payload == null
                        ? "(no payload)"
                        : JSON.stringify(ev.payload, null, 2)}
                    </pre>
                  </div>
                ) : null}
              </React.Fragment>
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
