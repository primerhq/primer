/* The Html element bootstrap (pivot P5, Task 17; design 8.6, plan Decision 10).
 *
 * The renderer (ui/canvas/renderer.js) writes this file into every Html element's srcdoc, after the element's narrowing media policy
 * and before any of the agent's HTML, so it runs first in the element's own opaque frame. It gives the agent's code window.primer:
 *  - primer.emit(name, data): an event that wakes the agent; needs a click or a key press first;
 *  - primer.setValue(value): the element's value, kept on the canvas and readable by the agent; needs none;
 *  - primer.copy(text): the console offers to copy the text; needs a click or a key press first;
 *  - primer.openLink(url): the console asks the user before it opens an http or https URL; needs a click or a key press first;
 *  - primer.value, and a "primer:value" event on window with the value in event.detail, when the canvas sends one.
 * It reports the page's height (a ResizeObserver) and its uncaught errors.
 *
 * The checks here are ADVISORY (audit G8-05): the agent's code shares this realm and can take the port. The renderer gates every
 * emit, copy and open_link on its own activation, focus and budget, and the frame CSP is the only script control (G8-06). The
 * bootstrap takes the renderer's port only from its parent (event.source), in the exact shape, first offer wins (the lead's
 * ruling 4 on S7), and drops anything else that reaches its window.
 *
 * The pure checks (PB_canAct, PB_validateEmit, PB_validateUrl, PB_acceptsConnect, PB_validateHostMessage) are exported on window and
 * run in MiniRacer in tests/ui/test_canvas_html_bootstrap.py. This file never names a closing script tag or a comment opener, so the
 * renderer can inline it into a <script>.
 */
(function (window) {
  "use strict";

  var EMIT_NAME = /^[A-Za-z0-9_.:-]{1,64}$/;
  var MAX_EMIT_DATA_BYTES = 4096;
  var MAX_VALUE_CHARS = 1000;
  var MAX_COPY_CHARS = 10000;
  var MAX_URL_CHARS = 2048;
  var MAX_ERROR_CHARS = 500;
  var MAX_QUEUE = 20;
  var URL_SHAPE = /^https?:\/\/([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*|\[[0-9a-f:.]{2,45}\])(?::[0-9]{1,5})?(?:[\/?#][^\s\\]*)?$/i;
  var THEME_NAME = /^--cv-[a-z0-9-]{1,40}$/;

  function isPlainObject(v) {
    if (v === null || typeof v !== "object" || Array.isArray(v)) return false;
    var proto = Object.getPrototypeOf(v);
    return proto === Object.prototype || proto === null;
  }

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

  //: The JSON text of v, or null when it has none (a function, a cycle, a BigInt).
  function jsonText(v) {
    try {
      var s = JSON.stringify(v === undefined ? null : v);
      return typeof s === "string" ? s : null;
    } catch (e) {
      return null;
    }
  }

  //: Whether this frame may act for a click: its activation is live and the page is visible.
  function PB_canAct(activation, visibilityState) {
    return !!activation && activation.isActive === true && visibilityState === "visible";
  }

  //: Why an emit is refused (the name rule of the canvas events route; data at most 4096 bytes as JSON), or null.
  function PB_validateEmit(name, data) {
    if (typeof name !== "string" || !EMIT_NAME.test(name)) {
      return "an emit name is 1 to 64 letters, digits, _ . : or -, and does not start with /";
    }
    var text = jsonText(data);
    if (text === null) return "emit data must be JSON";
    if (utf8Length(text) > MAX_EMIT_DATA_BYTES) return "emit data is at most " + MAX_EMIT_DATA_BYTES + " bytes of JSON";
    return null;
  }

  //: Why a URL cannot be opened (only http and https, with a plain host), or null.
  function PB_validateUrl(url) {
    if (typeof url !== "string" || url.length > MAX_URL_CHARS || /[\s\\\u0000-\u001f\u007f]/.test(url) || !URL_SHAPE.test(url)) {
      return "primer.openLink takes an http or https URL";
    }
    var authority = url.replace(/^https?:\/\//i, "").split(/[\/?#]/)[0];
    return authority.indexOf("@") >= 0 ? "primer.openLink takes an http or https URL" : null;
  }

  //: Whether a window message is the renderer's port offer: from the parent, the exact shape, exactly one port.
  function PB_acceptsConnect(data, source, parent, self, ports) {
    if (!parent || parent === self || source !== parent) return false;
    if (!isPlainObject(data) || data.type !== "primer.html.connect" || typeof data.element !== "string") return false;
    if (!/^[a-z][a-z0-9_]{0,31}$/.test(data.element)) return false;
    return !!ports && ports.length === 1 && !!ports[0];
  }

  //: A message from the renderer on the port, checked, or null: {type: "value", value} or {type: "theme", theme}.
  function PB_validateHostMessage(msg) {
    if (!isPlainObject(msg)) return null;
    if (msg.type === "value") {
      var text = jsonText(msg.value);
      return text !== null && text.length <= MAX_VALUE_CHARS ? {type: "value", value: JSON.parse(text)} : null;
    }
    if (msg.type === "theme" && isPlainObject(msg.theme)) {
      var theme = {};
      var names = Object.keys(msg.theme);
      for (var i = 0; i < names.length && i < 40; i++) {
        var value = msg.theme[names[i]];
        if (THEME_NAME.test(names[i]) && typeof value === "string" && value.length <= 200) theme[names[i]] = value;
      }
      return {type: "theme", theme: theme};
    }
    return null;
  }

  window.PB_canAct = PB_canAct;
  window.PB_validateEmit = PB_validateEmit;
  window.PB_validateUrl = PB_validateUrl;
  window.PB_acceptsConnect = PB_acceptsConnect;
  window.PB_validateHostMessage = PB_validateHostMessage;

  if (typeof window.addEventListener !== "function" || !window.document) return;

  var document = window.document;
  var port = null;
  var queue = [];
  var current = null;

  function post(msg) {
    if (port) {
      try { port.postMessage(msg); } catch (e) { /* the renderer is gone */ }
    } else if (queue.length < MAX_QUEUE) {
      queue.push(msg);
    }
  }

  function needsClick(fn) {
    if (!PB_canAct(window.navigator ? window.navigator.userActivation : null, document.visibilityState)) {
      throw new Error("primer." + fn + " needs a click or a key press first");
    }
  }

  function onHostMessage(ev) {
    var msg = PB_validateHostMessage(ev ? ev.data : null);
    if (!msg) return;
    if (msg.type === "value") {
      current = msg.value;
      if (typeof window.CustomEvent === "function") window.dispatchEvent(new window.CustomEvent("primer:value", {detail: current}));
      return;
    }
    var style = document.documentElement ? document.documentElement.style : null;
    var names = Object.keys(msg.theme);
    for (var i = 0; style && i < names.length; i++) style.setProperty(names[i], msg.theme[names[i]]);
  }

  window.addEventListener("message", function (ev) {
    if (port || !PB_acceptsConnect(ev.data, ev.source, window.parent, window, ev.ports)) return;
    if (typeof ev.stopImmediatePropagation === "function") ev.stopImmediatePropagation();
    port = ev.ports[0];
    port.onmessage = onHostMessage;
    var waiting = queue;
    queue = [];
    for (var i = 0; i < waiting.length; i++) post(waiting[i]);
  }, true);

  var primer = {
    emit: function (name, data) {
      needsClick("emit");
      var problem = PB_validateEmit(name, data);
      if (problem) throw new Error(problem);
      post({type: "emit", name: name, data: JSON.parse(jsonText(data))});
    },
    setValue: function (value) {
      var text = jsonText(value);
      if (text === null) throw new Error("primer.setValue takes a JSON value");
      if (text.length > MAX_VALUE_CHARS) throw new Error("a value is at most " + MAX_VALUE_CHARS + " characters of JSON");
      post({type: "set_value", value: JSON.parse(text)});
    },
    copy: function (text) {
      needsClick("copy");
      if (typeof text !== "string" || text.length > MAX_COPY_CHARS) throw new Error("primer.copy takes text of at most 10000 characters");
      post({type: "copy", text: text});
    },
    openLink: function (url) {
      needsClick("openLink");
      var problem = PB_validateUrl(url);
      if (problem) throw new Error(problem);
      post({type: "open_link", url: url});
    }
  };
  Object.defineProperty(primer, "value", {get: function () { return current; }, enumerable: true});
  window.primer = Object.freeze(primer);

  var lastHeight = -1;
  function reportSize() {
    var root = document.documentElement;
    var height = root ? Math.max(root.scrollHeight || 0, document.body ? document.body.scrollHeight || 0 : 0) : 0;
    if (height !== lastHeight) {
      lastHeight = height;
      post({type: "size", height: height});
    }
  }
  if (typeof window.ResizeObserver === "function" && document.documentElement) {
    new window.ResizeObserver(reportSize).observe(document.documentElement);
  }
  window.addEventListener("load", reportSize);

  window.addEventListener("error", function (ev) {
    var message = ev && typeof ev.message === "string" ? ev.message : "an error";
    post({type: "error", message: message.slice(0, MAX_ERROR_CHARS)});
  });
  window.addEventListener("unhandledrejection", function (ev) {
    var reason = ev && ev.reason && typeof ev.reason.message === "string" ? ev.reason.message : "a promise was rejected";
    post({type: "error", message: reason.slice(0, MAX_ERROR_CHARS)});
  });
})(window);
