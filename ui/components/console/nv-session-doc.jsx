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
// US-008 R3 item 2: session verbs, extracted to standalone functions so
// the SAME logic backs both an inline button (closed over this
// instance's own wid/sid/refetchAll) and a palette verb (resolved to
// whichever session tab is focused - see the registration effect below).
// Pure delegation to SH_api; no component state, so no closure to go
// stale.
// ---------------------------------------------------------------------------
function NV_doInterrupt(wid, sid, refetchAll, toast) {
  return SH_api.interrupt(wid, sid).then(refetchAll, function (err) {
    toast("Interrupt failed: " + err.message);
  });
}
function NV_doPark(wid, sid, refetchAll, toast) {
  return SH_api.pause(wid, sid).then(refetchAll, function (err) {
    toast("Park failed: " + err.message);
  });
}
// Nothing you can type resumes a parked session from the palette the way
// sending a message does from the composer (sh-api.jsx's pause/cancel
// comment) - so "Resume Session" lands the operator in the composer
// rather than calling an endpoint that does not exist.
function NV_doResume() {
  var el = document.querySelector('[data-testid="nv-composer-input"]');
  if (el && typeof el.focus === "function") el.focus();
}
function NV_doClose(wid, sid, refetchAll, toast) {
  return SH_api.cancel(wid, sid).then(refetchAll, function (err) {
    toast("Close failed: " + err.message);
  });
}
function NV_doRename(wid, sid, currentName, refetchAll, toast) {
  return window.promptDialog({
    title: "Rename session", defaultValue: currentName || "",
  }).then(function (name) {
    if (name == null) return;
    return SH_api.renameSession(wid, sid, name || null).then(
      refetchAll, function (err) { toast("Rename failed: " + err.message); }
    );
  });
}
// The tab-group model lives in the shell (nv-shell.jsx's con.tgModel /
// con.onTgModelChange) - this only ever reads/writes through that seam,
// same as every other doc-level caller.
function NV_doSplitRight(con, sid) {
  if (!con.tgModel || typeof con.onTgModelChange !== "function") return;
  var tabId = window.TG_tabId("session", sid);
  con.onTgModelChange(
    window.TG_splitWith(con.tgModel, tabId, "row"), "manage"
  );
}
// US-008 R3 item 4: Compact fires immediately (notes 2.4: "folds into a
// summary marker immediately") - no picker, so the palette verb and the
// overflow row both call this directly.
function NV_doCompact(wid, sid, refetchAll, toast) {
  return SH_api.compact(wid, sid).then(refetchAll, function (err) {
    toast("Compact failed: " + err.message);
  });
}
// Rewind needs a target turn first (notes 2.4: "picker overlay — radio
// list of user turns"), so both the overflow row and the palette verb
// only OPEN the picker; the actual SH_api.rewind call happens once the
// operator confirms a choice (NV_RewindPicker's onConfirm below).
function NV_doRewind(wid, sid, toSeq, refetchAll, toast) {
  return SH_api.rewind(wid, sid, toSeq).then(refetchAll, function (err) {
    toast("Rewind failed: " + err.message);
  });
}
// A rewind target must be a currently-visible user_input, strictly
// after the latest visible compaction marker and strictly before the
// newest visible record (primer/session/rewind.py's check_rewind_target,
// mirrored here so the picker never offers a choice the backend would
// 422/409 on). "Currently visible" is doing real work: a user_input a
// PRIOR rewind already discarded must not be offered again, which is
// exactly what deriving candidates from SA_visibleRecords' progressive
// fold gives for free (a discarded record is simply absent from it) -
// no separate floor bookkeeping needed here, only a genuinely separate
// concern (the compaction floor) stays explicit below.
function NV_rewindCandidates(records) {
  var folded = window.SA_visibleRecords
    ? window.SA_visibleRecords(records || [])
    : (records || []);
  // SA_visibleRecords keeps rewind_marker in ITS OWN result (frontend-
  // only, so a rewind renders as a divider) - the backend's own
  // visible_records() never returns it (a pure instruction, not
  // content). Excluded here too so "newest visible record" matches
  // check_rewind_target's definition exactly, not an inflated one that
  // could let a genuinely-newest user_input through because a later
  // rewind_marker's own seq shadowed it.
  var visible = folded.filter(function (r) { return r.kind !== "rewind_marker"; });
  var newest = 0;
  var newestCompactionSeq = 0;
  for (var i = 0; i < visible.length; i++) {
    var r = visible[i];
    if (r.seq > newest) newest = r.seq;
    if (r.kind === "compaction_marker" && r.seq > newestCompactionSeq) {
      newestCompactionSeq = r.seq;
    }
  }
  var out = [];
  for (var j = 0; j < visible.length; j++) {
    var rec = visible[j];
    if (rec.kind !== "user_input") continue;
    if (rec.seq <= newestCompactionSeq) continue;
    if (rec.seq >= newest) continue;
    out.push({ seq: rec.seq, text: (rec.payload && rec.payload.text) || "" });
  }
  return out;
}

function NV_RewindPicker(props) {
  var selState = React.useState(null);
  var sel = selState[0];
  var setSel = selState[1];
  var candidates = props.candidates || [];
  return (
    <div className="nv-scrim" data-testid="nv-rewind-scrim"
      onClick={props.onClose}>
      <div className="nv-overlay-panel nv-rewind-picker"
        data-testid="nv-rewind-picker"
        role="dialog" aria-label="Rewind session"
        onClick={function (ev) { ev.stopPropagation(); }}>
        <div className="nv-overlay-head">
          <h3 className="nv-overlay-title">Rewind to a kept turn</h3>
        </div>
        <div className="nv-rewind-body">
          {!candidates.length ? (
            <div className="nv-rewind-empty">
              No earlier turn to rewind to.
            </div>
          ) : candidates.map(function (c) {
            return (
              <label key={c.seq} className="nv-rewind-row"
                data-testid={"nv-rewind-option:" + c.seq}>
                <input type="radio" name="nv-rewind-target"
                  checked={sel === c.seq}
                  onChange={function () { setSel(c.seq); }} />
                <span className="nv-rewind-text">
                  {String(c.text || "(turn #" + c.seq + ")").slice(0, 140)}
                </span>
              </label>
            );
          })}
        </div>
        <div className="nv-rewind-actions">
          <button type="button" className="nv-menu-row"
            data-testid="nv-rewind-cancel"
            onClick={props.onClose}>Cancel</button>
          <button type="button" className="nv-btn-primary"
            data-testid="nv-rewind-confirm"
            disabled={sel == null}
            onClick={function () { props.onConfirm(sel); }}>
            Rewind
          </button>
        </div>
      </div>
    </div>
  );
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
  var matched = rows.filter(function (r) {
    return r.name.toLowerCase().indexOf(needle) >= 0;
  });
  // The list stays short - search is the navigation - but a silent cap
  // read as "that agent is gone" on installs with >20 bindables (BDD
  // pass 2026-08-24), so truncation now says so.
  var hidden = Math.max(0, matched.length - 20);
  rows = matched.slice(0, 20);

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
            {hidden ? (
              <div className="nv-bind-note" data-testid="nv-binding-more">
                {"+" + hidden + " more - type to narrow"}
              </div>
            ) : null}
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
          data-verb="session.rename"
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
            {!NV_sessionIsOver(session) ? (
              <button type="button" className="nv-menu-row"
                data-verb="session.park"
                onClick={function () {
                  setOvf(false);
                  var verb = con.registry.get("session.park");
                  if (verb) verb.run();
                }}>Park Session</button>
            ) : null}
            <button type="button" className="nv-menu-row"
              data-verb="session.resume"
              onClick={function () {
                setOvf(false);
                var verb = con.registry.get("session.resume");
                if (verb) verb.run();
              }}>Resume Session</button>
            <button type="button" className="nv-menu-row"
              data-verb="session.splitRight"
              onClick={function () {
                setOvf(false);
                var verb = con.registry.get("session.splitRight");
                if (verb) verb.run();
              }}>Split Right</button>
            <div className="nv-menu-sep" />
            <button type="button" className="nv-menu-row"
              data-verb="session.rewind"
              onClick={function () {
                setOvf(false);
                props.onOpenRewind();
              }}>
              Rewind…
            </button>
            <button type="button" className="nv-menu-row"
              data-verb="session.compact"
              onClick={function () {
                setOvf(false);
                props.onCompact();
              }}>
              Compact…
            </button>
            <button type="button" className="nv-menu-row"
              onClick={function () {
                setOvf(false);
                props.onExport();
              }}>Export transcript</button>
            <div className="nv-menu-sep" />
            {!NV_sessionIsOver(session) ? (
              <button type="button" className="nv-menu-row" data-danger="true"
                data-verb="session.close"
                onClick={function () {
                  setOvf(false);
                  var verb = con.registry.get("session.close");
                  if (verb) verb.run();
                }}>Close Session</button>
            ) : null}
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
// Thought + tool blocks (round 4, 2026-08-26). Thinking renders as a
// collapsed muted toggle. A tool call renders as ONE expandable block
// - collapsed it reads as a plain verb line ("ran ls artifacts"),
// expanded it shows the arguments and the paired result - because a
// bare label line answered neither "with what?" nor "and?", and the
// separate result rows rendered as empty chevron lines.
// ---------------------------------------------------------------------------
function NV_Thought(props) {
  var openState = React.useState(false);
  var open = openState[0];
  var setOpen = openState[1];
  var text = props.row.label || "";
  return (
    <div className="nv-thought" data-testid={"nv-thought:" + props.row.seq}
      data-open={open ? "true" : "false"}>
      <button type="button" className="nv-thought-toggle"
        onClick={function () { setOpen(!open); }}>
        <span className="nv-thought-mark">{open ? "▾" : "▸"}</span>
        thought
        {!open && text ? (
          <span className="nv-thought-peek">
            {String(text).slice(0, 110)}
          </span>
        ) : null}
      </button>
      {open ? <div className="nv-thought-body">{text}</div> : null}
    </div>
  );
}

function NV_ToolBlock(props) {
  var con = NV_useConsole();
  var openState = React.useState(false);
  var open = openState[0];
  var setOpen = openState[1];
  var maxState = React.useState(false);
  var maxed = maxState[0];
  var setMaxed = maxState[1];
  var row = props.row;
  var info = SH_toolChipLabel(row);
  var args = (row.payload && row.payload.arguments) || {};
  var rp = (props.result && props.result.payload) || null;
  // US-008 R3 item 1: an elapsed timer while the call has no result yet
  // and the session has a live turn (props.running) - a call that's
  // already durable (arguments complete) but still executing (no
  // tool_result durable record yet). Ticks once a second, only while
  // both conditions hold, so a finished or historical call never runs a
  // timer.
  var stillRunning = !rp && !!props.running;
  var tickState = React.useState(0);
  var setTick = tickState[1];
  var startedAtRef = React.useRef(null);
  if (startedAtRef.current == null) {
    var parsed = row.createdAt ? Date.parse(row.createdAt) : NaN;
    startedAtRef.current = isNaN(parsed) ? Date.now() : parsed;
  }
  React.useEffect(function () {
    if (!stillRunning) return;
    var t = setInterval(function () { setTick(function (n) { return n + 1; }); }, 1000);
    return function () { clearInterval(t); };
  }, [stillRunning]);
  var elapsedS = stillRunning
    ? Math.max(0, Math.floor((Date.now() - startedAtRef.current) / 1000))
    : null;
  var serialized = React.useMemo(function () {
    if (!open) return null;
    if (!rp) return { output: null, lines: [] };
    var output = typeof rp.output === "string"
      ? rp.output
      : JSON.stringify(rp.output, null, 2);
    return { output: output, lines: output.split("\n") };
  }, [rp, open]);
  var output = serialized && serialized.output;
  var lines = (serialized && serialized.lines) || [];
  return (
    <div className="nv-toolblock" data-testid={"nv-tool:" + row.seq}
      data-open={open ? "true" : "false"} data-tone={info.tone}>
      <button type="button" className="nv-toolblock-head"
        onClick={function () { setOpen(!open); }}>
        <span className="nv-thought-mark">{open ? "▾" : "▸"}</span>
        <span className="nv-chip-icon">
          {NV_CHIP_ICONS[info.tone] || NV_CHIP_ICONS.other}
        </span>
        <span className="nv-toolblock-label">{info.label}</span>
        {stillRunning ? (
          <span className="nv-toolblock-elapsed"
            data-testid={"nv-tool-elapsed:" + row.seq}>{elapsedS}s</span>
        ) : null}
        {rp && rp.error ? (
          <span className="nv-toolblock-err">failed</span>
        ) : null}
      </button>
      {open ? (
        <div className="nv-toolblock-body">
          <div className="nv-toolblock-sec">arguments</div>
          <pre className="nv-toolblock-pre">
            {JSON.stringify(args, null, 2)}
          </pre>
          <div className="nv-toolblock-sec">
            <span>result</span>
            <span style={{ flex: 1 }} />
            {info.path ? (
              <button type="button" className="nv-artifact-verb"
                onClick={function () {
                  con.setDoc({ kind: "file", ref: info.path });
                }}>Open as Tab</button>
            ) : null}
            {lines.length > 14 ? (
              <button type="button" className="nv-artifact-verb"
                onClick={function () { setMaxed(true); }}>Maximize</button>
            ) : null}
          </div>
          <pre className="nv-toolblock-pre" data-error={rp && rp.error
            ? "true" : "false"}>
            {output == null
              ? "(no result recorded)"
              : lines.slice(0, 14).join("\n")
                + (lines.length > 14 ? "\n…" : "")}
          </pre>
          {maxed ? (
            <NV_Lightbox title={info.label} content={output || ""}
              onClose={function () { setMaxed(false); }} />
          ) : null}
        </div>
      ) : null}
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
    <div className="nv-card nv-card-attention" data-kind="approval"
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
  var errState = React.useState(null);
  var err = errState[0];
  var setErr = errState[1];
  function submit() {
    setErr(null);
    SH_api.answer(item.sessionId, item.toolCallId, val).then(
      props.onResolved,
      // INLINE, never a toast: the operator sees the failure exactly
      // where the submission happened, and the card stays to retry.
      function (e) {
        setErr(e.detail || e.title || e.message || "Respond failed");
      }
    );
  }
  return (
    <div className="nv-card nv-card-attention" data-kind="question"
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
          onChange={function (ev) { setVal(ev.target.value); }}
          onKeyDown={function (ev) {
            // Enter submits, like the composer; Shift+Enter breaks a line.
            if (ev.key === "Enter" && !ev.shiftKey) {
              ev.preventDefault();
              submit();
            }
          }} />
        {err ? (
          <div className="nv-form-error" data-testid="nv-ask-error">{err}</div>
        ) : null}
        <div className="nv-card-actions">
          <button type="button" className="nv-btn-primary"
            data-testid="nv-ask-submit"
            onClick={submit}>Answer & resume</button>
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

// A node filter is a view of the same turns, not a different fetch: the
// canvas selection scopes what the turn list shows and nothing else. A
// turn that carries no node attribution is graph-level and stays visible
// at every filter, otherwise selecting a node would hide the run's own
// start and finish. (Ported from the sh doc on flag day.)
function NV_scopeToNode(rows, nodeId) {
  if (!nodeId) return rows;
  return (rows || []).filter(function (row) {
    var n = row && (row.node_id || row.node);
    return !n || n === nodeId;
  });
}

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
// Status strip - own 1s ticker, keeps the transcript from re-rendering for the clock
// ---------------------------------------------------------------------------
function NV_StatusStrip(props) {
  var setTick = React.useState(0)[1];
  React.useEffect(function () {
    var id = setInterval(function () {
      setTick(function (n) { return n + 1; });
    }, 1000);
    return function () { clearInterval(id); };
  }, []);
  var shown = props.shown;
  if (!shown) return null;
  var line = window.SH_statusLine({
    verb: shown.verb, object: shown.object,
    elapsedSec: Math.round((Date.now() - shown.startedMs) / 1000),
  });
  if (!line) return null;
  return (
    <div className="nv-status-strip" data-testid="nv-status-strip">
      <span className="nv-dot-pulse" />
      <span className="nv-status-verb">{line}</span>
      <span style={{ flex: 1 }} />
      <button type="button" className="nv-interrupt-btn"
        data-testid="nv-interrupt" data-verb="session.interrupt"
        onClick={props.onInterrupt}>◼ interrupt</button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Composer (status strip + input row + mic + stop + send)
// ---------------------------------------------------------------------------
// US-008 behavior 2 (notes 2.4): "drafts survive tab switches - keyed by
// session id." Module-level so it outlives any single NV_Composer mount;
// nv-doc-host.jsx does not key session docs by sid today, so a session
// switch may re-render this SAME component instance with a new sid rather
// than unmount/remount it - the sid-change effect below re-syncs `val`
// from this map either way, so two sessions never see each other's drafts
// regardless of which mounting behavior is in play.
var NV_DRAFTS = {};

// US-008 R3 item 2: the palette registers session verbs ONCE, globally
// (registry.register throws on a duplicate id), but a verb's run() fires
// long after registration and must act on WHICHEVER session tab is
// focused then - never the one instance that happened to register it.
// nv-shell.jsx's SH_liveShell comment documents the exact stale-closure
// bug this avoids for its own chrome-level verbs. Every mounted
// NV_SessionDoc refreshes this on render (plain ref writes, same idiom
// as terminalRef above), so a verb's run() always reads the CURRENT
// console object and can always find the currently-focused session's
// wid/refetchAll.
var NV_SESSION_CON_REF = { current: null };
var NV_SESSION_INSTANCES = {};

function NV_Composer(props) {
  var con = NV_useConsole();
  var valState = React.useState(function () { return NV_DRAFTS[props.sid] || ""; });
  var val = valState[0];
  var setVal = valState[1];
  var draftSidRef = React.useRef(props.sid);
  React.useEffect(function () {
    if (draftSidRef.current === props.sid) return;
    draftSidRef.current = props.sid;
    setVal(NV_DRAFTS[props.sid] || "");
  }, [props.sid]);
  var recState = React.useState(false);
  var recording = recState[0];
  var setRecording = recState[1];
  var recRef = React.useRef(null);
  // The active getUserMedia stream, so an unmount mid-recording can stop
  // its tracks directly (defect 3, R3 review) rather than only asking
  // the MediaRecorder to stop and hoping rec.onstop runs before the
  // component is gone.
  var streamRef = React.useRef(null);
  // US-008 R3 item 5: double-tap latches recording on (hands-free);
  // click again stops it. Hold-to-talk is unchanged - a normal
  // press-and-release still stops on release, same as before.
  var latchState = React.useState(false);
  var latched = latchState[0];
  var setLatched = latchState[1];
  var micLastUpRef = React.useRef(0);
  var micPendingStopRef = React.useRef(null);
  var MIC_DOUBLE_TAP_MS = 350;
  var sendingState = React.useState(false);
  var sending = sendingState[0];
  var setSending = sendingState[1];
  var sendErrState = React.useState(null);
  var sendErr = sendErrState[0];
  var setSendErr = sendErrState[1];
  var inputRef = React.useRef(null);

  function grow() {
    var el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 176) + "px";
  }

  function send() {
    var text = String(val || "").trim();
    if (!text || sending) return;
    setSending(true);
    setSendErr(null);
    // The store owns the optimistic row and the steer POST; it removes the
    // optimistic row on failure, and the composer restores the composer
    // text + shows the inline error (P0 send-failure behaviour).
    var clientId = "steer-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
    var promise = typeof props.onSend === "function"
      ? props.onSend(text, clientId)
      : SH_api.steer(con.wid, props.sid, text);
    promise.then(function () {
      setVal("");
      // Clear only on success - a failed send restores the typed text
      // (P0 behaviour, untouched below) and the draft must restore with
      // it, so a retry after a tab switch still has what was typed.
      delete NV_DRAFTS[props.sid];
      setSending(false);
      grow();
      props.onSendStarted();
    }, function (err) {
      setSending(false);
      var msg = (err && err.message) ? err.message : "Steer failed";
      var rid = (err && err.requestId) ? " (" + err.requestId + ")" : "";
      setSendErr(msg + rid);
      con.toast("Steer failed: " + msg);
    });
  }

  React.useEffect(function () { grow(); }, [val]);

  function micStart() {
    if (recording || !props.micEnabled) return;
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var rec = new MediaRecorder(stream);
      streamRef.current = stream;
      var chunks = [];
      rec.ondataavailable = function (ev) { chunks.push(ev.data); };
      rec.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        streamRef.current = null;
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
  function micClearPendingStop() {
    if (micPendingStopRef.current) {
      clearTimeout(micPendingStopRef.current);
      micPendingStopRef.current = null;
    }
  }
  // Pressing while latched is the "click again" gesture - handled
  // entirely on release below, so press does nothing but unlatch-guard.
  // Pressing while a first tap's release is still in its grace window
  // (see micUp) is the SECOND tap of a double-tap arriving early -
  // cancel that pending stop so the SAME recording carries through,
  // rather than stopping and restarting mid-gesture.
  function micDown() {
    if (latched) return;
    micClearPendingStop();
    micStart();
  }
  function micUp() {
    if (latched) {
      setLatched(false);
      micStop();
      return;
    }
    var now = Date.now();
    var isSecondTap = now - micLastUpRef.current < MIC_DOUBLE_TAP_MS;
    micLastUpRef.current = now;
    if (isSecondTap) {
      // Confirmed double-tap: latch on, recording carries through
      // unchanged (never stopped between the two taps).
      setLatched(true);
      return;
    }
    // Might be the first tap of a double-tap in progress - give a brief
    // grace window for a second press before treating this as a normal
    // hold-to-talk release.
    micClearPendingStop();
    micPendingStopRef.current = setTimeout(function () {
      micPendingStopRef.current = null;
      micStop();
    }, MIC_DOUBLE_TAP_MS);
  }
  function micLeave() {
    // Latched recording must survive the pointer leaving the button -
    // it is a toggle now, not a hold. Un-latched, dragging off the
    // button while still holding it down stops it exactly as before.
    if (!latched) micStop();
  }
  // R3 review defect 3: a latched (or mid-grace-window) recording has no
  // release event left to stop it if the composer unmounts instead -
  // closing the session tab, navigating away - so the mic would keep
  // recording in the background indefinitely. `[]` deps: this must run
  // once, at true unmount, so it reads refs (always current) rather than
  // `recording` (a state value this closure would otherwise freeze at
  // whatever it was on first render).
  React.useEffect(function () {
    return function () {
      micClearPendingStop();
      if (recRef.current && recRef.current.state !== "inactive") {
        recRef.current.stop();
      }
      if (streamRef.current) {
        streamRef.current.getTracks().forEach(function (t) { t.stop(); });
      }
    };
  }, []);

  return (
    <div className="nv-composer-wrap" data-testid="nv-composer">
      <NV_StatusStrip shown={props.statusShown}
        onInterrupt={props.onInterrupt} />
      {props.degraded ? (
        <div className="nv-status-strip" data-testid="nv-reconnect">
          <span className="nv-dot-attention" />
          <span className="nv-status-verb">reconnecting…</span>
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
          <textarea ref={inputRef} value={val} rows={1}
            data-testid="nv-composer-input"
            placeholder={props.terminal
              ? "Send to reopen this session…"
              : "Send a message — Enter queues mid-run…"}
            onChange={function (ev) {
              var v = ev.target.value;
              setVal(v);
              NV_DRAFTS[props.sid] = v;
            }}
            onKeyDown={function (ev) {
              if (ev.key === "Enter" && !ev.shiftKey
                  && !ev.nativeEvent.isComposing) {
                ev.preventDefault();
                send();
                return;
              }
              // US-008 behavior 3 (notes 2.4): "/" in an EMPTY composer
              // opens the palette instead of typing - non-empty text
              // types "/" normally (no special-casing once there is any
              // text), and an IME composition never triggers it, same
              // guard as Enter above.
              if (ev.key === "/" && !val && !ev.nativeEvent.isComposing) {
                ev.preventDefault();
                var verb = con.registry.get("palette.open");
                if (verb) verb.run();
              }
            }} />
        </div>
        {props.micEnabled ? (
          <button type="button" className="nv-composer-iconbtn"
            title={"Hold to talk, or double-tap to latch. Release "
              + "lands as editable text, never auto-sends."}
            data-testid="nv-mic"
            data-active={recording ? "true" : "false"}
            data-latched={latched ? "true" : "false"}
            onMouseDown={micDown} onMouseUp={micUp}
            onMouseLeave={micLeave}>
            <svg width="13" height="13" viewBox="0 0 14 14" fill="none"
              stroke="currentColor" strokeWidth="1.3">
              <rect x="5" y="1.5" width="4" height="7" rx="2" />
              <path d="M2.5 6.5a4.5 4.5 0 0 0 9 0M7 11v2" />
            </svg>
          </button>
        ) : null}
        {props.running ? (
          <button type="button" className="nv-stop-btn"
            data-testid="nv-stop" data-verb="session.interrupt"
            title="Interrupt the running turn"
            onClick={props.onInterrupt}>Stop</button>
        ) : null}
        <button type="button" className="nv-send-btn" data-testid="nv-send"
          data-mode={props.running ? "queue" : "send"}
          disabled={sending}
          onClick={send}>{sending ? "Sending…" : (props.running ? "Queue" : "Send")}</button>
      </div>
      {sendErr ? (
        <div className="nv-form-error" data-testid="nv-composer-error">
          {sendErr}
        </div>
      ) : null}
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
  // Phase 2: the per-session store is the single source for transcript,
  // status, and connection state. The tap hub routes live frames here;
  // the REST history seed below fills the durable records. We bind to
  // all three channels so a change re-renders the consumer without a
  // REST poll.
  var store = window.SS_getStore(con.wid, sid);
  var transcriptSnap = window.useSessionStore(con.wid, sid, "transcript");
  var statusSnap = window.useSessionStore(con.wid, sid, "status");
  var gatesSnap = window.useSessionStore(con.wid, sid, "gates");
  var terminalRef = React.useRef(false);
  // Declared BEFORE the resources on purpose: a send into an ENDED
  // session REOPENS it server-side (steer's fourth behaviour), so the
  // ended-session poll stop must lift while a send is in flight - the
  // old refetch-once in onSendStarted raced the POST, read the still
  // ended row, and froze the doc on a session that was actually
  // running again (BDD round 2, 2026-08-24).
  var optimisticState = React.useState(null);
  var optimistic = optimisticState[0];
  var setOptimistic = optimisticState[1];
  // Computed BEFORE the resources: the tap knows a reopened session is
  // running before the (stale, still "ended") detail row does, so the
  // poll gate must consult it - gating on the stale row alone froze
  // the doc the moment the live signal cleared the optimistic flag.
  var live = SH_statusFromTap(tap.events, sid, Date.now());
  var pollStopped = terminalRef.current && !optimistic && !live;
  var detail = window.primerApi.useResource(
    SH_api.keys.session(sid),
    function (signal) { return SH_api.session(sid, signal); },
    { pollMs: pollStopped ? 0 : 2000, deps: [sid], ignoreIdle: true }
  );
  terminalRef.current = !!(detail.data && NV_sessionIsOver(detail.data));
  // C3 poll demotion: once the tap is live the REST history poll is the
  // slow catch-up leg, not the live source; the store gets frames from
  // the hub. Keep the detail poll at 2000ms (turn_status / parked rows).
  var historyLive = !!(gatesSnap && gatesSnap.connState === "live");
  var history = window.primerApi.useResource(
    SH_api.keys.session(sid) + ":messages",
    function (signal) { return SH_api.messages(sid, 200, null, signal); },
    { pollMs: pollStopped ? 0 : (historyLive ? 15000 : 2000), deps: [sid], ignoreIdle: true }
  );
  var gates = window.primerApi.useResource(
    SH_api.keys.sessionPending(sid),
    function (signal) {
      return SH_api.sessionPendingYields(con.wid, sid, signal);
    },
    { pollMs: 5000, deps: [con.wid, sid] }
  );
  // Top-level hook call (it is a hook: useRef+useEffect inside); the hook
  // keeps the latest closure in a ref, so gates.refetch stays current.
  window.useWorkspaceTapListener(con.wid, function (ev) {
    if (ev && ev.session_id === sid
        && (ev["class"] === "yielded"
            || ev["class"] === "resumed"
            || ev["class"] === "done")) {
      gates.refetch();
    }
  });
  // Seed the store from the REST history resource so the first paint is
  // not slower than the old REST-only path. The store dedupes by seq,
  // so re-applying on every history poll is a no-op once caught up.
  React.useEffect(function () {
    var items = (history.data && history.data.items) || [];
    for (var i = 0; i < items.length; i++) {
      window.SS_apply(store, items[i]);
    }
  }, [store, history.data, history.error]);
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
  var graphViewState = React.useState(false);
  var graphView = graphViewState[0];
  var setGraphView = graphViewState[1];
  // Selecting a node on the graph canvas filters the transcript to it
  // (and closes the graph overlay so the filtered transcript shows).
  var nodeFilterState = React.useState(null);
  var nodeFilter = nodeFilterState[0];
  var setNodeFilter = nodeFilterState[1];
  // US-008 R3 item 4: the rewind picker overlay (notes 2.4). Lives here,
  // not in NV_SessionHeader, because it needs the record list to derive
  // candidates - the overflow menu's Rewind row and the palette verb
  // both just open it (see NV_SESSION_INSTANCES.openRewind below).
  var rewindOpenState = React.useState(false);
  var rewindOpen = rewindOpenState[0];
  var setRewindOpen = rewindOpenState[1];

  var session = detail.data || null;

  // US-008 R3 item 2: refresh the shared "current console + current
  // session instance" lookup on every render (plain ref writes - see
  // NV_SESSION_CON_REF's own comment) so the verbs registered below
  // always resolve the FOCUSED session tab, not this one specifically.
  NV_SESSION_CON_REF.current = con;
  // refetchAll is a hoisted function declaration further down this same
  // component body - available here regardless of textual order.
  NV_SESSION_INSTANCES[sid] = {
    wid: con.wid, session: session, refetchAll: refetchAll,
    openRewind: function () { setRewindOpen(true); },
  };
  React.useEffect(function () {
    return function () { delete NV_SESSION_INSTANCES[sid]; };
  }, [sid]);

  // Registered once, ever (registry.register throws on a repeat id);
  // every later NV_SessionDoc mount's attempt is a no-op guarded the
  // same way nv-shell.jsx's own core-verbs effect guards itself.
  // contexts: ["session"] hard-gates these out of the ranked list
  // unless the focused doc is a session (SH_rankVerbs), and nv-palette.jsx
  // already special-cases contexts: ["session"] to lead the list ahead
  // of everything else once that gate passes.
  React.useEffect(function () {
    var registry = con.registry;
    function reg(v) { if (!registry.get(v.id)) registry.register(v); }
    function focused() {
      var c = NV_SESSION_CON_REF.current;
      if (!c || !c.doc || c.doc.kind !== "session") return null;
      var fsid = c.doc.ref;
      var inst = NV_SESSION_INSTANCES[fsid] || {};
      var wid = (c.resolveSessionWid && c.resolveSessionWid(fsid))
        || inst.wid || c.wid;
      function refetch() {
        var live2 = NV_SESSION_INSTANCES[fsid];
        // Best-effort: the focused instance's own poll already lands
        // this within one cycle, this just skips the wait when the
        // instance is mounted and its resource handles are reachable.
        if (live2 && typeof live2.refetchAll === "function") live2.refetchAll();
      }
      return { sid: fsid, wid: wid, con: c, refetchAll: refetch };
    }
    // contexts: ["session"] hard-gates on docKind; requiresLive mirrors
    // nv-sessions-sidebar.jsx's NV_SessionContextMenu, the one place this
    // business rule already existed (Interrupt/Park/Close hide once a
    // session has ended, Rename/Split Right/Compact/Rewind do not).
    reg({
      id: "session.interrupt", label: "Interrupt Session",
      contexts: ["session"], requiresLive: true,
      surfaces: ["palette", "tab-menu"],
      run: function () {
        var f = focused(); if (!f) return;
        NV_doInterrupt(f.wid, f.sid, f.refetchAll, f.con.toast);
      },
    });
    reg({
      id: "session.park", label: "Park Session",
      contexts: ["session"], requiresLive: true,
      surfaces: ["palette", "tab-menu"],
      run: function () {
        var f = focused(); if (!f) return;
        NV_doPark(f.wid, f.sid, f.refetchAll, f.con.toast);
      },
    });
    reg({
      id: "session.resume", label: "Resume Session",
      contexts: ["session"],
      surfaces: ["palette", "tab-menu"],
      run: function () { NV_doResume(); },
    });
    reg({
      id: "session.close", label: "Close Session",
      contexts: ["session"], requiresLive: true,
      surfaces: ["palette", "tab-menu"],
      run: function () {
        var f = focused(); if (!f) return;
        NV_doClose(f.wid, f.sid, f.refetchAll, f.con.toast);
      },
    });
    reg({
      id: "session.rename", label: "Rename Session",
      contexts: ["session"],
      surfaces: ["palette", "tab-menu"],
      run: function () {
        var f = focused(); if (!f) return;
        var inst = NV_SESSION_INSTANCES[f.sid] || {};
        var name = inst.session && inst.session.name;
        NV_doRename(f.wid, f.sid, name, f.refetchAll, f.con.toast);
      },
    });
    reg({
      id: "session.splitRight", label: "Split Right",
      contexts: ["session"],
      surfaces: ["palette", "tab-menu"],
      run: function () {
        var f = focused(); if (!f) return;
        NV_doSplitRight(f.con, f.sid);
      },
    });
    reg({
      id: "session.compact", label: "Compact Session",
      contexts: ["session"],
      surfaces: ["palette", "tab-menu"],
      run: function () {
        var f = focused(); if (!f) return;
        NV_doCompact(f.wid, f.sid, f.refetchAll, f.con.toast);
      },
    });
    reg({
      id: "session.rewind", label: "Rewind Session",
      contexts: ["session"],
      surfaces: ["palette", "tab-menu"],
      run: function () {
        var f = focused(); if (!f) return;
        var inst = NV_SESSION_INSTANCES[f.sid] || {};
        if (typeof inst.openRewind === "function") inst.openRewind();
      },
    });
  }, [con.registry]);

  React.useEffect(function () { if (live) setOptimistic(null); }, [!!live]);
  // Bounded fallback: a reopen whose turn dies before the tap shows
  // life would otherwise hold "sending" (and the lifted poll stop)
  // forever. Once the polled row confirms the session settled back to
  // ended with no live turn, drop the optimistic flag.
  React.useEffect(function () {
    if (!optimistic || !session) return;
    if (NV_sessionIsOver(session)
        && session.turn_status === "idle"
        && Date.now() - optimistic > 10_000) {
      setOptimistic(null);
    }
  }, [detail.data]);
  // C4 three-source merge: the store's status channel already carries
  // the tap-derived live status and the optimistic "sending" leg; the
  // polled session row's turn_status is layered on below (rowBusy).
  var shown = statusSnap || (optimistic
    ? { verb: "sending", object: "", startedMs: optimistic }
    : null);
  // A reload mid-run has no tap history and no optimistic flag, but
  // the polled session row knows a turn is executing - without this
  // the busy indicator vanished on refresh (live finding 2026-08-26).
  var busySinceRef = React.useRef(null);
  var rowBusy = !!(session && !NV_sessionIsOver(session)
    && session.turn_status && session.turn_status !== "idle");
  if (rowBusy && busySinceRef.current == null) {
    busySinceRef.current = Date.now();
  }
  if (!rowBusy) busySinceRef.current = null;
  if (!shown && rowBusy) {
    shown = {
      verb: String(session.turn_status), object: "",
      startedMs: busySinceRef.current,
    };
  }
  var degraded = !!(gatesSnap && gatesSnap.degraded);
  var records = store.recordsBySeq;
  var shownActive = !!shown;
  var pipeline = React.useMemo(function () {
    var flat = SH_nestSubagentRows(
      window.SA_toTranscript(records, session));
    // Live text/reasoning parts stream into the transcript BEFORE their
    // durable record lands (A4: the record supersedes the part when it
    // arrives and the part goes final). Without this branch the delta
    // stream painted tool args only - partial answer text sat in
    // store.parts invisibly. Not gated on an active turn: a non-final
    // part IS the activity signal (frames only flow mid-turn).
    if (store && store.parts) {
      var liveSeq = flat.length ? flat[flat.length - 1].seq : 0;
      for (var lpId in store.parts) {
        if (!Object.prototype.hasOwnProperty.call(store.parts, lpId)) continue;
        var lp = store.parts[lpId];
        if (lp.final || !lp.text) continue;
        if (lp.kind !== "text" && lp.kind !== "reasoning") continue;
        liveSeq += 1;
        flat.push({
          seq: liveSeq,
          kind: lp.kind === "reasoning" ? "reasoning" : "assistant_message",
          nodeId: null,
          label: lp.text,
          payload: { streaming: true, part_id: lpId },
          createdAt: null,
        });
      }
    }
    var liveFromSeq = Infinity;
    if (shownActive) {
      for (var i = flat.length - 1; i >= 0; i--) {
        if (flat[i].kind === "user_message") {
          liveFromSeq = flat[i].seq;
          break;
        }
      }
    }
    var rows = SH_collapseTurns(flat, { liveFromSeq: liveFromSeq });

    // tool_result rows render INSIDE their call's block, paired by call
    // id, never as standalone lines (they have no label and drew as
    // empty chevron rows).
    var resultsByCallId = {};
    for (var ri = 0; ri < flat.length; ri++) {
      if (flat[ri].kind === "tool_result") {
        var cid = (flat[ri].payload || {}).call_id;
        if (cid != null) resultsByCallId[cid] = flat[ri];
      }
    }

    // 0-based turn ordinal per seq, matching the timeline endpoint's
    // terminal-counting contract (get_session_turn_timeline: turn_no is
    // the ordinal produced by counting done/cancelled/error terminals).
    // The old hardcoded fallback of 1 asked for the SECOND turn's trace
    // from every row of a first-turn session, which is why the split
    // came up empty (BDD/live finding 2026-08-25).
    var turnOfSeq = {};
    (function () {
      var ordinal = 0;
      for (var ti = 0; ti < flat.length; ti++) {
        turnOfSeq[flat[ti].seq] = ordinal;
        if (flat[ti].kind === "done" || flat[ti].kind === "cancelled"
            || flat[ti].kind === "error") ordinal += 1;
      }
    })();
    return {
      flat: flat, rows: rows,
      resultsByCallId: resultsByCallId, turnOfSeq: turnOfSeq,
    };
  }, [transcriptSnap, session, shownActive]);
  var flat = pipeline.flat;
  var rows = pipeline.rows;
  var resultsByCallId = pipeline.resultsByCallId;
  var turnOfSeq = pipeline.turnOfSeq;
  function resultFor(row) {
    var id = (row.payload || {}).id
      || (row.payload || {}).tool_call_id || null;
    return id != null ? resultsByCallId[id] || null : null;
  }
  function traceTurnFor(row) {
    if (row.turn_no != null) return row.turn_no;
    var t = turnOfSeq[row.seq];
    return t == null ? 0 : t;
  }

  // US-008 R3 item 1: live tool-call ARGUMENTS as they stream in, before
  // the call's durable record even exists.
  //
  // CONTRACT NOTE: two DIFFERENT part_id schemes, not one. Text and
  // reasoning deltas key by `part_id(node_id, kind)` - node+kind scoped
  // (primer/session/persistence.py:402 and :413, using the helper at
  // primer/tap/delta.py::part_id). Tool deltas do NOT go through that
  // helper at all - primer/session/persistence.py:434 passes
  // `event.id` (the tool call id itself) straight through as the part
  // id, because the paired TOOL_CALL durable record carries that same
  // id and the client reconciles the live arguments to it by it.
  // Either way, translate_stream_event coalesces a whole ToolCallDelta
  // series into ONE durable record per call, so the row for a tool_call
  // does not exist in `flat`/`rows` at all until that record lands.
  // There is nothing to "enhance" while streaming; the live preview has
  // to be synthesized from the store's parts directly and then hand off
  // to the real NV_ToolBlock once the durable record appears (below, in
  // the transcript body).
  //
  // Scanning store.parts for kind === "tool" (rather than trying to
  // derive a part_id from a row) also naturally covers more than one
  // concurrent forming call - a fan-out graph turn has one part per
  // running node, each independently keyed.
  var liveToolTickState = React.useState(0);
  var setLiveToolTick = liveToolTickState[1];
  var liveToolStartRef = React.useRef({});
  var liveToolParts = [];
  if (shownActive && store && store.parts) {
    for (var ltPid in store.parts) {
      if (!Object.prototype.hasOwnProperty.call(store.parts, ltPid)) continue;
      var ltPart = store.parts[ltPid];
      if (ltPart.kind !== "tool" || ltPart.final || !ltPart.text) continue;
      if (!liveToolStartRef.current[ltPid]) {
        liveToolStartRef.current[ltPid] = Date.now();
      }
      liveToolParts.push({
        partId: ltPid,
        text: ltPart.text,
        elapsedS: Math.max(0, Math.floor(
          (Date.now() - liveToolStartRef.current[ltPid]) / 1000)),
      });
    }
  }
  React.useEffect(function () {
    if (!liveToolParts.length) return;
    var t = setInterval(function () {
      setLiveToolTick(function (n) { return n + 1; });
    }, 1000);
    return function () { clearInterval(t); };
  }, [liveToolParts.length]);

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
  var seenRef = React.useRef(0);
  var distanceRef = React.useRef(0);
  var followRef = React.useRef(true);
  var followState = React.useState(true);
  var follow = followState[0];
  var setFollow = followState[1];
  function onScroll() {
    var el = scrollRef.current;
    if (!el) return;
    var d = el.scrollHeight - el.scrollTop - el.clientHeight;
    distanceRef.current = d;
    var isFollow = d <= SH_FOLLOW_PX;
    if (isFollow !== followRef.current) {
      followRef.current = isFollow;
      setFollow(isFollow);
      if (isFollow) seenRef.current = rows.length;
    }
  }
  function jumpLatest() {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
    seenRef.current = rows.length;
  }
  var newTurns = Math.max(0, rows.length - seenRef.current);
  var decision = SH_scrollDecision({
    distanceFromBottom: distanceRef.current,
    newTurns: newTurns,
  });
  React.useEffect(function () {
    if (follow) jumpLatest();
  }, [rows.length, follow]);

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
              if (child.kind === "tool_call") {
                return (
                  <NV_ToolBlock key={child.seq} row={child}
                    result={resultFor(child)} running={shownActive} />
                );
              }
              if (child.kind === "reasoning") {
                return <NV_Thought key={child.seq} row={child} />;
              }
              // Results render inside their call's block; a bare label
              // row here would be an empty chevron line.
              if (child.kind === "tool_result") return null;
              if (!child.label) return null;
              return (
                <div key={child.seq} className="nv-turn-section-line">
                  <svg width="8" height="8" viewBox="0 0 10 10" fill="none"
                    stroke="currentColor" strokeWidth="1.4">
                    <path d="M3.5 2 6.5 5 3.5 8" />
                  </svg>
                  {child.label}
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
    // Model thinking: collapsed + muted by design, never dressed as an
    // answer. The toggle opens the full thought in place.
    if (row.kind === "reasoning") {
      return <NV_Thought key={row.seq} row={row} />;
    }
    // Tool traffic in the LIVE turn: same expandable block as folded
    // sections use, so a call reads identically mid-run and after.
    if (row.kind === "tool_call") {
      return (
        <NV_ToolBlock key={row.seq} row={row} result={resultFor(row)} running={shownActive} />
      );
    }
    if (row.kind === "tool_result") return null;
    // Lifecycle markers: a slim muted line, not a full agent block. The
    // done line carries the turn's trace affordance (it IS the turn
    // boundary). Before this branch every one of these rendered as an
    // EMPTY "operator" block stacking under the answer (live finding
    // 2026-08-25).
    if (row.kind === "done" || row.kind === "yielded"
        || row.kind === "resumed" || row.kind === "cancelled") {
      return (
        <div key={row.seq} className="nv-lifecycle"
          data-kind={row.kind} data-testid={"nv-turn:" + row.seq}>
          <span className="nv-lifecycle-dot">
            {row.kind === "cancelled" ? "■ cancelled" : "· " + row.kind}
          </span>
          {row.kind === "done" || row.kind === "cancelled" ? (
            <button type="button" className="nv-trace-toggle"
              data-testid={"nv-trace-open:" + row.seq}
              onClick={function () { setTraceTurn(traceTurnFor(row)); }}>
              trace
            </button>
          ) : null}
        </div>
      );
    }
    if (row.kind === "error") {
      var msg = row.label
        || (row.payload && (row.payload.message || row.payload.code))
        || "turn failed";
      return (
        <div key={row.seq} className="nv-turn-error"
          data-testid={"nv-turn:" + row.seq}>
          <span>{msg}</span>
          <span style={{ flex: 1 }} />
          <button type="button" className="nv-trace-toggle"
            data-testid={"nv-trace-open:" + row.seq}
            onClick={function () { setTraceTurn(traceTurnFor(row)); }}>
            trace
          </button>
        </div>
      );
    }
    // A lifecycle row with nothing to say renders nothing at all.
    if (row.kind === "lifecycle" && !row.label) return null;
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
            {row.seq != null ? (
              <button type="button" className="nv-trace-toggle"
                data-testid={"nv-trace-open:" + row.seq}
                onClick={function () {
                  setTraceTurn(traceTurnFor(row));
                }}>trace</button>
            ) : null}
          </div>
          <div className="nv-turn-text md-body">
            {row.kind === "assistant_message"
              && typeof window.renderMarkdown === "function"
              ? window.renderMarkdown(
                String(row.label || "").replace(/^\s+/, ""))
              : row.label}
          </div>
          {(row.children || []).map(function (child) {
            if (child.kind === "tool_call") {
              return (
                <div key={child.seq} className="nv-subagent">
                  <div className="nv-subagent-head">
                    <span className="nv-subagent-name">
                      {(child.payload && child.payload.agent_id)
                        || "subagent"}
                    </span>
                  </div>
                  <NV_ToolBlock row={child} result={resultFor(child)} running={shownActive} />
                </div>
              );
            }
            if (!child.label) return null;
            return (
              <div key={child.seq} className="nv-subagent">
                <div className="nv-subagent-head">
                  <span className="nv-subagent-name">
                    {(child.payload && child.payload.agent_id) || "subagent"}
                  </span>
                </div>
                <div className="nv-turn-text">{child.label}</div>
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
        onExport={exportTranscript}
        onOpenRewind={function () { setRewindOpen(true); }}
        onCompact={function () {
          NV_doCompact(con.wid, sid, refetchAll, con.toast);
        }} />
      {rewindOpen ? (
        <NV_RewindPicker
          candidates={NV_rewindCandidates(records)}
          onClose={function () { setRewindOpen(false); }}
          onConfirm={function (toSeq) {
            setRewindOpen(false);
            NV_doRewind(con.wid, sid, toSeq, refetchAll, con.toast);
          }} />
      ) : null}
      {nodeFilter ? (
        <div className="nv-node-filter" data-testid="graph-node-filter">
          <span>Showing <span className="nv-mono">{nodeFilter}</span> only</span>
          <span style={{ flex: 1 }} />
          <button type="button" className="nv-btn-secondary"
            data-testid="graph-node-filter-clear"
            title="Show the full transcript (all nodes)"
            onClick={function () { setNodeFilter(null); }}>All nodes</button>
        </div>
      ) : null}
      <div className="nv-transcript-split">
        <div className="nv-transcript" ref={scrollRef}
          data-testid="nv-transcript"
          onScroll={onScroll}>
          <div className="nv-transcript-inner">
            {NV_scopeToNode(rows, nodeFilter).map(function (r) {
              return renderTurn(r, 0);
            })}
            {liveToolParts.map(function (ltp) {
              var parsedLive = typeof window.parsePartialJson === "function"
                ? window.parsePartialJson(ltp.text)
                : { value: undefined, state: "failed" };
              var shownText = parsedLive.value !== undefined
                ? JSON.stringify(parsedLive.value, null, 2)
                : ltp.text;
              return (
                <div key={ltp.partId} className="nv-toolblock nv-toolblock-live"
                  data-testid={"nv-tool-live:" + ltp.partId} data-open="true">
                  <div className="nv-toolblock-head">
                    <span className="nv-thought-mark">▾</span>
                    <span className="nv-chip-icon">{NV_CHIP_ICONS.other}</span>
                    <span className="nv-toolblock-label">forming a tool call…</span>
                    <span className="nv-toolblock-streaming">streaming</span>
                    <span className="nv-toolblock-elapsed">{ltp.elapsedS}s</span>
                  </div>
                  <div className="nv-toolblock-body">
                    <div className="nv-toolblock-sec">arguments</div>
                    <pre className="nv-toolblock-pre nv-toolblock-pre-live">
                      {shownText}
                    </pre>
                  </div>
                </div>
              );
            })}
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
        statusShown={shown}
        degraded={degraded}
        waitNote={session && session.parked_status
          ? "parked — waiting on " + (session.waiting_reason || "a wake")
          : null}
        terminal={NV_sessionIsOver(session)}
        micEnabled={!!(con.speech && con.speech.stt_configured)}
        onInterrupt={function () {
          NV_doInterrupt(con.wid, sid, refetchAll, con.toast);
        }}
        onSend={function (text, clientId) {
          return window.SS_sendUserMessage(store, text, clientId);
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
                onNodeSelect={function (id) {
                  setNodeFilter(id);
                  if (id) setGraphView(false);
                }}
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
window.NV_Thought = NV_Thought;
window.NV_ToolBlock = NV_ToolBlock;
