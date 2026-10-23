// primer UI - the fresh shell's URL grammar (S8 spec section 8).
//
// Canonical form, hosted inside the console's hash fragment because
// /console is a StaticFiles mount with html=True and no SPA catch-all
// (primer/api/_app_middleware.py:369-382), so "/console/w/<wid>" would
// 404. The console already carries query state in the hash
// (foundation/router.js parseHash; components/studio.jsx ST_tabFromUrl):
//
//   #/w/{wid}?doc=<kind>:<ref>&overlay=<name>[:<section>[:<id>]]#<anchor>
//
// and the view, view=<name>[:<nav>[:<sub>]], is one more query key (absent = the studio).
//
// The fragment is everything after the FIRST "#", so the second "#" is a
// character this module owns; that keeps the spec's literal "#turn-42"
// and "#L10-L30" spellings.
//
// Pure string work on purpose: it reaches for no Web API at all, so the
// tests run it straight through MiniRacer.
// Web APIs are not ECMAScript, and a URL grammar that cannot be executed
// in a test is a grammar nobody checks.

// On the split-view console trace and inbox are not document kinds: the
// trace is a split inside the session doc and attention lives in the
// Inbox (cross-workspace feed in the rail, beside the workspace tree) +
// the system dashboard, so neither is a document kind.
var SH_DOC_KINDS = ["session", "file", "diff", "wiki"];

// "admin" died on the flag day: users/sso/mcp/setup are the System
// view's navs now, so an overlay=admin address drops on parse.
var SH_OVERLAYS = [
  "providers", "collections", "agents", "graphs", "triggers",
  // "tools" retired (uiv2 US-011a): the standalone Tools catalog page is
  // gone, embedded pickers (AP_ToolPicker, GB_ToolPicker) are the only
  // surface left - an old ?overlay=tools link should fail to parse, not
  // open a blank panel.
  "toolsets", "approvals",
  "harnesses", "services", "channels", "workspaces", "new-session",
  // Create-workspace (wiring plan P3): the designer's instantiation
  // form, distinct from "workspaces" (the management surface).
  "new-workspace",
  // The internal-collections SUBSYSTEM: its config, bootstrap and the
  // search it powers. Distinct from "collections", which is the
  // knowledge browser; the two were conflated and the subsystem ended up
  // with no home at all.
  "internal-collections",
  // The Activity events console (revamp section 7).
  "activity",
];

// Refs are percent-encoded, then "/" is restored: file paths and wiki slug
// paths are the common case and "%2F" everywhere makes a pasted link
// unreadable. "/" is unambiguous because a ref runs to the next "&".
function SH_encodeRef(value) {
  return encodeURIComponent(String(value)).replace(/%2F/g, "/");
}

function SH_decode(value) {
  try {
    return decodeURIComponent(value);
  } catch (_e) {
    return value;
  }
}

function SH_parseQuery(query) {
  var out = {};
  if (!query) return out;
  var parts = String(query).split("&");
  for (var i = 0; i < parts.length; i++) {
    if (!parts[i]) continue;
    var eq = parts[i].indexOf("=");
    var key = eq >= 0 ? parts[i].slice(0, eq) : parts[i];
    var val = eq >= 0 ? parts[i].slice(eq + 1) : "";
    out[SH_decode(key)] = val;
  }
  return out;
}

function SH_indexOfIn(list, value) {
  for (var i = 0; i < list.length; i++) if (list[i] === value) return i;
  return -1;
}

// Is `key` a key of `table` itself? The tables of the grammar (SH_VIEWS, SH_VIEW_SUBS, SH_VIEW_REDIRECTS) are plain objects, and `key in table` is also true for what a
// plain object inherits ("constructor", "__proto__", "toString"). A pasted view=constructor:x then read SH_VIEWS.constructor.indexOf, which is not a function, and threw while
// the console loaded, and view=toString was taken for a view of that name. A name that came from an address is looked up here, never with `in`.
function SH_hasOwn(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key);
}

function SH_parseDoc(raw) {
  if (!raw) return null;
  var colon = raw.indexOf(":");
  if (colon <= 0) return null;
  var kind = raw.slice(0, colon);
  if (SH_indexOfIn(SH_DOC_KINDS, kind) < 0) return null;
  var ref = SH_decode(raw.slice(colon + 1));
  if (!ref) return null;
  return { kind: kind, ref: ref };
}

// The three-view console (wiring plan P0 T3). Absent view = studio,
// which is also how every pre-existing URL parses forward. Nav ids
// are the sidebar's, in the sidebar's order (Platform revamp, design 4.1):
// "web" and "templates" are custom pages (seam I-2) whose row exists only
// while the component that draws them is loaded, so the grammar admits them
// before they ship and a link to one that is not there opens Providers.
// "workspaces" (Platform) and "setup" (System) are retired navs: they are not
// in the lists, and a link that names one is redirected (SH_VIEW_REDIRECTS).
var SH_VIEWS = {
  studio: [],
  platform: [
    "providers", "profiles", "agents", "graphs", "templates", "toolsets",
    "tools", "collections", "web", "triggers", "channels", "harnesses",
    "services", "approvals",
  ],
  system: [
    "dashboard", "users", "apikeys", "sso", "mcp", "internal",
    "activity", "profile",
  ],
};

// Old links (seam I-7). A retired nav is answered by a view that exists, applied by SH_parseView BEFORE it validates the nav, so the address bar is then
// rewritten to the target's canonical form (a link is never replayed as written, and never lands on a blank page):
//   view=platform:workspaces  -> the studio (the Workspaces page left the Platform view; the studio rail's workspace menu has New session, Settings and Delete)
//   view=system:setup         -> the System dashboard (the grammar has no setup nav)
// The Setup page is gone: Plan 10 Task 8 moved its checks to the Dashboard's Setup card and removed the System sidebar's Setup row, so this redirect is
// what an old link to it gets.
// "platform:templates" needs none: it is the new Workspace templates page. Keyed "<name>:<nav>", like SH_VIEW_SUBS; a sub on a redirected link is dropped.
var SH_VIEW_REDIRECTS = {
  "platform:workspaces": { name: "studio", nav: null },
  "system:setup": { name: "system", nav: "dashboard" },
};

// A nav that has sub-entries admits a third segment, view=<name>:<nav>:<sub> (Platform revamp, seam I-1). Keyed "<name>:<nav>". Today only Platform > Providers has
// one: the provider type, in the order the sidebar lists them (TTS and ASR last). Every type is a key of PROVIDER_CLASSES (components/provider-catalog.jsx), which
// tests/ui/test_shell_url.py holds it to, and ui/fixtures/shell/manifest.json mirrors it as "view_subs". A sub the list does not name is dropped on parse and never
// written, so a pasted link with a type that does not exist opens the nav itself (Providers, every type) and the address bar is rewritten without it.
var SH_VIEW_SUBS = {
  "platform:providers": [
    "llm", "embedding", "ssp", "cross_encoder", "workspace", "web_search",
    "channel", "artifact_storage", "tts", "stt",
  ],
};

function SH_parseView(raw) {
  if (!raw) return { name: "studio", nav: null, sub: null };
  var bits = String(raw).split(":");
  var name = SH_decode(bits[0]);
  if (!SH_hasOwn(SH_VIEWS, name)) return { name: "studio", nav: null, sub: null };
  var nav = bits.length > 1 && bits[1] ? SH_decode(bits[1]) : null;
  var moved = nav && SH_hasOwn(SH_VIEW_REDIRECTS, name + ":" + nav) ? SH_VIEW_REDIRECTS[name + ":" + nav] : null;
  if (moved) return { name: moved.name, nav: moved.nav, sub: null };
  if (nav && SH_VIEWS[name].indexOf(nav) < 0) nav = null;
  var sub = null;
  var subs = nav && SH_hasOwn(SH_VIEW_SUBS, name + ":" + nav) ? SH_VIEW_SUBS[name + ":" + nav] : null;
  if (subs && bits.length > 2 && bits[2]) {
    sub = SH_decode(bits[2]);
    if (subs.indexOf(sub) < 0) sub = null;
  }
  return { name: name, nav: nav, sub: sub };
}

function SH_parseOverlay(raw) {
  if (!raw) return null;
  var bits = String(raw).split(":");
  var name = SH_decode(bits[0]);
  if (SH_indexOfIn(SH_OVERLAYS, name) < 0) return null;
  return {
    name: name,
    section: bits.length > 1 && bits[1] ? SH_decode(bits[1]) : null,
    id: bits.length > 2 && bits[2] ? SH_decode(bits[2]) : null,
  };
}

function SH_parseUrl(hash) {
  var raw = String(hash == null ? "" : hash);
  if (raw.charAt(0) === "#") raw = raw.slice(1);
  var anchor = null;
  var hIdx = raw.indexOf("#");
  if (hIdx >= 0) {
    anchor = raw.slice(hIdx + 1) || null;
    raw = raw.slice(0, hIdx);
  }
  var qIdx = raw.indexOf("?");
  var path = qIdx >= 0 ? raw.slice(0, qIdx) : raw;
  var params = SH_parseQuery(qIdx >= 0 ? raw.slice(qIdx + 1) : "");
  var wid = null;
  var m = /^\/w\/([^/?#]+)/.exec(path);
  if (m) wid = SH_decode(m[1]);
  return {
    wid: wid,
    view: SH_parseView(params.view),
    doc: SH_parseDoc(params.doc),
    overlay: SH_parseOverlay(params.overlay),
    anchor: anchor,
  };
}

// Only the four addressable facts are written. Palette state, toasts and
// every other transient are deliberately not representable here.
function SH_buildUrl(state) {
  var s = state || {};
  // A missing workspace used to discard the whole state and return "#/".
  // The PARSER reads "#/?overlay=agents" perfectly well -- the platform
  // surfaces are not workspace-scoped, so addressing one before a
  // workspace is resolved is legitimate -- but building could not express
  // it, so the url-sync effect rewrote that address to a bare "#/" and
  // the overlay was gone before anything could render it. Build must be
  // the parser's inverse: drop only the segment that is actually absent.
  var url = s.wid ? "#/w/" + SH_encodeRef(s.wid) : "#/";
  var query = [];
  // Studio is the unwritten default: only the other views serialize,
  // so every historical URL round-trips byte-identical.
  if (s.view && s.view.name && s.view.name !== "studio"
      && SH_hasOwn(SH_VIEWS, s.view.name)) {
    var vv = s.view.name;
    if (s.view.nav && SH_VIEWS[s.view.name].indexOf(s.view.nav) >= 0) {
      vv += ":" + SH_encodeRef(s.view.nav);
      // The type is the third segment, written only for a nav that has a list of them and only when it is on that list (the parser's own rule).
      var subKey = s.view.name + ":" + s.view.nav;
      var subs = SH_hasOwn(SH_VIEW_SUBS, subKey) ? SH_VIEW_SUBS[subKey] : null;
      if (subs && s.view.sub && subs.indexOf(s.view.sub) >= 0) {
        vv += ":" + SH_encodeRef(s.view.sub);
      }
    }
    query.push("view=" + vv);
  }
  if (s.doc && s.doc.kind && s.doc.ref
      && SH_indexOfIn(SH_DOC_KINDS, s.doc.kind) >= 0) {
    query.push("doc=" + s.doc.kind + ":" + SH_encodeRef(s.doc.ref));
  }
  if (s.overlay && s.overlay.name
      && SH_indexOfIn(SH_OVERLAYS, s.overlay.name) >= 0) {
    var ov = s.overlay.name;
    // The id used to be written only INSIDE the section branch, so an
    // overlay carrying a record but no tab -- which is every plain
    // detail view -- serialised as bare "overlay=agents" and lost the
    // record. Reloading such a URL landed back on the list, and a deep
    // link to a record was impossible unless it happened to have a tab.
    // The parser already reads an empty middle slot, so an id with no
    // section writes one.
    if (s.overlay.section) {
      ov += ":" + SH_encodeRef(s.overlay.section);
    } else if (s.overlay.id) {
      ov += ":";
    }
    if (s.overlay.id) ov += ":" + SH_encodeRef(s.overlay.id);
    query.push("overlay=" + ov);
  }
  if (query.length) url += "?" + query.join("&");
  if (s.anchor) url += "#" + s.anchor;
  return url;
}

function SH_parseAnchor(anchor) {
  if (!anchor) return null;
  var turn = /^turn-(\d+)$/.exec(anchor);
  if (turn) return { kind: "turn", turn: parseInt(turn[1], 10) };
  var lines = /^L(\d+)(?:-L(\d+))?$/.exec(anchor);
  if (lines) {
    return {
      kind: "lines",
      from: parseInt(lines[1], 10),
      to: lines[2] ? parseInt(lines[2], 10) : null,
    };
  }
  return null;
}

// Is the url ahead of the shell?
//
// Arriving at another workspace is not one render: the hashchange
// listener applies the new url's document first, and the workspace only
// catches up once the root gate re-reads the hash. In between, the shell
// holds the NEW document beside the OLD workspace, and anything that
// writes the url from that mixture overwrites the address still being
// navigated to. When the url names a workspace the shell has not adopted
// yet it is ahead, not wrong, and nothing may write over it.
function SH_urlIsAhead(parsed, wid) {
  return !!(parsed && parsed.wid && parsed.wid !== wid);
}

window.SH_urlIsAhead = SH_urlIsAhead;
window.SH_DOC_KINDS = SH_DOC_KINDS;
window.SH_OVERLAYS = SH_OVERLAYS;
window.SH_encodeRef = SH_encodeRef;
window.SH_parseUrl = SH_parseUrl;
window.SH_buildUrl = SH_buildUrl;
window.SH_parseAnchor = SH_parseAnchor;
window.SH_VIEWS = SH_VIEWS;
window.SH_VIEW_SUBS = SH_VIEW_SUBS;
window.SH_VIEW_REDIRECTS = SH_VIEW_REDIRECTS;
window.SH_parseView = SH_parseView;
