/* global React, SH_api, SH_useShell, SH_statusLine, SH_statusFromTap,
   SH_scrollDecision, SH_collapseTurns, SH_nestSubagentRows,
   SH_toolChipLabel */
// The session doc: the surface every other surface exists to serve.
//
// Five section-8 rules land here, and each is delegated to the pure module
// that owns it so the rule has exactly ONE implementation:
//
//   status  -> SH_statusLine / SH_statusFromTap (the same string the rail
//              row chip and the tab label render)
//   scroll  -> SH_scrollDecision (auto-follow only near the bottom;
//              upward scroll freezes and offers "Jump to latest". The
//              decision function owns whether the viewport moves; this
//              file never forces it.)
//   turns   -> SH_collapseTurns / SH_nestSubagentRows / SH_toolChipLabel
//   steers  -> the composer never locks; Enter posts a steer and the
//              unrealized PendingSessionMessage rows render as chips
//              (amendment M14)
//   voice   -> Composer's own micEnabled / onTranscribed (S4)
//
// The turn LIST is ours (pinned decision 16): transcript.jsx renders a
// flat chat, and two-phase collapse plus subagent nesting are not flat.
// The COMPOSER is reused verbatim.

// Deliberately non-human: a glyph set, never an avatar or a person's
// name. Section 8 prohibits human-passing agent identities.
var SH_GLYPHS = ["■", "▲", "●", "◆", "★", "⬢", "◐", "✦"];
var SH_HUES = [12, 45, 90, 140, 190, 225, 275, 320];

function SH_identityIndex(agentId) {
  var text = String(agentId || "agent");
  var sum = 0;
  for (var i = 0; i < text.length; i++) sum = (sum * 31 + text.charCodeAt(i)) % 997;
  return sum;
}

function SH_IdentityChip(props) {
  var idx = SH_identityIndex(props.agentId);
  var glyph = SH_GLYPHS[idx % SH_GLYPHS.length];
  var hue = SH_HUES[idx % SH_HUES.length];
  return (
    <span className="sh-identity" data-testid={"shell-identity:" + props.agentId}
      style={{ color: "hsl(" + hue + ", 55%, 45%)" }}>
      <span className="sh-identity-glyph" aria-hidden="true">{glyph}</span>
      <span className="sh-identity-name">{props.agentId || "agent"}</span>
      {props.onBehalfOf ? (
        <span className="sh-identity-authority">
          on behalf of {props.onBehalfOf}
        </span>
      ) : null}
    </span>
  );
}

// Two-phase: SH_collapseTurns emits {kind:"section"} rows for finished
// turns and leaves the live turn expanded. Tool chips speak plain
// language and never carry raw arguments; writes open their doc.
//
// Identity: tap-sourced rows carry payload.agent_id (see the tap fixture
// in Task 4); REST history rows do not, so the session's agent_id is the
// fallback. The "on behalf of" stamp needs the approval records, so it
// arrives with the approvedBy map in P4 Task 21 and is {} until then.
function SH_TurnList(props) {
  var shell = SH_useShell();
  var rows = props.rows || [];
  var approvedBy = props.approvedBy || {};

  function identityFor(row) {
    return (row.payload && row.payload.agent_id) || props.agentId || "agent";
  }

  function authorityFor(row) {
    var tcid = row.payload && row.payload.tool_call_id;
    return tcid ? (approvedBy[tcid] || null) : null;
  }

  function chip(row) {
    var info = SH_toolChipLabel(row);
    return (
      <button
        type="button"
        className="sh-chip"
        data-tone={info.tone}
        key={row.seq}
        onClick={function () {
          if (info.tone !== "write" || !info.path) return;
          shell.openDoc({ kind: "file", ref: info.path, preview: true });
        }}
      >{info.label}</button>
    );
  }

  function render(row, depth) {
    if (row.kind === "section") {
      return (
        <details key={row.seq} className="sh-turn-section" data-depth={depth}
          data-testid={"shell-turn:" + row.seq}>
          <summary>{row.label} ({row.count})</summary>
          {(row.rows || []).map(function (child) {
            return render(child, depth + 1);
          })}
        </details>
      );
    }
    return (
      <div key={row.seq} className="sh-turn" data-depth={depth}
        data-testid={"shell-turn:" + row.seq}>
        <SH_IdentityChip agentId={identityFor(row)} onBehalfOf={authorityFor(row)} />
        {row.kind === "tool_call" ? chip(row) : (
          <span className="sh-turn-body">{row.label}</span>
        )}
        {row.kind === "assistant_message"
          && typeof window.SH_SpeakerButton === "function" ? (
            <window.SH_SpeakerButton row={row} agentId={identityFor(row)} />
          ) : null}
        {(row.children || []).map(function (child) {
          return render(child, depth + 1);
        })}
      </div>
    );
  }

  return (
    <div className="sh-turns">{rows.map(function (r) { return render(r, 0); })}</div>
  );
}

function SH_BindingChip(props) {
  var shell = SH_useShell();
  var binding = props.binding || {};
  var openState = React.useState(false);
  var open = openState[0];
  var setOpen = openState[1];
  var label = binding.kind === "graph"
    ? "graph " + (binding.graph_id || "?")
    : "agent " + (binding.agent_id || "?");

  return (
    <span className="sh-binding" data-testid="shell-binding-chip"
      data-epoch={props.epoch == null ? "" : props.epoch}>
      <button type="button" className="sh-verb"
        onClick={function () { setOpen(!open); }}>{label}</button>
      {open ? (
        <span className="sh-binding-menu">
          {shell.agents.map(function (agent) {
            return (
              <button key={agent.id} type="button" className="sh-verb"
                data-testid={"shell-binding-option:" + agent.id}
                onClick={function () {
                  setOpen(false);
                  SH_api.switchBinding(shell.wid, props.sid, {
                    kind: "agent", agent_id: agent.id,
                  }).then(function () { shell.toast("Binding switched"); });
                }}
              >{agent.name || agent.id}</button>
            );
          })}
        </span>
      ) : null}
    </span>
  );
}

// The pending queue made visible (section 8, "Steering"). Each chip sits
// at its insertion point; its consumption appears in the timeline as an
// ordinary turn once the drain checkpoint realizes it.
//
// A PendingSessionMessage carries `parts`, never `content`: store_pending_steer
// writes parts=[{type:"text", text}] and realize_next_pending joins the text
// parts with "\n" (primer/session/pending_messages.py:41-52, :83-86). Render
// the same join so the chip shows what the turn will actually say.
//
// Dismiss exists because realization is NOT guaranteed: shipped dispatch
// realizes a queued row only on the clean-completion exit
// (primer/session/dispatch.py:687); an executor failure (:586) or a
// cancel/interrupt (:648) advances the cursor and leaves the row queued
// forever. Without a dismiss verb the user is left staring at a chip that
// will never resolve.
function SH_steerText(row) {
  return (row.parts || [])
    .filter(function (p) { return p && p.type === "text" && p.text; })
    .map(function (p) { return p.text; })
    .join("\n");
}

function SH_QueuedSteers(props) {
  var rows = props.rows || [];
  if (!rows.length) return null;
  return (
    <ul className="sh-queued">
      {rows.map(function (row) {
        return (
          <li key={row.id} className="sh-queued-chip"
            data-testid={"shell-queued-steer:" + row.id}>
            <span className="sh-queued-mark">queued</span>
            <span className="sh-queued-text">{SH_steerText(row)}</span>
            <button type="button" className="sh-queued-dismiss"
              title="Dismiss this queued steer"
              data-testid={"shell-queued-steer-dismiss:" + row.id}
              onClick={function () { props.onDismiss(row); }}>x</button>
          </li>
        );
      })}
    </ul>
  );
}

function SH_SessionComposer(props) {
  var shell = SH_useShell();
  var valueState = React.useState("");
  var value = valueState[0];
  var setValue = valueState[1];
  var slash = value.charAt(0) === "/";

  function send() {
    var text = String(value || "").trim();
    if (!text) return;
    setValue("");
    props.onSendStarted(text);
    SH_api.steer(shell.wid, props.sid, text).catch(function (err) {
      shell.toast("Steer failed: " + (err && err.message ? err.message : err));
    });
  }

  // Parking needs a live run to park. Named rather than inlined because
  // the composer gate forbids the run flag inside a disabled={...} in
  // this file, and it is right to: the composer's own send must never
  // bind to it.
  var canPark = props.status === "running";

  return (
    <div className="sh-composer" data-testid="shell-composer">
      <div className="sh-composer-status" data-testid="shell-composer-status">
        {props.status ? (
          <React.Fragment>
            <span>{props.status}</span>
            <button type="button" className="sh-verb" data-verb="session.interrupt"
              onClick={function () { shell.registry.get("session.interrupt").run(); }}
            >Interrupt Session</button>
          </React.Fragment>
        ) : "idle"}
      </div>
      {/* Park and Close, the two things a message cannot say. Sending
          already invokes a created session, steers a running one, resumes
          a parked one and reopens an ended one, so there is no Resume and
          no Restart here: the composer is both. Interrupt above stops the
          turn in flight and leaves the session alive, which is not the
          same as closing it. The strip renders at every status and
          disables rather than hides, with a title saying why. */}
      <div className="sh-composer-signals" data-testid="shell-session-signals">
        <button
          type="button"
          className="sh-verb"
          data-verb="session.pause"
          data-testid="ctrl-pause"
          disabled={!canPark}
          title={canPark ? "Park this session" : "Enabled only when running"}
          onClick={function () { shell.registry.get("session.pause").run(); }}
        >Park</button>
        <button
          type="button"
          className="sh-verb"
          data-verb="session.end"
          data-testid="ctrl-end"
          disabled={props.terminal}
          title={props.terminal ? "This session has already ended" : "End this session"}
          onClick={function () { shell.registry.get("session.end").run(); }}
        >Close</button>
      </div>
      {slash ? (
        <div className="sh-composer-verbs">
          {shell.registry.forSurface("composer-slash").map(function (verb) {
            return (
              <button type="button" key={verb.id} className="sh-verb"
                data-verb={verb.id}
                onClick={function () { verb.run({ sid: sid }); }}>
                {verb.label}
              </button>
            );
          })}
        </div>
      ) : null}
      {slash && typeof window.SH_PaletteRows === "function" ? (
        <window.SH_PaletteRows query={value.slice(1)}
          onRun={function (verb) { setValue(""); verb.run(); }} />
      ) : null}
      <window.Composer
        value={value}
        onChange={setValue}
        onSend={send}
        running={!!props.running}
        disabled={false}
        micEnabled={!!props.micEnabled}
        onTranscribed={function (text) {
          // Dictation ALWAYS lands as editable text; release never sends.
          setValue(function (prev) { return prev ? prev + " " + text : text; });
        }}
      />
    </div>
  );
}

function SH_registerSessionVerbs(shell) {
  function activeSid() {
    var group = shell.docs.groups[shell.docs.activeGroup];
    for (var i = 0; group && i < group.tabs.length; i++) {
      if (group.tabs[i].id === group.activeId
          && group.tabs[i].kind === "session") {
        return group.tabs[i].ref;
      }
    }
    return null;
  }

  shell.registry.register({
    id: "session.steer", label: "Send Steer", contexts: ["session"],
    surfaces: ["composer-slash", "palette"],
    run: function () { shell.focusComposer(); },
  });
  shell.registry.register({
    id: "session.rewind", label: "Rewind Session", destructive: true,
    contexts: ["session"], surfaces: ["tab-menu", "palette"],
    run: function (arg) {
      var sid = activeSid();
      var seq = arg && arg.seq;
      if (!sid || seq == null) {
        shell.toast("Pick the turn to rewind to first");
        return;
      }
      SH_api.rewind(shell.wid, sid, seq).catch(function (err) {
        shell.toast("Rewind refused: " + (err && err.message ? err.message : err));
      });
    },
  });
  shell.registry.register({
    id: "session.compact", label: "Compact Session", contexts: ["session"],
    surfaces: ["tab-menu", "palette"],
    run: function () {
      var sid = activeSid();
      if (!sid) return;
      SH_api.compact(shell.wid, sid).catch(function (err) {
        shell.toast("Compact refused: " + (err && err.message ? err.message : err));
      });
    },
  });
  shell.registry.register({
    id: "session.switchBinding", label: "Switch Binding",
    contexts: ["session"], surfaces: ["tab-menu", "palette"],
    run: function () { shell.openOverlay("agents"); },
  });
  shell.registry.register({
    id: "session.jumpLatest", label: "Jump Latest", contexts: ["session"],
    surfaces: ["tab-menu", "palette"],
    run: function () { shell.jumpLatestRef.current(); },
  });
}

// ---------------------------------------------------------------------------
// SH_TokenMeter -- read-only context meter for the session head. Compaction
// is the agent's own call, so the shared meter mounts with no compact
// button; when the shared component is absent it degrades to a bare count.
// ---------------------------------------------------------------------------

function SH_TokenMeter(props) {
  var session = props.session;
  var turns = (session && Array.isArray(session.turns)) ? session.turns : [];
  var last = turns.length > 0 ? turns[turns.length - 1] : null;
  var inputTokens = Number(last && last.tokens_in) || 0;
  var contextLength = Number(session && session.context_length) || 0;
  if (window.TokenMeter) {
    return (
      <window.TokenMeter inputTokens={inputTokens}
        contextLength={contextLength} onCompact={null} />
    );
  }
  return <span className="sh-meta" data-testid="shell-token-count">{inputTokens} tok</span>;
}

// The four statuses a session does not come back from on its own.
// session-frame.jsx owns the set; read it through the window rather than
// restating it here, so the shell cannot drift from the rest of the
// console about what "over" means.
function SH_sessionIsOver(session) {
  if (!session || !session.status) return false;
  var terminal = window.SESSION_TERMINAL;
  return !!(terminal && terminal.has(session.status));
}

// A node filter is a view of the same turns, not a different fetch: the
// canvas selection scopes what the turn list shows and nothing else. A
// turn that carries no node attribution is graph-level and stays visible
// at every filter, otherwise selecting a node would hide the run's own
// start and finish.
function SH_scopeToNode(rows, nodeId) {
  if (!nodeId) return rows;
  return (rows || []).filter(function (row) {
    var n = row && (row.node_id || row.node);
    return !n || n === nodeId;
  });
}

function SH_SessionDoc(props) {
  var shell = SH_useShell();
  var sid = props.sid;
  var tap = window.useWorkspaceTap(shell.wid);

  // Poll while there is something to learn, and stop when there is not.
  //
  // A flat 5s was wrong in both directions: a session that is CREATED is
  // about to change and the operator is watching it, so five seconds is
  // a visible lag on the one transition anyone waits for; a session that
  // has ENDED will never change again, and polling it forever costs a
  // request every five seconds per open tab for news that cannot come.
  var terminalRef = React.useRef(false);
  var detail = window.primerApi.useResource(
    SH_api.keys.session(sid),
    function (signal) { return SH_api.session(sid, signal); },
    { pollMs: terminalRef.current ? 0 : 2000, deps: [sid] }
  );
  terminalRef.current = !!(detail.data && SH_sessionIsOver(detail.data));
  // The transcript was fetched ONCE, on mount, and nothing ever asked
  // for it again: not the send, not the turn running, not the turn
  // finishing. Sending a message and watching the answer arrive is the
  // whole loop this document exists for, and it only worked if you
  // reloaded the page. It follows the same rule as the session row now:
  // poll while there is something to learn, stop once the session is
  // over and the transcript is final.
  var history = window.primerApi.useResource(
    SH_api.keys.session(sid) + ":messages",
    function (signal) { return SH_api.messages(sid, 200, null, signal); },
    { pollMs: terminalRef.current ? 0 : 2000, deps: [sid] }
  );

  // The gate at the pause point, and who approved what (the "on behalf
  // of" stamp). Both are S1/approvals reads; S6 is never consulted.
  var gates = window.primerApi.useResource(
    SH_api.keys.sessionPending(sid),
    function (signal) {
      return SH_api.sessionPendingYields(shell.wid, sid, signal);
    },
    { pollMs: 5000, deps: [shell.wid, sid] }
  );
  var records = window.primerApi.useResource(
    SH_api.keys.records(),
    function (signal) { return SH_api.approvalRecords(signal); },
    { pollMs: 0 }
  );
  var gateItems = window.SH_toAttentionItems({
    pending: (gates.data && gates.data.items) || [], records: [],
  });
  var approvedBy = window.SH_approvedByMap(
    (records.data && records.data.items) || []
  );

  var verbShellRef = React.useRef(shell);
  verbShellRef.current = shell;
  if (!shell.registry.get("session.rewind")) {
    // Live view, not this render's object: see SH_liveShell.
    SH_registerSessionVerbs(window.SH_liveShell(verbShellRef));
  }

  // "Mounted IMMEDIATELY on send": the tap frame that would produce a
  // status is still in flight when the user releases Enter, so send sets
  // a local start time and the tap takes over as soon as it lands.
  var optimisticStart = React.useState(null);
  var optimistic = optimisticStart[0];
  var setOptimistic = optimisticStart[1];

  var live = SH_statusFromTap(tap.events, sid, Date.now());
  React.useEffect(function () { if (live) setOptimistic(null); }, [!!live]);
  var shown = live || (optimistic
    ? { verb: "sending", object: "", startedMs: optimistic }
    : null);
  var status = shown
    ? SH_statusLine({
      verb: shown.verb, object: shown.object,
      elapsedSec: Math.round((Date.now() - shown.startedMs) / 1000),
    })
    : null;

  // GET /sessions/{sid} is response_model=WorkspaceSession: the row IS the
  // envelope. There is no `session` sub-object, and no top-level agent_id
  // either; the bound agent lives on the binding union's `agent` arm.
  var session = detail.data || null;
  var binding = (session && session.binding) || null;
  var agentId = (binding && binding.agent_id) || null;
  var graphId = (binding && binding.graph_id) || null;
  // null = all nodes. Clicking a node on the canvas scopes the turn list
  // to that node; the banner below says so and offers the way back.
  var nodeState = React.useState(null);
  var selectedNode = nodeState[0];
  var setSelectedNode = nodeState[1];
  var pending = (session && session.pending_messages) || [];
  var records = (history.data && history.data.items) || [];
  var flat = SH_nestSubagentRows(window.SA_toTranscript(records, session));
  // The live turn stays expanded; everything before it collapses to named
  // sections. The boundary is the last user_message while a run is active
  // (SA_KIND_TO_TRANSCRIPT maps user_input -> user_message,
  // session-adapter.jsx:41). Infinity, not null, when nothing is running:
  // SH_collapseTurns compares `row.seq >= liveFrom`, and null would read
  // as 0 and leave every row expanded.
  var liveFromSeq = Infinity;
  if (shown) {
    for (var i = flat.length - 1; i >= 0; i--) {
      if (flat[i].kind === "user_message") { liveFromSeq = flat[i].seq; break; }
    }
  }
  var rows = SH_collapseTurns(flat, { liveFromSeq: liveFromSeq });

  // Scroll anchoring: the viewport is only ever moved by the decision
  // function, and only when the reader is already near the bottom.
  var scrollRef = React.useRef(null);
  var seenState = React.useState(0);
  var seen = seenState[0];
  var setSeen = seenState[1];
  var distance = 0;
  if (scrollRef.current) {
    distance = scrollRef.current.scrollHeight - scrollRef.current.scrollTop
      - scrollRef.current.clientHeight;
  }
  var decision = SH_scrollDecision({
    distanceFromBottom: distance,
    newTurns: Math.max(0, rows.length - seen),
  });

  function jumpLatest() {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
    setSeen(rows.length);
  }
  shell.jumpLatestRef.current = jumpLatest;

  React.useEffect(function () {
    if (decision.follow) jumpLatest();
  }, [rows.length, decision.follow]);

  return (
    <div className="sh-session" data-testid={"shell-session:" + sid}>
      {typeof window.SH_ClientTools === "function"
        ? <window.SH_ClientTools sid={sid} />
        : null}
      <header className="sh-session-head">
        <SH_BindingChip sid={sid} binding={binding}
          epoch={session && session.binding_epoch} />
        <SH_TokenMeter session={session} />
        {shell.registry.forSurface("tab-menu").map(function (verb) {
          // The header offers the same verbs as the tab menu, so it has
          // to answer the same question: a verb that needs a live
          // session is not offered once the session is over.
          if (!window.SH_verbApplies(shell, verb, { kind: "session", ref: sid })) {
            return null;
          }
          return (
            <button key={verb.id} type="button" className="sh-verb"
              data-verb={verb.id} onClick={function () { verb.run(); }}
            >{verb.label}</button>
          );
        })}
      </header>

      {/* Invoker-supplied tool calls the conversation is blocked on. The
          banner polls on its own and renders nothing when the queue is
          empty, so mounting it unconditionally costs a no-op. */}
      {typeof window.ExternalPendingBanner === "function"
        ? <window.ExternalPendingBanner sessionId={sid} pushToast={shell.toast} />
        : null}

      {/* A graph-bound session runs a graph, and the canvas is how you see
          where it is. The Studio mounted this; the shell that replaced it
          carried over the binding chip and nothing else, so a graph
          session showed a transcript and no graph. SD_GraphRunView is the
          production component, still loaded, still the one to reuse.
          hideInspector drops its 360px node-event stream: the turn list
          below already says what the inspector said. */}
      {graphId && typeof window.SD_GraphRunView === "function" ? (
        <div className="sh-graph-viz" data-testid="graph-viz-region"
          style={{
            flex: "0 0 auto", height: 360, minHeight: 0, overflow: "auto",
            borderBottom: "1px solid var(--border)",
          }}>
          <window.SD_GraphRunView
            gid={graphId}
            rid={sid}
            wid={shell.wid}
            session={session}
            pushToast={window.primerApi.toastPush}
            onNodeSelect={setSelectedNode}
            hideInspector={true}
          />
        </div>
      ) : null}

      {selectedNode ? (
        <div className="sh-node-filter" data-testid="graph-node-filter"
          style={{
            display: "flex", alignItems: "center", gap: 8, padding: "6px 14px",
            borderBottom: "1px solid var(--border)", flex: "0 0 auto",
            fontSize: 11.5,
          }}>
          <span>Showing <span className="mono">{selectedNode}</span> only</span>
          <div style={{ flex: 1 }} />
          <button type="button" className="sh-verb"
            data-testid="graph-node-filter-clear"
            title="Show the full transcript (all nodes)"
            onClick={function () { setSelectedNode(null); }}
          >All nodes</button>
        </div>
      ) : null}

      <div className="sh-transcript" data-testid="shell-transcript" ref={scrollRef}
        onScroll={function () { if (decision.follow) setSeen(rows.length); }}>
        <SH_TurnList rows={SH_scopeToNode(rows, selectedNode)} sid={sid}
          agentId={agentId} approvedBy={approvedBy} />
        {window.SH_walkthroughState(flat).active
          && typeof window.SH_Walkthrough === "function"
          ? <window.SH_Walkthrough sid={sid} />
          : null}
        {typeof window.SH_VoiceReplies === "function" ? (
          <window.SH_VoiceReplies sid={sid} rows={rows}
            agentId={agentId}
            isForeground={shell.isForeground(sid)} />
        ) : null}
        <SH_QueuedSteers rows={pending} onDismiss={function (row) {
          SH_api.dismissQueuedSteer(shell.wid, sid, row.id)
            .then(function () { detail.refetch(); })
            .catch(function (err) {
              shell.toast("Dismiss failed: " + (err && err.message ? err.message : err));
            });
        }} />
        {gateItems.map(function (item) {
          return (
            <window.SH_DecisionCard key={item.id} item={item}
              onResolved={function () { gates.refetch(); detail.refetch(); }} />
          );
        })}
      </div>

      {decision.showJump ? (
        <button type="button" className="sh-jump" data-testid="shell-jump-latest"
          onClick={jumpLatest}>{decision.jumpLabel}</button>
      ) : null}

      <SH_SessionComposer
        sid={sid}
        running={!!shown}
        status={status}
        terminal={SH_sessionIsOver(session)}
        micEnabled={!!shell.speech.stt_configured}
        onSendStarted={function () {
          setOptimistic(Date.now());
          detail.refetch();
          // Do not wait up to a poll interval to show the operator their
          // own message.
          history.refetch();
        }}
      />
    </div>
  );
}

window.SH_GLYPHS = SH_GLYPHS;
window.SH_IdentityChip = SH_IdentityChip;
window.SH_TurnList = SH_TurnList;
window.SH_BindingChip = SH_BindingChip;
window.SH_QueuedSteers = SH_QueuedSteers;
window.SH_SessionComposer = SH_SessionComposer;
window.SH_registerSessionVerbs = SH_registerSessionVerbs;
window.SH_SessionDoc = SH_SessionDoc;
window.SH_TokenMeter = SH_TokenMeter;
