/* global React, NV_useConsole, NV_identity */
// The center doc host (wiring plan P2 T7): a single tab group with
// VS Code semantics - single-click preview (italic), double-click or
// edit promotes, x closes, the URL names the active doc. Kinds:
// session / file / diff / wiki. Prototype CENTER region.

function NV_docId(doc) {
  return doc ? doc.kind + ":" + doc.ref : null;
}

function NV_DocHost() {
  var con = NV_useConsole();
  var tabsState = React.useState([]);
  var tabs = tabsState[0];
  var setTabs = tabsState[1];

  var activeId = NV_docId(con.doc);

  // The URL names the active doc; the host materializes a tab for it.
  React.useEffect(function () {
    if (!con.doc) return;
    setTabs(function (prev) {
      var id = NV_docId(con.doc);
      var found = prev.find(function (t) { return t.id === id; });
      if (found) return prev;
      // Preview reuse: a new single-click doc replaces the previous
      // preview tab rather than accreting.
      var next = prev.filter(function (t) { return !t.preview; });
      var replaced = next.length !== prev.length;
      next.push({
        id: id, kind: con.doc.kind, ref: con.doc.ref,
        preview: true,
      });
      return replaced || next.length !== prev.length ? next : prev;
    });
  }, [activeId]);

  con.promoteDoc = function (id) {
    setTabs(function (prev) {
      return prev.map(function (t) {
        return t.id === id ? Object.assign({}, t, { preview: false }) : t;
      });
    });
  };

  function select(tab) {
    con.setDoc({ kind: tab.kind, ref: tab.ref });
  }

  function close(tab) {
    setTabs(function (prev) {
      var next = prev.filter(function (t) { return t.id !== tab.id; });
      if (tab.id === activeId) {
        con.setDoc(next.length
          ? { kind: next[next.length - 1].kind, ref: next[next.length - 1].ref }
          : null);
      }
      return next;
    });
  }

  function label(tab) {
    if (tab.kind === "session") return tab.ref;
    if (tab.kind === "diff") return String(tab.ref).slice(0, 7);
    return String(tab.ref).split("/").pop();
  }

  var active = tabs.find(function (t) { return t.id === activeId; });

  return (
    <div className="nv-dochost" data-testid="nv-dochost">
      {tabs.length ? (
        <div className="nv-tabbar" data-testid="nv-tabbar">
          {tabs.map(function (tab) {
            var isActive = tab.id === activeId;
            return (
              <div key={tab.id} className="nv-tab"
                data-active={isActive ? "true" : "false"}
                data-preview={tab.preview ? "true" : "false"}
                data-testid={"nv-tab:" + tab.id}
                onClick={function () { select(tab); }}
                onDoubleClick={function () { con.promoteDoc(tab.id); }}>
                {isActive ? <span className="nv-tab-edge" /> : null}
                <span className="nv-tab-label">{label(tab)}</span>
                <button type="button" className="nv-tab-close"
                  data-testid={"nv-tab-close:" + tab.id}
                  onClick={function (ev) {
                    ev.stopPropagation();
                    close(tab);
                  }}>×</button>
              </div>
            );
          })}
        </div>
      ) : null}
      <div className="nv-doc-body">
        {!active ? (
          <div className="nv-center-empty" data-testid="nv-center-empty">
            <svg width="34" height="34" viewBox="0 0 24 24"
              style={{ marginBottom: 10, color: "var(--text-4)" }}>
              <polygon points="12,3 21,12 12,21 3,12" fill="none"
                stroke="currentColor" strokeWidth="1.2" />
              <polygon points="12,12 16.5,16.5 12,21 7.5,16.5"
                fill="var(--brand-green)" />
            </svg>
            <div>Nothing open. Pick a session, or start one.</div>
            <div className="nv-center-empty-actions">
              <button type="button" className="nv-btn-primary"
                data-testid="nv-empty-new-session"
                onClick={function () {
                  var verb = con.registry.get("session.create");
                  if (verb) verb.run();
                }}>New session</button>
              <button type="button" className="nv-btn-secondary"
                onClick={function () {
                  var verb = con.registry.get("palette.open");
                  if (verb) verb.run();
                }}>Ctrl+K commands</button>
            </div>
          </div>
        ) : null}
        {active && active.kind === "session"
          && typeof window.NV_SessionDoc === "function"
          ? <window.NV_SessionDoc sid={active.ref} />
          : null}
        {active && active.kind === "file"
          && typeof window.NV_FileDoc === "function"
          ? <window.NV_FileDoc path={active.ref} />
          : null}
        {active && active.kind === "diff"
          && typeof window.NV_DiffDoc === "function"
          ? <window.NV_DiffDoc sha={active.ref} />
          : null}
        {active && active.kind === "wiki"
          && typeof window.NV_WikiDoc === "function"
          ? <window.NV_WikiDoc slug={active.ref} />
          : null}
      </div>
    </div>
  );
}

window.NV_DocHost = NV_DocHost;
