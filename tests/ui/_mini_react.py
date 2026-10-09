"""A small React stand-in for executing console components in V8 (no jsdom in this toolchain).

``mini_react_context(jsx_path)`` returns a MiniRacer context holding

* a hook runtime (``useState``, ``useEffect`` with dependency arrays and cleanups, ``useRef``, ``useCallback``, ``useMemo``,
  ``useReducer``, ``useId``) and ``React.createElement`` / ``Fragment``, which together render a function-component tree to a plain element
  tree, re-render it until no state change is pending, and run each effect after the render that changed its dependencies; plus the
  element helpers the shared components call (``isValidElement``, ``cloneElement``, ``Children.toArray``), implemented as React does them;
* the file's real code, transpiled with the same Babel the server bundles with.

What it deliberately is not: a DOM, a scheduler, or a reconciler. State lives per component position and type, an instance that
leaves the tree runs its cleanups and loses its state, and the whole tree is re-rendered on every change. That is enough to drive
a component through "mount, click, props change" and read what it rendered or called.

Class components run too (``React.Component``, ``setState``, ``componentDidMount``/``componentDidUpdate``, and ``static getDerivedStateFromError`` makes one an error boundary), and an object
handed over as a child THROWS, as it does in React ("Objects are not valid as a React child"), so a render test sees that whole class of crash.

Where it differs from React, so a green test here is not read as more than it is:

* Effects run PARENT-FIRST, in render order. React runs a child's effects before its parent's.
* There is no batching: every state change marks the tree dirty and the loop re-renders once after the current pass, so a handler
  that sets two states re-renders once, but effects see each intermediate pass.
* A setter that belongs to an instance that has since left the tree still marks the tree dirty and re-renders it. React ignores
  such a call.
* No strict-mode double render or double effect, no ``key`` reordering, no refs to DOM nodes, no context (callers stub the hooks
  that read it, such as ``NV_useConsole``).
* ``useId`` counts up from ``:r0:`` in each context, one id per component instance (no server/client id matching). ``Children`` has ``toArray`` only, and ``cloneElement`` treats
  ``ref`` as an ordinary prop and ignores ``defaultProps``. An API the harness lacks throws where the component calls it: add it here, as React does it, rather than
  stubbing it in the test (a stub that does something else, or nothing, tests the stub).

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
  var idCounter = 0;

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
  function isValidElement(x) { return x != null && typeof x === "object" && x.__el === true; }
  // React refuses an object as a child (a function it only warns about); a render, and Children.toArray, handed one must fail here as they do there
  function notAChild(node) {
    return new Error("Objects are not valid as a React child (found: object with keys {" + Object.keys(node).join(", ") + "})");
  }
  // cloneElement(el, config, ...children): the element's props with the config laid over them, as React does it. A `key` in the config replaces the element's key (an undefined one leaves it).
  // `ref` is an ordinary prop in this harness (there are no refs), so it merges like one, an undefined one leaving the original's. Children passed after the config replace the element's,
  // and so does a `children` in the config. The element it was made from is not touched. (`defaultProps` are not modelled, here or in createElement. React itself refuses only null and
  // undefined, and fails later, when the broken element it returns for anything else is rendered; the harness refuses anything that is not an element, at the call.)
  function cloneElement(el, config) {
    if (!isValidElement(el)) throw new Error("React.cloneElement(...): The argument must be a React element, but you passed " + el + ".");
    var p = {};
    for (var k in el.props) p[k] = el.props[k];
    var key = el.key;
    var kids = el.children;
    for (var c in (config || {})) {
      if (!Object.prototype.hasOwnProperty.call(config, c)) continue;
      if (c === "key") { if (config.key !== undefined) key = config.key != null ? config.key : null; continue; }
      if (c === "ref" && config.ref === undefined) continue;
      p[c] = config[c];
      if (c === "children") kids = [config.children];
    }
    var rest = Array.prototype.slice.call(arguments, 2);
    if (rest.length) { p.children = rest.length === 1 ? rest[0] : rest; kids = rest; }
    return { __el: true, type: el.type, props: p, key: key, children: kids, out: null };
  }
  // The key React gives a child of a list: its own ("$" + key, "=" and ":" escaped), else its position in base 36.
  function elementKey(child, i) {
    if (isValidElement(child) && child.key != null) return "$" + String(child.key).replace(/[=:]/g, function (m) { return m === "=" ? "=0" : "=2"; });
    return i.toString(36);
  }
  // Children.toArray: the children as one flat array, in order. null, undefined, booleans and functions are dropped, strings and numbers stay, and every element is cloned under the key React
  // gives it (".0", ".1", ".1:0" inside a nested list, ".$k" for the key k). An object that is not an element throws, as it does when it is rendered. Children.map/forEach/count/only are
  // not here: nothing under test calls them, and a harness grows an API when a component needs it, never as a no-op.
  function childrenToArray(children) {
    var out = [];
    (function flat(node, name) {
      if (Array.isArray(node)) {
        node.forEach(function (c, i) { flat(c, (name === "" ? "." : name + ":") + elementKey(c, i)); });
      } else if (typeof node === "string" || typeof node === "number") {
        out.push(node);
      } else if (isValidElement(node)) {
        out.push(cloneElement(node, { key: name === "" ? "." + elementKey(node, 0) : name }));
      } else if (typeof node === "object" && node !== null) {
        throw notAChild(node);
      }
    })(children, "");
    return out;
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
  // React 18's client ids: ":r" + a base-32 counter + ":". One per component instance, kept in its hook slot, so it holds across renders and a remount gets a new one.
  function useId() {
    var s = slotAt();
    if (!s.id) s.id = ":r" + (idCounter++).toString(32) + ":";
    return s.id;
  }
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
    if (!node.__el) {
      if (typeof node === "object") throw notAChild(node);
      return;
    }
    if (typeof node.type === "function" && node.type !== Fragment && node.type.prototype && node.type.prototype.isReactComponent) {
      renderClass(node, path, visited);
    } else if (typeof node.type === "function" && node.type !== Fragment) {
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
  // A class component: one instance per position, setState re-renders, and static getDerivedStateFromError makes it an error boundary (the render of everything below it is
  // tried; a throw below swaps in the state the boundary derives from the error, and the boundary renders again). componentDidMount/componentDidUpdate run after the pass, like effects.
  function renderClass(node, path, visited) {
    var id = path + "/" + typeId(node.type) + (node.key != null ? "#" + node.key : "");
    var inst = instances[id];
    var fresh = !inst || !inst.obj;
    if (!inst) inst = instances[id] = { hooks: [] };
    visited[id] = true;
    var prevProps = null;
    if (fresh) {
      inst.obj = new node.type(node.props);
      inst.obj.__mr_dirty = function () { dirty = true; };
    } else {
      prevProps = inst.obj.props;
    }
    inst.obj.props = node.props;
    var obj = inst.obj;
    try {
      node.out = obj.render();
      renderNode(node.out, id, visited);
    } catch (err) {
      if (typeof node.type.getDerivedStateFromError !== "function") throw err;
      obj.state = Object.assign({}, obj.state, node.type.getDerivedStateFromError(err));
      node.out = obj.render();
      renderNode(node.out, id, visited);
    }
    var hook = fresh ? obj.componentDidMount : obj.componentDidUpdate;
    if (typeof hook === "function") pendingEffects.push({ slot: {}, fn: function () { if (fresh) obj.componentDidMount(); else obj.componentDidUpdate(prevProps); } });
  }
  function Component(props) { this.props = props; this.state = null; }
  Component.prototype.isReactComponent = {};
  Component.prototype.setState = function (partial) {
    var next = typeof partial === "function" ? partial(this.state, this.props) : partial;
    this.state = Object.assign({}, this.state, next);
    this.__mr_dirty();
  };
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
    createElement: createElement, Fragment: Fragment, Component: Component, useState: useState, useReducer: useReducer, useRef: useRef,
    useMemo: useMemo, useCallback: useCallback, useEffect: useEffect, useLayoutEffect: useEffect, useId: useId,
    isValidElement: isValidElement, cloneElement: cloneElement, Children: { toArray: childrenToArray },
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
