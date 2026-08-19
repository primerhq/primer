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
// The fragment is everything after the FIRST "#", so the second "#" is a
// character this module owns; that keeps the spec's literal "#turn-42"
// and "#L10-L30" spellings.
//
// Pure string work on purpose: it reaches for no Web API at all, so the
// tests run it straight through MiniRacer.
// Web APIs are not ECMAScript, and a URL grammar that cannot be executed
// in a test is a grammar nobody checks.

var SH_DOC_KINDS = ["session", "file", "diff", "wiki", "trace"];

var SH_OVERLAYS = [
  "providers", "collections", "agents", "graphs", "triggers",
  "toolsets", "tools", "workers", "approvals", "admin",
  "harnesses", "services", "channels", "workspaces", "new-session",
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
    doc: SH_parseDoc(params.doc),
    overlay: SH_parseOverlay(params.overlay),
    anchor: anchor,
  };
}

// Only the four addressable facts are written. Palette state, toasts and
// every other transient are deliberately not representable here.
function SH_buildUrl(state) {
  var s = state || {};
  if (!s.wid) return "#/";
  var url = "#/w/" + SH_encodeRef(s.wid);
  var query = [];
  if (s.doc && s.doc.kind && s.doc.ref
      && SH_indexOfIn(SH_DOC_KINDS, s.doc.kind) >= 0) {
    query.push("doc=" + s.doc.kind + ":" + SH_encodeRef(s.doc.ref));
  }
  if (s.overlay && s.overlay.name
      && SH_indexOfIn(SH_OVERLAYS, s.overlay.name) >= 0) {
    var ov = s.overlay.name;
    if (s.overlay.section) {
      ov += ":" + SH_encodeRef(s.overlay.section);
      if (s.overlay.id) ov += ":" + SH_encodeRef(s.overlay.id);
    }
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

window.SH_DOC_KINDS = SH_DOC_KINDS;
window.SH_OVERLAYS = SH_OVERLAYS;
window.SH_encodeRef = SH_encodeRef;
window.SH_parseUrl = SH_parseUrl;
window.SH_buildUrl = SH_buildUrl;
window.SH_parseAnchor = SH_parseAnchor;
