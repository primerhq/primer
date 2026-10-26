/* The canvas frame's trusted renderer (pivot P5, Tasks 16 and 17; design 8.6, plan Decisions 6 and 10).
 *
 * This file runs in the canvas frame: the static page of GET /canvas/frame, served with its own CSP header (primer/canvas/frame.py)
 * and sandboxed into an opaque origin (no frame of the canvas is ever given its origin). It draws the agent's elements with real
 * controls and TEXT NODES ONLY: no string from the console or the agent is ever parsed as HTML here. Agent HTML exists only as the
 * srcdoc of a nested frame (an Html element); an about:srcdoc document clones its creator's policy container, so the header's policy
 * binds it too, and the one <meta> policy the renderer writes into it (CV.metaCsp, before any agent content) can only narrow its media
 * (a second policy is enforced alongside the first, never instead of it). The header is the only script control (audit G8-06).
 *
 * The bridge. Both sides validate every message and drop what does not fit.
 *  - Handshake: the console posts {type: "primer.canvas.connect", protocol: 1} to this window with targetOrigin "*" and ONE transferred
 *    MessagePort. The renderer takes the first such message whose event.source is window.parent, and drops every later one and every
 *    other window message (an Html frame's parent.postMessage included: it is open, so it is never trusted).
 *  - Console to frame, on that port: {type: "state", rev, elements, values, settings: {media_policy, media_allowlist, strict_mode},
 *    file_urls, own_origin, theme, run_html} (at most 2 MiB as JSON); {type: "busy", element, on}; {type: "theme", theme}.
 *    run_html (the lead's ruling 5 on S7): the console's word that THIS viewer may run the canvas's Html at once (S8 sets it true only
 *    for the session's creator). Absent or anything but true renders every Html element inert behind a Run button; strict_mode renders
 *    it inert whatever run_html says. A Run click holds for that element and that body for the page load: a changed body asks again.
 *  - Frame to console: {type: "ready", protocol: 1}; {type: "event", element, kind, value?, name?, data?, position?};
 *    {type: "open_link", element?, url} (element names a Link element; an Html element's openLink carries none, as Html sends no
 *    "open" event); {type: "copy", element, text} (at most 10000 characters); {type: "error", message} (at most 500 characters).
 *    At most 16 KiB each and 20 a second (a token bucket); the rest are dropped. Errors also go out at most 3 at once and then one
 *    a second, so an element that throws in a loop cannot crowd out a person's clicks.
 *  - Renderer to an Html frame: {type: "primer.html.connect", element} with one transferred port on each load of the frame (its
 *    bootstrap takes the first offer whose event.source is its parent), then {type: "value", value}, {type: "theme", theme} and
 *    {type: "error", message} (a call the click gate dropped) on it.
 *  - Html frame to renderer, on the port it was given: {type: "emit", name, data}, {type: "set_value", value}, {type: "copy", text},
 *    {type: "open_link", url}, {type: "size", height}, {type: "error", message} (CV.validateHtmlMessage). A message on an element's port
 *    speaks for that element only, and a port the renderer has replaced is no longer heard.
 *
 * The click gate (audit G8-05, the lead's ruling 1 on S7, the review of #925). Agent code shares its frame's realm with the bootstrap
 * and can take the port, so the bootstrap's own check is advisory. The renderer forwards an Html emit, copy or open_link only while
 * THIS document's navigator.userActivation.isActive holds (a click or key press inside an Html frame activates its ancestors, this
 * frame among them), the element's frame is this document's activeElement and the page is visible; and an activation is not every
 * frame's: input on the renderer's own controls claims it for the renderer, and the first Html frame that passes owns it, so a
 * frame that takes the focus by script gets nothing from a click it did not receive. The owner gets at most EMIT_BUDGET calls per
 * activation, which is one continuous isActive window (a few seconds after the last input), not one click. A dropped call is
 * answered with {type: "error"} on the element's port, and counted to the console. A browser without navigator.userActivation
 * forwards none. An Html setValue needs no activation: it is sent at most once a second across the canvas, the latest value of each
 * element kept, so a looping element cannot spend the session's silent budget.
 *
 * The pure part (CV.validateHostMessage, CV.mediaDecision, CV.inlineMarkup, CV.linkTarget, CV.themeCss, CV.selectOptions,
 * CV.metaCsp, CV.htmlSrcdoc and the small machines below) needs no DOM; tests/ui/test_canvas_renderer.py runs it in MiniRacer.
 * CV.boot(document, window) is the DOM side.
 */
(function (root) {
  "use strict";

  var PROTOCOL = 1;
  var TYPES = ["Column", "Row", "Card", "Divider", "Text", "Button", "Select", "Image", "Audio", "Link", "Html"];
  var CONTAINERS = {Column: true, Row: true, Card: true};
  var ALIGNS = ["start", "center", "end", "stretch"];
  var TEXT_STYLES = ["h1", "h2", "h3", "body", "caption", "code"];
  var BUTTON_STYLES = ["primary", "secondary", "danger"];
  var FITS = ["contain", "cover"];
  var POLICIES = ["any-https", "allowlist", "none"];

  //: An element id (primer.canvas.lang.IDENT_RE) and the words that mean a value.
  var ID_RE = /^[a-z][a-z0-9_]{0,31}$/;
  var RESERVED_IDS = {"true": 1, "false": 1, "null": 1};
  //: The most elements a canvas holds (CanvasSettings.max_elements at its ceiling) and the root.
  var MAX_ELEMENTS = 1001;

  var MAX_STATE_BYTES = 2 * 1024 * 1024;
  var MAX_STATE_NODES = 400000;
  var MAX_THEME_PAIRS = 40;
  var MAX_OUT_BYTES = 16 * 1024;
  var MAX_HTML_MESSAGE_BYTES = 16 * 1024;
  var OUT_PER_SECOND = 20;
  //: Error reports (the renderer's own and an Html element's) have their own, smaller budget, so an element that throws in a loop
  //: cannot spend the bucket a person's clicks go out on.
  var ERRORS_PER_SECOND = 1;
  var ERROR_BURST = 3;
  var EMIT_BUDGET = 3;
  var EMIT_NAME_RE = /^[A-Za-z0-9_.:-]{1,64}$/;
  var MAX_EMIT_DATA_BYTES = 4096;
  var MAX_VALUE_CHARS = 1000;
  var MAX_COPY_CHARS = 10000;
  var MAX_ERROR_CHARS = 500;
  var MAX_URL_CHARS = 2048;
  var MAX_JSON_DEPTH = 32;
  //: A float of this magnitude or more does not round-trip through the canvas row (primer.canvas.lang.MAX_FLOAT).
  var MAX_NUMBER = 1e15;
  var MAX_POSITION_SECONDS = 86400;
  var SET_VALUE_INTERVAL_MS = 1000;
  var BUSY_TIMEOUT_MS = 30000;
  var PENDING_CHOICE_MS = 10000;
  var ACTIVATION_POLL_MS = 100;
  //: The trusted input events on the renderer's own document that claim an activation for the renderer (the activation-triggering
  //: input events of the HTML Standard, and mousedown for a browser that sends no pointer events).
  var RENDERER_INPUTS = ["pointerdown", "pointerup", "mousedown", "keydown", "touchend"];
  var DROPPED_REPORT_MS = 1000;
  //: What an Html element hears when the gate drops a call, and what the console is told (the exact copy).
  var GATE_REFUSAL = "primer.{fn} was not sent: it needs a click or a key press inside this element, and at most 3 go out in one " +
    "activation (a few seconds after the input)";
  var DROPPED_REPORT = "{n} Html call(s) were not sent: an Html element's emit, copy and openLink go out only right after a click or " +
    "a key press inside it, at most 3 per activation";
  var HTML_CALL = {emit: "emit", copy: "copy", open_link: "openLink"};
  var HTML_AUTO_MIN = 40;
  var HTML_AUTO_MAX = 1200;
  var HTML_FIRST_HEIGHT = 150;
  //: Bounds of the strings the renderer reads, in UTF-16 units: twice the catalog's characters (an astral character is two units).
  var MAX_LABEL = 160;
  var MAX_TITLE = 400;
  var MAX_ALT = 600;
  var MAX_TEXT = 20000;
  var MAX_SRC = 70000;
  var MAX_HTML = 262144;
  var MAX_OPTION_VALUE = 400;
  var MAX_OPTIONS = 100;

  //: The exact copy of the placeholders (plan Task 16) and of strict mode (Task 17).
  var COPY = {
    loadImage: "Load image from {host}",
    loadAudio: "Load audio from {host}",
    blocked: "Media from {host} is blocked by this install's canvas policy",
    missing: "This file is not available",
    inert: "This element runs code the agent wrote.",
    run: "Run",
    choose: "Choose an option",
    badLink: "This link is not valid"
  };

  var ORIGIN_RE = /^https?:\/\/(?:[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?|\[[0-9a-f:.]{2,45}\])(?::[0-9]{1,5})?$/i;
  //: An allowlist entry (primer.model.canvas._HOST_RE).
  var ALLOW_HOST_RE = /^(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/;
  var MAX_ALLOW_HOSTS = 100;
  //: A minted capability URL (primer.canvas.files.mint_file_url): a path under /canvas/sessions/, never another host.
  var FILE_URL_RE = /^\/canvas\/sessions\/[^\/?#\\\s"'<>]{1,256}\/files\/[A-Za-z0-9_.~-]{1,2048}$/;
  var FILE_KEY_MAX = 600;
  var DATA_URL_RE = /^data:(?:image|audio)\/[a-z0-9.+-]+;base64,/i;
  var URL_RE = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([^\/?#]*)([\/?#].*)?$/;
  var HOST_PORT_RE = /^([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*|\[[0-9a-f:.]{2,45}\])(?::([0-9]{1,5}))?$/;
  var URL_BAD_CHARS = /[\s\\\u0000-\u001f\u007f]/;
  //: A NUL, or a lone surrogate: neither can be stored (primer.canvas.catalog.has_unstorable_char).
  var UNSTORABLE = /\u0000|[\ud800-\udbff](?![\udc00-\udfff])|(?:^|[^\ud800-\udbff])[\udc00-\udfff]/;
  var SCRIPT_BREAK_RE = /<\/script|<!--/i;
  //: The CSP3 directive that refuses WebRTC in an Html frame (where a browser knows it).
  var WEBRTC_BLOCK = "webrtc 'block'";

  var THEME_NAME_RE = /^--cv-[a-z0-9-]{1,40}$/;
  var THEME_VALUE_RE = /^[^;{}<>\\\r\n]{0,200}$/;
  //: The CSS functions a theme value may call: colours and arithmetic, nothing that loads anything.
  var CSS_FUNCTIONS = {"": 1, rgb: 1, rgba: 1, hsl: 1, hsla: 1, hwb: 1, lab: 1, lch: 1, oklab: 1, oklch: 1, color: 1, "color-mix": 1,
    calc: 1, "var": 1, min: 1, max: 1, clamp: 1};
  //: The base style of an Html frame, before the theme.
  var HTML_BASE_CSS = "html,body{margin:0;padding:0;background:transparent}" +
    "body{font:14px/1.45 var(--cv-font,system-ui,sans-serif);color:var(--cv-fg,inherit)}";

  function has(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }

  function isPlainObject(v) {
    if (v === null || typeof v !== "object" || Array.isArray(v)) return false;
    var proto = Object.getPrototypeOf(v);
    return proto === Object.prototype || proto === null;
  }

  function isId(v) { return typeof v === "string" && ID_RE.test(v) && !has(RESERVED_IDS, v); }

  function isCount(v) { return typeof v === "number" && Number.isSafeInteger(v) && v >= 0; }

  function clip(text, max) { return text.length > max ? text.slice(0, max) : text; }

  //: The UTF-8 length of a string, without TextEncoder (MiniRacer has none).
  function utf8Length(s) {
    var n = 0;
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      if (c < 0x80) n += 1;
      else if (c < 0x800) n += 2;
      else if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length && (s.charCodeAt(i + 1) & 0xfc00) === 0xdc00) { n += 4; i += 1; }
      else n += 3;
    }
    return n;
  }

  //: Bytes of the compact JSON of v, or -1 when it has none.
  function jsonBytes(v) {
    var s;
    try { s = JSON.stringify(v); } catch (e) { return -1; }
    return typeof s === "string" ? utf8Length(s) : -1;
  }

  //: Whether v is JSON data a canvas can hold: null, booleans, finite numbers under 1e15, strings without a NUL or a lone surrogate,
  //: arrays and plain objects, at most maxDepth deep, maxNodes values and maxChars characters of strings and keys in all (a
  //: structured clone can share and cycle). The bounds are checked BEFORE the work they bound: a string's length before it is
  //: scanned, an array's length and an object's key count before they are walked, so an over-long value costs nothing to refuse,
  //: and no caller stringifies or measures a value this has not passed. (A UTF-8 byte is at least one character, so a value over
  //: maxChars characters is over maxChars bytes of JSON too.)
  function isJsonData(value, maxDepth, maxNodes, maxChars) {
    var count = 0;
    var chars = 0;
    function text(s) {
      chars += s.length;
      return chars <= maxChars && !UNSTORABLE.test(s);
    }
    function walk(v, depth) {
      count += 1;
      if (count > maxNodes) return false;
      if (v === null || typeof v === "boolean") return true;
      if (typeof v === "number") return isFinite(v) && Math.abs(v) < MAX_NUMBER;
      if (typeof v === "string") return text(v);
      if (depth >= maxDepth) return false;
      var i;
      if (Array.isArray(v)) {
        if (v.length > maxNodes - count) return false;
        for (i = 0; i < v.length; i++) if (!walk(v[i], depth + 1)) return false;
        return true;
      }
      if (!isPlainObject(v)) return false;
      var keys = Object.keys(v);
      if (keys.length > maxNodes - count) return false;
      for (i = 0; i < keys.length; i++) {
        if (!text(keys[i]) || !walk(v[keys[i]], depth + 1)) return false;
      }
      return true;
    }
    return walk(value, 0);
  }

  function jsonEqual(a, b) {
    if (a === b) return true;
    try { return JSON.stringify(a) === JSON.stringify(b); } catch (e) { return false; }
  }

  function str(v, max, fallback) { return typeof v === "string" && v.length <= max ? v : fallback; }

  function num(v, lo, hi, fallback) { return typeof v === "number" && isFinite(v) && v >= lo && v <= hi ? v : fallback; }

  function oneOf(v, allowed, fallback) { return allowed.indexOf(v) >= 0 ? v : fallback; }

  // ---------------------------------------------------------------------------------------------------------------------------------
  // URLs, links and media
  // ---------------------------------------------------------------------------------------------------------------------------------

  //: {url, scheme, hostname, host} of an absolute URL of one of `schemes` with a plain host (letters, digits, dots and dashes, or a
  //: bracketed IPv6 address) and an optional port; null for anything else (userinfo, spaces, a backslash, a control character).
  function parseUrl(value, schemes) {
    if (typeof value !== "string" || value.length > MAX_URL_CHARS || URL_BAD_CHARS.test(value)) return null;
    var m = URL_RE.exec(value);
    if (!m || schemes.indexOf(m[1].toLowerCase()) < 0) return null;
    var authority = m[2].toLowerCase();
    var hp = HOST_PORT_RE.exec(authority);
    if (!hp || (hp[2] !== undefined && Number(hp[2]) > 65535)) return null;
    return {url: value, scheme: m[1].toLowerCase(), hostname: hp[1], host: authority, port: hp[2] === undefined ? "" : hp[2]};
  }

  function cleanAllowlist(list) {
    var out = [];
    if (!Array.isArray(list)) return out;
    for (var i = 0; i < list.length && out.length < MAX_ALLOW_HOSTS; i++) {
      var host = list[i];
      if (typeof host !== "string") continue;
      host = host.trim().toLowerCase();
      if (host.length <= 253 && ALLOW_HOST_RE.test(host) && out.indexOf(host) < 0) out.push(host);
    }
    return out;
  }

  function allowlisted(hostname, list) {
    var hosts = cleanAllowlist(list);
    for (var i = 0; i < hosts.length; i++) {
      var entry = hosts[i];
      if (entry.slice(0, 2) === "*.") {
        var suffix = entry.slice(1);
        if (hostname.length > suffix.length && hostname.slice(-suffix.length) === suffix) return true;
      } else if (hostname === entry) {
        return true;
      }
    }
    return false;
  }

  //: The key a media source is drawn and kept by: a workspace file is "file:<path>" (the scheme in any case, as the server matches it),
  //: never its minted URL, which changes every 5-minute window (the lead's ruling after S6).
  function mediaKey(src) {
    if (typeof src !== "string") return "";
    return src.slice(0, 5).toLowerCase() === "file:" ? "file:" + src.slice(5) : src;
  }

  function mediaDecision(src, settings, fileUrls, ownOrigin) {
    if (typeof src !== "string" || !src) return {kind: "missing"};
    var head = src.slice(0, 5).toLowerCase();
    if (head === "file:") {
      var key = mediaKey(src);
      var url = isPlainObject(fileUrls) && key.length <= FILE_KEY_MAX && has(fileUrls, key) ? fileUrls[key] : null;
      return typeof url === "string" && FILE_URL_RE.test(url) ? {kind: "load", url: url} : {kind: "missing"};
    }
    if (head === "data:") return DATA_URL_RE.test(src) ? {kind: "load", url: src} : {kind: "missing"};
    var target = parseUrl(src, ["https"]);
    if (!target) return {kind: "missing"};
    var policy = settings && settings.media_policy;
    if (policy === "any-https") return {kind: "load", url: target.url};
    if (policy === "allowlist") {
      // An allowlist entry becomes the source https://<host> in an Html frame's policy, which is port 443 only: a URL that names
      // another port is refused here too, so the frame and the renderer agree (review of #925).
      if (target.port !== "" && target.port !== "443") return {kind: "blocked", host: target.host};
      return allowlisted(target.hostname, settings.media_allowlist) ? {kind: "load", url: target.url} : {kind: "click", host: target.host};
    }
    return {kind: "blocked", host: target.host};
  }

  function linkTarget(href) {
    var target = parseUrl(href, ["http", "https"]);
    return target ? {url: target.url, host: target.host} : null;
  }

  // ---------------------------------------------------------------------------------------------------------------------------------
  // Text, options and the theme
  // ---------------------------------------------------------------------------------------------------------------------------------

  var WORD_CHAR = /[\p{L}\p{N}]/u;
  function isSpace(c) { return c === " " || c === "\t" || c === "\n" || c === "\r"; }

  //: [[style, text], ...] for **bold**, _italic_ and `code` (style "b", "i", "code"; "" for plain text). A marker without its pair, an
  //: empty span, a span across a line break, and an underscore inside a word (snake_case) stay literal; newlines are kept for the
  //: renderer to turn into line breaks.
  function inlineMarkup(text) {
    var out = [];
    if (typeof text !== "string") return out;
    var buf = "";
    var i = 0;
    var n = text.length;
    function flush() { if (buf) { out.push(["", buf]); buf = ""; } }
    function closeAt(marker, from) {
      var j = text.indexOf(marker, from);
      if (j <= from) return -1;
      var body = text.slice(from, j);
      return body.indexOf("\n") < 0 ? j : -1;
    }
    while (i < n) {
      var c = text.charAt(i);
      var j;
      if (c === "`") {
        j = closeAt("`", i + 1);
        if (j > 0) { flush(); out.push(["code", text.slice(i + 1, j)]); i = j + 1; continue; }
      } else if (c === "*" && text.charAt(i + 1) === "*" && i + 2 < n && !isSpace(text.charAt(i + 2))) {
        j = closeAt("**", i + 2);
        if (j > 0 && isSpace(text.charAt(j - 1))) j = -1;
        if (j > 0) { flush(); out.push(["b", text.slice(i + 2, j)]); i = j + 2; continue; }
      } else if (c === "_" && (i === 0 || !WORD_CHAR.test(text.charAt(i - 1))) && i + 1 < n && !isSpace(text.charAt(i + 1)) &&
                 text.charAt(i + 1) !== "_") {
        j = i + 1;
        var found = -1;
        while ((j = text.indexOf("_", j)) > i + 1) {
          if (text.slice(i + 1, j).indexOf("\n") >= 0) break;
          if (!isSpace(text.charAt(j - 1)) && (j + 1 >= n || !WORD_CHAR.test(text.charAt(j + 1)))) { found = j; break; }
          j += 1;
        }
        if (found > 0) { flush(); out.push(["i", text.slice(i + 1, found)]); i = found + 1; continue; }
      }
      buf += c;
      i += 1;
    }
    flush();
    return out;
  }

  //: [{value, label}] of a Select's options: strings or [value, label] pairs of strings; a malformed or repeated option is left out.
  function selectOptions(options) {
    var out = [];
    if (!Array.isArray(options)) return out;
    var seen = Object.create(null);
    for (var i = 0; i < options.length && out.length < MAX_OPTIONS; i++) {
      var o = options[i];
      var value;
      var label;
      if (typeof o === "string") {
        value = o;
        label = o;
      } else if (Array.isArray(o) && o.length === 2 && typeof o[0] === "string" && typeof o[1] === "string") {
        value = o[0];
        label = o[1];
      } else {
        continue;
      }
      if (value.length > MAX_OPTION_VALUE || label.length > MAX_OPTION_VALUE || seen[value]) continue;
      seen[value] = true;
      out.push({value: value, label: label});
    }
    return out;
  }

  function themeValueOk(v) {
    if (typeof v !== "string" || !THEME_VALUE_RE.test(v) || v.indexOf("/*") >= 0 || v.indexOf("*/") >= 0) return false;
    if (/url\s*\(/i.test(v)) return false;
    var call = /([A-Za-z-]*)\s*\(/g;
    var m;
    while ((m = call.exec(v)) !== null) {
      if (!has(CSS_FUNCTIONS, m[1].toLowerCase())) return false;
    }
    return quotesClose(v);
  }

  //: Whether every quoted string in a theme value is closed. Parsed, not counted: "'"' has two of each quote and still leaves a
  //: string open. A backslash and a line break are refused before this, so a string ends at the next quote of its own kind.
  function quotesClose(v) {
    for (var i = 0; i < v.length; i++) {
      var c = v.charAt(i);
      if (c === '"' || c === "'") {
        var end = v.indexOf(c, i + 1);
        if (end < 0) return false;
        i = end;
      }
    }
    return true;
  }

  //: The theme's [name, value] pairs a frame may use: only --cv-* names, values matching ^[^;{}<>]{0,200}$ with no url(, no
  //: backslash (a CSS escape could spell one), no comment and no function but a colour or arithmetic one. Others are dropped.
  function themePairs(theme) {
    var out = [];
    if (!isPlainObject(theme)) return out;
    var names = Object.keys(theme).sort();
    for (var i = 0; i < names.length && out.length < MAX_THEME_PAIRS; i++) {
      var name = names[i];
      if (THEME_NAME_RE.test(name) && themeValueOk(theme[name])) out.push([name, theme[name].trim()]);
    }
    return out;
  }

  function themeObject(theme) {
    var pairs = themePairs(theme);
    var out = Object.create(null);
    for (var i = 0; i < pairs.length; i++) out[pairs[i][0]] = pairs[i][1];
    return out;
  }

  function themeCss(theme) {
    var pairs = themePairs(theme);
    if (!pairs.length) return "";
    var body = "";
    for (var i = 0; i < pairs.length; i++) body += pairs[i][0] + ":" + pairs[i][1] + ";";
    return ":root{" + body + "}";
  }

  // ---------------------------------------------------------------------------------------------------------------------------------
  // Messages
  // ---------------------------------------------------------------------------------------------------------------------------------

  function normalizeSettings(raw) {
    if (!isPlainObject(raw) || POLICIES.indexOf(raw.media_policy) < 0 || typeof raw.strict_mode !== "boolean") return null;
    return {media_policy: raw.media_policy, media_allowlist: cleanAllowlist(raw.media_allowlist), strict_mode: raw.strict_mode};
  }

  function choiceValue(v) {
    if (typeof v === "string") return v.length <= MAX_OPTION_VALUE ? v : null;
    if (!Array.isArray(v) || v.length > MAX_OPTIONS) return null;
    for (var i = 0; i < v.length; i++) if (typeof v[i] !== "string" || v[i].length > MAX_OPTION_VALUE) return null;
    return v.slice();
  }

  //: {type, props, children} of a stored element with every prop the renderer reads checked and defaulted, or null for an element
  //: it cannot draw. An unknown prop is left out; a bad one reads as its default.
  function normalizeElement(raw) {
    if (!isPlainObject(raw) || TYPES.indexOf(raw.type) < 0) return null;
    var p = isPlainObject(raw.props) ? raw.props : {};
    var type = raw.type;
    var props = {hidden: p.hidden === true, disabled: p.disabled === true};
    if (type === "Column" || type === "Row") {
      props.gap = num(p.gap, 0, 64, 8);
      props.align = oneOf(p.align, ALIGNS, type === "Row" ? "center" : "stretch");
      if (type === "Row") props.wrap = typeof p.wrap === "boolean" ? p.wrap : true;
    } else if (type === "Card") {
      props.title = str(p.title, MAX_TITLE, null);
    } else if (type === "Text") {
      props.text = str(p.text, MAX_TEXT, "");
      props.style = oneOf(p.style, TEXT_STYLES, "body");
    } else if (type === "Button") {
      props.label = str(p.label, MAX_LABEL, "");
      props.style = oneOf(p.style, BUTTON_STYLES, "secondary");
    } else if (type === "Select") {
      props.label = str(p.label, MAX_LABEL, "");
      props.options = selectOptions(p.options);
      props.multiple = p.multiple === true;
      props.value = choiceValue(p.value);
    } else if (type === "Image") {
      props.src = str(p.src, MAX_SRC, "");
      props.alt = str(p.alt, MAX_ALT, "");
      props.fit = oneOf(p.fit, FITS, "contain");
      props.height = num(p.height, 16, 2000, null);
    } else if (type === "Audio") {
      props.src = str(p.src, MAX_SRC, "");
      props.title = str(p.title, MAX_TITLE, null);
      props.loop = p.loop === true;
    } else if (type === "Link") {
      props.label = str(p.label, MAX_LABEL, "");
      props.href = str(p.href, MAX_URL_CHARS, "");
    } else if (type === "Html") {
      props.html = str(p.html, MAX_HTML, "");
      props.height = num(p.height, 20, 2000, null);
    }
    var children = [];
    if (CONTAINERS[type] && Array.isArray(raw.children)) {
      for (var i = 0; i < raw.children.length && children.length < MAX_ELEMENTS; i++) {
        var child = raw.children[i];
        if (isId(child) && child !== "root" && children.indexOf(child) < 0) children.push(child);
      }
    }
    return {type: type, props: props, children: children};
  }

  function validateState(msg) {
    if (!isJsonData(msg, MAX_JSON_DEPTH + 8, MAX_STATE_NODES, MAX_STATE_BYTES)) return null;
    var size = jsonBytes(msg);
    if (size < 0 || size > MAX_STATE_BYTES) return null;
    if (!isCount(msg.rev) || !isPlainObject(msg.elements) || !isPlainObject(msg.values)) return null;
    var settings = normalizeSettings(msg.settings);
    if (!settings) return null;
    var ids = Object.keys(msg.elements);
    if (ids.length > MAX_ELEMENTS) return null;
    var elements = Object.create(null);
    var i;
    for (i = 0; i < ids.length; i++) {
      if (!isId(ids[i])) continue;
      var element = normalizeElement(msg.elements[ids[i]]);
      if (element) elements[ids[i]] = element;
    }
    if (!elements.root || elements.root.type !== "Column") return null;
    var values = Object.create(null);
    var valueIds = Object.keys(msg.values);
    for (i = 0; i < valueIds.length; i++) {
      if (isId(valueIds[i])) values[valueIds[i]] = msg.values[valueIds[i]];
    }
    var fileUrls = Object.create(null);
    if (msg.file_urls !== undefined && msg.file_urls !== null) {
      if (!isPlainObject(msg.file_urls)) return null;
      var keys = Object.keys(msg.file_urls);
      for (i = 0; i < keys.length; i++) {
        var key = keys[i];
        var url = msg.file_urls[key];
        if (key.slice(0, 5) === "file:" && key.length > 5 && key.length <= FILE_KEY_MAX && typeof url === "string" && FILE_URL_RE.test(url)) {
          fileUrls[key] = url;
        }
      }
    }
    var own = typeof msg.own_origin === "string" && ORIGIN_RE.test(msg.own_origin) ? msg.own_origin : "";
    return {
      type: "state",
      rev: msg.rev,
      elements: elements,
      values: values,
      settings: settings,
      file_urls: fileUrls,
      own_origin: own,
      theme: isPlainObject(msg.theme) ? themeObject(msg.theme) : null,
      run_html: msg.run_html === true
    };
  }

  //: A message from the console, checked and copied (only what the renderer reads), or null.
  function validateHostMessage(msg) {
    if (!isPlainObject(msg) || typeof msg.type !== "string") return null;
    if (msg.type === "state") return validateState(msg);
    if (msg.type === "busy") {
      if (!isId(msg.element) || typeof msg.on !== "boolean") return null;
      return {type: "busy", element: msg.element, on: msg.on};
    }
    if (msg.type === "theme") {
      if (!isPlainObject(msg.theme) || !isJsonData(msg.theme, 2, 1000, 20000)) return null;
      return {type: "theme", theme: themeObject(msg.theme)};
    }
    return null;
  }

  //: Whether a window message is THE handshake: framed, from the parent (the console), of the exact shape, with exactly one port.
  function acceptsHandshake(data, source, parent, self, ports) {
    if (!parent || parent === self || source !== parent) return false;
    if (!isPlainObject(data) || data.type !== "primer.canvas.connect" || data.protocol !== PROTOCOL) return false;
    return !!ports && ports.length === 1 && !!ports[0];
  }

  function emitProblem(name, data) {
    if (typeof name !== "string" || !EMIT_NAME_RE.test(name)) {
      return "an emit name is 1 to 64 letters, digits, _ . : or -, and does not start with /";
    }
    if (data === undefined || data === null) return null;
    if (!isJsonData(data, MAX_JSON_DEPTH, 5000, MAX_EMIT_DATA_BYTES)) return "emit data must be JSON of at most " + MAX_EMIT_DATA_BYTES + " bytes";
    var size = jsonBytes(data);
    return size >= 0 && size <= MAX_EMIT_DATA_BYTES ? null : "emit data is at most " + MAX_EMIT_DATA_BYTES + " bytes of JSON";
  }

  //: A message from an Html frame (on the port the renderer gave it), checked and copied, or null.
  function validateHtmlMessage(msg) {
    if (!isPlainObject(msg) || typeof msg.type !== "string") return null;
    if (!isJsonData(msg, MAX_JSON_DEPTH + 2, 5000, MAX_HTML_MESSAGE_BYTES)) return null;
    var size = jsonBytes(msg);
    if (size < 0 || size > MAX_HTML_MESSAGE_BYTES) return null;
    var keys = Object.keys(msg);
    function only(allowed) {
      for (var i = 0; i < keys.length; i++) if (allowed.indexOf(keys[i]) < 0) return false;
      return true;
    }
    if (msg.type === "emit") {
      if (!only(["type", "name", "data"]) || emitProblem(msg.name, msg.data)) return null;
      return {type: "emit", name: msg.name, data: msg.data === undefined ? null : msg.data};
    }
    if (msg.type === "set_value") {
      if (!only(["type", "value"]) || msg.value === undefined) return null;
      var length = JSON.stringify(msg.value).length;
      return length <= MAX_VALUE_CHARS ? {type: "set_value", value: msg.value} : null;
    }
    if (msg.type === "copy") {
      if (!only(["type", "text"]) || typeof msg.text !== "string" || msg.text.length > MAX_COPY_CHARS) return null;
      return {type: "copy", text: msg.text};
    }
    if (msg.type === "open_link") {
      var target = only(["type", "url"]) ? linkTarget(msg.url) : null;
      return target ? {type: "open_link", url: target.url} : null;
    }
    if (msg.type === "size") {
      if (!only(["type", "height"]) || typeof msg.height !== "number" || !isFinite(msg.height) || msg.height < 0) return null;
      return {type: "size", height: msg.height};
    }
    if (msg.type === "error") {
      if (!only(["type", "message"]) || typeof msg.message !== "string") return null;
      return {type: "error", message: clip(msg.message, MAX_ERROR_CHARS)};
    }
    return null;
  }

  // ---------------------------------------------------------------------------------------------------------------------------------
  // Html elements: the narrowing media policy and the srcdoc
  // ---------------------------------------------------------------------------------------------------------------------------------

  //: The <meta> policy an Html frame adds (Decision 10; the lead's WebRTC ruling on #925). Under any-https only webrtc 'block' (the
  //: header's own media rule holds); under allowlist, media only from data:, blob:, the minted-URL origin and the listed hosts (port
  //: 443); under none (and any policy this renderer does not know), only data:, blob: and the minted-URL origin; and webrtc 'block'
  //: under every policy. It names only img-src, media-src, font-src and webrtc, the media ones a subset of the header's, and a second
  //: policy is enforced with the first, so it can only narrow. A browser that does not know the webrtc directive ignores it (Chromium
  //: does, and its WebRTC still sends STUN past connect-src); the bootstrap also removes the RTC constructors, advisory only.
  function metaCsp(settings, ownOrigin) {
    var policy = settings && settings.media_policy;
    if (policy === "any-https") return WEBRTC_BLOCK;
    var own = typeof ownOrigin === "string" && ORIGIN_RE.test(ownOrigin) ? " " + ownOrigin.toLowerCase() : "";
    if (policy === "allowlist") {
      var hosts = "";
      var list = cleanAllowlist(settings.media_allowlist);
      for (var i = 0; i < list.length; i++) hosts += " https://" + list[i];
      return "img-src data: blob:" + own + hosts + "; media-src data: blob:" + own + hosts + "; font-src data:" + hosts + "; " + WEBRTC_BLOCK;
    }
    return "img-src data: blob:" + own + "; media-src data: blob:" + own + "; font-src data:; " + WEBRTC_BLOCK;
  }

  //: The srcdoc of an Html element: the charset, the narrowing media policy, the theme, the bootstrap, and only then the agent's HTML,
  //: so nothing the agent wrote comes before the policy. null when the bootstrap could end its own <script>.
  function htmlSrcdoc(html, opts) {
    var o = opts || {};
    if (typeof html !== "string" || typeof o.bootstrap !== "string" || SCRIPT_BREAK_RE.test(o.bootstrap)) return null;
    var csp = metaCsp(o.settings, o.ownOrigin);
    return '<!doctype html><html><head><meta charset="utf-8">' +
      (csp ? '<meta http-equiv="Content-Security-Policy" content="' + csp + '">' : "") +
      "<style>" + HTML_BASE_CSS + themeCss(o.theme) + "</style>" +
      "<script>" + o.bootstrap + "</script></head><body>" + html + "</body></html>";
  }

  // ---------------------------------------------------------------------------------------------------------------------------------
  // Small machines: the click gate, the outgoing bucket and the setValue throttle
  // ---------------------------------------------------------------------------------------------------------------------------------

  //: The renderer's click gate (audit G8-05; the lead's ruling 1 on S7; the review of #925). A person's input activates the frame
  //: it lands in AND every ancestor, this document among them, so isActive alone says that someone did something, not where. So:
  //:  - input on the renderer's own controls (a pointerdown or keydown this document sees: a click on a native Button, a Tab press
  //:    that moves the focus on into an Html frame) claims the activation for the renderer (claimForRenderer): no Html frame passes
  //:    until isActive has read false again;
  //:  - the first Html frame that passes owns the activation: no other frame gets anything from it (a frame that took the focus by
  //:    script during another element's activation gets nothing);
  //:  - the owner gets at most `budget` passes.
  //: An activation is one continuous isActive window, not one click: it lasts a few seconds after the last input (about 5 s in
  //: Chromium) and further input inside it extends it, so the budget is per window. A window is new only once the gate has read
  //: isActive false since the last one (allow() and observe() both read it; the renderer calls observe() every ACTIVATION_POLL_MS
  //: while a window is open or claimed).
  function makeEmitGate(activation, budget) {
    var spent = 0;
    var open = false;
    var owner = null;
    var claimed = false;
    function active() { return !!activation && activation.isActive === true; }
    function reset() { spent = 0; open = false; owner = null; claimed = false; }
    return {
      allow: function (who) {
        if (!active()) { reset(); return false; }
        if (claimed) return false;
        if (!open) { open = true; spent = 0; owner = who; }
        if (who !== owner || spent >= budget) return false;
        spent += 1;
        return true;
      },
      claimForRenderer: function () { claimed = true; },
      observe: function () { if (!active()) reset(); return open || claimed; },
      watching: function () { return open || claimed; }
    };
  }

  //: A token bucket of `capacity` tokens refilled at `perSecond`: take() is true while a token is left.
  function makeBucket(perSecond, capacity, now) {
    var tokens = capacity;
    var last = now();
    return {
      take: function () {
        var t = now();
        tokens = Math.min(capacity, tokens + Math.max(0, t - last) * perSecond / 1000);
        last = t;
        if (tokens >= 1) { tokens -= 1; return true; }
        return false;
      }
    };
  }

  //: Sends at most one value every intervalMs across all elements, the latest value of each element kept until its turn.
  function makeValueThrottle(opts) {
    var pending = [];
    var timer = null;
    var lastSent = -Infinity;
    function flush() {
      timer = null;
      var entry = pending.shift();
      if (!entry) return;
      lastSent = opts.now();
      opts.send(entry.element, entry.value);
      if (pending.length) timer = opts.setTimer(flush, opts.intervalMs);
    }
    return {
      push: function (element, value) {
        for (var i = 0; i < pending.length; i++) {
          if (pending[i].element === element) { pending[i].value = value; return; }
        }
        pending.push({element: element, value: value});
        if (timer === null) timer = opts.setTimer(flush, Math.max(0, lastSent + opts.intervalMs - opts.now()));
      },
      drop: function (element) {
        pending = pending.filter(function (entry) { return entry.element !== element; });
      },
      pending: function (element) {
        for (var i = 0; i < pending.length; i++) if (pending[i].element === element) return true;
        return false;
      }
    };
  }

  // ---------------------------------------------------------------------------------------------------------------------------------
  // The DOM side
  // ---------------------------------------------------------------------------------------------------------------------------------

  function boot(doc, win) {
    var rootNode = doc && doc.getElementById ? doc.getElementById("cv-root") : null;
    if (!rootNode || !win) return null;

    var now = function () { return win.Date.now(); };
    var bootstrap = typeof win.CV_BOOTSTRAP === "string" ? win.CV_BOOTSTRAP : "";
    var activation = win.navigator ? win.navigator.userActivation : undefined;
    var gate = makeEmitGate(activation, EMIT_BUDGET);
    var gatePoll = null;
    var dropped = Object.create(null);
    var droppedTimer = null;
    var bucket = makeBucket(OUT_PER_SECOND, OUT_PER_SECOND, now);
    var errorBucket = makeBucket(ERRORS_PER_SECOND, ERROR_BURST, now);
    var host = null;
    var current = null;
    var records = Object.create(null);
    var consoleBusy = Object.create(null);
    var localBusy = Object.create(null);
    var approved = Object.create(null);
    var clickedToLoad = Object.create(null);
    //: round 4b instrumentation: the bridge handshake trace. Whether the renderer sent `ready`, how many `state`
    //: messages it got, the run_html of the last one, and the last state's rev/time. Behavior-neutral (it only records).
    //: If a still-red run shows state:0 or runHtml:undefined, the ready->state exchange is the remaining mechanism.
    var bridge = {ready: 0, state: 0, runHtml: null, lastStateRev: null, lastStateAt: 0};
    var knownValue = Object.create(null);
    var themeNow = Object.create(null);
    var throttle = makeValueThrottle({
      intervalMs: SET_VALUE_INTERVAL_MS,
      now: now,
      setTimer: function (fn, ms) { return win.setTimeout(fn, ms); },
      send: function (id, value) {
        var rec = records[id];
        if (!rec || rec.type !== "Html" || rec.unavailable) return;
        knownValue[id] = value;
        send({type: "event", element: id, kind: "set_value", value: value});
      }
    });

    function send(msg) {
      if (!host) return false;
      var size = jsonBytes(msg);
      if (size < 0 || size > MAX_OUT_BYTES || !bucket.take()) return false;
      try {
        host.postMessage(msg);
        return true;
      } catch (e) {
        return false;
      }
    }

    function reportError(text) {
      if (host && errorBucket.take()) send({type: "error", message: clip(String(text), MAX_ERROR_CHARS)});
    }

    function element(tag, cls) {
      var node = doc.createElement(tag);
      if (cls) node.className = cls;
      return node;
    }

    function textNode(text) { return doc.createTextNode(text); }

    function empty(node) { while (node.firstChild) node.removeChild(node.firstChild); }

    function detach(node) { if (node && node.parentNode) node.parentNode.removeChild(node); }

    function isBusy(id) { return !!consoleBusy[id] || !!localBusy[id]; }

    // --- the handshake and the console's port -------------------------------------------------------------------------------------

    win.addEventListener("message", function (ev) {
      if (host) return;
      if (!acceptsHandshake(ev.data, ev.source, win.parent, win, ev.ports)) return;
      host = ev.ports[0];
      host.onmessage = function (portEvent) { onHostMessage(host, portEvent); };
      send({type: "ready", protocol: PROTOCOL});
      bridge.ready += 1;
      bridge.lastStateAt = now();
    });

    win.addEventListener("error", function (ev) {
      if (ev && typeof ev.message === "string") reportError("The canvas renderer failed: " + ev.message);
    });

    // A person's input on the renderer's own controls is the renderer's: it claims the activation it makes, so no Html frame can use
    // it (a Tab press that moves the focus into an Html frame included). Input inside an Html frame never reaches this document.
    function claim(ev) {
      if (!ev || ev.isTrusted !== true) return;
      gate.claimForRenderer();
      watchActivation();
    }
    for (var k = 0; k < RENDERER_INPUTS.length; k++) doc.addEventListener(RENDERER_INPUTS[k], claim, true);

    function onHostMessage(port, ev) {
      if (port !== host) return;
      var msg = validateHostMessage(ev ? ev.data : null);
      if (!msg) return;
      try {
        if (msg.type === "state") {
          bridge.state += 1;
          bridge.runHtml = msg.run_html === true;
          bridge.lastStateRev = msg.rev;
          bridge.lastStateAt = now();
          if (msg.theme) applyTheme(msg.theme);
          render(msg);
        } else if (msg.type === "busy") {
          consoleBusy[msg.element] = msg.on;
          if (!msg.on) clearLocalBusy(msg.element);
          refreshBusy(msg.element);
        } else if (msg.type === "theme") {
          applyTheme(msg.theme);
        }
      } catch (e) {
        reportError("The canvas could not draw: " + (e && e.message ? e.message : String(e)));
      }
    }

    function applyTheme(theme) {
      var style = doc.documentElement ? doc.documentElement.style : null;
      if (!style || jsonEqual(theme, themeNow)) return;
      var names = Object.keys(theme);
      var before = Object.keys(themeNow);
      for (var i = 0; i < before.length; i++) if (names.indexOf(before[i]) < 0) style.removeProperty(before[i]);
      for (i = 0; i < names.length; i++) style.setProperty(names[i], theme[names[i]]);
      themeNow = theme;
      var ids = Object.keys(records);
      for (i = 0; i < ids.length; i++) {
        var rec = records[ids[i]];
        if (rec.type === "Html" && rec.port) postToHtml(rec, {type: "theme", theme: theme});
      }
    }

    // --- busy controls ---------------------------------------------------------------------------------------------------------------

    function setLocalBusy(id) {
      clearLocalBusy(id);
      localBusy[id] = win.setTimeout(function () { delete localBusy[id]; refreshBusy(id); }, BUSY_TIMEOUT_MS);
      refreshBusy(id);
    }

    function clearLocalBusy(id) {
      if (localBusy[id]) {
        win.clearTimeout(localBusy[id]);
        delete localBusy[id];
      }
    }

    function refreshBusy(id) {
      var rec = records[id];
      if (rec) applyBusy(rec);
    }

    function applyBusy(rec) {
      var busy = isBusy(rec.id);
      if (busy) rec.node.setAttribute("aria-busy", "true");
      else rec.node.removeAttribute("aria-busy");
      if (rec.control) rec.control.disabled = rec.el.props.disabled || busy || rec.badTarget === true;
      if (rec.checks) {
        for (var i = 0; i < rec.checks.length; i++) rec.checks[i].disabled = rec.el.props.disabled || busy;
      }
    }

    // --- drawing ---------------------------------------------------------------------------------------------------------------------

    function render(state) {
      current = state;
      var seen = Object.create(null);
      var top = draw("root", 0, false, seen);
      if (top && top.node.parentNode !== rootNode) {
        empty(rootNode);
        rootNode.appendChild(top.node);
      }
      var ids = Object.keys(records);
      for (var i = 0; i < ids.length; i++) {
        if (!seen[ids[i]]) destroy(records[ids[i]]);
      }
    }

    function draw(id, depth, hiddenAbove, seen) {
      if (seen[id] || depth > MAX_ELEMENTS) return null;
      var el = current.elements[id];
      if (!el) return null;
      seen[id] = true;
      var rec = records[id];
      if (rec && rec.type !== el.type) {
        destroy(rec);
        rec = null;
      }
      if (!rec) rec = records[id] = create(id, el);
      rec.el = el;
      rec.unavailable = el.props.hidden || el.props.disabled || hiddenAbove;
      rec.node.hidden = el.props.hidden;
      UPDATE[el.type](rec, el);
      applyBusy(rec);
      if (CONTAINERS[el.type]) {
        var nodes = [];
        for (var i = 0; i < el.children.length; i++) {
          var child = draw(el.children[i], depth + 1, hiddenAbove || el.props.hidden, seen);
          if (child) nodes.push(child.node);
        }
        place(rec.body, nodes);
      }
      return rec;
    }

    //: Puts `nodes` into `parent` in order, moving only a node that is out of place (a moved frame reloads).
    function place(parent, nodes) {
      for (var i = 0; i < nodes.length; i++) {
        var at = parent.childNodes[i] || null;
        if (at === nodes[i]) continue;
        if (nodes[i].parentNode && typeof parent.moveBefore === "function") {
          try {
            parent.moveBefore(nodes[i], at);
            continue;
          } catch (e) {
            // not movable in place: insert it
          }
        }
        parent.insertBefore(nodes[i], at);
      }
      while (parent.childNodes.length > nodes.length) parent.removeChild(parent.childNodes[parent.childNodes.length - 1]);
    }

    function destroy(rec) {
      if (records[rec.id] === rec) delete records[rec.id];
      if (rec.port) {
        try { rec.port.close(); } catch (e) { /* already closed */ }
        rec.port = null;
      }
      if (rec.media && rec.type === "Audio" && typeof rec.media.pause === "function") {
        try { rec.media.pause(); } catch (e) { /* not playing */ }
      }
      throttle.drop(rec.id);
      clearLocalBusy(rec.id);
      detach(rec.node);
    }

    function create(id, el) {
      var rec = {id: id, type: el.type, el: el, node: null, body: null, control: null, checks: null, sig: null};
      var node;
      switch (el.type) {
        case "Column":
          node = element("div", "cv-col");
          rec.body = node;
          break;
        case "Row":
          node = element("div", "cv-row");
          rec.body = node;
          break;
        case "Card":
          node = element("section", "cv-card");
          rec.title = element("h3", "cv-card-title");
          rec.body = element("div", "cv-card-body");
          node.appendChild(rec.title);
          node.appendChild(rec.body);
          break;
        case "Divider":
          node = element("hr", "cv-divider");
          break;
        case "Text":
          node = element("div", "cv-text");
          break;
        case "Button":
          node = element("button", "cv-btn");
          node.setAttribute("type", "button");
          rec.control = node;
          node.addEventListener("click", function (ev) { onButton(rec, ev); });
          break;
        case "Select":
          node = element("div", "cv-select");
          break;
        case "Image":
          node = element("div", "cv-image");
          break;
        case "Audio":
          node = element("div", "cv-audio");
          break;
        case "Link":
          node = element("button", "cv-link");
          node.setAttribute("type", "button");
          rec.control = node;
          rec.labelNode = element("span", "cv-link-label");
          rec.hostNode = element("span", "cv-link-host");
          node.appendChild(rec.labelNode);
          node.appendChild(rec.hostNode);
          node.addEventListener("click", function (ev) { onLink(rec, ev); });
          break;
        default:
          node = element("div", "cv-html");
          break;
      }
      node.setAttribute("data-cv-id", id);
      rec.node = node;
      return rec;
    }

    function live(rec) { return records[rec.id] === rec && current !== null; }

    var UPDATE = {
      Column: updateStack,
      Row: updateStack,
      Card: function (rec, el) {
        rec.title.textContent = el.props.title || "";
        rec.title.hidden = !el.props.title;
      },
      Divider: function () {},
      Text: updateText,
      Button: function (rec, el) {
        var sig = el.props.label + "\u0000" + el.props.style;
        if (rec.sig === sig) return;
        rec.sig = sig;
        rec.node.className = "cv-btn cv-btn-" + el.props.style;
        rec.node.textContent = el.props.label;
      },
      Select: updateSelect,
      Image: updateImage,
      Audio: updateAudio,
      Link: updateLink,
      Html: updateHtml
    };

    var JUSTIFY = {start: "flex-start", center: "center", end: "flex-end", stretch: "stretch"};

    function updateStack(rec, el) {
      rec.node.style.setProperty("gap", el.props.gap + "px");
      rec.node.style.setProperty("align-items", JUSTIFY[el.props.align]);
      if (el.type === "Row") rec.node.style.setProperty("flex-wrap", el.props.wrap ? "wrap" : "nowrap");
    }

    var TEXT_TAGS = {h1: "h1", h2: "h2", h3: "h3", body: "p", caption: "p", code: "pre"};

    function appendLines(parent, text) {
      var lines = text.split("\n");
      for (var i = 0; i < lines.length; i++) {
        if (i > 0) parent.appendChild(element("br"));
        if (lines[i]) parent.appendChild(textNode(lines[i]));
      }
    }

    function updateText(rec, el) {
      var sig = el.props.style + "\u0000" + el.props.text;
      if (rec.sig === sig) return;
      rec.sig = sig;
      empty(rec.node);
      var block = element(TEXT_TAGS[el.props.style], "cv-text-" + el.props.style);
      if (el.props.style === "code") {
        var code = element("code");
        code.textContent = el.props.text;
        block.appendChild(code);
      } else {
        var spans = inlineMarkup(el.props.text);
        for (var i = 0; i < spans.length; i++) {
          var style = spans[i][0];
          if (!style) {
            appendLines(block, spans[i][1]);
            continue;
          }
          var span = element(style === "b" ? "strong" : style === "i" ? "em" : "code");
          span.textContent = spans[i][1];
          block.appendChild(span);
        }
      }
      rec.node.appendChild(block);
    }

    function onButton(rec, ev) {
      if (!ev || ev.isTrusted !== true || !live(rec) || rec.unavailable || isBusy(rec.id)) return;
      if (send({type: "event", element: rec.id, kind: "click"})) setLocalBusy(rec.id);
    }

    // --- Select ----------------------------------------------------------------------------------------------------------------------

    function chosen(rec, el) {
      var stored = has(current.values, rec.id) ? choiceValue(current.values[rec.id]) : el.props.value;
      if (rec.pending) {
        if (!jsonEqual(stored, rec.pending.value) && now() - rec.pending.at < PENDING_CHOICE_MS) return rec.pending.value;
        rec.pending = null;
      }
      return stored;
    }

    function updateSelect(rec, el) {
      var sig = JSON.stringify([el.props.label, el.props.options, el.props.multiple]);
      if (rec.sig !== sig) {
        rec.sig = sig;
        rec.options = el.props.options;
        empty(rec.node);
        rec.control = null;
        rec.checks = null;
        if (el.props.multiple) buildChecks(rec, el);
        else buildSelect(rec, el);
      }
      var value = chosen(rec, el);
      var i;
      if (rec.checks) {
        var list = Array.isArray(value) ? value : [];
        for (i = 0; i < rec.checks.length; i++) rec.checks[i].checked = list.indexOf(rec.options[i].value) >= 0;
      } else if (rec.control) {
        var index = 0;
        for (i = 0; i < rec.options.length; i++) if (rec.options[i].value === value) index = i + 1;
        rec.control.selectedIndex = index;
      }
    }

    function buildSelect(rec, el) {
      var label = element("label", "cv-field");
      var name = element("span", "cv-field-label");
      name.textContent = el.props.label;
      var select = element("select", "cv-select-control");
      var placeholder = element("option");
      placeholder.textContent = COPY.choose;
      placeholder.disabled = true;
      select.appendChild(placeholder);
      for (var i = 0; i < rec.options.length; i++) {
        var option = element("option");
        option.textContent = rec.options[i].label;
        select.appendChild(option);
      }
      select.addEventListener("change", function (ev) {
        if (!ev || ev.isTrusted !== true || !live(rec) || rec.unavailable) return;
        var picked = rec.options[select.selectedIndex - 1];
        if (!picked) return;
        rec.pending = {value: picked.value, at: now()};
        send({type: "event", element: rec.id, kind: "change", value: picked.value});
      });
      label.appendChild(name);
      label.appendChild(select);
      rec.node.appendChild(label);
      rec.control = select;
    }

    function buildChecks(rec, el) {
      var group = element("fieldset", "cv-checks");
      var legend = element("legend", "cv-field-label");
      legend.textContent = el.props.label;
      group.appendChild(legend);
      rec.checks = [];
      for (var i = 0; i < rec.options.length; i++) {
        var row = element("label", "cv-check");
        var box = element("input");
        box.setAttribute("type", "checkbox");
        box.addEventListener("change", function (ev) {
          if (!ev || ev.isTrusted !== true || !live(rec) || rec.unavailable) return;
          var picked = [];
          for (var k = 0; k < rec.checks.length; k++) if (rec.checks[k].checked) picked.push(rec.options[k].value);
          rec.pending = {value: picked, at: now()};
          send({type: "event", element: rec.id, kind: "change", value: picked});
        });
        var caption = element("span");
        caption.textContent = rec.options[i].label;
        row.appendChild(box);
        row.appendChild(caption);
        group.appendChild(row);
        rec.checks.push(box);
      }
      rec.node.appendChild(group);
    }

    // --- Image and Audio -------------------------------------------------------------------------------------------------------------

    function decisionFor(el) {
      var key = mediaKey(el.props.src);
      var decision = mediaDecision(el.props.src, current.settings, current.file_urls, current.own_origin);
      if (decision.kind === "click" && clickedToLoad[key]) decision = {kind: "load", url: el.props.src};
      return {key: key, decision: decision};
    }

    function note(rec, text) {
      var p = element("p", "cv-media-note");
      p.textContent = text;
      rec.node.appendChild(p);
    }

    function clickToLoad(rec, key, copy, host) {
      var button = element("button", "cv-media-load");
      button.setAttribute("type", "button");
      button.textContent = copy.replace("{host}", host);
      button.addEventListener("click", function (ev) {
        if (!ev || ev.isTrusted !== true || !live(rec)) return;
        clickedToLoad[key] = true;
        rec.mediaSig = null;
        UPDATE[rec.type](rec, rec.el);
      });
      rec.node.appendChild(button);
    }

    //: A media element keeps its source while its key ("file:<path>", or the URL) is the same: a re-minted file URL is only held as
    //: the next one, used when the old one fails (it expired) or nothing was loaded from it yet, so a playing Audio never restarts.
    function keepOrSwap(rec, url) {
      if (rec.url === url) return;
      rec.nextUrl = url;
      var media = rec.media;
      var unloaded = rec.type === "Audio" ? media.readyState === 0 && media.paused !== false : false;
      if (rec.failed || unloaded) swapToNext(rec);
    }

    function swapToNext(rec) {
      if (!rec.nextUrl || rec.nextUrl === rec.url) return;
      var media = rec.media;
      var position = rec.type === "Audio" && typeof media.currentTime === "number" ? media.currentTime : 0;
      rec.url = rec.nextUrl;
      rec.nextUrl = null;
      rec.failed = false;
      media.src = rec.url;
      if (position > 0) {
        try { media.currentTime = position; } catch (e) { /* not seekable yet */ }
      }
    }

    function updateImage(rec, el) {
      var d = decisionFor(el);
      var sig = d.key + "\u0000" + d.decision.kind + "\u0000" + (d.decision.host || "");
      if (rec.mediaSig === sig && rec.media) {
        keepOrSwap(rec, d.decision.url);
      } else if (rec.mediaSig !== sig) {
        rec.mediaSig = sig;
        empty(rec.node);
        rec.media = null;
        rec.url = null;
        rec.nextUrl = null;
        rec.failed = false;
        if (d.decision.kind === "load") {
          var img = element("img", "cv-img");
          img.addEventListener("error", function () {
            if (rec.media !== img) return;
            rec.failed = true;
            swapToNext(rec);
          });
          rec.media = img;
          rec.url = d.decision.url;
          img.src = d.decision.url;
          rec.node.appendChild(img);
        } else if (d.decision.kind === "click") {
          clickToLoad(rec, d.key, COPY.loadImage, d.decision.host);
        } else if (d.decision.kind === "blocked") {
          note(rec, COPY.blocked.replace("{host}", d.decision.host));
        } else {
          note(rec, COPY.missing);
        }
      }
      if (rec.media) {
        rec.media.alt = el.props.alt;
        rec.media.style.setProperty("object-fit", el.props.fit);
        if (el.props.height !== null) rec.media.style.setProperty("height", el.props.height + "px");
        else rec.media.style.removeProperty("height");
      }
    }

    function position(media) {
      var t = media.currentTime;
      return typeof t === "number" && isFinite(t) && t >= 0 && t <= MAX_POSITION_SECONDS ? Math.round(t * 1000) / 1000 : null;
    }

    function audioEvent(rec, audio, kind) {
      return function (ev) {
        if (!ev || ev.isTrusted !== true || rec.media !== audio || !live(rec) || rec.unavailable) return;
        var msg = {type: "event", element: rec.id, kind: kind};
        var at = position(audio);
        if (at !== null) msg.position = at;
        send(msg);
      };
    }

    function updateAudio(rec, el) {
      var d = decisionFor(el);
      var sig = d.key + "\u0000" + d.decision.kind + "\u0000" + (d.decision.host || "");
      if (rec.mediaSig === sig && rec.media) {
        keepOrSwap(rec, d.decision.url);
      } else if (rec.mediaSig !== sig) {
        if (rec.media && typeof rec.media.pause === "function") {
          try { rec.media.pause(); } catch (e) { /* not playing */ }
        }
        rec.mediaSig = sig;
        empty(rec.node);
        rec.media = null;
        rec.url = null;
        rec.nextUrl = null;
        rec.failed = false;
        rec.titleNode = element("div", "cv-audio-title");
        rec.node.appendChild(rec.titleNode);
        if (d.decision.kind === "load") {
          var audio = element("audio", "cv-audio-control");
          audio.controls = true;
          audio.preload = "metadata";
          audio.addEventListener("play", audioEvent(rec, audio, "play"));
          audio.addEventListener("pause", audioEvent(rec, audio, "pause"));
          audio.addEventListener("ended", audioEvent(rec, audio, "ended"));
          audio.addEventListener("error", function () {
            if (rec.media !== audio) return;
            rec.failed = true;
            swapToNext(rec);
          });
          rec.media = audio;
          rec.url = d.decision.url;
          audio.src = d.decision.url;
          rec.node.appendChild(audio);
        } else if (d.decision.kind === "click") {
          clickToLoad(rec, d.key, COPY.loadAudio, d.decision.host);
        } else if (d.decision.kind === "blocked") {
          note(rec, COPY.blocked.replace("{host}", d.decision.host));
        } else {
          note(rec, COPY.missing);
        }
      }
      rec.titleNode.textContent = el.props.title || "";
      rec.titleNode.hidden = !el.props.title;
      if (rec.media) {
        rec.media.loop = el.props.loop;
        rec.media.setAttribute("aria-label", el.props.title || "Audio");
        if (el.props.disabled) rec.media.setAttribute("inert", "");
        else rec.media.removeAttribute("inert");
      }
    }

    // --- Link ------------------------------------------------------------------------------------------------------------------------

    function updateLink(rec, el) {
      rec.target = linkTarget(el.props.href);
      rec.labelNode.textContent = el.props.label;
      rec.hostNode.textContent = rec.target ? rec.target.host : COPY.badLink;
      rec.badTarget = !rec.target;
    }

    function onLink(rec, ev) {
      if (!ev || ev.isTrusted !== true || !live(rec) || rec.unavailable || !rec.target) return;
      send({type: "open_link", element: rec.id, url: rec.target.url});
    }

    // --- Html ------------------------------------------------------------------------------------------------------------------------

    function htmlKey(el) { return el.props.html + "\u0000" + (metaCsp(current.settings, current.own_origin) || ""); }

    function mayRun(rec, key) {
      if (current.run_html === true && current.settings.strict_mode !== true) return true;
      return approved[rec.id] === key;
    }

    function updateHtml(rec, el) {
      var key = htmlKey(el);
      var run = mayRun(rec, key);
      var sig = (run ? "run" : "inert") + "\u0000" + key;
      if (rec.sig !== sig) {
        rec.sig = sig;
        closeHtml(rec);
        empty(rec.node);
        if (run) buildFrame(rec, el);
        else buildInert(rec, key);
      }
      if (el.props.disabled) rec.node.setAttribute("inert", "");
      else rec.node.removeAttribute("inert");
      if (rec.frame) {
        if (el.props.height !== null) rec.frame.style.setProperty("height", el.props.height + "px");
        else if (!rec.autoHeight) rec.frame.style.setProperty("height", HTML_FIRST_HEIGHT + "px");
        pushValue(rec);
      }
    }

    function closeHtml(rec) {
      if (rec.port) {
        try { rec.port.close(); } catch (e) { /* already closed */ }
      }
      rec.port = null;
      rec.frame = null;
      rec.autoHeight = false;
      rec.sentValue = undefined;
      throttle.drop(rec.id);
    }

    function buildInert(rec, key) {
      //: round 4b instrumentation: record that buildInert ran and why, so a still-red run yields the exact remaining mechanism.
      rec.diag = rec.diag || {buildFrame: 0, buildInert: 0, srcdocSet: false, srcdocLen: 0, innerReady: null, innerLen: 0, handshake: []};
      rec.diag.buildInert += 1;
      var box = element("div", "cv-html-inert");
      var text = element("p", "cv-html-inert-text");
      text.textContent = COPY.inert;
      var run = element("button", "cv-btn cv-btn-secondary");
      run.setAttribute("type", "button");
      run.setAttribute("data-cv", "html-run");
      run.textContent = COPY.run;
      run.addEventListener("click", function (ev) {
        if (!ev || ev.isTrusted !== true || !live(rec) || rec.unavailable) return;
        approved[rec.id] = key;
        UPDATE.Html(rec, rec.el);
      });
      box.appendChild(text);
      box.appendChild(run);
      rec.node.appendChild(box);
    }

    function buildFrame(rec, el) {
      //: round 4b instrumentation: record that buildFrame ran and the srcdoc it set.
      rec.diag = rec.diag || {buildFrame: 0, buildInert: 0, srcdocSet: false, srcdocLen: 0, innerReady: null, innerLen: 0, handshake: []};
      rec.diag.buildFrame += 1;
      var doc0 = htmlSrcdoc(el.props.html, {
        settings: current.settings, ownOrigin: current.own_origin, theme: themeNow, bootstrap: bootstrap
      });
      if (doc0 === null) {
        note(rec, COPY.missing);
        return;
      }
      var frame = element("iframe", "cv-html-frame");
      frame.setAttribute("sandbox", "allow-scripts allow-forms");
      frame.setAttribute("title", "Html element " + rec.id);
      frame.addEventListener("load", function () {
        rec.diag.srcdocSet = true;
        try {
          var d = frame.contentDocument;
          rec.diag.innerReady = d ? d.readyState : "no-doc";
          rec.diag.innerLen = d && d.documentElement ? d.documentElement.outerHTML.length : 0;
        } catch (e) { rec.diag.innerReady = "err:" + (e && e.message ? e.message : "?" ); }
        offerPort(rec, frame);
      });
      frame.srcdoc = doc0;
      rec.diag.srcdocLen = doc0.length;
      rec.frame = frame;
      rec.node.appendChild(frame);
    }

    //: Each load of an Html frame gets a fresh port; the one before is closed, so only the frame's current document speaks.
    function offerPort(rec, frame) {
      if (rec.frame !== frame || !live(rec) || !frame.contentWindow) return;
      if (rec.port) {
        try { rec.port.close(); } catch (e) { /* already closed */ }
      }
      var channel = new win.MessageChannel();
      var port = channel.port1;
      rec.port = port;
      rec.sentValue = undefined;
      port.onmessage = function (ev) { onHtmlMessage(rec, port, ev); };
      frame.contentWindow.postMessage({type: "primer.html.connect", element: rec.id}, "*", [channel.port2]);
      pushValue(rec);
    }

    function postToHtml(rec, msg) {
      try { rec.port.postMessage(msg); } catch (e) { /* the frame is gone */ }
    }

    function pushValue(rec) {
      if (!rec.port) return;
      var value = has(current.values, rec.id) ? current.values[rec.id] : null;
      if (!throttle.pending(rec.id)) knownValue[rec.id] = value;
      if (rec.sentValue !== undefined && jsonEqual(rec.sentValue, value)) return;
      rec.sentValue = value;
      postToHtml(rec, {type: "value", value: value});
    }

    //: Reads isActive every ACTIVATION_POLL_MS while the gate holds an activation, so it sees one end.
    function watchActivation() {
      if (gatePoll !== null || !gate.watching()) return;
      gatePoll = win.setInterval(function () {
        if (!gate.observe()) {
          win.clearInterval(gatePoll);
          gatePoll = null;
        }
      }, ACTIVATION_POLL_MS);
    }

    //: Whether this activation lets the Html element `rec` speak (see "The click gate" above makeEmitGate).
    function htmlMayAct(rec) {
      if (!rec.frame || doc.activeElement !== rec.frame || doc.visibilityState !== "visible") return false;
      var passed = gate.allow(rec.frame);
      watchActivation();
      return passed;
    }

    //: A call the gate dropped: the element hears why at once, and the console gets a count, at most once a second.
    function refuse(rec, msg) {
      postToHtml(rec, {type: "error", message: GATE_REFUSAL.replace("{fn}", HTML_CALL[msg.type])});
      dropped[rec.id] = (dropped[rec.id] || 0) + 1;
      if (droppedTimer !== null) return;
      droppedTimer = win.setTimeout(function () {
        droppedTimer = null;
        var ids = Object.keys(dropped);
        for (var i = 0; i < ids.length; i++) {
          reportError(ids[i] + ": " + DROPPED_REPORT.replace("{n}", String(dropped[ids[i]])));
        }
        dropped = Object.create(null);
      }, DROPPED_REPORT_MS);
    }

    function onHtmlMessage(rec, port, ev) {
      if (rec.port !== port || !live(rec)) return;
      var msg = validateHtmlMessage(ev ? ev.data : null);
      if (!msg) return;
      if (msg.type === "size") {
        if (rec.el.props.height === null && rec.frame) {
          rec.autoHeight = true;
          var h = Math.round(Math.min(HTML_AUTO_MAX, Math.max(HTML_AUTO_MIN, msg.height)));
          rec.frame.style.setProperty("height", h + "px");
        }
        return;
      }
      if (msg.type === "error") {
        reportError(rec.id + ": " + msg.message);
        return;
      }
      if (rec.unavailable) return;
      if (msg.type === "set_value") {
        if (!throttle.pending(rec.id) && has(knownValue, rec.id) && jsonEqual(knownValue[rec.id], msg.value)) return;
        throttle.push(rec.id, msg.value);
        return;
      }
      if (!htmlMayAct(rec)) {
        refuse(rec, msg);
        return;
      }
      if (msg.type === "emit") send({type: "event", element: rec.id, kind: "emit", name: msg.name, data: msg.data});
      else if (msg.type === "copy") send({type: "copy", element: rec.id, text: msg.text});
      else if (msg.type === "open_link") send({type: "open_link", url: msg.url});
    }

    return {
      // For the tests: what the renderer holds.
      connected: function () { return host !== null; },
      record: function (id) { return records[id] || null; }
    };
  }

  root.CV = {
    version: 2,
    PROTOCOL: PROTOCOL,
    EMIT_BUDGET: EMIT_BUDGET,
    COPY: COPY,
    validateHostMessage: validateHostMessage,
    validateHtmlMessage: validateHtmlMessage,
    acceptsHandshake: acceptsHandshake,
    mediaDecision: mediaDecision,
    mediaKey: mediaKey,
    inlineMarkup: inlineMarkup,
    linkTarget: linkTarget,
    themeCss: themeCss,
    themePairs: themePairs,
    selectOptions: selectOptions,
    metaCsp: metaCsp,
    htmlSrcdoc: htmlSrcdoc,
    makeEmitGate: makeEmitGate,
    makeBucket: makeBucket,
    makeValueThrottle: makeValueThrottle,
    utf8Length: utf8Length,
    boot: boot
  };
})(typeof window !== "undefined" ? window : this);
