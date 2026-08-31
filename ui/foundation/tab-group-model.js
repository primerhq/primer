// tab-group-model.js - the pure state machine behind the multi-group tab
// host (uiv2 R2, replacing nv-doc-host.jsx's single tab group).
//
// A PURE model (TG_ prefix, no DOM, no React): every operation takes a
// model and returns a FRESH model, unit-tested in MiniRacer exactly like
// the other foundation modules. The binding (rendering groups, wiring drag-and-drop, calling
// these operations from event handlers) lives in nv-tab-groups.jsx.
//
// Design source: uiv2/implementer-notes.md sections 2.3 (tab groups) +
// the prototype's own openDoc/closeTab/promoteTab/moveTab logic (Primer
// Console.dc.html). Model shape:
//   { groups: [{ id, tabs: [{ id, kind, ref, preview }], activeTabId }],
//     direction: "row" | "column",
//     focusedGroupId }
//
// Tab identity is derived, never caller-supplied: TG_tabId(kind, ref) is
// the same "kind:ref" scheme nv-doc-host.jsx's NV_docId already uses, so
// a given (kind, ref) pair is the same tab wherever it appears and can
// only ever exist in ONE group at a time (openTab checks every group
// before creating a new tab; every other operation relies on that
// invariant to search by id without ambiguity).

// ---------------------------------------------------------------------------
// Tab identity
// ---------------------------------------------------------------------------

function TG_tabId(kind, ref) {
  return kind + ":" + ref;
}

// ---------------------------------------------------------------------------
// Internal helpers (not exported)
// ---------------------------------------------------------------------------

function TG_findTab(model, tabId) {
  for (var i = 0; i < model.groups.length; i++) {
    var g = model.groups[i];
    for (var j = 0; j < g.tabs.length; j++) {
      if (g.tabs[j].id === tabId) return { groupIndex: i, tabIndex: j, group: g, tab: g.tabs[j] };
    }
  }
  return null;
}

function TG_groupIndex(model, groupId) {
  for (var i = 0; i < model.groups.length; i++) {
    if (model.groups[i].id === groupId) return i;
  }
  return -1;
}

function TG_newGroupId() {
  // No Math.random() ban here (this is app code, not a workflow script);
  // collisions are harmless even if they happened, since callers always
  // address groups by the id a prior operation just returned.
  return "g-" + Math.random().toString(36).slice(2, 9);
}

// Remove groups[idx]'s tab at tabIdx, returning a NEW groups array with
// that group's tabs list updated (and its activeTabId fixed up if the
// removed tab was active). Does NOT collapse an emptied group - callers
// that want collapse-on-empty call TG_collapseEmpty afterward.
function TG_withTabRemoved(groups, groupIdx, tabIdx) {
  var out = groups.slice();
  var g = out[groupIdx];
  var removedId = g.tabs[tabIdx].id;
  var tabs = g.tabs.slice();
  tabs.splice(tabIdx, 1);
  var activeTabId = g.activeTabId;
  if (activeTabId === removedId) {
    activeTabId = tabs.length ? tabs[tabs.length - 1].id : null;
  }
  out[groupIdx] = { id: g.id, tabs: tabs, activeTabId: activeTabId };
  return out;
}

// Drop every group with zero tabs. If that would leave nothing, install
// one fresh empty group (the "nothing open" empty state, notes 2.3) with
// a NEW id (fresh state deserves a fresh identity; nothing addresses the
// old empty group by id since it never held anything to reopen).
// Also resets direction to "row" once only one group remains - the one-
// direction-at-a-time constraint (notes 2.3) is about SPLIT groups, and a
// single group has nothing to be split from.
function TG_collapseEmpty(model, groups) {
  var nonEmpty = groups.filter(function (g) { return g.tabs.length > 0; });
  var nextGroups = nonEmpty.length ? nonEmpty : [{ id: TG_newGroupId(), tabs: [], activeTabId: null }];
  var direction = nextGroups.length > 1 ? model.direction : "row";
  var focusedGroupId = model.focusedGroupId;
  if (TG_groupIndex({ groups: nextGroups }, focusedGroupId) < 0) {
    focusedGroupId = nextGroups[0].id;
  }
  return {
    groups: nextGroups,
    direction: direction,
    focusedGroupId: focusedGroupId,
  };
}

// ---------------------------------------------------------------------------
// Public operations (TG_ prefix) - every one takes a model, returns a
// FRESH model. None mutate their input.
// ---------------------------------------------------------------------------

// TG_init(opts) -> a fresh model: one empty group, row direction, that
// group focused. opts.groupId lets a caller pin the first group's id
// (mainly for deterministic tests); otherwise one is minted.
function TG_init(opts) {
  opts = opts || {};
  var gid = opts.groupId || TG_newGroupId();
  return {
    groups: [{ id: gid, tabs: [], activeTabId: null }],
    direction: "row",
    focusedGroupId: gid,
  };
}

// TG_openTab(model, { kind, ref }, opts) -> model.
// opts: { groupId (defaults to the focused group), promote (bool) }.
//
// If the (kind, ref) tab already exists anywhere, this does NOT create a
// second copy: it promotes it (if opts.promote) and focuses its group,
// exactly like clicking an already-open tab's row elsewhere in the shell.
// Otherwise a new tab is inserted into the target group: a preview tab
// REPLACES that group's existing preview tab at the same position (a new
// single-click doc never accretes previews); a promoted tab always
// appends. Opening always focuses the host group.
function TG_openTab(model, doc, opts) {
  opts = opts || {};
  var tabId = TG_tabId(doc.kind, doc.ref);
  var found = TG_findTab(model, tabId);
  var groups = model.groups.slice();

  if (found) {
    if (opts.promote && found.tab.preview) {
      var tabs = found.group.tabs.slice();
      tabs[found.tabIndex] = { id: found.tab.id, kind: found.tab.kind, ref: found.tab.ref, preview: false };
      groups[found.groupIndex] = { id: found.group.id, tabs: tabs, activeTabId: tabId };
    } else {
      groups[found.groupIndex] = { id: found.group.id, tabs: found.group.tabs, activeTabId: tabId };
    }
    return { groups: groups, direction: model.direction, focusedGroupId: found.group.id };
  }

  var groupId = opts.groupId || model.focusedGroupId;
  var hostIdx = TG_groupIndex(model, groupId);
  if (hostIdx < 0) hostIdx = 0; // an invalid target falls back to the first group rather than dropping the open
  var host = groups[hostIdx];
  var tab = { id: tabId, kind: doc.kind, ref: doc.ref, preview: !opts.promote };
  var hostTabs = host.tabs.slice();
  if (tab.preview) {
    var previewIdx = -1;
    for (var i = 0; i < hostTabs.length; i++) {
      if (hostTabs[i].preview) { previewIdx = i; break; }
    }
    if (previewIdx >= 0) hostTabs[previewIdx] = tab;
    else hostTabs.push(tab);
  } else {
    hostTabs.push(tab);
  }
  groups[hostIdx] = { id: host.id, tabs: hostTabs, activeTabId: tabId };
  return { groups: groups, direction: model.direction, focusedGroupId: host.id };
}

// TG_promoteTab(model, tabId) -> model. A no-op (fresh-but-equivalent
// model) if tabId is not found or already promoted.
function TG_promoteTab(model, tabId) {
  var found = TG_findTab(model, tabId);
  if (!found || !found.tab.preview) {
    return { groups: model.groups, direction: model.direction, focusedGroupId: model.focusedGroupId };
  }
  var groups = model.groups.slice();
  var tabs = found.group.tabs.slice();
  tabs[found.tabIndex] = { id: found.tab.id, kind: found.tab.kind, ref: found.tab.ref, preview: false };
  groups[found.groupIndex] = { id: found.group.id, tabs: tabs, activeTabId: found.group.activeTabId };
  return { groups: groups, direction: model.direction, focusedGroupId: model.focusedGroupId };
}

// TG_closeTab(model, tabId) -> model. Closing a group's last tab removes
// the group and collapses the layout (notes 2.3: "closing a group's last
// tab removes the group"); if every group ends up empty, one empty group
// remains so the center always has somewhere to show the empty state.
function TG_closeTab(model, tabId) {
  var found = TG_findTab(model, tabId);
  if (!found) {
    return { groups: model.groups, direction: model.direction, focusedGroupId: model.focusedGroupId };
  }
  var groups = TG_withTabRemoved(model.groups, found.groupIndex, found.tabIndex);
  return TG_collapseEmpty(model, groups);
}

// TG_focusGroup(model, groupId) -> model. Pure focus change; invalid ids
// are a no-op (the model already names a valid focused group, so an
// unresolvable request cannot leave the model in a broken state).
function TG_focusGroup(model, groupId) {
  var idx = TG_groupIndex(model, groupId);
  return {
    groups: model.groups,
    direction: model.direction,
    focusedGroupId: idx >= 0 ? groupId : model.focusedGroupId,
  };
}

// TG_moveTab(model, tabId, targetGroupId, position) -> model. Moves an
// EXISTING tab into targetGroupId at `position` (an index into the
// target's tabs array after removal; omitted/out-of-range clamps to the
// end). Works for cross-group moves and same-group reorders alike.
// Moving ALWAYS promotes (notes 2.3: "moving a tab always promotes it,
// preview -> real") and focuses the destination group. If the source
// group empties out, it collapses per the same rule as TG_closeTab.
function TG_moveTab(model, tabId, targetGroupId, position) {
  var found = TG_findTab(model, tabId);
  if (!found) {
    return { groups: model.groups, direction: model.direction, focusedGroupId: model.focusedGroupId };
  }
  var targetIdx = TG_groupIndex(model, targetGroupId);
  if (targetIdx < 0) {
    return { groups: model.groups, direction: model.direction, focusedGroupId: model.focusedGroupId };
  }
  var promoted = { id: found.tab.id, kind: found.tab.kind, ref: found.tab.ref, preview: false };
  var sameGroup = found.group.id === targetGroupId;

  var groups = model.groups.slice();
  if (sameGroup) {
    var tabs = found.group.tabs.slice();
    tabs.splice(found.tabIndex, 1);
    var pos = position == null ? tabs.length : Math.max(0, Math.min(position, tabs.length));
    tabs.splice(pos, 0, promoted);
    groups[found.groupIndex] = { id: found.group.id, tabs: tabs, activeTabId: tabId };
    return { groups: groups, direction: model.direction, focusedGroupId: found.group.id };
  }

  groups = TG_withTabRemoved(groups, found.groupIndex, found.tabIndex);
  var afterRemovalTargetIdx = TG_groupIndex({ groups: groups }, targetGroupId);
  var target = groups[afterRemovalTargetIdx];
  var targetTabs = target.tabs.slice();
  var insertAt = position == null ? targetTabs.length : Math.max(0, Math.min(position, targetTabs.length));
  targetTabs.splice(insertAt, 0, promoted);
  groups[afterRemovalTargetIdx] = { id: target.id, tabs: targetTabs, activeTabId: tabId };

  var collapsed = TG_collapseEmpty({ groups: groups, direction: model.direction, focusedGroupId: targetGroupId }, groups);
  return { groups: collapsed.groups, direction: collapsed.direction, focusedGroupId: targetGroupId };
}

// TG_splitWith(model, tabId, direction, targetGroupId) -> model. Pulls
// tabId out of whichever group currently holds it and opens a brand-new
// group for it, inserted immediately after targetGroupId - "split
// right"/"split down" ON THE GROUP THE DRAG WAS DROPPED ONTO, which is
// not necessarily tabId's own source group (the prototype's drop zones
// live inside each group's own document area, so a tab dragged from
// group A can be split against group B). targetGroupId is OPTIONAL and
// defaults to tabId's current group, preserving "split my own group"
// as the simple case. An unresolvable targetGroupId is a no-op (never
// guessed). direction is "row" or "column".
//
// ONE-DIRECTION-AT-A-TIME (notes 2.3: "one split direction at a time - a
// row OR a column of groups, no nested grids"): the requested direction
// only takes effect when there is currently at most one non-empty group.
// Once 2+ groups exist, every further split keeps the model's EXISTING
// direction regardless of what is requested here - this mirrors the
// prototype's own splitDir logic exactly (it only ever assigns a new
// direction when the count of non-empty groups is <= 1).
function TG_splitWith(model, tabId, direction, targetGroupId) {
  var found = TG_findTab(model, tabId);
  if (!found) {
    return { groups: model.groups, direction: model.direction, focusedGroupId: model.focusedGroupId };
  }
  var target = targetGroupId == null ? found.group.id : targetGroupId;
  if (TG_groupIndex(model, target) < 0) {
    return { groups: model.groups, direction: model.direction, focusedGroupId: model.focusedGroupId };
  }
  var nonEmptyCount = model.groups.filter(function (g) { return g.tabs.length > 0; }).length;
  var nextDirection = nonEmptyCount <= 1 ? direction : model.direction;

  var promoted = { id: found.tab.id, kind: found.tab.kind, ref: found.tab.ref, preview: false };
  var groups = TG_withTabRemoved(model.groups, found.groupIndex, found.tabIndex);
  var targetStillAt = TG_groupIndex({ groups: groups }, target);
  var newGroup = { id: TG_newGroupId(), tabs: [promoted], activeTabId: promoted.id };
  var insertAt = targetStillAt >= 0 ? targetStillAt + 1 : groups.length;
  groups.splice(insertAt, 0, newGroup);

  var collapsed = TG_collapseEmpty(
    { groups: groups, direction: nextDirection, focusedGroupId: newGroup.id },
    groups
  );
  return { groups: collapsed.groups, direction: collapsed.direction, focusedGroupId: newGroup.id };
}

// TG_activeDoc(model) -> the focused group's active tab, or null. This is
// what the URL doc= param and palette context-gating (session verbs only
// when a session tab is focused) both read.
function TG_activeDoc(model) {
  var idx = TG_groupIndex(model, model.focusedGroupId);
  if (idx < 0) return null;
  var g = model.groups[idx];
  if (!g.activeTabId) return null;
  for (var i = 0; i < g.tabs.length; i++) {
    if (g.tabs[i].id === g.activeTabId) return g.tabs[i];
  }
  return null;
}

// ---------------------------------------------------------------------------
// No-build window exports
// ---------------------------------------------------------------------------

window.TG_tabId = TG_tabId;
window.TG_init = TG_init;
window.TG_openTab = TG_openTab;
window.TG_promoteTab = TG_promoteTab;
window.TG_closeTab = TG_closeTab;
window.TG_focusGroup = TG_focusGroup;
window.TG_moveTab = TG_moveTab;
window.TG_splitWith = TG_splitWith;
window.TG_activeDoc = TG_activeDoc;
