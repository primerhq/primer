/* global React */

function MobileTabs({ tabs, active, onSelect }) {
  const activeId = active || (tabs[0] && tabs[0].id);
  const activeTab = tabs.find((t) => t.id === activeId) || tabs[0];
  return (
    <div className="mobile-tabs-wrap">
      <div className="mobile-tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={t.id === activeId}
            className={`mobile-tab touch-target ${t.id === activeId ? "active" : ""}`}
            onClick={() => onSelect && onSelect(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="mobile-tab-panel" role="tabpanel">
        {activeTab && activeTab.content}
      </div>
    </div>
  );
}

window.MobileTabs = MobileTabs;
