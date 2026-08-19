// primer UI - useRouter, re-implemented on top of overlay state.
//
// Pinned decision 14. Eight of the thirteen re-hosted pages read
// window.primerApi.useRouter() for params.id or navigate:
//
//   providers.jsx:268-270   agents.jsx:48-50      graphs.jsx:239-241
//   triggers.jsx:151-154    toolsets.jsx:50-52    harnesses.jsx:40-43
//   workspaces.jsx:55-57    knowledge.jsx:33-35
//
// P5 deletes foundation/router.js, but the NAME has to survive or every
// one of those pages breaks the moment it is opened. So the shell
// publishes the same {path, params, query, navigate} contract, sourced
// from the active overlay's <name>:<section>:<id> segments:
//
//   overlay=providers:tts:pv-1  ->  path "/providers/tts/pv-1",
//                                   params {section: "tts", id: "pv-1"}
//
// navigate(path) is translated back into openOverlay, so a page that
// "navigates" to a detail view deep-links the overlay instead of
// rewriting the shell's hash out from under it.

function SH_shimPath(overlay) {
  if (!overlay || !overlay.name) return "/";
  var path = "/" + overlay.name;
  if (overlay.section) path += "/" + overlay.section;
  if (overlay.id) path += "/" + overlay.id;
  return path;
}

function SH_shimParams(overlay) {
  var params = {};
  if (!overlay) return params;
  if (overlay.section) params.section = overlay.section;
  if (overlay.id) params.id = overlay.id;
  return params;
}

// A page calling navigate("/providers/llm/pv-9") means "show me this
// deeper thing", not "leave the shell". Segment 0 is the overlay name
// when it is one we host, otherwise the current overlay keeps its name
// and the remaining segments become section/id.
function SH_shimNavigate(overlay, openOverlay, path) {
  var parts = String(path || "").split("?")[0].split("/");
  var clean = [];
  for (var i = 0; i < parts.length; i++) if (parts[i]) clean.push(parts[i]);
  if (!clean.length) return;
  var known = window.SH_OVERLAYS || [];
  var name = overlay && overlay.name;
  var rest = clean;
  for (var k = 0; k < known.length; k++) {
    if (known[k] === clean[0]) { name = clean[0]; rest = clean.slice(1); break; }
  }
  if (!name) return;
  openOverlay(name, rest[0] || null, rest[1] || null);
}

function SH_installRouterShim(getOverlay, openOverlay) {
  var ns = (window.primerApi = window.primerApi || {});
  ns.useRouter = function () {
    var overlay = getOverlay();
    return {
      path: SH_shimPath(overlay),
      params: SH_shimParams(overlay),
      query: {},
      navigate: function (path) {
        SH_shimNavigate(getOverlay(), openOverlay, path);
      },
    };
  };
  // routes/matchRoute existed on the old namespace; a re-hosted page that
  // reads them must get an empty answer rather than a TypeError.
  ns.routes = ns.routes || [];
  ns.matchRoute = ns.matchRoute || function () { return null; };
}

window.SH_shimPath = SH_shimPath;
window.SH_shimParams = SH_shimParams;
window.SH_installRouterShim = SH_installRouterShim;
