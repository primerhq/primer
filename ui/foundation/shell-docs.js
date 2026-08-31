// primer UI - the fresh shell's tab model (S8 spec section 8, "Tabs").
//
// VS Code semantics wholesale: one reused italic PREVIEW tab per group,
// promotion on edit or double click, pinning, MRU cycling, split groups.
// Agent-driven opens (S3 open_file) pass focus:false and land as a
// badged background preview: no focus theft, no tab creep.
//
// Pure state transitions. Every function returns a NEW state object so a
// React host can setState(next) without aliasing.

function SH_docId(kind, ref) {
  return String(kind) + ":" + String(ref);
}

function SH_emptyDocState() {
  return {
    groups: [{ id: 0, tabs: [], activeId: null }],
    activeGroup: 0,
    mru: [],
  };
}

function SH_cloneState(state) {
  return {
    groups: state.groups.map(function (g) {
      return { id: g.id, tabs: g.tabs.slice(), activeId: g.activeId };
    }),
    activeGroup: state.activeGroup,
    mru: state.mru.slice(),
  };
}

function SH_touchMru(next, id) {
  next.mru = [id].concat(next.mru.filter(function (x) { return x !== id; }));
}

function SH_titleFor(kind, ref, title) {
  if (title) return title;
  if (kind === "file" || kind === "wiki") {
    var parts = String(ref).split("/");
    return parts[parts.length - 1] || String(ref);
  }
  return String(ref);
}

function SH_openDoc(state, req) {
  var kind = req && req.kind;
  var ref = req && req.ref;
  if (!kind || !ref) return state;
  if (window.SH_DOC_KINDS.indexOf(kind) < 0) return state;
  var next = SH_cloneState(state);
  var gi = req.group === undefined ? next.activeGroup : req.group;
  if (gi < 0 || gi >= next.groups.length) gi = next.activeGroup;
  var group = next.groups[gi];
  var id = SH_docId(kind, ref);
  var focus = req.focus === undefined ? true : !!req.focus;
  var preview = !!req.preview;

  var existing = null;
  for (var i = 0; i < group.tabs.length; i++) {
    if (group.tabs[i].id === id) { existing = group.tabs[i]; break; }
  }
  if (existing) {
    if (!preview) existing.preview = false;
    existing.badge = focus ? false : true;
    if (focus) {
      group.activeId = id;
      next.activeGroup = gi;
      SH_touchMru(next, id);
    }
    return next;
  }

  var tab = {
    id: id,
    kind: kind,
    ref: ref,
    title: SH_titleFor(kind, ref, req.title),
    preview: preview,
    pinned: false,
    badge: !focus,
    group: gi,
  };

  if (preview) {
    // Exactly one preview slot per group, and pinned tabs never occupy it.
    group.tabs = group.tabs.filter(function (t) {
      return !(t.preview && !t.pinned);
    });
  }
  group.tabs = group.tabs.concat([tab]);
  if (focus) {
    group.activeId = id;
    next.activeGroup = gi;
    SH_touchMru(next, id);
  } else if (!group.activeId) {
    group.activeId = id;
  }
  return next;
}

function SH_forEachTab(state, id, fn) {
  var next = SH_cloneState(state);
  for (var g = 0; g < next.groups.length; g++) {
    for (var i = 0; i < next.groups[g].tabs.length; i++) {
      if (next.groups[g].tabs[i].id === id) {
        next.groups[g].tabs[i] = fn(
          Object.assign({}, next.groups[g].tabs[i])
        );
      }
    }
  }
  return next;
}

function SH_promoteDoc(state, id) {
  return SH_forEachTab(state, id, function (tab) {
    tab.preview = false;
    tab.badge = false;
    return tab;
  });
}

function SH_pinDoc(state, id, pinned) {
  return SH_forEachTab(state, id, function (tab) {
    tab.pinned = !!pinned;
    if (pinned) tab.preview = false;
    return tab;
  });
}

function SH_closeDoc(state, id) {
  var next = SH_cloneState(state);
  for (var g = 0; g < next.groups.length; g++) {
    var group = next.groups[g];
    var idx = -1;
    for (var i = 0; i < group.tabs.length; i++) {
      if (group.tabs[i].id === id) { idx = i; break; }
    }
    if (idx < 0) continue;
    group.tabs = group.tabs.filter(function (t) { return t.id !== id; });
    if (group.activeId === id) {
      // Left neighbour in the POST-filter array (studio.jsx:352-360 learned
      // this the hard way): compute against one consistent array.
      var target = Math.min(Math.max(idx - 1, 0), group.tabs.length - 1);
      group.activeId = group.tabs.length ? group.tabs[target].id : null;
    }
  }
  next.mru = next.mru.filter(function (x) { return x !== id; });
  return next;
}

function SH_splitRight(state) {
  var next = SH_cloneState(state);
  next.groups.push({ id: next.groups.length, tabs: [], activeId: null });
  next.activeGroup = next.groups.length - 1;
  return next;
}

function SH_cycleMru(state, step) {
  var next = SH_cloneState(state);
  if (next.mru.length < 2) return next;
  var delta = step || 1;
  var id = next.mru[((delta % next.mru.length) + next.mru.length) % next.mru.length];
  for (var g = 0; g < next.groups.length; g++) {
    for (var i = 0; i < next.groups[g].tabs.length; i++) {
      if (next.groups[g].tabs[i].id === id) {
        next.groups[g].activeId = id;
        next.activeGroup = g;
        SH_touchMru(next, id);
        return next;
      }
    }
  }
  return next;
}

function SH_activeDoc(state) {
  var group = state.groups[state.activeGroup];
  if (!group || !group.activeId) return null;
  for (var i = 0; i < group.tabs.length; i++) {
    if (group.tabs[i].id === group.activeId) return group.tabs[i];
  }
  return null;
}

window.SH_docId = SH_docId;
window.SH_emptyDocState = SH_emptyDocState;
window.SH_openDoc = SH_openDoc;
window.SH_promoteDoc = SH_promoteDoc;
window.SH_pinDoc = SH_pinDoc;
window.SH_closeDoc = SH_closeDoc;
window.SH_splitRight = SH_splitRight;
window.SH_cycleMru = SH_cycleMru;
window.SH_activeDoc = SH_activeDoc;
