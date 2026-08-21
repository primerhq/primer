// primer UI - the fresh shell's verb registry (S8 spec sections 3 and 8).
//
// The palette is the router, so the registry is the routing table. Three
// rules are enforced here rather than in review:
//   1. verb-noun Title Case at REGISTRATION (agent-registered verbs too);
//   2. every verb declares at least one POINTER surface, so the
//      dual-render rule has something to check (nothing is palette-only);
//   3. context gating is hard filtering, not a ranking nudge.
//
// Pure logic: no React, no DOM, no fetch. Hosts adapt it.

var SH_SURFACES = [
  "palette", "rail", "tab-menu", "overlay-button", "attention-item",
  "composer-slash", "topbar",
];

// Only these open a label. An allowlist is the one lint that actually
// stops "Sessions" from shipping as a palette row.
var SH_VERB_WORDS = [
  "Open", "Close", "Switch", "Park", "Resume", "Split", "Approve",
  "Reject", "Snooze", "Mute", "Resolve", "Create", "Delete", "Rename",
  "Copy", "Show", "Hide", "Run", "Stop", "Interrupt", "Rewind",
  "Compact", "Send", "Toggle", "Pin", "Unpin", "Focus", "Search",
  "Jump", "Attach", "Detach", "Speak", "Record", "Reset", "Install",
  "Refresh", "Edit", "Save", "Move", "Add", "Remove", "Enable",
  "Disable", "Invoke", "Steer",
];

var SH_DESTRUCTIVE_DAMPENER = 0.35;

function SH_hasWord(list, word) {
  for (var i = 0; i < list.length; i++) if (list[i] === word) return true;
  return false;
}

// Returns null when the label is fine, otherwise the reason.
function SH_lintVerbLabel(label) {
  var text = String(label == null ? "" : label).trim();
  if (!text) return "empty label";
  var words = text.split(/\s+/);
  if (words.length < 2) {
    return "label " + JSON.stringify(label) + " is not verb-noun";
  }
  if (!SH_hasWord(SH_VERB_WORDS, words[0])) {
    return "label " + JSON.stringify(label) + " does not open with a known verb";
  }
  for (var i = 0; i < words.length; i++) {
    var w = words[i];
    var bare = w.charAt(0) === "(" ? w.slice(1) : w;
    if (!/^[A-Z]/.test(bare)) {
      return "label " + JSON.stringify(label) + " is not Title Case";
    }
  }
  return null;
}

function SH_displayLabel(verb) {
  if (!verb.aliases || !verb.aliases.length) return verb.label;
  return verb.label + " (" + verb.aliases.join(", ") + ")";
}

function SH_createVerbRegistry() {
  var byId = {};
  var order = [];

  function register(verb) {
    if (!verb || !verb.id) throw new Error("a verb needs an id");
    if (byId[verb.id]) throw new Error("duplicate verb id " + verb.id);
    var reason = SH_lintVerbLabel(verb.label);
    if (reason) throw new Error(reason);
    var surfaces = verb.surfaces || [];
    var pointer = false;
    for (var i = 0; i < surfaces.length; i++) {
      if (SH_SURFACES.indexOf(surfaces[i]) < 0) {
        throw new Error("unknown surface " + surfaces[i]);
      }
      if (surfaces[i] !== "palette") pointer = true;
    }
    if (!pointer) {
      throw new Error(
        "verb " + verb.id + " declares no pointer surface; nothing is "
        + "palette-only (dual-render rule)"
      );
    }
    if (typeof verb.run !== "function") {
      throw new Error("verb " + verb.id + " has no run()");
    }
    var stored = {
      id: verb.id,
      label: SH_displayLabel(verb),
      canonical: verb.label,
      aliases: verb.aliases || [],
      weight: verb.weight === undefined ? 1 : verb.weight,
      destructive: !!verb.destructive,
      contexts: verb.contexts || null,
      // Whether the verb needs a session that is still going. Interrupt
      // and Park mean nothing once one has ended, and a menu that still
      // offers them says the shell has not noticed. The stored shape is
      // an explicit whitelist, so a field that is not listed here is
      // silently dropped and any gate reading it never fires.
      requiresLive: !!verb.requiresLive,
      surfaces: surfaces,
      chord: verb.chord || null,
      run: verb.run,
    };
    byId[verb.id] = stored;
    order.push(stored);
    return stored;
  }

  return {
    register: register,
    get: function (id) { return byId[id] || null; },
    all: function () { return order.slice(); },
    ids: function () { return order.map(function (v) { return v.id; }); },
    forSurface: function (name) {
      return order.filter(function (v) { return v.surfaces.indexOf(name) >= 0; });
    },
  };
}

// Subsequence match with a contiguity bonus. 0 means "no match at all",
// which the ranker treats as exclusion once a query is present.
function SH_fuzzyScore(query, text) {
  var q = String(query || "").toLowerCase();
  var t = String(text || "").toLowerCase();
  if (!q) return 1;
  var ti = 0;
  var hits = 0;
  var streak = 0;
  var best = 0;
  for (var qi = 0; qi < q.length; qi++) {
    var ch = q.charAt(qi);
    if (ch === " ") { streak = 0; continue; }
    var found = -1;
    for (var k = ti; k < t.length; k++) {
      if (t.charAt(k) === ch) { found = k; break; }
    }
    if (found < 0) return 0;
    if (found === ti) { streak += 1; } else { streak = 1; }
    if (streak > best) best = streak;
    hits += 1;
    ti = found + 1;
  }
  return (hits / q.length) * (1 + best / (q.length + 1));
}

function SH_bestFuzzy(verb, query) {
  var score = SH_fuzzyScore(query, verb.canonical);
  for (var i = 0; i < verb.aliases.length; i++) {
    var alt = SH_fuzzyScore(query, verb.aliases[i]);
    if (alt > score) score = alt;
  }
  return score;
}

function SH_rankVerbs(registry, query, ctx) {
  var context = ctx || {};
  var frecency = context.frecency || null;
  var preferred = frecency && query ? frecency.preferredFor(query) : null;
  var rows = [];
  var all = registry.all();
  for (var i = 0; i < all.length; i++) {
    var verb = all[i];
    if (verb.contexts && verb.contexts.indexOf(context.docKind) < 0) continue;
    var fuzzy = SH_bestFuzzy(verb, query);
    if (query && fuzzy <= 0) continue;
    var score = verb.weight * fuzzy;
    if (verb.destructive) score *= SH_DESTRUCTIVE_DAMPENER;
    if (frecency) score *= 1 + frecency.scoreFor(verb.id);
    if (preferred && preferred === verb.id) score *= 4;
    rows.push({ verb: verb, score: score, order: i });
  }
  rows.sort(function (a, b) {
    if (b.score !== a.score) return b.score - a.score;
    return a.order - b.order;
  });
  return rows.map(function (r) { return r.verb; });
}

// Client-side frecency plus query-to-target memory. Injected clock so the
// decay is testable without sleeping.
function SH_createFrecency(now) {
  var clock = typeof now === "function" ? now : function () { return Date.now(); };
  var hits = {};
  var memory = {};
  var HALF_LIFE_MS = 1000 * 60 * 60 * 24 * 7;

  return {
    record: function (id) {
      if (!id) return;
      var entry = hits[id] || { count: 0, at: clock() };
      entry.count += 1;
      entry.at = clock();
      hits[id] = entry;
    },
    remember: function (query, id) {
      var q = String(query || "").toLowerCase().trim();
      if (!q || !id) return;
      memory[q] = id;
    },
    scoreFor: function (id) {
      var entry = hits[id];
      if (!entry) return 0;
      var age = Math.max(0, clock() - entry.at);
      return entry.count * Math.pow(0.5, age / HALF_LIFE_MS);
    },
    preferredFor: function (query) {
      var q = String(query || "").toLowerCase().trim();
      return memory[q] || null;
    },
  };
}

// A verb is registered once, on the shell's first render, but it runs
// much later. A closure over that first render's shell object therefore
// reads state as it was at mount: an empty tab list, no sessions, the
// starting workspace. Every session verb resolved its target from
// shell.docs and so found nothing, which is why Close Session, Park
// Session and Interrupt Session all silently did nothing rather than
// failing loudly.
//
// SH_liveShell returns a stand-in whose every property read delegates to
// the CURRENT render's object, so registration stays one-shot while the
// verbs still see the shell as it is when the operator invokes them.
// The shell object is built from a fixed literal each render, so its key
// set does not change and reading them once is enough.
function SH_liveShell(ref) {
  var view = {};
  var keys = Object.keys(ref.current || {});
  for (var i = 0; i < keys.length; i++) {
    (function (key) {
      Object.defineProperty(view, key, {
        enumerable: true,
        get: function () { return ref.current[key]; },
      });
    })(keys[i]);
  }
  return view;
}

window.SH_liveShell = SH_liveShell;
window.SH_SURFACES = SH_SURFACES;
window.SH_VERB_WORDS = SH_VERB_WORDS;
window.SH_DESTRUCTIVE_DAMPENER = SH_DESTRUCTIVE_DAMPENER;
window.SH_lintVerbLabel = SH_lintVerbLabel;
window.SH_createVerbRegistry = SH_createVerbRegistry;
window.SH_fuzzyScore = SH_fuzzyScore;
window.SH_rankVerbs = SH_rankVerbs;
window.SH_createFrecency = SH_createFrecency;
