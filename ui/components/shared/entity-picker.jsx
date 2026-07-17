/* global React, Icon */

// EntityPicker - reusable searchable + paginated agent/graph picker.
//
// Replaces the old "GET /<plural>?limit=200 dumped into a <select>" pattern:
// results are fetched server-side via usePagedList against
// `GET <path>?q=&limit=&offset=` (the case-insensitive substring search over
// each entity's id + description shipped alongside this component), with the
// raw search text DEBOUNCED before it becomes the `q` param so typing does
// not fire a request per keystroke. This means the picker searches ALL
// matching rows, not just whatever page happened to be loaded.
//
// Registered on window.primerApi the same way shared/pager.jsx registers
// usePagedList/Pager, so any component can render
// `window.primerApi.EntityPicker` (or the bare global `EntityPicker`).
//
// Props:
//   path         list endpoint, e.g. "/agents" or "/graphs"
//   value        currently selected id, or "" for none
//   onChange(id) called with the picked id, or "" when cleared
//   placeholder  search input placeholder (optional)
//   label        label rendered above the search box (optional)
//   testid       data-testid prefix (optional; derived from `path` otherwise)
//
// Deliberately does NOT do a separate GET for the selected item's label -
// showing the id is sufficient, and callers that need the full selected
// object (e.g. a graph's Begin.input_schema) already have it from their own
// list/detail fetch.

(function () {
  const { useState, useEffect } = window.React;

  const DEBOUNCE_MS = 220;

  function EntityPicker(props) {
    const path = props.path;
    const value = props.value || "";
    const onChange = props.onChange || function () {};
    const placeholder = props.placeholder || "Search…";
    const label = props.label || null;
    const testid = props.testid || "entity-picker" + path.replace(/[^a-zA-Z0-9]+/g, "-");

    const api = window.primerApi || {};
    const usePagedList = api.usePagedList;
    const Pager = api.Pager;

    // Raw input text vs. the debounced `q` param sent to the server.
    const [text, setText] = useState("");
    const [q, setQ] = useState("");

    useEffect(() => {
      const t = setTimeout(function () { setQ(text.trim()); }, DEBOUNCE_MS);
      return function () { clearTimeout(t); };
    }, [text]);

    const list = usePagedList({
      key: "picker:" + path,
      path: path,
      pageSize: 20,
      params: q ? { q: q } : null,
      resetKey: q,
    });

    const items = list.items || [];
    const noun = path.replace(/^\//, "") || "items";

    return (
      <div className="col" data-testid={testid} style={{ gap: 6 }}>
        {label && <label className="field-label">{label}</label>}
        {value && (
          <div
            style={{
              display: "flex", alignItems: "center", gap: 6, fontSize: 12,
              background: "var(--bg-2)", border: "1px solid var(--border)",
              borderRadius: 6, padding: "5px 8px",
            }}
          >
            <span className="muted">Selected:</span>
            <span
              className="mono"
              style={{ fontWeight: 600, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
            >
              {value}
            </span>
            <button
              type="button"
              data-testid={testid + "-clear"}
              title="Clear selection"
              aria-label="Clear selection"
              onClick={function () { onChange(""); }}
              style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-3)", display: "flex", alignItems: "center", padding: 0 }}
            >
              <Icon name="x" size={12} />
            </button>
          </div>
        )}
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            data-testid={testid + "-search"}
            placeholder={placeholder}
            value={text}
            onChange={function (e) { setText(e.target.value); }}
            style={{ width: "100%" }}
          />
        </div>
        <div
          data-testid={testid + "-results"}
          role="listbox"
          style={{ maxHeight: 220, overflowY: "auto", border: "1px solid var(--border)", borderRadius: 6 }}
        >
          {list.loading && items.length === 0 ? (
            <div className="muted text-sm" style={{ padding: 10 }}>Loading…</div>
          ) : list.error && items.length === 0 ? (
            <div className="field-help warn" style={{ padding: 10, margin: 0 }}>
              <Icon name="alert" size={11} />{" "}
              {(list.error && (list.error.detail || list.error.message)) || ("Couldn't load " + noun)}
            </div>
          ) : items.length === 0 ? (
            <div className="muted text-sm" style={{ padding: 10 }}>
              No {noun} match {q ? '"' + q + '"' : "your search"}.
            </div>
          ) : (
            items.map(function (item) {
              const selected = item.id === value;
              return (
                <div
                  key={item.id}
                  role="option"
                  aria-selected={selected}
                  tabIndex={0}
                  data-testid={testid + "-row"}
                  onClick={function () { onChange(item.id); }}
                  onKeyDown={function (e) {
                    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onChange(item.id); }
                  }}
                  style={{
                    display: "flex", flexDirection: "column", gap: 2,
                    padding: "6px 8px", cursor: "pointer",
                    background: selected ? "var(--accent-dim)" : "transparent",
                    borderLeft: selected ? "2px solid var(--accent)" : "2px solid transparent",
                  }}
                >
                  <span className="mono" style={{ fontWeight: 600, fontSize: 12.5 }}>{item.id}</span>
                  {item.description && (
                    <span className="muted text-sm" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {item.description}
                    </span>
                  )}
                </div>
              );
            })
          )}
        </div>
        <Pager pager={list} label={noun} />
      </div>
    );
  }

  window.EntityPicker = EntityPicker;
  const ns = (window.primerApi = window.primerApi || {});
  ns.EntityPicker = EntityPicker;
})();
