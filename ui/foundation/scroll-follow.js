// scroll-follow.js - the auto-pin-to-bottom engine for a chat transcript.
//
// A PURE state machine (SF_init / SF_measure) plus a thin DOM binding
// (SF_bind). The pure core has no DOM and is unit-tested in MiniRacer
// exactly like the other foundation modules; the binding is the only part
// that touches scroll / ResizeObserver / requestAnimationFrame.
//
// Design source: assistant-ui scroll findings (docs/superpowers/
// chat-research/assistant-ui.md section 2.4) + plan Phase 3.3. The one
// idea worth stealing is that a content-driven scroll (scrollHeight grows)
// is never the user, so the only thing that disengines auto-follow is the
// user pulling UP while the height is unchanged.

// ---------------------------------------------------------------------------
// Pure core (SF_ prefix)
// ---------------------------------------------------------------------------

// The follow threshold reuses the status module's constant so the composer
// strip and the scroll law cannot drift. Falls back to 100 if shell-status
// is not loaded (the binding is feature-independent of it).
function SF_defaultFollowPx() {
  return typeof window.SH_FOLLOW_PX === "number" ? window.SH_FOLLOW_PX : 100;
}

// SF_init() -> a fresh follow state. The transcript starts pinned to the
// bottom (followBottom true); the caller flips it off only on a user
// gesture the core recognises as intent.
function SF_init(opts) {
  opts = opts || {};
  var followPx = typeof opts.followPx === "number"
    ? opts.followPx
    : SF_defaultFollowPx();
  var guardMs = typeof opts.programmaticGuardMs === "number"
    ? opts.programmaticGuardMs
    : 150;
  return {
    followBottom: true,
    pendingPin: false,
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    atBottom: true,
    newContentWhileAway: 0,
    options: { followPx: followPx, programmaticGuardMs: guardMs },
  };
}

// SF_measure(state, {scrollTop, scrollHeight, clientHeight, source})
// returns {state, actions}. The state is a fresh object (the binding keeps
// the latest); actions is {scrollToBottom, showJumpPill}.
//
// source is one of:
//   "scroll"       - a real scroll event (may be a user gesture).
//   "content"      - the content grew (ResizeObserver / onContentGrew).
//   "pointerdown"  - the user touched the transcript: cancel any pending pin.
//   "programmatic" - a scroll inside the guard window after a programmatic
//                    scroll: treated as our own action, never user intent.
function SF_measure(state, geom) {
  var source = (geom && geom.source) || "scroll";
  var followPx = state.options.followPx;

  var scrollTop = geom && geom.scrollTop != null ? geom.scrollTop : 0;
  var scrollHeight = geom && geom.scrollHeight != null ? geom.scrollHeight : 0;
  var clientHeight = geom && geom.clientHeight != null ? geom.clientHeight : 0;

  // Distance from the bottom; content that fits the viewport is "at bottom".
  var distance = scrollHeight - scrollTop - clientHeight;
  var atBottom = distance <= followPx || scrollHeight <= clientHeight;

  // Content growth (scrollHeight up) is never the user. Only an upward move
  // with the height UNCHANGED is a genuine user scroll-up.
  var contentGrew = scrollHeight > state.scrollHeight;
  var heightUnchanged = scrollHeight === state.scrollHeight;
  var scrolledUp = scrollTop < state.scrollTop;
  var isUserScrollUp = scrolledUp && heightUnchanged;

  var ns = {
    followBottom: state.followBottom,
    pendingPin: state.pendingPin,
    scrollTop: scrollTop,
    scrollHeight: scrollHeight,
    clientHeight: clientHeight,
    atBottom: atBottom,
    newContentWhileAway: state.newContentWhileAway,
    options: state.options,
  };

  if (source === "programmatic") {
    // Our own scroll (a pin or a jump). It must not read as the user
    // pulling away: keep followBottom, clear the pending pin. Only re-
    // engage (and clear the away count) if we actually landed at bottom.
    ns.pendingPin = false;
    if (atBottom) {
      ns.followBottom = true;
      ns.newContentWhileAway = 0;
    }
  } else if (source === "pointerdown") {
    // A touch/press cancels the pending pin intent so the next content
    // growth (e.g. expanding a tool block) does not hijack the scroll the
    // user is no longer asking for. It does not change followBottom.
    ns.pendingPin = false;
  } else if (source === "content") {
    // Content grew. Never user intent, so it never disengines follow.
    if (ns.followBottom) {
      ns.pendingPin = true;
      if (atBottom) ns.newContentWhileAway = 0;
    } else {
      ns.newContentWhileAway += 1;
    }
  } else {
    // A real scroll event.
    if (atBottom) {
      ns.followBottom = true;
      ns.newContentWhileAway = 0;
      ns.pendingPin = false;
    } else if (isUserScrollUp) {
      ns.followBottom = false;
    }
  }

  // scrollToBottom only when we are following AND content grew; showJumpPill
  // only when we are NOT following (the pill is the "jump to latest" affordance).
  var actions = {
    scrollToBottom: ns.followBottom && contentGrew,
    showJumpPill: !ns.followBottom,
  };

  return { state: ns, actions: actions };
}

// ---------------------------------------------------------------------------
// Thin DOM binding (SF_bind) - the only part that touches the DOM.
// ---------------------------------------------------------------------------

// SF_bind(el, opts) -> {dispose, jumpToBottom, onContentGrew, getState}.
//   el    - the scroll container.
//   opts  - { contentEl, onChange, followPx, programmaticGuardMs }.
// It attaches scroll + pointerdown listeners, a ResizeObserver on the
// content element when available (falling back to explicit onContentGrew
// calls), batches scrollToBottom in requestAnimationFrame, marks
// programmatic scrolls for a guard window, and calls opts.onChange ONLY when
// the visible outcome (pill shown/hidden, or the away count for its label)
// changes - never per scroll event.
function SF_bind(el, opts) {
  opts = opts || {};
  var state = SF_init(opts);
  var guardUntil = 0;
  var pendingPin = false;
  var pendingPinId = 0;
  var last = null;
  var ro = null;

  function snap() {
    return {
      followBottom: state.followBottom,
      showJumpPill: !state.followBottom,
      newContentWhileAway: state.newContentWhileAway,
      pendingPin: state.pendingPin,
    };
  }

  // Emit only when the visible outcome changes: the pill appearing/hiding,
  // or the away count its label reads (so "1 new" -> "2 new" re-renders).
  function emit() {
    var s = snap();
    if (
      !last
      || last.showJumpPill !== s.showJumpPill
      || last.newContentWhileAway !== s.newContentWhileAway
    ) {
      last = s;
      if (typeof opts.onChange === "function") opts.onChange(s);
    }
  }

  function apply(geom) {
    var res = SF_measure(state, geom);
    state = res.state;
    if (res.actions.scrollToBottom) schedulePin();
    emit();
  }

  function geomNow() {
    return {
      scrollTop: el.scrollTop,
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
    };
  }

  function onScroll() {
    var now = Date.now();
    var source = now < guardUntil ? "programmatic" : "scroll";
    var g = geomNow();
    g.source = source;
    apply(g);
  }

  function onContentGrew() {
    var g = geomNow();
    g.source = "content";
    apply(g);
  }

  function onPointerDown() {
    var g = geomNow();
    g.source = "pointerdown";
    apply(g);
    // Cancel a pin that is queued but not yet run.
    if (pendingPin) {
      pendingPin = false;
      if (typeof window.cancelAnimationFrame === "function") window.cancelAnimationFrame(pendingPinId);
      pendingPinId = 0;
    }
  }

  function schedulePin() {
    if (pendingPin) return;
    pendingPin = true;
    var run = function () {
      pendingPin = false;
      el.scrollTop = el.scrollHeight;
      // The resulting scroll is our own: guard it so it is not read as a
      // user pulling away from the bottom.
      guardUntil = Date.now() + state.options.programmaticGuardMs;
    };
    if (typeof window.requestAnimationFrame === "function") {
      pendingPinId = window.requestAnimationFrame(run);
    } else {
      run();
    }
  }

  // Explicit jump (the "jump to latest" pill). Pins now and re-engines follow
  // via the programmatic source so the resulting scroll does not flip off.
  function jumpToBottom() {
    el.scrollTop = el.scrollHeight;
    guardUntil = Date.now() + state.options.programmaticGuardMs;
    var g = geomNow();
    g.source = "programmatic";
    apply(g);
  }

  el.addEventListener("scroll", onScroll, { passive: true });
  el.addEventListener("pointerdown", onPointerDown);

  // Prefer a ResizeObserver on the content element (it fires on growth even
  // when the viewport does not scroll); fall back to explicit onContentGrew
  // calls when it is unavailable.
  var contentEl = opts.contentEl || el;
  if (typeof window.ResizeObserver === "function") {
    ro = new window.ResizeObserver(function () { onContentGrew(); });
    ro.observe(contentEl);
  }

  function dispose() {
    el.removeEventListener("scroll", onScroll);
    el.removeEventListener("pointerdown", onPointerDown);
    if (ro) ro.disconnect();
    if (pendingPin && typeof window.cancelAnimationFrame === "function") {
      window.cancelAnimationFrame(pendingPinId);
    }
  }

  return {
    dispose: dispose,
    jumpToBottom: jumpToBottom,
    onContentGrew: onContentGrew,
    getState: function () { return state; },
  };
}

// ---------------------------------------------------------------------------
// No-build window exports
// ---------------------------------------------------------------------------

window.SF_init = SF_init;
window.SF_measure = SF_measure;
window.SF_bind = SF_bind;
window.SF_defaultFollowPx = SF_defaultFollowPx;
