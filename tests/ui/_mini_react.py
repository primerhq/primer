"""A small React stand-in for executing console components in V8 (no jsdom in this toolchain).

``mini_react_context(jsx_path)`` returns a MiniRacer context holding

* a hook runtime (``useState``, ``useEffect`` with dependency arrays and cleanups, ``useRef``, ``useCallback``, ``useMemo``,
  ``useReducer``) and ``React.createElement`` / ``Fragment``, which together render a function-component tree to a plain element
  tree, re-render it until no state change is pending, and run each effect after the render that changed its dependencies;
* the file's real code, transpiled with the same Babel the server bundles with.

What it deliberately is not: a DOM, a scheduler, or a reconciler. State lives per component position and type, an instance that
leaves the tree runs its cleanups and loses its state, and the whole tree is re-rendered on every change. That is enough to drive
a component through "mount, click, props change" and read what it rendered or called.

Where it differs from React, so a green test here is not read as more than it is:

* Effects run PARENT-FIRST, in render order. React runs a child's effects before its parent's.
* There is no batching: every state change marks the tree dirty and the loop re-renders once after the current pass, so a handler
  that sets two states re-renders once, but effects see each intermediate pass.
* A setter that belongs to an instance that has since left the tree still marks the tree dirty and re-renders it. React ignores
  such a call.
* No strict-mode double render or double effect, no ``key`` reordering, no refs to DOM nodes, no context (callers stub the hooks
  that read it, such as ``NV_useConsole``).

Driver API, inside the context (``MR``): ``MR.mount(Component, props)``, ``MR.rerender(props?)``, ``MR.find(testid)`` (the element
or null), ``MR.findAll(prefix)``, ``MR.click(testid)``, ``MR.texts()``, ``MR.subtree(testid)`` (every element at or under the one with that
test id, as ``{type, className, role, ariaLive, testid}``; null when there is none). The caller owns ``ctx.close()``.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_RUNTIME = r"""
var window = globalThis;
(function (g) {
  var instances = {};
  var current = null;
  var dirty = false;
  var pendingEffects = [];
  var root = null;
  var typeIds = [];

  function typeId(t) {
    var i = typeIds.indexOf(t);
    if (i < 0) { typeIds.push(t); i = typeIds.length - 1; }
    return i;
  }
  function Fragment() {}
  function createElement(type, props) {
    var kids = Array.prototype.slice.call(arguments, 2);
    var p = {};
    for (var k in (props || {})) p[k] = props[k];
    if (kids.length === 1) p.children = kids[0];
    else if (kids.length > 1) p.children = kids;
    return { __el: true, type: type, props: p, key: props && props.key != null ? props.key : null, children: kids, out: null };
  }
  function slotAt() {
    var i = current.hookIdx++;
    if (!current.hooks[i]) current.hooks[i] = {};
    return current.hooks[i];
  }
  function depsChanged(a, b) {
    if (!a || !b || a.length !== b.length) return true;
    for (var i = 0; i < a.length; i++) if (!Object.is(a[i], b[i])) return true;
    return false;
  }
  function useState(init) {
    var s = slotAt();
    if (!("v" in s)) {
      s.v = typeof init === "function" ? init() : init;
      s.set = function (next) {
        var nv = typeof next === "function" ? next(s.v) : next;
        if (!Object.is(nv, s.v)) { s.v = nv; dirty = true; }
      };
    }
    return [s.v, s.set];
  }
  function useReducer(reducer, init) {
    var s = slotAt();
    if (!("v" in s)) {
      s.v = init;
      s.dispatch = function (a) { var nv = reducer(s.v, a); if (!Object.is(nv, s.v)) { s.v = nv; dirty = true; } };
    }
    return [s.v, s.dispatch];
  }
  function useRef(init) {
    var s = slotAt();
    if (!s.ref) s.ref = { current: init === undefined ? null : init };
    return s.ref;
  }
  function useMemo(fn, deps) {
    var s = slotAt();
    if (!("v" in s) || depsChanged(s.deps, deps)) { s.v = fn(); s.deps = deps; }
    return s.v;
  }
  function useCallback(fn, deps) { return useMemo(function () { return fn; }, deps); }
  function useEffect(fn, deps) {
    var s = slotAt();
    if (!("ran" in s) || depsChanged(s.deps, deps)) {
      s.ran = true;
      s.deps = deps;
      pendingEffects.push({ slot: s, fn: fn });
    }
  }
  function renderNode(node, path, visited) {
    if (node == null || typeof node === "boolean" || typeof node === "string" || typeof node === "number") return;
    if (Array.isArray(node)) {
      node.forEach(function (c, i) {
        renderNode(c, path + "." + (c && c.__el && c.key != null ? "k" + c.key : "i" + i), visited);
      });
      return;
    }
    if (!node.__el) return;
    if (typeof node.type === "function" && node.type !== Fragment) {
      var id = path + "/" + typeId(node.type) + (node.key != null ? "#" + node.key : "");
      var inst = instances[id] || (instances[id] = { hooks: [] });
      visited[id] = true;
      inst.hookIdx = 0;
      var prev = current;
      current = inst;
      var out;
      try { out = node.type(node.props); } finally { current = prev; }
      node.out = out;
      renderNode(out, id, visited);
    } else {
      renderNode(node.children, path + "/" + (typeof node.type === "string" ? node.type : "f"), visited);
    }
  }
  function pass() {
    var visited = {};
    root.tree = createElement(root.type, root.props);
    renderNode(root.tree, "r", visited);
    Object.keys(instances).forEach(function (id) {
      if (visited[id]) return;
      instances[id].hooks.forEach(function (h) { if (typeof h.cleanup === "function") h.cleanup(); });
      delete instances[id];
    });
    var effects = pendingEffects;
    pendingEffects = [];
    effects.forEach(function (e) {
      if (typeof e.slot.cleanup === "function") e.slot.cleanup();
      var c = e.fn();
      e.slot.cleanup = typeof c === "function" ? c : null;
    });
  }
  function flush() {
    var n = 0;
    do {
      dirty = false;
      pass();
      if (++n > 60) throw new Error("render loop: a state change on every render");
    } while (dirty);
  }
  function walk(node, visit) {
    if (node == null || typeof node !== "object") return;
    if (Array.isArray(node)) { node.forEach(function (c) { walk(c, visit); }); return; }
    if (!node.__el) return;
    visit(node);
    if (typeof node.type === "function" && node.type !== Fragment) walk(node.out, visit);
    else walk(node.children, visit);
  }
  function findAll(prefix) {
    var found = [];
    walk(root.tree, function (el) {
      var t = el.props && el.props["data-testid"];
      if (typeof t === "string" && t.indexOf(prefix) === 0) found.push(el);
    });
    return found;
  }
  function find(testid) {
    var found = null;
    walk(root.tree, function (el) {
      if (!found && el.props && el.props["data-testid"] === testid) found = el;
    });
    return found;
  }
  function texts() {
    var out = [];
    (function collect(n) {
      if (n == null || typeof n === "boolean") return;
      if (typeof n === "string" || typeof n === "number") { out.push(String(n)); return; }
      if (Array.isArray(n)) { n.forEach(collect); return; }
      if (n.__el) collect(typeof n.type === "function" && n.type !== Fragment ? n.out : n.children);
    })(root.tree);
    return out;
  }
  g.React = {
    createElement: createElement, Fragment: Fragment, useState: useState, useReducer: useReducer, useRef: useRef,
    useMemo: useMemo, useCallback: useCallback, useEffect: useEffect, useLayoutEffect: useEffect,
  };
  g.MR = {
    mount: function (type, props) { root = { type: type, props: props || {}, tree: null }; flush(); },
    rerender: function (props) { if (props) root.props = props; flush(); },
    find: find, findAll: findAll, texts: texts,
    subtree: function (testid) {
      var start = find(testid);
      if (!start) return null;
      var out = [];
      walk(start, function (el) {
        out.push({
          type: typeof el.type === "string" ? el.type : "component", className: (el.props && el.props.className) || "",
          role: el.props && el.props.role, ariaLive: el.props && el.props["aria-live"], testid: el.props && el.props["data-testid"],
        });
      });
      return out;
    },
    click: function (testid) {
      var el = find(testid);
      if (!el) throw new Error("no element with data-testid " + testid);
      el.props.onClick({ preventDefault: function () {}, stopPropagation: function () {} });
      flush();
    },
  };
})(globalThis);
"""


def transpile(jsx_path: Path) -> str:
    """The file as the server's bundler would emit it. The bundler's own V8 context is closed before returning."""
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform(jsx_path.read_text(encoding="utf-8"), str(jsx_path.relative_to(ui)))
    finally:
        bundler._ctx.close()


def mini_react_context(code: str, prelude: str = ""):
    """A fresh V8 context with the hook runtime, then ``prelude`` (the stubs the file's globals need), then ``code``."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(_RUNTIME)
    if prelude:
        ctx.eval(prelude)
    ctx.eval(code)
    return ctx
