/* global React, SH_api */
// The Activity console (revamp spec section 7): a filterable window
// over the platform event log (GET /v1/events), closing the event-bus
// arc's "console events UI" gap. A debugging window, not a tail -f:
// refresh is a verb, never a timer.

function SH_ActivityPanel() {
  var filterState = React.useState({ eventType: "", entityKind: "", workspaceId: "" });
  var filters = filterState[0];
  var setFilters = filterState[1];
  var rowsState = React.useState([]);
  var rows = rowsState[0];
  var setRows = rowsState[1];
  var maxIdState = React.useState(null);
  var maxId = maxIdState[0];
  var setMaxId = maxIdState[1];
  var loadingState = React.useState(false);
  var loading = loadingState[0];
  var setLoading = loadingState[1];
  var errorState = React.useState(null);
  var error = errorState[0];
  var setError = errorState[1];

  function query(afterId, replace) {
    setLoading(true);
    setError(null);
    SH_api.events({
      afterId: afterId,
      limit: 100,
      eventType: filters.eventType || null,
      entityKind: filters.entityKind || null,
      workspaceId: filters.workspaceId || null,
    }).then(function (out) {
      setLoading(false);
      var items = (out && out.items) || [];
      setMaxId(out && out.max_id);
      setRows(replace ? items : rows.concat(items));
    }, function (err) {
      setLoading(false);
      setError(err && err.message ? err.message : String(err));
    });
  }

  // Open on the newest window: everything after (max_id - 100). Two
  // requests only on first load; Refresh repeats it.
  function refresh() {
    SH_api.events({ limit: 1 }).then(function (out) {
      var head = (out && out.max_id) || 0;
      query(Math.max(0, head - 100), true);
    }, function (err) {
      setError(err && err.message ? err.message : String(err));
    });
  }
  React.useEffect(refresh, []);

  function field(key, placeholder) {
    return (
      <input type="text" className="sh-admin-search"
        data-testid={"activity-filter:" + key}
        placeholder={placeholder}
        value={filters[key]}
        onChange={function (ev) {
          var next = Object.assign({}, filters);
          next[key] = ev.target.value;
          setFilters(next);
        }}
        onKeyDown={function (ev) { if (ev.key === "Enter") refresh(); }} />
    );
  }

  var lastId = rows.length ? rows[rows.length - 1].id : null;

  return (
    <div className="sh-activity" data-testid="shell-activity">
      <div className="sh-activity-bar">
        {field("eventType", "type glob, e.g. session.*")}
        {field("entityKind", "entity kind")}
        {field("workspaceId", "workspace id")}
        <button type="button" className="sh-verb"
          data-testid="activity-refresh"
          onClick={refresh}>Refresh</button>
      </div>
      {error ? (
        <div className="sh-file-conflict">
          <span>{error}</span>
          <button type="button" className="sh-verb" onClick={refresh}>Retry</button>
        </div>
      ) : null}
      <div className="sh-activity-tablewrap">
        <table className="sh-activity-table">
          <thead>
            <tr>
              <th>id</th><th>type</th><th>entity</th><th>actor</th>
              <th>session</th><th>occurred</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(function (ev) {
              return (
                <tr key={ev.id} data-testid={"activity-row:" + ev.id}>
                  <td className="mono">{ev.id}</td>
                  <td className="mono">{ev.event_type}</td>
                  <td className="mono">
                    {ev.entity_kind ? ev.entity_kind + "/" + ev.entity_id : ""}
                  </td>
                  <td>{ev.actor}</td>
                  <td className="mono">{ev.session_id || ""}</td>
                  <td>{ev.occurred_at}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!rows.length && !loading ? (
          <div className="sh-empty">
            <span>No events in this window.</span>
            <button type="button" className="sh-verb" onClick={refresh}>
              Refresh
            </button>
          </div>
        ) : null}
      </div>
      <div className="sh-activity-foot">
        <span className="sh-activity-count">
          {rows.length} events{maxId ? " - head " + maxId : ""}
        </span>
        <button type="button" className="sh-verb"
          data-testid="activity-load-more"
          disabled={loading || lastId == null || (maxId != null && lastId >= maxId)}
          onClick={function () { query(lastId, false); }}>
          Load More
        </button>
      </div>
      <SH_EventSubscriptionsTable />
    </div>
  );
}

// Below the masked log: the durable subscription CONFIG rows (what
// consumes the log, not the log itself). notes section 4 + gap map
// 03-backend-gap-map.md:156 (BACKEND-GAP #12, size S): the backend
// currently has only ONE managed_by tier - always "system" on the 3
// seeded rows, no separate flag for "managed, pause allowed" vs
// "seeded, pause refused too" - and the live pause endpoint has ZERO
// server guard on managed rows. Since every managed_by row IS a
// seeded row today, refusing pause client-side whenever managed_by is
// set matches the notes' "seeded refuse even pause" behaviour exactly
// for the current data; the request is never sent (not sent-then-
// ignored), so this stays honest about what actually happened. When
// the backend grows the second tier this predicate should read that
// field instead of reusing managed_by. Unmanaged rows Pause/Resume for
// real via SH_api.setSubscriptionPaused (the one mutation the server
// allows on managed rows too, just not refused-here ones).
function SH_EventSubscriptionsTable() {
  var rowsState = React.useState([]);
  var rows = rowsState[0];
  var setRows = rowsState[1];
  var loadingState = React.useState(true);
  var loading = loadingState[0];
  var setLoading = loadingState[1];
  var errorState = React.useState(null);
  var error = errorState[0];
  var setError = errorState[1];
  var busyState = React.useState(null);
  var busyId = busyState[0];
  var setBusyId = busyState[1];

  function load() {
    setLoading(true);
    setError(null);
    SH_api.eventSubscriptions().then(function (out) {
      setLoading(false);
      setRows((out && out.items) || []);
    }, function (err) {
      setLoading(false);
      setError(err && err.message ? err.message : String(err));
    });
  }
  React.useEffect(load, []);

  function togglePause(row) {
    if (row.managed_by) {
      window.primerApi.toastPush({
        kind: "warning",
        title: "Managed subscription",
        detail: "\"" + (row.description || row.id)
          + "\" is system-managed and cannot be paused from here.",
      });
      return;
    }
    setBusyId(row.id);
    SH_api.setSubscriptionPaused(row.id, !row.paused).then(function (updated) {
      setBusyId(null);
      setRows(function (prev) {
        return prev.map(function (r) { return r.id === updated.id ? updated : r; });
      });
    }, function (err) {
      setBusyId(null);
      window.primerApi.toastPush({
        kind: "error",
        title: "Pause/resume failed",
        detail: err && err.message ? err.message : String(err),
      });
    });
  }

  return (
    <div className="sh-activity-subs" data-testid="activity-subscriptions">
      <div className="sh-activity-bar">
        <span className="sh-activity-subs-title">Event subscriptions</span>
        <button type="button" className="sh-verb"
          data-testid="activity-subs-refresh"
          onClick={load}>
          Refresh
        </button>
      </div>
      {error ? (
        <div className="sh-file-conflict">
          <span>{error}</span>
          <button type="button" className="sh-verb" onClick={load}>Retry</button>
        </div>
      ) : null}
      <div className="sh-activity-tablewrap">
        <table className="sh-activity-table">
          <thead>
            <tr>
              <th>description</th><th>events</th><th>sink</th>
              <th>state</th><th>managed</th><th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(function (row) {
              var managed = !!row.managed_by;
              var eventTypes = (row.filter && row.filter.event_types) || [];
              return (
                <tr key={row.id} data-testid={"sub-row:" + row.id}>
                  <td>{row.description || row.id}</td>
                  <td className="mono">{eventTypes.join(", ")}</td>
                  <td className="mono">{row.sink && row.sink.kind}</td>
                  <td>
                    <span className={"pill " + (row.paused ? "pill-paused" : "pill-claimed")}>
                      {row.paused ? "paused" : "active"}
                    </span>
                  </td>
                  <td>
                    {managed ? (
                      <span className="pill pill-failed"
                        title="System-managed: edits refused server-side, pause refused here too">
                        managed
                      </span>
                    ) : <span className="muted">—</span>}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <button type="button" className="sh-verb"
                      data-testid={"sub-toggle-pause:" + row.id}
                      disabled={busyId === row.id}
                      onClick={function () { togglePause(row); }}>
                      {busyId === row.id ? "…" : (row.paused ? "Resume" : "Pause")}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!rows.length && !loading ? (
          <div className="sh-empty">
            <span>No event subscriptions.</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

window.SH_ActivityPanel = SH_ActivityPanel;
window.SH_EventSubscriptionsTable = SH_EventSubscriptionsTable;
