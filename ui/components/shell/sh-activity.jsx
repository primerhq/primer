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
    </div>
  );
}

window.SH_ActivityPanel = SH_ActivityPanel;
