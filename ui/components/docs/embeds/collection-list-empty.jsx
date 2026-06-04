/* global React, Icon */

// collection-list-empty mockup. The /knowledge/collections empty
// state: filter bar + boxed message + Create collection action.

function CollectionListEmptyMockup({
  emptyLine = "No collections yet",
  ctaLabel = "Create collection",
}) {
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 6,
      background: "var(--bg)",
      padding: 16,
      minHeight: 220,
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 8,
        marginBottom: 16,
      }}>
        <input
          className="input"
          placeholder="Filter collections..."
          disabled
          style={{ flex: 1, fontSize: 12, background: "var(--bg-2)" }}
        />
        <button className="btn btn-primary" style={{ fontSize: 12 }}>
          <Icon name="plus" size={11} style={{ marginRight: 4 }} />
          {ctaLabel}
        </button>
      </div>
      <div style={{
        display: "flex", flexDirection: "column", alignItems: "center",
        justifyContent: "center", padding: "40px 0", gap: 8,
        color: "var(--text-3)",
      }}>
        <Icon name="book" size={28} className="muted" />
        <div style={{ fontWeight: 600, fontSize: 14, color: "var(--text-2)" }}>
          {emptyLine}
        </div>
        <div className="muted text-sm">
          A collection holds documents that agents can search.
        </div>
      </div>
    </div>
  );
}

window.CollectionListEmptyMockup = CollectionListEmptyMockup;
