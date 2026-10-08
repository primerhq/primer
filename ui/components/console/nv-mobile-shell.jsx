/* global React, SH_api, NV_useConsole, NV_identity, NV_errText,
   NV_renderStudioDoc, NV_themeStorageKey, NV_HealthCards, NV_PLAT_GROUPS */
// The mobile shell (US-014 mobile support): entry + skeleton (M1), the
// Inbox tab's real cards (M2), the mobile chat screen (M3), and the
// Spaces + Files tabs (M4).
// NV_Shell swaps here on primerApi.
// useViewport().isMobile instead of rendering the actbar/topbar/studio
// tree - per the phase plan (docs/superpowers/plans/2026-08-29-mobile-
// shell-phases.md) this is a DISTINCT mobile UX, not a squeezed desktop
// one, sharing all state/resources with the desktop shell via the same
// NV_ConsoleContext.
//
// Bottom nav (Inbox/Spaces/Files/More) over shared/mobile-tabs.jsx.
// Spaces/Files/More are still M4/M5 stub panels. Inbox (M2) is real:
// the same cross-workspace aggregate + cache key nv-rail.jsx's own Inbox
// uses ("nv-rail-inbox"), rendered via the shared/card-list.jsx
// primitives (CardList/Card - already in-tree, unused by the console
// until now) as cards (kind / workspace / agent identity).
//
// Deliberately the LIGHT shape (design ruling, 2026-08-29): the mobile
// Inbox is a TRIAGE feed, not a squeezed decision screen - no inline
// answer textarea, no inline Reject-with-reason; those belong to the full
// session, which Review reaches. That ruling is amended (console review
// C-033): an inline Approve is never blind. The aggregate row (GET
// /v1/yields/pending) now carries the parked tool_call_id and a short
// preview of the call (tool, decisive arguments) or of the question, so
// the feed still does zero extra fetches at render; the card shows what
// it would approve, and Approve and Deny name that exact call (a card
// that has gone stale answers 404 and is refreshed away rather than
// approving whatever the session is parked on now). A card that cannot
// say what it approves offers "Open to review" instead of Approve, and
// "Show all" fetches the full arguments of that one call on demand.
//
// "Review…" opens a session via the same con.openInWorkspace +
// promoteDoc combo the rail and palette already use (single history
// push, promoted) - it lands on NV_MobileChatScreen (M3, below), a
// full-screen takeover over the bottom nav, same shape as M4's file/diff
// screens. The URL still names it (spec pt 6).
//
// testids: nv-mobile-shell, nv-mobile-panel:{tabId},
// nv-mobile-inbox-card:{sid}, nv-mobile-inbox-approve:{sid},
// nv-mobile-inbox-deny:{sid}, nv-mobile-inbox-review:{sid},
// nv-mob-ib-showall:{sid}, nv-mob-session-screen, nv-mob-screen-back

function NV_MobileStub(props) {
  return (
    <div className="nv-mobile-stub" data-testid={"nv-mobile-panel:" + props.tabId}>
      <p>{props.text}</p>
    </div>
  );
}

// Whether this user may decide a gated call: the server's ApproverSpec.allows. Admins always; "anyone" and no spec admit everyone;
// "roles" admits the listed roles; "users" the named usernames; a malformed spec fails open, as it does there. The route answers 403
// approver_mismatch for anyone else, so the card does not offer a button that can only fail.
function NV_mobileMayDecide(approvers, who) {
  if (!approvers || !who) return true;
  if (who.role === "admin") return true;
  if (approvers.kind === "roles") return (approvers.roles || []).indexOf(who.role) >= 0;
  if (approvers.kind === "users") return (approvers.users || []).indexOf(who.username) >= 0;
  return true;
}

// The card's words (console review C-032, C-033). Pure, so V8 tests drive it: what the kind is called, the one descriptive line, the
// decisive arguments of an approval, and whether the card may offer a decision inline. An inline decision is offered only when the
// card shows WHAT it decides (the tool, which the aggregate row carries), names the call (its id) and the user may decide it. A card
// with no `approval` may not be a real _approval gate at all (a graph park whose primary gate is something else), so it gets neither
// Approve nor Deny: it says to open the session.
function NV_mobileInboxView(it, who) {
  var view = { kindLabel: "Parked", line: "", args: "", truncated: false, canApprove: false, canDeny: false, notApprover: false, note: "" };
  if (it.kind === "approval") {
    var a = it.approval;
    view.kindLabel = "Approval";
    view.line = a && a.tool_name ? a.tool_name : "A tool call is waiting for your approval";
    if (a && a.tool_name) {
      view.args = a.arguments || "";
      view.truncated = !!a.truncated;
    }
    var decidable = !!(a && a.tool_name && it.tool_call_id);
    var may = NV_mobileMayDecide(it.approvers, who);
    view.canApprove = decidable && may;
    view.canDeny = decidable && may;
    view.notApprover = decidable && !may;
    if (view.notApprover) view.note = "Only its approvers can decide this. You can still open it.";
  } else if (it.kind === "ask") {
    view.kindLabel = "Question";
    view.line = it.prompt || "The agent has a question for you";
  } else {
    view.line = it.prompt || "Waiting on you";
  }
  return view;
}

function NV_mobileInboxHeading(count) {
  return { title: "Inbox", count: count ? count + " waiting on you" : "Nothing waiting on you" };
}

// Approve or Deny the call a card showed. The decision names the card's own tool_call_id, so a session that has since parked on
// something else answers 404 instead of having that other call approved (it used to fetch the session's first pending yield AFTER
// the tap and approve whatever that was). Every outcome reaches the toast; a stale card is refreshed away.
// Resolves {ok}, {stale} or {failed}; never rejects.
function NV_inboxDecide(decision, it, toast, onResolved) {
  var verb = decision === "approve" ? "Approve" : "Deny";
  if (!it.tool_call_id) {
    toast(verb + " failed: this approval does not name a call. Open it to review.", { kind: "error", requestId: null });
    return Promise.resolve({ failed: true });
  }
  var request = decision === "approve"
    ? SH_api.approve(it.session_id, it.tool_call_id)
    : SH_api.reject(it.session_id, it.tool_call_id, "");
  return request.then(function () {
    toast((decision === "approve" ? "Approved " : "Denied ") + ((it.approval && it.approval.tool_name) || "the call"));
    if (onResolved) onResolved();
    return { ok: true };
  }, function (err) {
    if (err && (err.status === 404 || err.status === 409)) {
      toast("That approval has moved on. Open the session to review what it is waiting on now.", {
        kind: "error", requestId: err.requestId || err.request_id || null,
      });
      if (onResolved) onResolved();
      return { stale: true };
    }
    toast(verb + " failed: " + ((err && (err.detail || err.message)) || "unknown error"), {
      kind: "error", requestId: (err && (err.requestId || err.request_id)) || null,
    });
    return { failed: true };
  });
}

// "Show all": the full arguments of the exact call the card names (one request, only when asked for).
// Resolves {text}, {gone} (the session is no longer parked on that call) or {failed, error}; never rejects.
function NV_inboxFullCall(it) {
  return SH_api.sessionPendingYields(it.workspace_id, it.session_id).then(function (out) {
    var row = ((out && out.items) || []).filter(function (r) { return r.tool_call_id === it.tool_call_id; })[0];
    var call = row && row.resume_metadata && row.resume_metadata.original_call;
    if (!call) return { gone: true };
    return { text: JSON.stringify(call.arguments === undefined ? {} : call.arguments, null, 2) };
  }, function (err) {
    return { failed: true, error: (err && (err.detail || err.message)) || "unknown error" };
  });
}

function NV_MobileDecisionButton(props) {
  var con = NV_useConsole();
  var it = props.item;
  var approve = props.decision === "approve";
  var busyState = React.useState(false);
  var busy = busyState[0];
  var setBusy = busyState[1];
  return (
    <button type="button" className={(approve ? "nv-btn-primary" : "nv-btn-secondary") + " touch-target"}
      data-testid={"nv-mobile-inbox-" + props.decision + ":" + it.session_id}
      disabled={busy}
      onClick={function (ev) {
        ev.stopPropagation();
        setBusy(true);
        NV_inboxDecide(props.decision, it, con.toast, props.onResolved).then(function () { setBusy(false); });
      }}>{busy ? (approve ? "Approving…" : "Denying…") : (approve ? "Approve" : "Deny")}</button>
  );
}

function NV_MobileInboxCard(props) {
  var con = NV_useConsole();
  var it = props.item;
  var view = NV_mobileInboxView(it, { username: con.username, role: con.role });
  var ident = NV_identity(it.agent_binding);
  var fullState = React.useState(null);
  var full = fullState[0];
  var setFull = fullState[1];

  function review() {
    if (con.openInWorkspace) {
      con.openInWorkspace(it.workspace_id, { kind: "session", ref: it.session_id });
    } else {
      con.setDoc({ kind: "session", ref: it.session_id });
    }
    if (con.promoteDoc) con.promoteDoc("session:" + it.session_id);
  }

  function toggleFull(ev) {
    ev.stopPropagation();
    if (full) { setFull(null); return; }
    setFull({ loading: true });
    NV_inboxFullCall(it).then(function (res) {
      if (res.text !== undefined) { setFull({ text: res.text }); return; }
      setFull(null);
      con.toast(res.gone
        ? "That call has moved on. Open the session to review what it is waiting on now."
        : "Could not load the full call: " + res.error, { kind: "error", requestId: null });
    });
  }

  // A question or a wait: the whole card is the Review link. An approval stays non-interactive at the card level, so the
  // decision buttons are the only things that act and tapping the text never decides anything.
  var whole = it.kind !== "approval";
  // An approval that cannot be decided inline and is not merely off-limits to this user: the card says to open it.
  var openToReview = it.kind === "approval" && !view.canApprove && !view.notApprover;
  return (
    <article className="nv-mob-ib-card" data-kind={it.kind}
      data-testid={"nv-mobile-inbox-card:" + it.session_id}
      role={whole ? "button" : undefined} tabIndex={whole ? 0 : undefined}
      onClick={whole ? review : undefined}
      onKeyDown={whole ? function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); review(); }
      } : undefined}>
      <div className="nv-mob-ib-top">
        <span className="nv-mob-ib-kind" data-testid="nv-mob-ib-kind">{view.kindLabel}</span>
        <span className="nv-mob-ib-where">{it.workspace_name || it.workspace_id}</span>
      </div>
      <div className="nv-mob-ib-title">
        <svg width="11" height="11" viewBox="0 0 12 12" style={{ flexShrink: 0, color: ident.color }}>
          <path d={ident.d} fill="currentColor" />
        </svg>
        <span>{it.session_name || it.session_id}</span>
      </div>
      <div className="nv-mob-ib-line" data-testid="nv-mob-ib-line">{view.line}</div>
      {view.note ? <div className="nv-mob-ib-note" data-testid="nv-mob-ib-note">{view.note}</div> : null}
      {view.args ? <div className="nv-mob-ib-args" data-testid="nv-mob-ib-args">{view.args}</div> : null}
      {view.truncated ? (
        <button type="button" className="nv-mob-ib-showall"
          data-testid={"nv-mob-ib-showall:" + it.session_id}
          onClick={toggleFull}>{full ? "Hide" : "Show all"}</button>
      ) : null}
      {full ? (
        <pre className="nv-mob-ib-full" data-testid={"nv-mob-ib-full:" + it.session_id}>
          {full.loading ? "Loading…" : full.text}
        </pre>
      ) : null}
      <div className="nv-mobile-inbox-actions">
        {view.canApprove ? (
          <NV_MobileDecisionButton decision="approve" item={it} onResolved={props.onResolved} />
        ) : null}
        {view.canDeny ? (
          <NV_MobileDecisionButton decision="deny" item={it} onResolved={props.onResolved} />
        ) : null}
        <button type="button"
          className={(openToReview ? "nv-btn-primary" : "nv-btn-secondary") + " touch-target"}
          data-testid={"nv-mobile-inbox-review:" + it.session_id}
          onClick={function (ev) { ev.stopPropagation(); review(); }}>
          {openToReview ? "Open to review" : "Review…"}
        </button>
      </div>
    </article>
  );
}

function NV_MobileInboxPanel(props) {
  var heading = NV_mobileInboxHeading(props.items.length);
  return (
    <div className="nv-mob-inbox" data-testid="nv-mobile-panel:inbox">
      <div className="nv-mob-ib-head">
        <h1 className="nv-mob-ib-h">{heading.title}</h1>
        <span className="nv-mob-ib-count" data-testid="nv-mob-ib-count">{heading.count}</span>
      </div>
      {props.items.length ? (
        <div className="nv-mob-ib-list">
          {props.items.map(function (it) {
            return <NV_MobileInboxCard key={it.session_id} item={it} onResolved={props.onResolved} />;
          })}
        </div>
      ) : (
        <div className="nv-mob-ib-empty">Nothing needs you right now.</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// US-014 M3: the mobile chat screen - full-screen session doc, replacing
// M2's "not built yet" fallback. Same shell-level takeover shape as M4's
// NV_MobileFileScreen (.nv-mob-screen head+body, a real Back button).
//
// Reuses NV_SessionDoc verbatim - the exact mount nv-studio.jsx's own
// NV_renderStudioDoc uses on desktop (sid is its only prop; everything
// else comes off con.wid and its own resources) - wrapped in
// .nv-mobile-chat so styles.css can restyle its EXISTING pieces for
// touch/messenger-layout without forking the component:
//   - .nv-turn-user / .nv-turn-agent (already distinct classes on every
//     turn, just not yet styled differently) become right/left message
//     bubbles with NV_identity's own colors.
//   - .nv-bind-menu and the "⋯" session-actions .nv-menu-right dropdown -
//     both already close via their own row click or the desktop outside-
//     click listener, no CSS dependency there - reposition to slide up
//     from the bottom, the same visual language as shared/bottom-
//     sheet.jsx's own .sheet, without a second BottomSheet component.
//   - .nv-trace-split (a side panel on desktop, closed by its own
//     nv-trace-close button) gets the same bottom-sheet repositioning
//     instead of a side-by-side split.
//   - --hit (the composer/mic/send sizing token) is bumped to 44px for
//     the whole mobile shell - nv-composer-iconbtn/nv-send-btn/
//     nv-stop-btn are its only consumers (test_touch_targets.py's
//     HIT_SIZED list), so this has no effect outside the composer.
//
// Mid-run queueing ("+Q" in the design source) is already live and
// reused as-is: the send button already renders data-mode="queue" and
// labels itself "Queue" while props.running is true - not
// reimplemented, and not relabelled just to chase the mockup's
// shorthand for something already clear. Mic hold/double-tap-latch also
// reused as-is, now with real onTouchStart/End handlers alongside the
// existing mouse ones (nv-session-doc.jsx) for touch devices where a
// synthesized mousedown/up is unreliable.
// ---------------------------------------------------------------------------

function NV_MobileChatScreen(props) {
  var con = NV_useConsole();
  var sid = props.doc.ref;
  var meta = con.resolveSessionMeta && con.resolveSessionMeta(sid);
  return (
    <div className="nv-mob-screen" data-testid="nv-mob-session-screen">
      <div className="nv-mob-screen-head">
        <button type="button" className="nv-mob-back touch-target"
          data-testid="nv-mob-screen-back"
          onClick={function () { con.setDoc(null); }}>&lsaquo; Back</button>
        <span className="nv-mob-screen-title">{(meta && meta.name) || sid}</span>
      </div>
      <div className="nv-mob-screen-body nv-mobile-chat">
        {typeof window.NV_SessionDoc === "function" ? (
          // queueLabel: the mockup's compact "+Q" for the mid-run send
          // button - the queue BEHAVIOR itself (data-mode="queue", the
          // desktop's own steer-while-running semantics) is unchanged,
          // this only overrides the button's text (nv-session-doc.jsx's
          // NV_Composer, default "Queue" for every other caller).
          <window.NV_SessionDoc key={sid} sid={sid} queueLabel="+Q" />
        ) : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// US-014 M4: file/diff docs push FULL-SCREEN over the whole shell (nav bar
// included), the same shell-level takeover NV_MobileChatScreen (M3) uses
// for con.doc.kind === "session".
//
// diff reuses nv-studio.jsx's own NV_renderStudioDoc(con, doc) dispatcher
// (-> NV_DiffDoc) verbatim - that doc is already pure display, nothing to
// strip. file does NOT reuse NV_FileDoc the same way: this phase is
// explicitly read-only (no upload/new/rename on mobile), and NV_FileDoc is
// a full editor (draft state, Ctrl+S, etag/412-conflict UI) - reusing it
// would have quietly shipped Save on mobile. NV_MobileFileView below is a
// deliberately small, separate, read-only read of the same SH_api.fileRead
// route instead.
// ---------------------------------------------------------------------------

function NV_MobileFileView(props) {
  var con = NV_useConsole();
  var path = props.path;
  var read = window.primerApi.useResource(
    SH_api.keys.file(con.wid, path),
    function (signal) { return SH_api.fileRead(con.wid, path, signal); },
    { pollMs: 0, deps: [con.wid, path] }
  );
  var meta = read.data || {};
  var binary = meta.encoding === "base64";
  return (
    <div className="nv-mob-file-view" data-testid={"nv-mob-file-view:" + path}>
      {binary ? (
        <div className="card-list-empty">
          {path} is binary - open it on desktop to view.
        </div>
      ) : (
        <pre className="nv-mob-file-content">{meta.content || ""}</pre>
      )}
    </div>
  );
}

function NV_MobileFileScreen(props) {
  var con = NV_useConsole();
  var doc = props.doc;
  return (
    <div className="nv-mob-screen" data-testid={"nv-mob-" + doc.kind + "-screen"}>
      <div className="nv-mob-screen-head">
        <button type="button" className="nv-mob-back touch-target"
          data-testid="nv-mob-screen-back"
          onClick={function () { con.setDoc(null); }}>&lsaquo; Back</button>
        <span className="nv-mob-screen-title">
          {doc.kind === "diff" ? String(doc.ref).slice(0, 7) : doc.ref}
        </span>
      </div>
      <div className="nv-mob-screen-body">
        {doc.kind === "diff" ? NV_renderStudioDoc(con, doc)
          : <NV_MobileFileView path={doc.ref} />}
      </div>
    </div>
  );
}

// One hook call per mounted pulse dot (React's rules of hooks) - a LOCAL
// copy of nv-rail.jsx's NV_Rail_SessionPulse (and nv-tab-groups.jsx's own
// NV_TG_SessionPulse), same reasoning both of those give: a hook-bearing
// leaf stays local rather than shared bare across files, so none of the
// three has a load-order dependency on either of the others.
function NV_Mobile_Pulse(props) {
  var statusSnap = typeof window.useSessionStore === "function"
    ? window.useSessionStore(props.wid, props.sid, "status")
    : null;
  if (!statusSnap || !statusSnap.verb) return null;
  return <span className="nv-dot-pulse" title="running" />;
}

// ---------------------------------------------------------------------------
// US-014 M4: Spaces tab - the workspace/session tree (expand -> sessions,
// pulse/attention dots) + the Create Session FAB.
//
// Data: con.workspaces (nv-shell.jsx's own "nv-workspaces" resource, free -
// no extra fetch) plus the rail's own "nv-rail-all-sessions"/"nv-rail-inbox"
// cache keys (nv-rail.jsx) so the tree's session list and attention dots
// are the SAME fetch the desktop rail and the Inbox tab already pay for,
// not a third independent poll for the same data.
//
// Opening a session reuses the Inbox tab's own review() shape verbatim
// (con.openInWorkspace + promoteDoc, single history push) - it lands on
// the same NV_MobileChatScreen (M3) takeover in NV_MobileShell below,
// not a bespoke mobile navigation path.
// ---------------------------------------------------------------------------

function NV_MobileSpaces() {
  var con = NV_useConsole();
  var workspaces = con.workspaces || [];
  var sessRes = window.primerApi.useResource(
    "nv-rail-all-sessions",
    function (signal) { return SH_api.allSessions(signal); },
    { pollMs: 5000, deps: [] }
  );
  var inboxRes = window.primerApi.useResource(
    "nv-rail-inbox",
    function (signal) {
      return SH_api.pendingAttention(signal).catch(function (err) {
        if (err && err.status !== 404) throw err;
        return { items: [] };
      });
    },
    { pollMs: 10000, deps: [] }
  );

  var expandedState = React.useState({});
  var expanded = expandedState[0];
  var setExpanded = expandedState[1];
  var createOpenState = React.useState(false);
  var createOpen = createOpenState[0];
  var setCreateOpen = createOpenState[1];

  var sessions = (sessRes.data && sessRes.data.items) || [];
  var inboxItems = (inboxRes.data && inboxRes.data.items) || [];

  var sessionsByWs = {};
  sessions.forEach(function (s) {
    var wid = s.workspace_id;
    if (!sessionsByWs[wid]) sessionsByWs[wid] = [];
    sessionsByWs[wid].push(s);
  });
  var attentionSidsByWs = {};
  inboxItems.forEach(function (it) {
    var wid = it.workspace_id;
    if (!attentionSidsByWs[wid]) attentionSidsByWs[wid] = {};
    attentionSidsByWs[wid][it.session_id] = true;
  });

  function openSession(s, wid) {
    if (con.openInWorkspace) {
      con.openInWorkspace(wid, { kind: "session", ref: s.session_id });
    } else {
      con.setDoc({ kind: "session", ref: s.session_id });
    }
    if (con.promoteDoc) con.promoteDoc("session:" + s.session_id);
    // F1/F10-style stamp (2026-08-29 UI review, desktop rail/palette):
    // the tree's own row already carries name/binding, so hand it to the
    // shared cache immediately rather than waiting on the next poll.
    if (con.stampSessionMeta) {
      con.stampSessionMeta(s.session_id, { wid: wid, name: s.name, binding: s.binding });
    }
  }

  return (
    <div className="nv-mob-spaces" data-testid="nv-mobile-panel:spaces">
      <div className="card-list" data-testid="nv-mob-ws-tree">
        {!workspaces.length ? (
          <div className="card-list-empty">No workspaces yet.</div>
        ) : null}
        {workspaces.map(function (w) {
          var isOpen = !!expanded[w.id];
          var wsSessions = sessionsByWs[w.id] || [];
          var attnCount = Object.keys(attentionSidsByWs[w.id] || {}).length;
          return (
            <div key={w.id} className="nv-mob-ws-group">
              <button type="button" className="card card-interactive nv-mob-ws-row"
                data-testid={"nv-mob-ws:" + w.id}
                onClick={function () {
                  setExpanded(function (prev) {
                    var next = Object.assign({}, prev);
                    next[w.id] = !prev[w.id];
                    return next;
                  });
                }}>
                <div className="card-row">
                  <span className={"nv-mob-chevron" + (isOpen ? " nv-mob-chevron-open" : "")}>
                    &rsaquo;
                  </span>
                  <div className="card-title-wrap">
                    <div className="card-title">{w.name || w.id}</div>
                    <div className="card-subtitle">
                      {wsSessions.length} session{wsSessions.length === 1 ? "" : "s"}
                    </div>
                  </div>
                  {attnCount > 0 ? (
                    <span className="nv-rail-attn-count">{attnCount}</span>
                  ) : null}
                </div>
              </button>
              {isOpen ? (
                <div className="nv-mob-ws-sessions">
                  {!wsSessions.length ? (
                    <div className="card-list-empty">No sessions yet.</div>
                  ) : null}
                  {wsSessions.map(function (s) {
                    var sid = s.session_id;
                    var ident = NV_identity(s.binding);
                    var isAttention = !!(attentionSidsByWs[w.id] && attentionSidsByWs[w.id][sid]);
                    return (
                      <button type="button" key={sid}
                        className="card card-interactive nv-mob-session-row"
                        data-testid={"nv-mob-session:" + sid}
                        onClick={function () { openSession(s, w.id); }}>
                        <div className="card-row">
                          <svg width="12" height="12" viewBox="0 0 12 12"
                            style={{ flexShrink: 0, color: ident.color }}>
                            <path d={ident.d} fill="currentColor" />
                          </svg>
                          <div className="card-title-wrap">
                            <div className="card-title">{s.name || sid}</div>
                          </div>
                          {isAttention ? (
                            <span className="nv-dot-attention" title="needs you" />
                          ) : (
                            <NV_Mobile_Pulse wid={w.id} sid={sid} />
                          )}
                        </div>
                      </button>
                    );
                  })}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
      <window.Fab icon="plus" label="Create session"
        onClick={function () { setCreateOpen(true); }} />
      <NV_MobileCreateSessionSheet open={createOpen} workspaces={workspaces}
        onClose={function () { setCreateOpen(false); }}
        onCreated={function (row, wid, name, binding) {
          sessRes.refetch();
          var sid = row && (row.session_id || row.id);
          con.toast("Session created" + (sid ? ": " + sid : ""));
          if (!sid) return;
          if (con.openInWorkspace) {
            con.openInWorkspace(wid, { kind: "session", ref: sid });
          } else {
            con.setDoc({ kind: "session", ref: sid });
          }
          if (con.promoteDoc) con.promoteDoc("session:" + sid);
          if (con.stampSessionMeta) {
            con.stampSessionMeta(sid, { wid: wid, name: name, binding: binding });
          }
        }} />
    </div>
  );
}

// Staged BottomSheet form (task brief: "workspace picker sheet -> agent/
// graph picker sheet with search ... -> name + first-instruction inputs ->
// create via the same POST the overlay uses, then deep-link the new
// session") - three full-attention screens instead of nv-overlays.jsx's
// own NV_CreateSessionOverlay (one form with an inline dropdown-style bind
// picker): a touch list you scroll and tap reads better than a tiny
// desktop dropdown, per the phase plan's "distinct mobile UX, not squeezed
// desktop" principle. Submit body mirrors that overlay's SharedNewSessionForm
// contract verbatim (omitting binding asks for the system default agent;
// auto_start follows whether an instruction was typed) - deliberately NOT
// the overlay's advanced/graph-input-schema section, which the brief does
// not ask for.
function NV_MobileCreateSessionSheet(props) {
  var stageState = React.useState("workspace");
  var stage = stageState[0];
  var setStage = stageState[1];
  var widState = React.useState(null);
  var wid = widState[0];
  var setWid = widState[1];
  var bindState = React.useState(null);
  var bind = bindState[0];
  var setBind = bindState[1];
  var qState = React.useState("");
  var q = qState[0];
  var setQ = qState[1];
  var nameState = React.useState("");
  var name = nameState[0];
  var setName = nameState[1];
  var instrState = React.useState("");
  var instr = instrState[0];
  var setInstr = instrState[1];
  var busyState = React.useState(false);
  var busy = busyState[0];
  var setBusy = busyState[1];
  var errState = React.useState(null);
  var err = errState[0];
  var setErr = errState[1];

  // SAME cache keys as nv-overlays.jsx's NV_CreateSessionOverlay - the
  // desktop and mobile pickers share one fetch for the same lists.
  var agents = window.primerApi.useResource(
    "nv-ov:agents",
    function (signal) { return SH_api.agents(signal); },
    { pollMs: 0 }
  );
  var graphs = window.primerApi.useResource(
    "nv-ov:graphs",
    function (signal) { return SH_api.graphs(signal); },
    { pollMs: 0 }
  );

  function reset() {
    setStage("workspace"); setWid(null); setBind(null); setQ("");
    setName(""); setInstr(""); setBusy(false); setErr(null);
  }
  function close() { reset(); if (props.onClose) props.onClose(); }

  function submit() {
    if (!wid || busy) return;
    setBusy(true);
    setErr(null);
    var binding = bind
      ? (bind.kind === "graph"
        ? { kind: "graph", graph_id: bind.id }
        : { kind: "agent", agent_id: bind.id })
      : null;
    var trimmedName = name.trim() || null;
    var body = { auto_start: instr.trim().length > 0 };
    if (binding) body.binding = binding;
    if (trimmedName) body.name = trimmedName;
    if (instr.trim()) body.initial_instructions = instr.trim();
    SH_api.createSession(wid, body).then(function (row) {
      setBusy(false);
      // Stamp with what THIS form already knows, same as nv-overlays.jsx's
      // own NV_CreateSessionOverlay - the new tab's label/glyph/pulse then
      // don't sit blind/default until the next poll, and it does not
      // depend on the response echoing the same shape back.
      if (props.onCreated) {
        props.onCreated(row || {}, wid, trimmedName, binding);
      }
      close();
    }, function (e) {
      setBusy(false);
      setErr(e);
    });
  }

  var agentItems = (agents.data && agents.data.items) || [];
  var graphItems = (graphs.data && graphs.data.items) || [];
  var rows = [];
  agentItems.forEach(function (a) {
    rows.push({ kind: "agent", id: a.id, desc: a.description || "" });
  });
  graphItems.forEach(function (g) {
    rows.push({ kind: "graph", id: g.id, desc: g.description || "" });
  });
  var ql = q.trim().toLowerCase();
  var visible = !ql ? rows : rows.filter(function (r) {
    return (r.id + " " + r.desc).toLowerCase().indexOf(ql) >= 0;
  });

  var title = stage === "workspace" ? "New session in…"
    : stage === "bind" ? "Bind to an agent or graph"
      : "Name it";

  return (
    <window.BottomSheet open={!!props.open} onClose={close} title={title}
      footer={stage === "details" ? (
        <React.Fragment>
          <button type="button" className="nv-btn-secondary touch-target"
            onClick={close}>Cancel</button>
          <button type="button" className="nv-btn-primary touch-target"
            data-testid="nv-mob-ns-create" disabled={busy}
            onClick={submit}>
            {busy ? "Creating…" : "Create session"}
          </button>
        </React.Fragment>
      ) : null}>
      {err ? (
        <div className="nv-form-error" data-testid="nv-mob-ns-error">
          {NV_errText(err)}
        </div>
      ) : null}
      {stage === "workspace" ? (
        <div className="card-list" data-testid="nv-mob-ns-workspace-list">
          {!(props.workspaces || []).length ? (
            <div className="card-list-empty">No workspaces yet.</div>
          ) : null}
          {(props.workspaces || []).map(function (w) {
            return (
              <button type="button" key={w.id} className="card card-interactive"
                data-testid={"nv-mob-ns-wid:" + w.id}
                onClick={function () { setWid(w.id); setStage("bind"); }}>
                <div className="card-title">{w.name || w.id}</div>
              </button>
            );
          })}
        </div>
      ) : null}
      {stage === "bind" ? (
        <React.Fragment>
          <input className="nv-input" autoFocus value={q}
            data-testid="nv-mob-ns-search"
            placeholder="Search agents & graphs…"
            onChange={function (ev) { setQ(ev.target.value); }} />
          <div className="card-list" data-testid="nv-mob-ns-bind-list">
            <button type="button" className="card card-interactive"
              data-testid="nv-mob-ns-bind-default"
              onClick={function () { setBind(null); setStage("details"); }}>
              <div className="card-title">Default agent</div>
            </button>
            {visible.map(function (r) {
              return (
                <button type="button" key={r.kind + ":" + r.id}
                  className="card card-interactive"
                  data-testid={"nv-mob-ns-bind:" + r.id}
                  onClick={function () {
                    setBind({ kind: r.kind, id: r.id });
                    setStage("details");
                  }}>
                  <div className="card-title">{r.id}</div>
                  <div className="card-subtitle">
                    {r.kind}{r.desc ? " · " + r.desc : ""}
                  </div>
                </button>
              );
            })}
            {!visible.length ? (
              <div className="card-list-empty">No agent or graph matches.</div>
            ) : null}
          </div>
        </React.Fragment>
      ) : null}
      {stage === "details" ? (
        <React.Fragment>
          <input className="nv-input" autoFocus value={name}
            data-testid="nv-mob-ns-name"
            placeholder="Name (optional)"
            onChange={function (ev) { setName(ev.target.value); }} />
          <textarea className="nv-textarea" data-testid="nv-mob-ns-instr"
            style={{ minHeight: 96, marginTop: 10 }}
            placeholder="What should it do first? (optional)"
            value={instr}
            onChange={function (ev) { setInstr(ev.target.value); }} />
        </React.Fragment>
      ) : null}
    </window.BottomSheet>
  );
}

// ---------------------------------------------------------------------------
// US-014 M4: Files tab - workspace picker sheet + tree -> full-screen file
// view (con.setDoc kind "file", the shell-level takeover above); History
// -> commit list -> diff screen (con.setDoc kind "diff", same takeover).
//
// The tree/history fetches use the EXACT cache keys nv-files-sidebar.jsx's
// desktop panel already uses (SH_api.keys.tree/log) - expanding a folder
// here and on desktop share one fetch, and workspace switching goes
// through the SAME "workspace.switch" verb the rail's own tree row and
// nv-studio.jsx's onSelectWorkspace call (con.registry.get(...).run({wid})),
// not a bespoke mobile-only setWid path.
// ---------------------------------------------------------------------------

function NV_Mobile_FilesSubtree(props) {
  // Local copy of nv-files-sidebar.jsx's own NV_FilesSubtree (hook-bearing
  // leaf, same "kept local" reasoning as NV_Mobile_Pulse above) - same
  // cache key though, so this shares its fetch/poll with the desktop tree.
  var tree = window.primerApi.useResource(
    SH_api.keys.tree(props.wid, props.path),
    function (signal) { return SH_api.filesTree(props.wid, props.path, signal); },
    { pollMs: 5000, deps: [props.wid, props.path] }
  );
  var items = (tree.data && tree.data.items) || [];
  return (
    <React.Fragment>
      {items.map(function (entry) { return props.row(entry, props.depth); })}
    </React.Fragment>
  );
}

function NV_MobileFiles() {
  var con = NV_useConsole();
  var wid = con.wid;
  var workspaces = con.workspaces || [];
  var ws = workspaces.find(function (w) { return w.id === wid; });

  var tree = window.primerApi.useResource(
    SH_api.keys.tree(wid || "_", "."),
    function (signal) {
      return wid ? SH_api.filesTree(wid, ".", signal) : Promise.resolve({ items: [] });
    },
    { pollMs: 5000, deps: [wid] }
  );
  var commits = window.primerApi.useResource(
    SH_api.keys.log(wid || "_"),
    function (signal) {
      return wid ? SH_api.commitLog(wid, 50, signal) : Promise.resolve({ commits: [] });
    },
    { pollMs: 0, deps: [wid] }
  );

  var pickerOpenState = React.useState(false);
  var pickerOpen = pickerOpenState[0];
  var setPickerOpen = pickerOpenState[1];
  var openDirsState = React.useState({});
  var openDirs = openDirsState[0];
  var setOpenDirs = openDirsState[1];
  var historyState = React.useState(false);
  var history = historyState[0];
  var setHistory = historyState[1];

  var items = (tree.data && tree.data.items) || [];
  var commitRows = SH_api.commitRows(commits.data);

  function pickWorkspace(newWid) {
    var verb = con.registry.get("workspace.switch");
    if (verb) verb.run({ wid: newWid });
    setPickerOpen(false);
    setHistory(false);
  }

  function row(entry, depth) {
    var isOpen = !!openDirs[entry.path];
    return (
      <React.Fragment key={entry.path}>
        <button type="button" className="card card-interactive nv-mob-file-row"
          style={{ paddingLeft: 14 + depth * 16 }}
          data-testid={"nv-mob-file:" + entry.path}
          onClick={function () {
            if (entry.is_dir) {
              setOpenDirs(function (prev) {
                var next = Object.assign({}, prev);
                if (next[entry.path]) delete next[entry.path];
                else next[entry.path] = true;
                return next;
              });
            } else {
              con.setDoc({ kind: "file", ref: entry.path });
            }
          }}>
          <div className="card-row">
            <span className={"nv-mob-chevron"
              + (entry.is_dir && isOpen ? " nv-mob-chevron-open" : "")}>
              {entry.is_dir ? "›" : ""}
            </span>
            <span className="card-title">{entry.path.split("/").pop()}</span>
          </div>
        </button>
        {entry.is_dir && isOpen ? (
          <NV_Mobile_FilesSubtree wid={wid} path={entry.path} depth={depth + 1} row={row} />
        ) : null}
      </React.Fragment>
    );
  }

  return (
    <div className="nv-mob-files" data-testid="nv-mobile-panel:files">
      <div className="nv-mob-files-head">
        {/* Only worth a picker when there is a real choice - one
            workspace (or none loaded yet) makes con.wid implicit, same
            "distinct mobile UX, not squeezed desktop" call as the rest of
            this phase: no tap wasted opening a sheet with one row in it. */}
        {workspaces.length > 1 ? (
          <button type="button" className="nv-mob-ws-picker touch-target"
            data-testid="nv-mob-files-ws-picker"
            onClick={function () { setPickerOpen(true); }}>
            {(ws && (ws.name || ws.id)) || wid || "Choose a workspace"}{" "}
            <span aria-hidden="true">&#9662;</span>
          </button>
        ) : (
          <span className="nv-mob-ws-picker" data-testid="nv-mob-files-ws-label">
            {(ws && (ws.name || ws.id)) || wid || "No workspace"}
          </span>
        )}
        <div style={{ flex: 1 }} />
        {wid ? (
          <button type="button" className="nv-rail-iconbtn touch-target"
            title="History" data-testid="nv-mob-files-history"
            data-active={history ? "true" : "false"}
            onClick={function () { setHistory(function (v) { return !v; }); }}>
            <svg width="13" height="13" viewBox="0 0 13 13" fill="none"
              stroke="currentColor" strokeWidth="1.3">
              <circle cx="6.5" cy="6.5" r="5" />
              <path d="M6.5 3.8v2.9l2 1.4" />
            </svg>
          </button>
        ) : null}
      </div>
      {!wid ? (
        <div className="card-list-empty">Pick a workspace to browse its files.</div>
      ) : history ? (
        <div className="card-list" data-testid="nv-mob-files-history-list">
          {!commitRows.length ? (
            <div className="card-list-empty">No turn commits yet.</div>
          ) : null}
          {commitRows.map(function (c) {
            return (
              <button type="button" key={c.sha} className="card card-interactive"
                data-testid={"nv-mob-commit:" + c.sha}
                onClick={function () { con.setDoc({ kind: "diff", ref: c.sha }); }}>
                <div className="card-title">
                  {String(c.sha).slice(0, 7)} {c.subject}
                </div>
                <div className="card-subtitle">
                  {(c.session_id || "") + (c.op ? " · " + c.op : "")}
                </div>
              </button>
            );
          })}
        </div>
      ) : (
        <div className="card-list" data-testid="nv-mob-files-tree">
          {!items.length ? (
            <div className="card-list-empty">This workspace has no files yet.</div>
          ) : null}
          {items.map(function (entry) { return row(entry, 0); })}
        </div>
      )}
      <window.BottomSheet open={pickerOpen}
        onClose={function () { setPickerOpen(false); }}
        title="Choose a workspace">
        <div className="card-list" data-testid="nv-mob-files-ws-list">
          {!workspaces.length ? (
            <div className="card-list-empty">No workspaces yet.</div>
          ) : null}
          {workspaces.map(function (w) {
            return (
              <button type="button" key={w.id} className="card card-interactive"
                data-testid={"nv-mob-files-wid:" + w.id}
                onClick={function () { pickWorkspace(w.id); }}>
                <div className="card-title">{w.name || w.id}</div>
              </button>
            );
          })}
        </div>
      </window.BottomSheet>
    </div>
  );
}

// ---------------------------------------------------------------------------
// US-014 M5: More tab - profile/theme, system health cards, and Platform
// sections as read-only lists with a generic fact-sheet BottomSheet.
// ---------------------------------------------------------------------------

// The SAME 4-step persistence nv-chrome.jsx's own NV_ProfileMenu setTheme
// uses (setTweak, the data-theme attribute, localStorage keyed by
// NV_themeStorageKey, con.bump()) - not a forked scheme. That setter is a
// closure local to NV_ProfileMenu (not exported), so this is a second call
// site rather than a shared function reference; NV_themeStorageKey itself
// (the actual persistence key, a plain pure function) IS shared bare, so
// the stored key is identical either way.
function NV_mobileSetTheme(con, next) {
  window.primerApi.setTweak("theme", next);
  document.documentElement.setAttribute("data-theme", next);
  try {
    localStorage.setItem(NV_themeStorageKey(con.username), next);
  } catch (_e) { /* private mode, quota, etc. - non-fatal */ }
  con.bump();
}

// The rows of the More tab's Settings group (review ADM-29): what the desktop profile menu and Platform nav reach and a phone could not.
// System settings stays hidden from a restricted user, as in the desktop menu.
function NV_mobileSettingsRows(role) {
  var rows = [{ id: "providers", label: "Providers" }];
  if (role !== "restricted") rows.push({ id: "system", label: "System settings" });
  return rows;
}

function NV_MobileProfileTheme() {
  var con = NV_useConsole();
  var theme = document.documentElement.getAttribute("data-theme") || "dark";
  var initials = String(con.username || "?").slice(0, 2).toLowerCase();
  return (
    <div className="nv-mob-profile" data-testid="nv-mob-profile">
      <div className="nv-profile-head">
        <div className="nv-avatar">{initials}</div>
        <div style={{ minWidth: 0 }}>
          <div className="nv-profile-name">{con.username}</div>
          <div className="nv-profile-role">{con.role}</div>
        </div>
      </div>
      <div className="nv-seg" data-testid="nv-mob-theme-seg">
        <button type="button" className="touch-target"
          data-active={theme === "dark" ? "true" : "false"}
          onClick={function () { NV_mobileSetTheme(con, "dark"); }}>Dark</button>
        <button type="button" className="touch-target"
          data-active={theme === "light" ? "true" : "false"}
          onClick={function () { NV_mobileSetTheme(con, "light"); }}>Light</button>
      </div>
      <button type="button" className="nv-btn-secondary nv-mob-logout touch-target"
        data-testid="nv-mob-logout"
        onClick={function () {
          fetch("/v1/auth/logout", { method: "POST" }).then(
            function () { window.location.reload(); },
            function () { window.location.reload(); }
          );
        }}>Log out</button>
    </div>
  );
}

// Generic across every entity kind (13+ Platform navs) - rather than a
// bespoke sheet per kind, this reads the SAME {name, sub, chip, facts}
// view-model nv-platform.jsx's own page.card(row) already produces for
// its desktop cards. Triggers keep a real action (the fire_now endpoint
// this phase ports to the console - see sh-api.jsx); everything else's
// footer is a plain "Edit on desktop" note - no create/edit/delete here.
function NV_MobileFactSheet(props) {
  var con = NV_useConsole();
  var firingState = React.useState(false);
  var firing = firingState[0];
  var setFiring = firingState[1];
  var cardVM = props.cardVM || {};

  function fireNow() {
    if (!props.row || firing) return;
    setFiring(true);
    SH_api.fireTrigger(props.row.id).then(function () {
      setFiring(false);
      con.toast("Fired " + (props.row.id || ""));
    }, function (e) {
      setFiring(false);
      con.toast("Fire failed: " + ((e && e.message) || "unknown error"));
    });
  }

  return (
    <window.BottomSheet open={!!props.open} onClose={props.onClose}
      title={cardVM.name}
      footer={props.kind === "triggers" ? (
        <button type="button" className="nv-btn-primary touch-target"
          data-testid="nv-mob-fact-fire" disabled={firing}
          onClick={fireNow}>{firing ? "Firing…" : "Fire now"}</button>
      ) : (
        <span className="nv-mob-fact-edit-note"
          data-testid="nv-mob-fact-edit-note">Edit on desktop</span>
      )}>
      <div className="nv-mob-fact-sheet" data-testid="nv-mob-fact-body">
        {cardVM.sub ? <div className="card-subtitle">{cardVM.sub}</div> : null}
        {cardVM.chip ? (
          <span className="nv-mob-fact-chip" style={{ color: cardVM.chip.color }}>
            {cardVM.chip.label}
          </span>
        ) : null}
        {/* NV_fact(k, v) (nv-platform.jsx) returns null for an empty/
            missing value - the desktop card renderer already
            .filter(Boolean)s that away before mapping (line ~433);
            mapping the raw array here crashed on f[0] the first time a
            row had any empty fact (e.g. an agent with no profile set
            yet), a real bug this live pass caught. */}
        {(cardVM.facts || []).filter(Boolean).map(function (f) {
          return (
            <div key={f[0]} className="nv-mob-fact-row">
              <span className="nv-mob-fact-k">{f[0]}</span>
              <span className="nv-mob-fact-v">{f[1]}</span>
            </div>
          );
        })}
      </div>
    </window.BottomSheet>
  );
}

// Section list (NV_PLAT_GROUPS - the SAME grouped navs nv-platform.jsx's
// own left nav renders, "providers" filtered out below since it has no
// NV_PLAT_PAGES entry - its class-catalog shape does not fit the generic
// {list, card} contract every other nav uses) -> filter + entity rows
// (NV_PLAT_PAGES[nav].list/.card, same "nv-plat:" + nav cache key the
// desktop Platform page uses) -> a row opens the fact sheet above.
//
// props.pending ({kind, id} | null): M5 spec pt 6 - an incoming URL
// naming a desktop-only overlay (or a palette entity row, while mobile)
// resolves to this SAME fact sheet once that section's list has loaded
// far enough to find the row by id, rather than a second bespoke
// single-item fetch per kind.
function NV_MobilePlatform(props) {
  var apiFetch = window.primerApi.apiFetch;
  var navState = React.useState(null);
  var nav = navState[0];
  var setNav = navState[1];
  var qState = React.useState("");
  var q = qState[0];
  var setQ = qState[1];
  var sheetState = React.useState(null);
  var sheet = sheetState[0];
  var setSheet = sheetState[1];

  var page = nav ? window.NV_PLAT_PAGES[nav] : null;
  // The More tab hides its dashboard (profile, settings, health cards) while a section is open, so the section fills the screen.
  React.useEffect(function () {
    if (props.onNavChange) props.onNavChange(!!nav);
  }, [nav]);
  var res = window.primerApi.useResource(
    nav ? "nv-plat:" + nav : "nv-mob-plat:_none",
    function (signal) {
      return page ? page.list(apiFetch, signal) : Promise.resolve({ items: [] });
    },
    { pollMs: 15000, deps: [nav] }
  );
  var items = (res.data && res.data.items) || [];

  React.useEffect(function () {
    if (!props.pending) return;
    if (nav !== props.pending.kind) {
      setNav(props.pending.kind);
      return;
    }
    // A link to a whole section (no row id) is done once the section is open. Left pending until the list had loaded, a Back tap
    // in that first moment re-opened the section (the effect saw a pending kind that was not the open nav).
    if (!props.pending.id) {
      if (props.onPendingConsumed) props.onPendingConsumed();
      return;
    }
    if (res.loading) return;
    var row = items.find(function (r) { return r.id === props.pending.id; });
    if (row && page) setSheet({ kind: nav, cardVM: page.card(row), row: row });
    if (props.onPendingConsumed) props.onPendingConsumed();
  }, [props.pending, nav, res.loading]);

  var ql = q.trim().toLowerCase();
  var visible = !page ? [] : items.filter(function (row) {
    if (!ql) return true;
    var vm = page.card(row);
    return (vm.name + " " + (vm.sub || "")).toLowerCase().indexOf(ql) >= 0;
  });

  if (!nav) {
    return (
      <div className="nv-mob-plat-sections" data-testid="nv-mob-plat-sections">
        {NV_PLAT_GROUPS.map(function (g) {
          var ids = g.ids.filter(function (id) { return window.NV_PLAT_PAGES[id]; });
          if (!ids.length) return null;
          return (
            <div key={g.label} className="nv-mob-plat-group">
              <div className="nv-mob-plat-group-label">{g.label}</div>
              <div className="card-list">
                {ids.map(function (id) {
                  return (
                    <button type="button" key={id} className="card card-interactive"
                      data-testid={"nv-mob-plat-nav:" + id}
                      onClick={function () { setNav(id); setQ(""); }}>
                      <div className="card-title">{window.NV_PLAT_PAGES[id].title}</div>
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div className="nv-mob-plat-list" data-testid={"nv-mob-plat-page:" + nav}>
      <div className="nv-mob-files-head">
        <button type="button" className="nv-mob-back touch-target"
          data-testid="nv-mob-plat-back"
          onClick={function () { setNav(null); }}>&lsaquo; Back</button>
        <span className="nv-mob-screen-title">{page.title}</span>
      </div>
      <input className="nv-input" data-testid="nv-mob-plat-filter"
        aria-label={"Filter " + page.title.toLowerCase()}
        placeholder={"Filter " + page.title.toLowerCase() + "…"}
        value={q} onChange={function (ev) { setQ(ev.target.value); }} />
      <div className="card-list" data-testid={"nv-mob-plat-rows:" + nav}>
        {!visible.length ? (
          <div className="card-list-empty">No matches.</div>
        ) : null}
        {visible.map(function (row) {
          var vm = page.card(row);
          return (
            <button type="button" key={row.id} className="card card-interactive"
              data-testid={"nv-mob-plat-row:" + row.id}
              onClick={function () { setSheet({ kind: nav, cardVM: vm, row: row }); }}>
              <div className="card-title">{vm.name}</div>
              {vm.sub ? <div className="card-subtitle">{vm.sub}</div> : null}
            </button>
          );
        })}
      </div>
      <NV_MobileFactSheet open={!!sheet} kind={sheet && sheet.kind}
        cardVM={sheet && sheet.cardVM} row={sheet && sheet.row}
        onClose={function () { setSheet(null); }} />
    </div>
  );
}

// A system view (System settings, or a pasted ?view=system:* link) fills the phone like the other takeover screens, with a way back.
// The System pages are the desktop ones, hosted as they are; a read-mostly phone layout for each is a separate piece of work.
function NV_MobileSystemScreen() {
  var con = NV_useConsole();
  return (
    <div className="nv-mob-screen" data-testid="nv-mob-system-screen">
      <div className="nv-mob-screen-head">
        <button type="button" className="nv-mob-back touch-target"
          data-testid="nv-mob-system-back"
          onClick={function () { con.goView("studio"); }}>&lsaquo; Back</button>
        <span className="nv-mob-screen-title">System</span>
      </div>
      <div className="nv-mob-screen-body nv-mob-system-body">
        {typeof window.NV_System === "function" ? <window.NV_System /> : null}
      </div>
    </div>
  );
}

function NV_MobileMore(props) {
  var con = NV_useConsole();
  var navOpenState = React.useState(false);
  var navOpen = navOpenState[0];
  var setNavOpen = navOpenState[1];
  var settings = NV_mobileSettingsRows(con.role);
  return (
    <div className="nv-mob-more" data-testid="nv-mobile-panel:more"
      data-section-open={navOpen ? "true" : "false"}>
      {!navOpen ? (
        <React.Fragment>
          <NV_MobileProfileTheme />
          <div className="card-list nv-mob-settings" data-testid="nv-mob-settings">
            {settings.map(function (row) {
              return (
                <button type="button" key={row.id} className="card card-interactive"
                  data-testid={"nv-mob-setting:" + row.id}
                  onClick={function () {
                    if (row.id === "providers") con.openOverlay("providers");
                    else con.goView("system");
                  }}>
                  <div className="card-title">{row.label}</div>
                </button>
              );
            })}
          </div>
          <div className="nv-mob-more-section-label">System health</div>
          <NV_HealthCards />
          <div className="nv-mob-more-section-label">Platform</div>
        </React.Fragment>
      ) : null}
      <NV_MobilePlatform pending={props.pending}
        onPendingConsumed={props.onPendingConsumed}
        onNavChange={setNavOpen} />
    </div>
  );
}

function NV_MobileShell() {
  var con = NV_useConsole();
  var tabState = React.useState("inbox");
  var activeTab = tabState[0];
  var setActiveTab = tabState[1];

  // Same aggregate + cache key as the rail's own Inbox (nv-rail.jsx) -
  // use-resource.js dedupes by that string, so switching between the
  // desktop and mobile shells at a resize boundary never runs two
  // independent poll loops side by side.
  var inboxRes = window.primerApi.useResource(
    "nv-rail-inbox",
    function (signal) {
      return SH_api.pendingAttention(signal).catch(function (err) {
        if (err && err.status !== 404) throw err;
        return { items: [] };
      });
    },
    { pollMs: 10000, deps: [] }
  );
  var inboxItems = (inboxRes.data && inboxRes.data.items) || [];

  // Tap-driven nudge, same compromise nv-rail.jsx's own Inbox makes: one
  // EventSource per workspace, so this only watches whichever workspace
  // is currently selected rather than every workspace at once - other
  // workspaces' attention changes still arrive within one 10s poll.
  // Debounced so a burst of frames from the same session doesn't refetch
  // once per frame.
  var refetchTimerRef = React.useRef(null);
  window.useWorkspaceTapListener(con.wid, function (ev) {
    var cls = ev && ev["class"];
    if (cls !== "yielded" && cls !== "resumed" && cls !== "done") return;
    if (refetchTimerRef.current) return;
    inboxRes.refetch();
    refetchTimerRef.current = setTimeout(function () {
      refetchTimerRef.current = null;
    }, 500);
  });

  // M5 spec pt 6: an incoming con.overlay naming a desktop-only overlay
  // (a pasted link, or the palette's Platform entity rows) intercepts to
  // the More tab's fact sheet instead of NV_OverlayHost's full desktop
  // page - new-session/new-workspace are NOT in NV_PLAT_PAGES so they
  // fall straight through untouched (M4's own FAB flow is what mobile
  // actually uses for session creation; a deep-linked overlay=new-session
  // still works via the existing desktop panel). An unmapped name was
  // already a no-op before this (NV_OverlayHost returns null), unchanged.
  var pendingFactSheetState = React.useState(null);
  var pendingFactSheet = pendingFactSheetState[0];
  var setPendingFactSheet = pendingFactSheetState[1];
  React.useEffect(function () {
    var name = con.overlay && con.overlay.name;
    if (!name || !window.NV_PLAT_PAGES || !window.NV_PLAT_PAGES[name]) return;
    setPendingFactSheet({ kind: name, id: con.overlay.id });
    setActiveTab("more");
    con.closeOverlay();
  }, [con.overlay]);

  // A pasted ?view=platform:<nav> link (or the palette's Platform verb) lands on the More tab with that section open; it used to
  // leave the shell on Inbox. ?view=system:* is a full-screen takeover below.
  // Keyed on the view OBJECT, not its name and nav: running the same link again after Back is a navigation that changes neither, and
  // the shell's goView (or a pasted link, which arrives as a hashchange) hands over a fresh object each time. A fresh object also
  // arrives on every popstate, though, and on a phone nothing else clears ?view=platform:<x>, so an Android back gesture used to
  // re-open the section behind whatever the user had moved on to. So the link is CONSUMED once it has been acted on: the view is
  // dropped from state and from the URL (a replace), and there is nothing left to replay. The system view is the screen itself and
  // stays.
  React.useEffect(function () {
    if (!(con.view && con.view.name === "platform")) return;
    setActiveTab("more");
    if (con.view.nav && window.NV_PLAT_PAGES && window.NV_PLAT_PAGES[con.view.nav]) {
      setPendingFactSheet({ kind: con.view.nav, id: null });
    }
    if (con.clearView) con.clearView();
  }, [con.view]);

  var tabs = [
    {
      id: "inbox",
      label: "Inbox" + (inboxItems.length > 0 ? " (" + inboxItems.length + ")" : ""),
      content: (
        <NV_MobileInboxPanel items={inboxItems} onResolved={inboxRes.refetch} />
      ),
    },
    {
      id: "spaces",
      label: "Spaces",
      content: <NV_MobileSpaces />,
    },
    {
      id: "files",
      label: "Files",
      content: <NV_MobileFiles />,
    },
    {
      id: "more",
      label: "More",
      content: (
        <NV_MobileMore pending={pendingFactSheet}
          onPendingConsumed={function () { setPendingFactSheet(null); }} />
      ),
    },
  ];

  // Session (M3) and file/diff (M4) docs all push full-screen the same
  // way - one shell-level takeover point, dispatching on con.doc.kind,
  // rather than independent "hide the tabs" mechanisms per doc kind.
  return (
    <div className="nv-mobile-shell" data-testid="nv-mobile-shell" role="main">
      {con.doc && con.doc.kind === "session" ? (
        <NV_MobileChatScreen doc={con.doc} />
      ) : con.doc && (con.doc.kind === "file" || con.doc.kind === "diff") ? (
        <NV_MobileFileScreen doc={con.doc} />
      ) : con.view && con.view.name === "system" ? (
        <NV_MobileSystemScreen />
      ) : (
        <window.MobileTabs tabs={tabs} active={activeTab} onSelect={setActiveTab} />
      )}
    </div>
  );
}

window.NV_MobileShell = NV_MobileShell;
