/* global React, NV_identity */
// The multi-group tab host (uiv2 R2, US-007 phase 1): renders a TG_
// model (ui/foundation/tab-group-model.js) - preview/promote tabs, HTML
// drag-and-drop with the three "move here / split right / split down"
// zones, replacing nv-doc-host.jsx's single tab group.
//
// Mounted unconditionally by nv-studio.jsx as the center slot (the
// phase-2 rollback gate to nv-doc-host.jsx retired in US-011a).
//
// Props:
//   model              - a TG_ model, see tab-group-model.js.
//   onModelChange       - (nextModel, op) => void. Called after every
//                         interaction that should mutate the model.
//                         op is "open" for selectTab/promoteTab (a real
//                         navigation - the caller should push a history
//                         entry) or "manage" for close/move/split/focus
//                         (tab-management; the active doc can change as a
//                         SIDE EFFECT, e.g. closing the active tab falls
//                         back to the last one - the caller should
//                         replace, not push, per US-007 R2 phase 2's
//                         design). This component never holds model state
//                         itself - the caller owns it (no direct URL
//                         writes here).
//   renderDoc           - (tab | null) => ReactNode. Renders a group's
//                         document body for its active tab; called with
//                         null when a group has no active tab (only
//                         possible for the sole empty-state group).
//                         Delegates entirely to the caller (nv-studio.jsx's
//                         NV_renderStudioDoc) so this file never needs to
//                         know about NV_SessionDoc / NV_FileDoc /
//                         NV_DiffDoc / NV_WikiDoc directly.
//   resolveSessionMeta  - OPTIONAL (sid) => {wid, name, binding} |
//                         undefined. A session tab's running-pulse dot
//                         needs the session store, which is keyed by
//                         (wid, sid); tabs are GLOBAL across workspaces
//                         and the TG_ model does not carry a wid per tab
//                         (out of the pure model's scope, see the
//                         tab-group-model.js deliverable report). name
//                         and binding drive the tab's label and identity
//                         glyph (F1, 2026-08-29 UI review) - the same
//                         seam generalized rather than adding a second
//                         one, since both ride the shell's existing
//                         session cache alongside the wid. Omitted, or
//                         returning undefined for a given sid, just means
//                         that tab shows no pulse and falls back to the
//                         bare id label - never an error.
//
// testids (the R2 BDD scenarios pin these, per the uiv2 migration map):
//   nv-tg-group:{n}          - the nth group's wrapper (n = array index)
//   nv-tg-tab:{kind}:{ref}   - a tab (== "nv-tg-tab:" + TG_tabId, so it
//                              is stable across whichever group currently
//                              hosts it)
//   nv-tg-tab-close:{kind}:{ref}
//   nv-tg-drop-move:{n}      - "move here" zone inside group n
//   nv-tg-drop-right:{n}     - "split right" zone inside group n
//   nv-tg-drop-down:{n}      - "split down" zone inside group n
// (the zone ids append the group index - the brief's flat
// nv-tg-drop-move/-right/-down names would collide across groups, and a
// scenario dropping onto a SPECIFIC group's zone needs to address it.)

function NV_TG_SessionPulse(props) {
  // Isolated so its (possibly absent) session-store subscription is one
  // hook call per mounted tab, never a variable number of hook calls in
  // the parent's render (React's rules of hooks) - own component per
  // list item is the standard pattern for that.
  var wid = props.wid;
  var sid = props.sid;
  var statusSnap = wid && typeof window.useSessionStore === "function"
    ? window.useSessionStore(wid, sid, "status")
    : null;
  if (!statusSnap || !statusSnap.verb) return null;
  return <span className="nv-dot-pulse" title="running" />;
}

function NV_TG_KindGlyph(props) {
  var kind = props.kind;
  if (kind === "session") {
    // F1 (2026-08-29 UI review): was a fixed accent diamond for every
    // session tab, losing which agent (or graph) is actually bound.
    // NV_identity(binding) never returns null (falls back to the
    // operator glyph for an unresolved/absent binding), so an
    // unresolved session tab still renders A glyph, just not yet the
    // bound one - same "never an error" contract resolveSessionMeta
    // documents for its own undefined case.
    var ident = NV_identity(props.binding);
    return (
      <svg width="10" height="10" viewBox="0 0 12 12" className="nv-tg-tab-glyph" style={{ flexShrink: 0, color: ident.color }}>
        <path d={ident.d} fill="currentColor" />
      </svg>
    );
  }
  if (kind === "diff") {
    return <span className="nv-tg-tab-glyph nv-tg-glyph-diff" title="diff">±</span>;
  }
  if (kind === "wiki") {
    return (
      <svg width="10" height="10" viewBox="0 0 12 12" className="nv-tg-tab-glyph" style={{ flexShrink: 0, color: "var(--text-4)" }}>
        <path d="M3 1.5h5.5L11 4v6.5H3Z M8.5 1.5V4H11" fill="none" stroke="currentColor" strokeWidth="1.1" />
      </svg>
    );
  }
  // file, and any future kind: a bare document glyph.
  return (
    <svg width="10" height="10" viewBox="0 0 12 12" className="nv-tg-tab-glyph" style={{ flexShrink: 0, color: "var(--text-4)" }}>
      <path d="M3 1.5h4l1.5 2h4.5v7h-10Z" fill="none" stroke="currentColor" strokeWidth="1.1" />
    </svg>
  );
}

function NV_TG_tabLabel(tab, meta) {
  // F1 (2026-08-29 UI review): was always tab.ref (the raw session id) -
  // meta is resolveSessionMeta's result, undefined/nameless until the
  // session cache resolves it, hence the id fallback staying live.
  if (tab.kind === "session") return (meta && meta.name) || tab.ref;
  if (tab.kind === "diff") return String(tab.ref).slice(0, 7);
  return String(tab.ref).split("/").pop();
}

// F7: pure drop-X-vs-tab-midpoints math, no DOM - the drop handler below
// extracts each rendered tab's rect.left/rect.width and this decides
// where the dragged tab lands. `midpoints` is the target group's OTHER
// tabs' midpoints, left to right, EXCLUDING the dragged tab itself -
// matching TG_moveTab's own documented `position` contract ("an index
// into the target's tabs array AFTER removal").
function NV_TG_dropIndex(midpoints, clientX) {
  var index = 0;
  for (var i = 0; i < midpoints.length; i++) {
    if (clientX > midpoints[i]) index++;
    else break;
  }
  return index;
}

function NV_TabGroups(props) {
  var model = props.model;
  var dragState = React.useState(null);
  var dragging = dragState[0]; // the dragged tab's id, or null
  var setDragging = dragState[1];

  function change(next, op) {
    if (typeof props.onModelChange === "function") props.onModelChange(next, op);
  }

  function selectTab(tab, ev) {
    // Review finding #1 (US-007 R2 phase 1): the group wrapper's onClick
    // (focus-on-click) reads the SAME pre-click model closure. Without
    // stopPropagation it fires right after this handler and overwrites
    // whatever this call just dispatched with a stale-model result -
    // mirrors closeTab's existing guard below.
    if (ev) ev.stopPropagation();
    change(window.TG_openTab(model, { kind: tab.kind, ref: tab.ref }, {}), "open");
  }
  function promoteTab(tab, ev) {
    if (ev) ev.stopPropagation();
    change(window.TG_openTab(model, { kind: tab.kind, ref: tab.ref }, { promote: true }), "open");
  }
  function closeTab(tab, ev) {
    if (ev) ev.stopPropagation();
    change(window.TG_closeTab(model, tab.id), "manage");
  }
  function focusGroup(groupId) {
    change(window.TG_focusGroup(model, groupId), "manage");
  }

  function onTabDragStart(tab, ev) {
    try {
      ev.dataTransfer.setData("text/plain", tab.id);
      ev.dataTransfer.effectAllowed = "move";
    } catch (e) { /* Some environments (older MiniRacer-adjacent shims) lack dataTransfer; drag still tracks via state. */ }
    setDragging(tab.id);
  }
  function onTabDragEnd() {
    setDragging(null);
  }
  function onDragOverIfDragging(ev) {
    if (dragging) ev.preventDefault();
  }
  function onDropMoveHere(groupId, ev) {
    ev.preventDefault();
    if (!dragging) return;
    change(window.TG_moveTab(model, dragging, groupId, null), "manage");
    setDragging(null);
  }
  // F7: dropping ON THE TAB BAR itself (as opposed to the "move here"
  // document-body zone above, which has no useful position concept)
  // reorders in place - derive the insertion index from the drop's X
  // against the OTHER tabs' rendered midpoints, instead of always
  // appending.
  function onTabbarDrop(groupId, ev) {
    ev.preventDefault();
    if (!dragging) return;
    var tabEls = ev.currentTarget.querySelectorAll(".nv-tg-tab");
    var midpoints = [];
    for (var i = 0; i < tabEls.length; i++) {
      var el = tabEls[i];
      if (el.getAttribute("data-tab-id") === dragging) continue;
      var rect = el.getBoundingClientRect();
      midpoints.push(rect.left + rect.width / 2);
    }
    var position = NV_TG_dropIndex(midpoints, ev.clientX);
    change(window.TG_moveTab(model, dragging, groupId, position), "manage");
    setDragging(null);
  }
  function onDropSplitRight(groupId, ev) {
    ev.preventDefault();
    if (!dragging) return;
    change(window.TG_splitWith(model, dragging, "row", groupId), "manage");
    setDragging(null);
  }
  function onDropSplitDown(groupId, ev) {
    ev.preventDefault();
    if (!dragging) return;
    change(window.TG_splitWith(model, dragging, "column", groupId), "manage");
    setDragging(null);
  }

  return (
    <div className="nv-tg-groups" data-testid="nv-tg-groups" data-direction={model.direction}
      style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: model.direction }}>
      {model.groups.map(function (g, gi) {
        var isFocused = g.id === model.focusedGroupId;
        var activeTab = null;
        for (var i = 0; i < g.tabs.length; i++) {
          if (g.tabs[i].id === g.activeTabId) { activeTab = g.tabs[i]; break; }
        }
        return (
          <div key={g.id} className="nv-tg-group" data-testid={"nv-tg-group:" + gi}
            data-focused={isFocused ? "true" : "false"}
            onClick={function () { if (!isFocused) focusGroup(g.id); }}
            style={{ flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column" }}>
            <div className="nv-tg-tabbar"
              onDragOver={onDragOverIfDragging}
              onDrop={function (ev) { onTabbarDrop(g.id, ev); }}>
              {g.tabs.map(function (tab) {
                var isActive = tab.id === g.activeTabId;
                // F1: one resolve per session tab feeds the glyph, the
                // pulse AND the label, instead of each reaching into the
                // seam separately (resolveSessionMeta replaces the old
                // wid-only resolveSessionWid).
                var meta = tab.kind === "session"
                  && typeof props.resolveSessionMeta === "function"
                  ? props.resolveSessionMeta(tab.ref)
                  : null;
                return (
                  <div key={tab.id} className="nv-tg-tab"
                    data-active={isActive ? "true" : "false"}
                    data-preview={tab.preview ? "true" : "false"}
                    data-testid={"nv-tg-tab:" + tab.id}
                    data-tab-id={tab.id}
                    draggable="true"
                    onDragStart={function (ev) { onTabDragStart(tab, ev); }}
                    onDragEnd={onTabDragEnd}
                    onClick={function (ev) { selectTab(tab, ev); }}
                    onDoubleClick={function (ev) { promoteTab(tab, ev); }}>
                    {isActive ? <span className="nv-tg-tab-edge" /> : null}
                    <NV_TG_KindGlyph kind={tab.kind} binding={meta && meta.binding} />
                    {tab.kind === "session" ? (
                      <NV_TG_SessionPulse sid={tab.ref} wid={meta && meta.wid} />
                    ) : null}
                    <span className="nv-tg-tab-label">{NV_TG_tabLabel(tab, meta)}</span>
                    <button type="button" className="nv-tg-tab-close"
                      data-testid={"nv-tg-tab-close:" + tab.id}
                      onClick={function (ev) { closeTab(tab, ev); }}>x</button>
                  </div>
                );
              })}
            </div>
            <div className="nv-tg-doc-body">
              {dragging ? (
                <div className="nv-tg-drop-overlay">
                  <div className="nv-tg-drop-zone nv-tg-drop-zone-move"
                    data-testid={"nv-tg-drop-move:" + gi}
                    onDragOver={onDragOverIfDragging}
                    onDrop={function (ev) { onDropMoveHere(g.id, ev); }}>move here</div>
                  <div className="nv-tg-drop-zone nv-tg-drop-zone-right"
                    data-testid={"nv-tg-drop-right:" + gi}
                    onDragOver={onDragOverIfDragging}
                    onDrop={function (ev) { onDropSplitRight(g.id, ev); }}>split right</div>
                  <div className="nv-tg-drop-zone nv-tg-drop-zone-down"
                    data-testid={"nv-tg-drop-down:" + gi}
                    onDragOver={onDragOverIfDragging}
                    onDrop={function (ev) { onDropSplitDown(g.id, ev); }}>split down</div>
                </div>
              ) : null}
              {typeof props.renderDoc === "function" ? props.renderDoc(activeTab) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

window.NV_TabGroups = NV_TabGroups;
