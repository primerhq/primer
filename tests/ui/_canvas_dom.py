"""A small fake DOM to run the canvas renderer's DOM side (``CV.boot``) in MiniRacer (pivot P5, S7).

MiniRacer has no DOM, no ``MessageChannel`` and no ``navigator.userActivation``, so this file gives the renderer just the surface it uses:
elements with children, attributes, properties, styles and listeners; text nodes; a document with ``activeElement`` and
``visibilityState``; a window with a parent, ``navigator.userActivation`` (a plain object a test flips), timers on a fake clock, and
``MessageChannel`` ports that deliver synchronously. Every element records how often its ``src`` and ``srcdoc`` were written, and every
iframe has a ``contentWindow`` that records what was posted to it, so a test can see a media element was NOT reloaded and which port an
Html frame was given.

``canvas_dom_context(prelude)`` returns a MiniRacer context with the fake DOM, the Html bootstrap source as ``CV_BOOTSTRAP``, the
renderer, and ``CV.boot`` already run (its handle is ``R``); ``FD`` is the fake, and the helpers ``FD.connect()``, ``FD.state(...)``,
``FD.sent()`` and ``FD.find(...)`` drive it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "ui"
RENDERER = UI / "canvas" / "renderer.js"
BOOTSTRAP = UI / "canvas" / "html-bootstrap.js"

FAKE_DOM = r"""
var FD = (function () {
  var clock = {t: 1000000, timers: [], seq: 0};
  function setTimer(fn, ms, every) { clock.seq += 1; clock.timers.push({id: clock.seq, at: clock.t + (ms || 0), fn: fn, every: every ? ms : 0}); return clock.seq; }
  function clearTimer(id) { clock.timers = clock.timers.filter(function (t) { return t.id !== id; }); }
  function advance(ms) {
    var end = clock.t + ms;
    for (;;) {
      var due = clock.timers.filter(function (t) { return t.at <= end; }).sort(function (a, b) { return a.at - b.at || a.id - b.id; })[0];
      if (!due) break;
      clock.t = Math.max(clock.t, due.at);
      if (due.every) due.at = clock.t + due.every; else clearTimer(due.id);
      due.fn();
    }
    clock.t = end;
  }

  function Port() { this.other = null; this.onmessage = null; this.closed = false; this.sent = []; }
  Port.prototype.postMessage = function (data) {
    if (this.closed) return;
    this.sent.push(data);
    var o = this.other;
    if (o && !o.closed && typeof o.onmessage === "function") o.onmessage({data: data, target: o, source: null});
  };
  Port.prototype.close = function () { this.closed = true; };
  Port.prototype.start = function () {};
  function MessageChannel() { this.port1 = new Port(); this.port2 = new Port(); this.port1.other = this.port2; this.port2.other = this.port1; }

  function Node() { this.parentNode = null; this.childNodes = []; }
  Object.defineProperty(Node.prototype, "firstChild", {get: function () { return this.childNodes[0] || null; }});
  Node.prototype.appendChild = function (n) { return this.insertBefore(n, null); };
  Node.prototype.insertBefore = function (n, ref) {
    if (n.parentNode) n.parentNode.removeChild(n);
    var i = ref ? this.childNodes.indexOf(ref) : this.childNodes.length;
    if (i < 0) throw new Error("insertBefore: the reference is not a child");
    this.childNodes.splice(i, 0, n);
    n.parentNode = this;
    n.inserted = (n.inserted || 0) + 1;
    return n;
  };
  Node.prototype.removeChild = function (n) {
    var i = this.childNodes.indexOf(n);
    if (i < 0) throw new Error("removeChild: not a child");
    this.childNodes.splice(i, 1);
    n.parentNode = null;
    return n;
  };
  Object.defineProperty(Node.prototype, "textContent", {
    get: function () { return this.childNodes.map(function (c) { return c.nodeType === 3 ? c.data : (c.tagName === "BR" ? "\n" : c.textContent); }).join(""); },
    set: function (t) { this.childNodes.forEach(function (c) { c.parentNode = null; }); this.childNodes = []; if (t !== "") this.appendChild(new Text(String(t))); }
  });

  function Text(t) { Node.call(this); this.nodeType = 3; this.data = t; }
  Text.prototype = Object.create(Node.prototype);
  Object.defineProperty(Text.prototype, "textContent", {get: function () { return this.data; }, set: function (t) { this.data = String(t); }});

  function Style() { this.props = {}; }
  Style.prototype.setProperty = function (k, v) { this.props[k] = String(v); };
  Style.prototype.removeProperty = function (k) { delete this.props[k]; };
  Style.prototype.getPropertyValue = function (k) { return this.props[k] || ""; };

  function El(tag) {
    Node.call(this);
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.attrs = {};
    this.listeners = {};
    this.style = new Style();
    this.hidden = false;
    this.disabled = false;
    this.className = "";
    this.writes = {src: 0, srcdoc: 0};
    if (this.tagName === "IFRAME") {
      var posted = [];
      this.contentWindow = {posted: posted, postMessage: function (data, origin, transfer) { posted.push({data: data, origin: origin, transfer: transfer || []}); }};
    }
    if (this.tagName === "AUDIO") {
      this.readyState = 0; this.networkState = 0; this.paused = true; this.currentTime = 0; this.loop = false; this.controls = false;
      this.pauses = 0;
    }
    if (this.tagName === "SELECT") this.selectedIndex = -1;
    if (this.tagName === "INPUT") this.checked = false;
  }
  El.prototype = Object.create(Node.prototype);
  El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
  El.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
  El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
  El.prototype.hasAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); };
  El.prototype.addEventListener = function (type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); };
  El.prototype.removeEventListener = function (type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(function (f) { return f !== fn; }); };
  El.prototype.dispatch = function (type, extra) {
    var ev = {type: type, isTrusted: true, target: this, currentTarget: this, preventDefault: function () {}, stopPropagation: function () {}};
    for (var k in (extra || {})) ev[k] = extra[k];
    var self = this;
    (this.listeners[type] || []).slice().forEach(function (fn) { fn.call(self, ev); });
    return ev;
  };
  El.prototype.click = function (extra) { return this.dispatch("click", extra); };
  El.prototype.focus = function () { document.activeElement = this; };
  El.prototype.pause = function () { this.paused = true; this.pauses = (this.pauses || 0) + 1; };
  ["src", "srcdoc"].forEach(function (name) {
    Object.defineProperty(El.prototype, name, {
      get: function () { return this["_" + name] || ""; },
      set: function (v) { this["_" + name] = String(v); this.writes[name] += 1; }
    });
  });
  El.prototype.all = function () {
    var out = [];
    (function walk(n) { n.childNodes.forEach(function (c) { if (c.nodeType === 1) { out.push(c); walk(c); } }); })(this);
    return out;
  };
  El.prototype.query = function (pred) { return this.all().filter(pred); };

  var root = new El("main");
  root.attrs.id = "cv-root";
  var documentElement = new El("html");
  var docListeners = {};
  var document = {
    activeElement: null,
    visibilityState: "visible",
    documentElement: documentElement,
    addEventListener: function (type, fn, capture) { (docListeners[type] = docListeners[type] || []).push({fn: fn, capture: !!capture}); },
    getElementById: function (id) { return id === "cv-root" ? root : null; },
    createElement: function (tag) { return new El(tag); },
    createTextNode: function (t) { return new Text(String(t)); }
  };

  var consoleWindow = {name: "console"};
  var listeners = {};
  var window = {
    parent: consoleWindow,
    navigator: {userActivation: {isActive: false, hasBeenActive: false}},
    Date: {now: function () { return clock.t; }},
    setTimeout: function (fn, ms) { return setTimer(fn, ms, false); },
    clearTimeout: clearTimer,
    setInterval: function (fn, ms) { return setTimer(fn, ms, true); },
    clearInterval: clearTimer,
    MessageChannel: MessageChannel,
    addEventListener: function (type, fn) { (listeners[type] = listeners[type] || []).push(fn); }
  };

  var consolePort = null;
  var api = {
    clock: clock, advance: advance, root: root, document: document, window: window, consoleWindow: consoleWindow,
    MessageChannel: MessageChannel, El: El,
    activeTimers: function () { return clock.timers.length; },
    //: A person's input event on the renderer's own document (a click on a native control, a key press): what its capture listeners see.
    input: function (type, extra) {
      var ev = {type: type, isTrusted: true};
      for (var k in (extra || {})) ev[k] = extra[k];
      (docListeners[type] || []).slice().forEach(function (l) { l.fn(ev); });
      return ev;
    },
    docListenerTypes: function () { return Object.keys(docListeners).filter(function (t) { return docListeners[t].every(function (l) { return l.capture; }); }); },
    //: Delivers a window message to the renderer as the browser would.
    deliver: function (data, source, ports) {
      (listeners.message || []).slice().forEach(function (fn) { fn({data: data, source: source, ports: ports || [], origin: "null"}); });
    },
    //: The console's handshake; returns the console's end of the channel.
    connect: function (source) {
      var channel = new MessageChannel();
      consolePort = channel.port2;
      consolePort.received = [];
      consolePort.onmessage = function (ev) { consolePort.received.push(ev.data); };
      api.deliver({type: "primer.canvas.connect", protocol: 1}, source === undefined ? consoleWindow : source, [channel.port1]);
      return consolePort;
    },
    //: What the renderer sent the console since the handshake.
    sent: function () { return consolePort ? consolePort.received : []; },
    post: function (msg) { consolePort.postMessage(msg); },
    state: function (elements, extra) {
      var msg = {type: "state", rev: 1, elements: elements, values: {},
                 settings: {media_policy: "any-https", media_allowlist: [], strict_mode: false},
                 file_urls: {}, own_origin: "", theme: {}, run_html: true};
      for (var k in (extra || {})) msg[k] = extra[k];
      consolePort.postMessage(msg);
    },
    byId: function (id) { return root.query(function (n) { return n.attrs["data-cv-id"] === id; })[0] || null; },
    find: function (id, tag) { var n = api.byId(id); return n ? (n.tagName === tag.toUpperCase() ? n : n.query(function (c) { return c.tagName === tag.toUpperCase(); })[0] || null) : null; },
    //: The Html frame of element `id` loads: the renderer offers it a port; returns the frame's end.
    load: function (id) {
      var frame = api.find(id, "iframe");
      frame.dispatch("load");
      var offer = frame.contentWindow.posted[frame.contentWindow.posted.length - 1];
      var port = offer.transfer[0];
      port.received = [];
      port.onmessage = function (ev) { port.received.push(ev.data); };
      return port;
    }
  };
  return api;
})();
"""


def canvas_dom_context():
    """A MiniRacer context with the fake DOM, the bootstrap source, the renderer and ``CV.boot`` run (``R`` is its handle)."""
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    ctx.eval(FAKE_DOM)
    ctx.eval("var window = FD.window; var document = FD.document;")
    ctx.eval("window.CV_BOOTSTRAP = " + json.dumps(BOOTSTRAP.read_text(encoding="utf-8")) + ";")
    ctx.eval(RENDERER.read_text(encoding="utf-8"))
    ctx.eval("var CV = window.CV; var R = CV.boot(FD.document, FD.window);")
    return ctx


def js(ctx, expr: str):
    """The value of ``expr`` in ``ctx``, through JSON."""
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))
