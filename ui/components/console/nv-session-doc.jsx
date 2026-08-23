/* global React, SH_api, NV_useConsole, NV_identity, SH_statusFromTap,
   SH_statusLine, SH_collapseTurns, SH_nestSubagentRows, SH_toolChipLabel,
   SH_scrollDecision */
// The session tab (wiring plan P2 T7). DATA layer inherited from the
// sh session doc (same resources, same pure modules); RENDER is the
// designer prototype's SESSION DOC / TRANSCRIPT / cards / TRACE /
// STATUS+COMPOSER regions, inline styles extracted to nv- classes.

var NV_CHIP_ICONS = { write: "✎", read: "→", other: "⚙" };

function NV_sessionIsOver(session) {
  return !!session && session.status === "ended";
}

// ---------------------------------------------------------------------------
// Header: binding chip + picker, inline rename, usage, graph view,
// voice replies, overflow.
// ---------------------------------------------------------------------------
function NV_BindingChip(props) {
  var con = NV_useConsole();
  var openState = React.useState(false);
  var open = openState[0];
  var setOpen = openState[1];
  var qState = React.useState("");
  var q = qState[0];
  var setQ = qState[1];
  var agents = window.primerApi.useResource(
    "nv-bind-agents",
    function (signal) { return SH_api.agents(signal); },
    { pollMs: 0 }
  );
  var graphs = window.primerApi.useResource(
    "nv-bind-graphs",
    function (signal) { return SH_api.graphs(signal); },
    { pollMs: 0 }
  );
  var binding = props.binding || {};
  var ident = NV_identity(binding);
  var name = binding.kind === "graph"
    ? (binding.graph_id || "graph")
    : (binding.agent_id || "agent");
  var rows = [];
  ((agents.data && agents.data.items) || []).forEach(function (a) {
    rows.push({
      key: "a:" + a.id, name: a.id, kind: "agent",
      desc: a.description || "",
      binding: { kind: "agent", agent_id: a.id },
    });
  });
  ((graphs.data && graphs.data.items) || []).forEach(function (g) {
    rows.push({
      key: "g:" + g.id, name: g.id, kind: "graph",
      desc: g.description || "",
      binding: { kind: "graph", graph_id: g.id },
    });
  });
  var needle = q.toLowerCase();
  rows = rows.filter(function (r) {
    return r.name.toLowerCase().indexOf(needle) >= 0;
  }).slice(0, 20);

  return (
    <div className="nv-bind-wrap">
      <button type="button" className="nv-bind-chip"
        data-testid="nv-binding-chip"
        title="Switch binding — takes effect at the turn boundary"
        onClick={function (ev) { ev.stopPropagation(); setOpen(!open); }}>
        <svg width="11" height="11" viewBox="0 0 12 12"
          style={{ color: ident.color }}>
          <path d={ident.d} fill="currentColor" />
        </svg>
        <span>{name}</span>
        <svg width="8" height="8" viewBox="0 0 10 10" fill="none"
          stroke="var(--text-3)" strokeWidth="1.5">
          <path d="M2 3.5 5 6.5 8 3.5" />
        </svg>
      </button>
      {open ? (
        <div className="nv-bind-menu" data-testid="nv-binding-menu"
          onClick={function (ev) { ev.stopPropagation(); }}>
          <div className="nv-bind-note">
            Switching mid-run queues to the turn boundary.
          </div>
          <div className="nv-bind-search">
            <input autoFocus value={q} placeholder="Search agents & graphs…"
              onChange={function (ev) { setQ(ev.target.value); }} />
          </div>
          <div className="nv-bind-rows">
            {rows.map(function (r) {
              var rid = NV_identity(r.binding);
              return (
                <button type="button" key={r.key} className="nv-bind-row"
                  data-testid={"nv-binding-option:" + r.name}
                  onClick={function () {
                    setOpen(false);
                    SH_api.switchBinding(con.wid, props.sid, r.binding)
                      .then(function () {
                        con.toast("Binding switched — applies at the turn boundary");
                        props.onChanged();
                      }, function (err) {
                        con.toast("Switch failed: " + err.message);
                      });
                  }}>
                  <svg width="12" height="12" viewBox="0 0 12 12"
                    style={{ color: rid.color }}>
                    <path d={rid.d} fill="currentColor" />
                  </svg>
                  <div className="nv-bind-main">
                    <div className="nv-bind-name">{r.name}</div>
                    <div className="nv-bind-desc">{r.desc}</div>
                  </div>
                  <span className="nv-bind-kind">{r.kind}</span>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function NV_SessionHeader(props) {
  var con = NV_useConsole();
  var session = props.session;
  var sid = props.sid;
  var renameState = React.useState(null);
  var draft = renameState[0];
  var setDraft = renameState[1];
  var ovfState = React.useState(false);
  var ovfOpen = ovfState[0];
  var setOvf = ovfState[1];
  var usage = props.usage || {};
  var pct = usage.pct || 0;
  var isGraph = session && session.binding
    && session.binding.kind === "graph";

  function saveTitle() {
    var name = draft;
    setDraft(null);
    if (name == null || name === (session && session.name)) return;
    SH_api.renameSession(con.wid, sid, name || null).then(
      props.onChanged,
      function (err) { con.toast("Rename failed: " + err.message); }
    );
  }

  return (
    <div className="nv-session-head" data-testid="nv-session-head">
      <NV_BindingChip sid={sid}
        binding={session && session.binding}
        onChanged={props.onChanged} />
      {draft != null ? (
        <input className="nv-title-input" autoFocus value={draft}
          data-testid="nv-title-input"
          onChange={function (ev) { setDraft(ev.target.value); }}
          onKeyDown={function (ev) {
            if (ev.key === "Enter") saveTitle();
            if (ev.key === "Escape") setDraft(null);
          }}
          onBlur={saveTitle} />
      ) : (
        <div className="nv-title" data-testid="nv-session-title"
          title="Click to rename"
          onClick={function () {
            setDraft((session && session.name) || "");
          }}>
          {(session && session.name) || sid}
        </div>
      )}
      <div className="nv-usage" title="context used">
        <div className="nv-usage-bar">
          <div className="nv-usage-fill" style={{ width: pct + "%" }} />
        </div>
        <span className="nv-usage-label">{usage.label || ""}</span>
      </div>
      {isGraph ? (
        <button type="button" className="nv-graphview-btn"
          data-testid="nv-graph-view"
          onClick={props.onGraphView}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"
            stroke="currentColor" strokeWidth="1.2">
            <rect x="0.8" y="1" width="4" height="3.2" rx="0.8" />
            <rect x="7.2" y="7.8" width="4" height="3.2" rx="0.8" />
            <path d="M4.8 2.6h3.4v5.2" />
          </svg>
          Graph view
        </button>
      ) : null}
      {props.ttsOk ? (
        <button type="button" className="nv-head-iconbtn"
          title="Voice replies — final answers only"
          data-testid="nv-voice-toggle"
          data-active={props.voiceOn ? "true" : "false"}
          onClick={props.onToggleVoice}>
          <svg width="13" height="13" viewBox="0 0 14 14" fill="none"
            stroke="currentColor" strokeWidth="1.3">
            <path d="M2 5.5v3h2.5L8 11V3L4.5 5.5Z M10 5a3 3 0 0 1 0 4" />
          </svg>
        </button>
      ) : null}
      <div className="nv-bind-wrap">
        <button type="button" className="nv-head-iconbtn"
          data-testid="nv-session-overflow"
          onClick={function (ev) { ev.stopPropagation(); setOvf(!ovfOpen); }}>
          ⋯
        </button>
        {ovfOpen ? (
          <div className="nv-menu nv-menu-right"
            onClick={function (ev) { ev.stopPropagation(); }}>
            <button type="button" className="nv-menu-row" disabled
              title="Needs the S1 P2 rewind endpoint (spec section 12)">
              Rewind…
            </button>
            <button type="button" className="nv-menu-row" disabled
              title="Needs the S1 P2 compact endpoint (spec section 12)">
              Compact…
            </button>
            <button type="button" className="nv-menu-row"
              onClick={function () {
                setOvf(false);
                props.onExport();
              }}>Export transcript</button>
            <div className="nv-menu-sep" />
            <button type="button" className="nv-menu-row" data-danger="true"
              onClick={function () {
                setOvf(false);
                confirmDialog({
                  title: "Delete session",
                  message: "Permanently delete this session?",
                  danger: true,
                }).then(function (ok) {
                  if (ok) {
                    SH_api.deleteSession(con.wid, sid)
                      .then(props.onDeleted);
                  }
                });
              }}>Delete session</button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------
function NV_DecisionCard(props) {
  var con = NV_useConsole();
  var item = props.item;
  var rejState = React.useState(false);
  var rejOpen = rejState[0];
  var setRej = rejState[1];
  var reasonState = React.useState("");
  var reason = reasonState[0];
  var setReason = reasonState[1];
  // The ApproverSpec stamped at park time (P6): kind anyone|roles|users.
  var routing = item.approvers
    ? (item.approvers.kind === "anyone"
      ? "anyone may decide"
      : "awaiting " + ((item.approvers.kind === "roles"
        ? item.approvers.roles
        : item.approvers.users) || []).join(", "))
    : "anyone may decide";
  return (
    <div className="nv-card nv-card-attention"
      data-testid={"nv-decision:" + item.toolCallId}>
      <div className="nv-card-head">
        <span className="nv-dot-attention" />
        <span className="nv-card-title">Approval required</span>
        <span className="nv-card-tool">{item.toolName}</span>
        <span style={{ flex: 1 }} />
        <span className="nv-card-routing">{routing}</span>
      </div>
      <div className="nv-card-body">
        {item.preview ? (
          <pre className="nv-card-preview">{item.preview}</pre>
        ) : null}
        <div className="nv-card-actions">
          <button type="button" className="nv-btn-primary"
            data-testid="nv-approve"
            onClick={function () {
              SH_api.approve(item.sessionId, item.toolCallId).then(
                props.onResolved,
                function (err) { con.toast("Approve failed: " + err.message); }
              );
            }}>Approve</button>
          <button type="button" className="nv-btn-reject"
            data-testid="nv-reject"
            onClick={function () {
              if (!rejOpen) { setRej(true); return; }
              SH_api.reject(item.sessionId, item.toolCallId, reason).then(
                props.onResolved,
                function (err) { con.toast("Reject failed: " + err.message); }
              );
            }}>{rejOpen ? "Reject with feedback" : "Reject…"}</button>
          <span className="nv-card-note">
            also in your attention feed — keep scrolling while you judge
          </span>
        </div>
        {rejOpen ? (
          <textarea className="nv-card-reason" value={reason}
            data-testid="nv-reject-reason"
            placeholder="Why not — the agent reads this"
            onChange={function (ev) { setReason(ev.target.value); }} />
        ) : null}
      </div>
    </div>
  );
}

function NV_AskCard(props) {
  var con = NV_useConsole();
  var item = props.item;
  var valState = React.useState("");
  var val = valState[0];
  var setVal = valState[1];
  return (
    <div className="nv-card nv-card-attention"
      data-testid={"nv-ask:" + item.toolCallId}>
      <div className="nv-card-head">
        <span className="nv-dot-attention" />
        <span className="nv-card-title">The agent is asking</span>
        <span className="nv-card-tool">ask_user</span>
      </div>
      <div className="nv-card-body">
        <div className="nv-ask-prompt">{item.preview || item.title}</div>
        <textarea className="nv-card-reason" value={val}
          data-testid="nv-ask-answer"
          placeholder="Your answer — the agent resumes with it"
          onChange={function (ev) { setVal(ev.target.value); }} />
        <div className="nv-card-actions">
          <button type="button" className="nv-btn-primary"
            data-testid="nv-ask-submit"
            onClick={function () {
              SH_api.answer(item.sessionId, item.toolCallId, val).then(
                props.onResolved,
                function (err) { con.toast("Answer failed: " + err.message); }
              );
            }}>Answer & resume</button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The trace split (right of the transcript; never an overlay)
// ---------------------------------------------------------------------------
// Inline artifacts (revamp decision 4, ported from the sh doc on flag
// day): a write chip toggles a capped file preview IN PLACE under its
// row; "Open as Tab" and "Maximize" are the escalations, and the
// lightbox is transient UI that never reaches the URL.
var NV_ARTIFACT_PREVIEW_LINES = 200;

function NV_Lightbox(props) {
  React.useEffect(function () {
    function onKey(ev) { if (ev.key === "Escape") props.onClose(); }
    window.addEventListener("keydown", onKey);
    return function () { window.removeEventListener("keydown", onKey); };
  });
  return (
    <div className="nv-lightbox" data-testid="nv-lightbox"
      onClick={function (ev) {
        if (ev.target === ev.currentTarget) props.onClose();
      }}>
      <div className="nv-lightbox-body">
        <div className="nv-lightbox-bar">
          <span className="nv-lightbox-title">{props.title}</span>
          <button type="button" className="nv-btn-secondary"
            data-testid="nv-lightbox-close"
            onClick={props.onClose}>Close</button>
        </div>
        <pre className="nv-lightbox-content">{props.content}</pre>
      </div>
    </div>
  );
}

function NV_ArtifactBlock(props) {
  var con = NV_useConsole();
  var maxState = React.useState(false);
  var maximized = maxState[0];
  var setMaximized = maxState[1];
  var isFile = !!props.path;
  var read = window.primerApi.useResource(
    isFile
      ? SH_api.keys.file(con.wid, props.path)
      : "nv-artifact-none:" + props.seq,
    function (signal) {
      return isFile
        ? SH_api.fileRead(con.wid, props.path, signal)
        : Promise.resolve(null);
    },
    { pollMs: 0, deps: [con.wid, props.path || ""] }
  );
  var full = isFile
    ? ((read.data && read.data.content) || "")
    : String(props.output || "");
  var lines = full.split("\n");
  var capped = lines.length > NV_ARTIFACT_PREVIEW_LINES;
  var preview = capped
    ? lines.slice(0, NV_ARTIFACT_PREVIEW_LINES).join("\n")
    : full;

  return (
    <div className="nv-artifact" data-testid={"nv-artifact:" + props.seq}>
      <div className="nv-artifact-bar">
        <span className="nv-artifact-title">
          {isFile ? props.path : "output"}
        </span>
        {capped ? (
          <span className="nv-artifact-note">
            first {NV_ARTIFACT_PREVIEW_LINES} of {lines.length} lines
          </span>
        ) : null}
        <span style={{ flex: 1 }} />
        {isFile ? (
          <button type="button" className="nv-artifact-verb"
            data-testid="nv-artifact-open-tab"
            onClick={function () {
              con.setDoc({ kind: "file", ref: props.path });
            }}>Open as Tab</button>
        ) : null}
        <button type="button" className="nv-artifact-verb"
          data-testid="nv-artifact-maximize"
          onClick={function () { setMaximized(true); }}>Maximize</button>
      </div>
      <pre className="nv-artifact-content">{preview}</pre>
      {maximized ? (
        <NV_Lightbox title={isFile ? props.path : "output"}
          content={full}
          onClose={function () { setMaximized(false); }} />
      ) : null}
    </div>
  );
}

// What a chip can expand into: a written file's live content, else the
// call's captured output. null = the chip is a plain label.
function NV_artifactFor(row, info) {
  if (info.tone === "write" && info.path) return { path: info.path };
  var payload = row.payload || {};
  var out = payload.output || payload.result_preview
    || (typeof payload.result === "string" ? payload.result : null);
  if (out) return { output: out };
  return null;
}

function NV_TraceSplit(props) {
  var timeline = window.primerApi.useResource(
    SH_api.keys.timeline(props.sid, props.turnNo),
    function (signal) {
      return SH_api.timeline(props.sid, props.turnNo, signal);
    },
    { pollMs: 0, deps: [props.sid, props.turnNo] }
  );
  var rows = (timeline.data && timeline.data.children) || [];
  function flat(children, depth, out) {
    (children || []).forEach(function (c) {
      out.push({ node: c, depth: depth });
      flat(c.children, depth + 1, out);
    });
    return out;
  }
  var flatRows = flat(rows, 0, []);
  return (
    <div className="nv-trace-split" data-testid="nv-trace-split">
      <div className="nv-trace-head">
        <span>trace · turn {props.turnNo}</span>
        <span style={{ flex: 1 }} />
        <button type="button" className="nv-rail-iconbtn"
          data-testid="nv-trace-close"
          onClick={props.onClose}>×</button>
      </div>
      <div className="nv-trace-body">
        {flatRows.map(function (r, i) {
          var n = r.node;
          return (
            <div key={i} className="nv-trace-row"
              style={{ paddingLeft: r.depth * 12 }}>
              <div className="nv-trace-line">
                <span className="nv-trace-icon">
                  {n.kind === "llm_call" ? "◇" : n.kind === "tool" ? "⚙" : "·"}
                </span>
                <span className="nv-trace-label">
                  {n.label || n.kind || ""}
                </span>
                <span style={{ flex: 1 }} />
                <span className="nv-trace-dur">
                  {n.duration_ms != null ? n.duration_ms + "ms" : ""}
                </span>
              </div>
              {n.args ? (
                <div className="nv-trace-args">
                  {JSON.stringify(n.args)}
                </div>
              ) : null}
            </div>
          );
        })}
        <div className="nv-trace-foot">
          The trace is the only place raw tool arguments appear. It opens
          beside the transcript — comparison never goes in an overlay.
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Composer (status strip + input row + mic + stop + send)
// ---------------------------------------------------------------------------
function NV_Composer(props) {
  var con = NV_useConsole();
  var valState = React.useState("");
  var val = valState[0];
  var setVal = valState[1];
  var recState = React.useState(false);
  var recording = recState[0];
  var setRecording = recState[1];
  var recRef = React.useRef(null);

  function send() {
    var text = String(val || "").trim();
    if (!text) return;
    setVal("");
    props.onSendStarted();
    SH_api.steer(con.wid, props.sid, text).catch(function (err) {
      con.toast("Steer failed: " + (err && err.message ? err.message : err));
    });
  }

  function micStart() {
    if (recording || !props.micEnabled) return;
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var rec = new MediaRecorder(stream);
      var chunks = [];
      rec.ondataavailable = function (ev) { chunks.push(ev.data); };
      rec.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        setRecording(false);
        var blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
        var form = new FormData();
        form.append("file", blob, "dictation.webm");
        fetch("/v1/audio/transcriptions", { method: "POST", body: form })
          .then(function (r) { return r.json(); })
          .then(function (out) {
            // Dictation ALWAYS lands as editable text; never auto-sends.
            if (out && out.text) {
              setVal(function (prev) {
                return prev ? prev + " " + out.text : out.text;
              });
            }
          })
          .catch(function () { con.toast("Transcription failed"); });
      };
      rec.start();
      recRef.current = rec;
      setRecording(true);
    }, function () { con.toast("Microphone unavailable"); });
  }
  function micStop() {
    if (recRef.current && recording) recRef.current.stop();
  }

  return (
    <div className="nv-composer-wrap" data-testid="nv-composer">
      {props.status ? (
        <div className="nv-status-strip" data-testid="nv-status-strip">
          <span className="nv-dot-pulse" />
          <span className="nv-status-verb">{props.status}</span>
          <span style={{ flex: 1 }} />
          <button type="button" className="nv-interrupt-btn"
            data-testid="nv-interrupt"
            onClick={props.onInterrupt}>◼ interrupt</button>
        </div>
      ) : null}
      {props.waitNote ? (
        <div className="nv-status-strip">
          <span className="nv-dot-attention" />
          <span className="nv-status-verb">{props.waitNote}</span>
        </div>
      ) : null}
      <div className="nv-composer-row">
        <button type="button" className="nv-composer-iconbtn" title="Attach"
          data-testid="nv-attach"
          onClick={function () {
            con.toast("Attachments land with the upload flow polish.");
          }}>
          <svg width="13" height="13" viewBox="0 0 14 14" fill="none"
            stroke="currentColor" strokeWidth="1.3">
            <path d="M12 6.5 7.5 11a3 3 0 0 1-4.2-4.2L8 2a2 2 0 0 1 2.8 2.8L6.2 9.4a1 1 0 0 1-1.4-1.4l4-4" />
          </svg>
        </button>
        <div className="nv-composer-field"
          data-recording={recording ? "true" : "false"}>
          {recording ? (
            <span className="nv-wave">
              <span /><span /><span />
            </span>
          ) : null}
          <input value={val}
            data-testid="nv-composer-input"
            placeholder={props.terminal
              ? "Send to reopen this session…"
              : "Send a message — Enter queues mid-run…"}
            onChange={function (ev) { setVal(ev.target.value); }}
            onKeyDown={function (ev) { if (ev.key === "Enter") send(); }} />
        </div>
        {props.micEnabled ? (
          <button type="button" className="nv-composer-iconbtn"
            title="Hold to talk — release lands as editable text, never auto-sends"
            data-testid="nv-mic"
            data-active={recording ? "true" : "false"}
            onMouseDown={micStart} onMouseUp={micStop}
            onMouseLeave={micStop}>
            <svg width="13" height="13" viewBox="0 0 14 14" fill="none"
              stroke="currentColor" strokeWidth="1.3">
              <rect x="5" y="1.5" width="4" height="7" rx="2" />
              <path d="M2.5 6.5a4.5 4.5 0 0 0 9 0M7 11v2" />
            </svg>
          </button>
        ) : null}
        {props.running ? (
          <button type="button" className="nv-stop-btn"
            data-testid="nv-stop"
            title="Interrupt the running turn"
            onClick={props.onInterrupt}>Stop</button>
        ) : null}
        <button type="button" className="nv-send-btn" data-testid="nv-send"
          onClick={send}>Send</button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The session doc
// ---------------------------------------------------------------------------
function NV_SessionDoc(props) {
  var con = NV_useConsole();
  var sid = props.sid;
  var tap = window.useWorkspaceTap(con.wid);
  var terminalRef = React.useRef(false);
  var detail = window.primerApi.useResource(
    SH_api.keys.session(sid),
    function (signal) { return SH_api.session(sid, signal); },
    { pollMs: terminalRef.current ? 0 : 2000, deps: [sid] }
  );
  terminalRef.current = !!(detail.data && NV_sessionIsOver(detail.data));
  var history = window.primerApi.useResource(
    SH_api.keys.session(sid) + ":messages",
    function (signal) { return SH_api.messages(sid, 200, null, signal); },
    { pollMs: terminalRef.current ? 0 : 2000, deps: [sid] }
  );
  var gates = window.primerApi.useResource(
    SH_api.keys.sessionPending(sid),
    function (signal) {
      return SH_api.sessionPendingYields(con.wid, sid, signal);
    },
    { pollMs: 5000, deps: [con.wid, sid] }
  );
  var traceState = React.useState(null);
  var traceTurn = traceState[0];
  var setTraceTurn = traceState[1];
  var voiceState = React.useState(false);
  var voiceOn = voiceState[0];
  var setVoiceOn = voiceState[1];
  // What auto-play may speak (the sh-voice rule, ported on flag day):
  // FINAL ANSWERS ONLY, and only the newest row - tool narration never
  // speaks, and toggling on never replays backlog beyond the last
  // answer. Dictation is the composer's; nothing here commits a gate.
  var spokenRef = React.useRef({});
  // seq -> expanded artifact descriptor. Inline-only (decision 4): a
  // chip toggles its artifact IN PLACE; tabs are an explicit escalation
  // on the block, never the click's side effect.
  var expandedState = React.useState({});
  var expanded = expandedState[0];
  var setExpanded = expandedState[1];
  var graphViewState = React.useState(false);
  var graphView = graphViewState[0];
  var setGraphView = graphViewState[1];
  var optimisticState = React.useState(null);
  var optimistic = optimisticState[0];
  var setOptimistic = optimisticState[1];

  var session = detail.data || null;
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

  var records = (history.data && history.data.items) || [];
  var flat = SH_nestSubagentRows(window.SA_toTranscript(records, session));
  var liveFromSeq = Infinity;
  if (shown) {
    for (var i = flat.length - 1; i >= 0; i--) {
      if (flat[i].kind === "user_message") {
        liveFromSeq = flat[i].seq;
        break;
      }
    }
  }
  var rows = SH_collapseTurns(flat, { liveFromSeq: liveFromSeq });

  var ttsOk = !!(con.speech && con.speech.tts_configured);
  React.useEffect(function () {
    if (!voiceOn || !ttsOk || !flat.length) return;
    var last = flat[flat.length - 1];
    if (last.kind !== "assistant_message" || !last.final) return;
    if (spokenRef.current[last.seq]) return;
    spokenRef.current[last.seq] = true;
    if (typeof window.CT_speakTurn === "function") {
      con.voiceRef.current = window.CT_speakTurn(
        String(last.text || last.label || ""),
        session && session.binding && session.binding.agent_id
      );
    }
  }, [flat.length, voiceOn, ttsOk]);
  var pending = (session && session.pending_messages) || [];
  var gateItems = window.SH_toAttentionItems({
    pending: (gates.data && gates.data.items) || [], records: [],
  });
  var agentId = session && session.binding
    ? (session.binding.agent_id || session.binding.graph_id)
    : null;
  var graphId = session && session.binding && session.binding.graph_id;

  // Scroll anchoring (same decision function as the sh doc).
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
  React.useEffect(function () {
    if (decision.follow) jumpLatest();
  }, [rows.length, decision.follow]);

  function refetchAll() {
    detail.refetch();
    history.refetch();
    gates.refetch();
  }

  function exportTranscript() {
    var blob = new Blob(
      [JSON.stringify(records, null, 2)],
      { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = sid + "-transcript.json";
    a.click();
  }

  var usage = window.NV_usageOf ? window.NV_usageOf(session) : {};

  function renderTurn(row, depth) {
    if (row.kind === "section") {
      return (
        <div key={row.seq} className="nv-turn-agent">
          <div className="nv-turn-sections">
            {(row.rows || []).map(function (child) {
              var info = child.kind === "tool_call"
                ? SH_toolChipLabel(child)
                : { label: child.label, tone: "other" };
              return (
                <div key={child.seq} className="nv-turn-section-line">
                  <svg width="8" height="8" viewBox="0 0 10 10" fill="none"
                    stroke="currentColor" strokeWidth="1.4">
                    <path d="M3.5 2 6.5 5 3.5 8" />
                  </svg>
                  {info.label}
                </div>
              );
            })}
          </div>
        </div>
      );
    }
    var ident = NV_identity(session && session.binding);
    if (row.kind === "user_message") {
      return (
        <div key={row.seq} className="nv-turn nv-turn-user"
          data-testid={"nv-turn:" + row.seq}>
          <div className="nv-user-avatar">
            {String(con.username).slice(0, 2).toLowerCase()}
          </div>
          <div className="nv-turn-main">
            <div className="nv-turn-byline">
              <span className="nv-turn-name">{con.username}</span>
            </div>
            <div className="nv-turn-text">{row.label}</div>
          </div>
        </div>
      );
    }
    var isChip = row.kind === "tool_call";
    var chip = isChip ? SH_toolChipLabel(row) : null;
    return (
      <div key={row.seq} className="nv-turn nv-turn-agent"
        data-depth={depth} data-testid={"nv-turn:" + row.seq}>
        <div className="nv-agent-avatar">
          <svg width="11" height="11" viewBox="0 0 12 12"
            style={{ color: ident.color }}>
            <path d={ident.d} fill="currentColor" />
          </svg>
        </div>
        <div className="nv-turn-main">
          <div className="nv-turn-byline">
            <span className="nv-turn-name" style={{ color: ident.color }}>
              {(row.payload && row.payload.agent_id) || agentId || "agent"}
            </span>
            <span style={{ flex: 1 }} />
            {row.turn_no != null || row.seq != null ? (
              <button type="button" className="nv-trace-toggle"
                data-testid={"nv-trace-open:" + row.seq}
                onClick={function () {
                  setTraceTurn(row.turn_no != null ? row.turn_no : 1);
                }}>trace</button>
            ) : null}
          </div>
          {isChip ? (
            <div className="nv-turn-chips">
              {(function () {
                var artifact = NV_artifactFor(row, chip);
                return (
                  <button type="button" className="nv-chip-pill"
                    data-tone={chip.tone}
                    data-expandable={artifact ? "true" : "false"}
                    onClick={function () {
                      if (!artifact) return;
                      setExpanded(function (prev) {
                        var next = Object.assign({}, prev);
                        if (next[row.seq]) delete next[row.seq];
                        else next[row.seq] = artifact;
                        return next;
                      });
                    }}>
                    <span className="nv-chip-icon">
                      {NV_CHIP_ICONS[chip.tone] || NV_CHIP_ICONS.other}
                    </span>
                    {chip.label}
                  </button>
                );
              })()}
            </div>
          ) : (
            <div className="nv-turn-text">{row.label}</div>
          )}
          {expanded[row.seq] ? (
            <NV_ArtifactBlock seq={row.seq}
              path={expanded[row.seq].path}
              output={expanded[row.seq].output} />
          ) : null}
          {(row.children || []).map(function (child) {
            var cInfo = child.kind === "tool_call"
              ? SH_toolChipLabel(child) : null;
            return (
              <div key={child.seq} className="nv-subagent">
                <div className="nv-subagent-head">
                  <span className="nv-subagent-name">
                    {(child.payload && child.payload.agent_id) || "subagent"}
                  </span>
                </div>
                {cInfo ? (
                  <span className="nv-chip-pill" data-tone={cInfo.tone}>
                    <span className="nv-chip-icon">
                      {NV_CHIP_ICONS[cInfo.tone] || "⚙"}
                    </span>
                    {cInfo.label}
                  </span>
                ) : (
                  <div className="nv-turn-text">{child.label}</div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  return (
    <div className="nv-session-doc" data-testid={"nv-session-doc:" + sid}>
      {typeof window.NV_ClientTools === "function"
        ? <window.NV_ClientTools sid={sid} />
        : null}
      {typeof window.ExternalPendingBanner === "function" ? (
        <window.ExternalPendingBanner sessionId={sid}
          pushToast={window.primerApi.toastPush} />
      ) : null}
      <NV_SessionHeader sid={sid} session={session} usage={usage}
        voiceOn={voiceOn} ttsOk={ttsOk}
        onToggleVoice={function () {
          // Toggling off is also the stop control (WCAG 1.4.2): pause
          // whatever is speaking rather than letting it run out.
          if (voiceOn && con.voiceRef.current
              && typeof con.voiceRef.current.pause === "function") {
            con.voiceRef.current.pause();
          }
          setVoiceOn(!voiceOn);
        }}
        onGraphView={function () { setGraphView(true); }}
        onChanged={refetchAll}
        onDeleted={function () { con.setDoc(null); }}
        onExport={exportTranscript} />
      <div className="nv-transcript-split">
        <div className="nv-transcript" ref={scrollRef}
          data-testid="nv-transcript"
          onScroll={function () { if (decision.follow) setSeen(rows.length); }}>
          <div className="nv-transcript-inner">
            {rows.map(function (r) { return renderTurn(r, 0); })}
            {pending.map(function (row) {
              var text = (row.parts || [])
                .filter(function (p) { return p && p.type === "text"; })
                .map(function (p) { return p.text; }).join("\n");
              return (
                <div key={row.id} className="nv-steer-chip-wrap">
                  <div className="nv-steer-chip"
                    data-testid={"nv-queued:" + row.id}>
                    <span className="nv-steer-mark">queued</span>
                    <span>{text}</span>
                    <button type="button"
                      title="Dismiss — a failed turn leaves this queued forever"
                      onClick={function () {
                        SH_api.dismissQueuedSteer(con.wid, sid, row.id)
                          .then(refetchAll);
                      }}>×</button>
                  </div>
                </div>
              );
            })}
            {gateItems.map(function (item) {
              return item.kind === "approval" ? (
                <NV_DecisionCard key={item.id} item={item}
                  onResolved={refetchAll} />
              ) : (
                <NV_AskCard key={item.id} item={item}
                  onResolved={refetchAll} />
              );
            })}
            {NV_sessionIsOver(session) ? (
              <div className="nv-fold-line">
                <span /><span className="nv-fold-label">
                  session ended{session.ended_reason
                    ? " · " + session.ended_reason : ""}
                </span><span />
              </div>
            ) : null}
          </div>
          {decision.showJump ? (
            <div className="nv-jump-wrap">
              <button type="button" className="nv-jump"
                data-testid="nv-jump-latest"
                onClick={jumpLatest}>↓ Jump to latest</button>
            </div>
          ) : null}
        </div>
        {traceTurn != null ? (
          <NV_TraceSplit sid={sid} turnNo={traceTurn}
            onClose={function () { setTraceTurn(null); }} />
        ) : null}
      </div>
      <NV_Composer sid={sid}
        running={!!shown}
        status={status}
        waitNote={session && session.parked_status
          ? "parked — waiting on " + (session.waiting_reason || "a wake")
          : null}
        terminal={NV_sessionIsOver(session)}
        micEnabled={!!(window.primerApi.capabilities
          && window.primerApi.capabilities.stt_configured)}
        onInterrupt={function () {
          SH_api.interrupt(con.wid, sid).then(refetchAll);
        }}
        onSendStarted={function () {
          setOptimistic(Date.now());
          detail.refetch();
          history.refetch();
        }} />
      {graphView && graphId
        && typeof window.SD_GraphRunView === "function" ? (
          <div className="nv-scrim"
            onClick={function () { setGraphView(false); }}>
            <div className="nv-graph-overlay"
              onClick={function (ev) { ev.stopPropagation(); }}>
              <div className="nv-trace-head">
                <span>graph view</span>
                <span style={{ flex: 1 }} />
                <button type="button" className="nv-rail-iconbtn"
                  onClick={function () { setGraphView(false); }}>×</button>
              </div>
              <window.SD_GraphRunView gid={graphId} rid={sid} wid={con.wid}
                session={session}
                pushToast={window.primerApi.toastPush}
                hideInspector={true} />
            </div>
          </div>
        ) : null}
    </div>
  );
}

window.NV_SessionDoc = NV_SessionDoc;
window.NV_DecisionCard = NV_DecisionCard;
window.NV_AskCard = NV_AskCard;
window.NV_TraceSplit = NV_TraceSplit;
window.NV_Composer = NV_Composer;
