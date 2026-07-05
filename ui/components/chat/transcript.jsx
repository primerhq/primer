/* global React, Icon */
//
// <Transcript> — the pure single-column agent-timeline renderer (Task
// B3 of the chat-refactor plan). Consumes an already-coalesced message
// list (window.chatCoalesce(messages), called by <Conversation> before
// handing off) and renders it — no data fetching, no WebSocket, props
// only. <Conversation> (ui/components/chat/conversation.jsx) owns the
// WS/data lifecycle and mounts this component for the scrollable
// message area.
//
// The row renderers that used to live inline in ChatDetail
// (ui/components/chats.jsx) move here wholesale: Message,
// CT_ExpandableToolRow, CT_AttachmentPart, CompactionMarker,
// CT_ThinkingBubble — plus Message's two private text/role helpers,
// CT_roleForKind and CT_textOf. Behavior is byte-identical to the
// inline versions; only the file (and, for the two helpers, adjacent
// grouping) changed.
//
// chatId / agentId are accepted so the prop surface stays stable for
// future consumers; onRewind + compactionBoundarySeq (Task F3, R4) drive
// CT_RewindButton — see that component below for the compaction-guard
// gating and the confirm -> POST /rewind flow.
// pendingToolCall + sendMessage (Task C4) drive CT_ApprovalGate — see
// that component below for why a still-open approval-mode gate is
// resolved through the SAME sendMessage <Conversation> uses for composer
// sends, not a new endpoint.

// Map a chat_messages row kind → simple bubble role for layout.
function CT_roleForKind(kind) {
  if (kind === "user_message") return "user";
  if (kind === "assistant_token" || kind === "agent_message") return "agent";
  return kind;
}

// Pull the "text" out of a chat_messages payload across the kinds we
// render as a bubble. The server keeps the per-kind schema loose
// (everything other than seq/kind is spread from `payload`), so we
// check the common shapes.
function CT_textOf(m) {
  if (typeof m.text === "string") return m.text;
  if (typeof m.content === "string") return m.content;
  if (typeof m.delta === "string") return m.delta;
  if (typeof m.message === "string") return m.message;
  return "";
}

// Format a row's `created_at` (ISO 8601, from the persisted ChatMessage —
// REST rows carry it directly; see window.chatFlatten) into a short
// local-time label for the per-message timestamp (Task C1 / §4.1).
// Returns "" for a missing/unparsable value (e.g. a live WS frame, whose
// `_message_to_wire` envelope doesn't include `created_at`) so callers
// can conditionally render without flashing "Invalid Date".
function CT_formatTime(createdAt) {
  if (!createdAt) return "";
  const d = new Date(createdAt);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// Duration between a tool_call and its paired tool_result, in ms — used
// for the collapsed chip's "✓ 2.1s" badge (Task C3). Both rows need a
// `created_at` to compute this; a live WS frame doesn't carry one (see
// CT_formatTime above), so a still-fresh pairing renders without a
// duration until the REST reload backfills it — same graceful-degrade
// precedent as the timestamp.
function CT_toolDuration(call, result) {
  if (!call || !result) return null;
  const start = call.created_at ? new Date(call.created_at).getTime() : NaN;
  const end = result.created_at ? new Date(result.created_at).getTime() : NaN;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  const ms = end - start;
  return ms >= 0 ? ms : null;
}

function CT_formatDuration(ms) {
  if (ms == null || !Number.isFinite(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

// Pick a short "key argument" to surface next to the tool name in the
// collapsed chip (Task C3) — `bash(ls -la)` reads far better than
// `bash({"command":"ls -la"})`. A single-arg call just shows that
// value; a multi-arg call prefers a handful of common single-purpose
// keys (command/path/query/…); anything else falls back to compact JSON.
const _TOOL_KEY_ARG_PRIORITY = ["command", "cmd", "path", "file", "filename", "query", "url", "name"];
function CT_keyArgPreview(args) {
  if (!args || typeof args !== "object") return "";
  const keys = Object.keys(args);
  if (keys.length === 0) return "";
  const pick = keys.length === 1
    ? keys[0]
    : (_TOOL_KEY_ARG_PRIORITY.find((k) => k in args) || keys[0]);
  const v = args[pick];
  const s = typeof v === "string" ? v : (() => { try { return JSON.stringify(v); } catch { return ""; } })();
  return (s || "").replace(/\s+/g, " ");
}

// ============================================================================
// Helpers: thinking indicator + attachment part
// ============================================================================
//
// Assistant-token coalescing lives in ui/components/chat/use-transcript.js
// (window.chatCoalesce) as of Task B2 — shared with <Conversation> and
// unit-testable on its own.

// Subtle "Thinking…" placeholder shown after the user sends a frame
// but before the first assistant_token / tool_call / done row lands.
// Same horizontal layout as a real agent bubble so it doesn't shift
// when the first delta arrives.
//
// `label` (Task C2) lets the caller swap the generic animated
// "Thinking…" text for a tool-labeled live state ("running <tool>…")
// when the last row is a still-running tool_call — same bubble so the
// layout doesn't jump when the label changes.
function CT_ThinkingBubble({ label }) {
  return (
    <div style={{ display: "flex", gap: 12, marginBottom: 14 }} aria-live="polite">
      <div style={{
        width: 48, flexShrink: 0,
        fontFamily: "IBM Plex Mono, monospace",
        fontSize: 10.5,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: "var(--accent)",
        fontWeight: 600,
        paddingTop: 2,
      }}>agent</div>
      <div style={{
        flex: 1,
        fontSize: 13,
        lineHeight: 1.55,
        color: "var(--text-2)",
        borderLeft: "2px solid var(--accent)",
        paddingLeft: 12,
        fontStyle: "italic",
      }}>
        {label ? label : (
          <>
            Thinking
            <span className="thinking-dots" style={{ marginLeft: 2 }}>
              <span>.</span><span>.</span><span>.</span>
            </span>
          </>
        )}
      </div>
    </div>
  );
}

// ============================================================================
// CT_ApprovalGate — inline Approve/Deny under a gating assistant message
// (Task C4)
// ============================================================================
//
// A chat-surface approval gate (primer/chat/executor.py::soft_yield,
// tool_name == "_approval") never appends a `tool_call` row — it appends an
// assistant_token prompt ("I'd like to run `X`... Approve? (yes/no)") and
// stamps `chat.pending_tool_call = {mode: "approval", ...}` on the chat row
// itself. The gate blocks further turn activity, so that prompt is
// guaranteed to be the LAST row in the timeline for as long as
// `pendingToolCall.mode === "approval"` holds — rendering this right after
// the message list is exactly "under the gating assistant message".
//
// No protocol change: Approve/Deny are plain `user_message` sends of the
// literal tokens `"yes"`/`"no"` through the SAME `sendMessage` <Conversation>
// already uses for composer sends — `primer/chat/executor.py::resume_pending`
// parses those tokens on the next turn. Disables both buttons for the
// duration of a single decision so a double-click can't fire twice; the gate
// disappearing (pendingToolCall polled back to null in <Conversation>) is
// what actually clears it, per the plan's "vanish once the gate clears".
function CT_ApprovalGate({ sendMessage }) {
  const [sending, setSending] = React.useState(false);
  const decide = (text) => {
    if (sending) return;
    setSending(true);
    const enqueued = typeof sendMessage === "function" ? sendMessage(text) : false;
    if (!enqueued) setSending(false); // send failed (WS not open) — let the operator retry
  };
  return (
    <div style={{ marginLeft: 60, marginTop: 2, marginBottom: 14, display: "flex", gap: 8 }}>
      <button
        type="button"
        className="btn"
        data-testid="chat-gate-deny"
        disabled={sending}
        onClick={() => decide("no")}
      >
        Deny
      </button>
      <button
        type="button"
        className="btn btn-primary"
        data-testid="chat-gate-approve"
        disabled={sending}
        onClick={() => decide("yes")}
      >
        Approve
      </button>
    </div>
  );
}

// ============================================================================
// CT_ExpandableToolRow — collapsed-by-default tool_call / tool_result row
// ============================================================================
//
// Tool outputs (HTTP bodies, file contents, large JSON) easily exceed the
// chat width and pollute the visible flow. We render a one-line summary by
// default with a chevron toggle. When expanded, the full payload appears
// in a monospace block with internal scroll capped to a sensible height
// so the chat keeps its rhythm.
//
// PREVIEW_CHARS chosen so the inline summary fits one line in a typical
// chat column without the truncation creating visual confusion.

const _TOOL_PREVIEW_CHARS = 80;

function CT_ExpandableToolRow({
  icon, iconColor, borderColor,
  name, separator, previewText, fullText, endBadge,
  defaultOpen,
}) {
  // Task C3: initial open state is caller-driven — a still-running tool
  // call auto-expands (defaultOpen=true); a completed one starts
  // collapsed to its result chip (defaultOpen=false, the useState
  // default). The row that calls this with defaultOpen also keys the
  // element on run/done state (see Transcript below) so a running ->
  // paired transition remounts fresh instead of staying stuck open.
  const [open, setOpen] = React.useState(!!defaultOpen);
  const preview = (previewText || "").replace(/\s+/g, " ");
  const truncated = preview.length > _TOOL_PREVIEW_CHARS;
  const previewShown = truncated
    ? preview.slice(0, _TOOL_PREVIEW_CHARS) + "…"
    : preview;
  const hasExpand = (fullText || "").length > _TOOL_PREVIEW_CHARS;
  const toggle = () => { if (hasExpand) setOpen((o) => !o); };
  return (
    <div style={{ marginLeft: 60, marginTop: 2, marginBottom: 6 }}>
      <div
        className="tool-call"
        style={{
          borderLeft: `2px solid ${borderColor}`,
          cursor: hasExpand ? "pointer" : "default",
          userSelect: "none",
        }}
        onClick={toggle}
        role={hasExpand ? "button" : undefined}
        tabIndex={hasExpand ? 0 : undefined}
        onKeyDown={(e) => {
          if (!hasExpand) return;
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
        }}
      >
        {hasExpand && (
          <Icon
            name={open ? "chevron-down" : "chevron-right"}
            size={10}
            style={{ color: "var(--text-3)" }}
          />
        )}
        <Icon name={icon} size={10} style={{ color: iconColor }} />
        <span className="name">{name}</span>
        <span className="arrow">{separator}</span>
        <span className="muted" style={{
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
          flex: 1,
          minWidth: 0,
        }}>{previewShown}</span>
        {endBadge && <span style={{ marginLeft: "auto" }}>{endBadge}</span>}
      </div>
      {open && (
        <pre style={{
          marginTop: 6,
          padding: "10px 12px",
          background: "var(--bg-0)",
          border: "1px solid var(--border)",
          borderRadius: 6,
          fontSize: 11.5,
          lineHeight: 1.5,
          fontFamily: "IBM Plex Mono, monospace",
          color: "var(--text-2)",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          maxHeight: 360,
          overflow: "auto",
        }}>
          {/* Task C3: schema-light highlighted payload (B1's vendored
              highlighter) instead of a bare wall of JSON text — falls back
              to the raw string if the vendor script hasn't loaded. */}
          {window.primerVendor && window.primerVendor.highlightCode
            ? <code dangerouslySetInnerHTML={{ __html: window.primerVendor.highlightCode(fullText || "", "json") }} />
            : (fullText || "")}
        </pre>
      )}
    </div>
  );
}


// ============================================================================
// CT_Attribution — per-message agent/user label + timestamp (Task C1)
// ============================================================================
//
// The left-rail label used to be a hardcoded `isUser ? "user" : "agent"`
// literal — every agent reply read as generically "agent" regardless of
// which one produced it. This renders the real producing agent id
// (`m.agent_id`, stamped by the backend on every non-user row — Task A4)
// for agent rows, "user" for the user's own rows, plus the row's
// `created_at` underneath when one is available.

function CT_Attribution({ label, isUser, time }) {
  return (
    <div style={{
      width: 48, flexShrink: 0,
      fontFamily: "IBM Plex Mono, monospace",
      fontSize: 10.5,
      textTransform: "uppercase",
      letterSpacing: "0.06em",
      color: isUser ? "var(--text-2)" : "var(--accent)",
      fontWeight: 600,
      paddingTop: 2,
    }}>
      <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {label}
      </div>
      {time && (
        <div style={{
          fontWeight: 400,
          textTransform: "none",
          letterSpacing: "normal",
          color: "var(--text-3)",
          fontSize: 9.5,
          marginTop: 2,
        }}>{time}</div>
      )}
    </div>
  );
}

// ============================================================================
// CT_MarkdownBody — assistant markdown body with a collapsible wrapper for
// long sections (Task E1 / D6)
// ============================================================================
//
// A long assistant reply (multi-paragraph analysis, a large embedded code
// dump rendered by ui/vendor/markdown.jsx's fenced-block branch) otherwise
// always renders in full, pushing everything above it out of view. Past a
// length threshold this wraps the rendered markdown in a native
// `<details>` — OPEN by default (nothing that was visible before now
// hides) so it's purely an opt-in "collapse this" affordance, not a
// de-facto truncation. Short replies (the common case) skip the wrapper
// entirely and render exactly as before.
const _LONG_SECTION_CHARS = 1200;

function CT_MarkdownBody({ text }) {
  const body = typeof window.renderMarkdown === "function"
    ? window.renderMarkdown(text)
    : <div style={{ whiteSpace: "pre-wrap" }}>{text}</div>;
  const isLong = typeof text === "string" && text.length > _LONG_SECTION_CHARS;
  if (!isLong) return body;
  return (
    <details className="md-collapsible" open>
      <summary style={{ cursor: "pointer", fontSize: 11, color: "var(--text-3)", marginBottom: 6 }}>
        Long response ({text.length.toLocaleString()} chars) — click to collapse
      </summary>
      {body}
    </details>
  );
}

// ============================================================================
// CT_RewindButton — rewind-to-here affordance on a user message (Task F3
// / R4)
// ============================================================================
//
// Rendered by Message (below) only for a user_message that is (a) not
// still-optimistic (a pending echo has no persisted `seq` yet — nothing
// to rewind to) and (b) strictly AFTER the compaction boundary
// <Conversation> computed and passed down as `compactionBoundarySeq` —
// rewinding to (or behind) the latest `compaction_marker` would desync
// that marker's summary from the history it replaced, so the affordance
// simply doesn't appear there (mirrors backend A7's 422 for the same
// case, without letting the operator click into a guaranteed error).
//
// `disabled` (turn_status === "running") mirrors A7's 409-while-running
// guard — greyed out rather than hidden so the operator can see the
// message is a valid future rewind target, just not mid-turn.
//
// No new endpoint: clicking confirms (window.confirm, same pattern as
// triggers.jsx / studio-center.jsx's destructive-action confirms) then
// hands the row's `seq` up to `onRewind` — <Conversation> owns the
// actual `POST /chats/{id}/rewind` call + local truncation.
function CT_RewindButton({ seq, disabled, onRewind }) {
  const handleClick = () => {
    if (disabled || typeof onRewind !== "function") return;
    const ok = window.confirm(
      "Rewind to this message? Everything sent or received after it will be discarded."
    );
    if (!ok) return;
    onRewind(seq);
  };
  return (
    <button
      type="button"
      className="chat-rewind-btn"
      data-testid="chat-rewind-btn"
      title={disabled ? "Cannot rewind while a turn is running" : "Rewind to this message"}
      aria-label="Rewind to this message"
      disabled={!!disabled}
      onClick={handleClick}
      style={{
        marginLeft: "auto",
        flexShrink: 0,
        alignSelf: "flex-start",
        padding: 2,
        border: "none",
        background: "none",
        color: "var(--text-3)",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.35 : 0.55,
      }}
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
        <path d="M11 19L2 12l9-7v14z" />
        <path d="M22 19l-9-7 9-7v14z" />
      </svg>
    </button>
  );
}

// ============================================================================
// Message — one row in the conversation
// ============================================================================

function Message({ m, pairedResult, chatId, onRewind, rewindDisabled, compactionBoundarySeq }) {
  const kind = m.kind;

  if (kind === "tool_call") {
    const name = m.name || m.tool_name || "tool";
    const args = m.args || m.arguments || {};
    const argsFull = (() => { try { return JSON.stringify(args, null, 2); } catch { return ""; } })();
    const argsPreview = (() => { try { return JSON.stringify(args); } catch { return ""; } })();

    // Task C3 / D4: hybrid rendering. While unpaired (no tool_result with
    // a matching `id` has landed yet) the call is still running — render
    // it expanded so the args are visible live. Once Transcript pairs it
    // with its tool_result, collapse to a one-line result chip
    // (`name(key-arg) ✓ 2.1s`) that re-expands on click.
    if (pairedResult) {
      const isError = !!pairedResult.error;
      const resultFull = typeof pairedResult.result === "string"
        ? pairedResult.result
        : (pairedResult.result != null ? JSON.stringify(pairedResult.result, null, 2) : "");
      const combinedFull = `// args\n${argsFull}\n\n// result\n${resultFull}`;
      const durationLabel = CT_formatDuration(CT_toolDuration(m, pairedResult));
      const keyArg = CT_keyArgPreview(args);
      return (
        <>
          <CT_ExpandableToolRow
            icon={isError ? "x-circle" : "check"}
            iconColor={isError ? "var(--red)" : "var(--green)"}
            borderColor={isError ? "var(--red)" : "var(--border)"}
            name={name}
            separator="("
            previewText={`${keyArg})`}
            fullText={combinedFull}
            defaultOpen={false}
            endBadge={
              <span className={isError ? "fail" : "ok"}>
                {isError ? "✗" : "✓"}{durationLabel ? ` ${durationLabel}` : ""}
              </span>
            }
          />
          {/* Task E1: a tool-produced media part (payload.media, flattened
              onto the row — see primer/chat/executor.py::_tool_media_parts)
              renders inline right under the collapsed result chip. */}
          <CT_ToolMedia media={pairedResult.media} chatId={chatId} />
        </>
      );
    }

    return (
      <CT_ExpandableToolRow
        icon="play"
        iconColor={m.pending_approval ? "var(--amber)" : "var(--text-3)"}
        borderColor="var(--border)"
        name={name}
        separator="("
        previewText={argsPreview}
        fullText={argsFull}
        defaultOpen={true}
        endBadge={m.pending_approval ? (
          <span className="pill pill-paused"><span className="dot"></span>awaiting approval</span>
        ) : null}
      />
    );
  }

  if (kind === "tool_result") {
    // Fallback-only path (Task C3): a tool_result whose matching
    // tool_call is present in the same loaded window folds into that
    // call's row instead (see Transcript below) and never reaches this
    // branch. This renders standalone only when the pairing call has
    // scrolled out of the loaded history — same look as before C3.
    const name = m.name || m.tool_name || "tool";
    const isError = !!m.error;
    const fullStr = typeof m.result === "string"
      ? m.result
      : (m.result != null ? JSON.stringify(m.result, null, 2) : "");
    const previewStr = typeof m.result === "string"
      ? m.result
      : (m.result != null ? JSON.stringify(m.result) : "");
    return (
      <>
        <CT_ExpandableToolRow
          icon={isError ? "x-circle" : "check"}
          iconColor={isError ? "var(--red)" : "var(--green)"}
          borderColor={isError ? "var(--red)" : "var(--green)"}
          name={name}
          separator="→"
          previewText={previewStr}
          fullText={fullStr}
        />
        <CT_ToolMedia media={m.media} chatId={chatId} />
      </>
    );
  }

  if (kind === "error") {
    return (
      <div style={{ marginLeft: 60, marginTop: 6, marginBottom: 6 }}>
        <div className="banner banner-error" style={{ margin: 0, fontSize: 12 }}>
          <Icon name="x-circle" size={12} className="ico" />
          <div>{CT_textOf(m) || "error"}</div>
        </div>
      </div>
    );
  }

  if (kind === "yielded" || kind === "resumed" || kind === "done") {
    return (
      <div style={{ marginLeft: 60, marginTop: 4, marginBottom: 4 }}>
        <span className="muted text-sm mono">· {kind}</span>
      </div>
    );
  }

  // First-class marker row for a Stop (Task A6) — promoted out of the
  // generic "· kind" dot above so a cancelled turn reads as a distinct
  // timeline event rather than blending in with the terminal-status dots.
  if (kind === "cancelled") {
    return (
      <div style={{ marginLeft: 60, marginTop: 6, marginBottom: 6 }}>
        <span className="muted text-sm mono" style={{ color: "var(--red)" }}>
          ■ cancelled
        </span>
      </div>
    );
  }

  // First-class marker row for an attribution boundary (Task A5): the
  // agent handling the chat changed. `marker` distinguishes an operator-
  // driven switch (POST /chats/{id}/agent) from a tool-driven handoff
  // (switch_to_agent) from an initial join; `agent_id` is the row's
  // incoming/new agent (stamped per Task A4).
  if (kind === "agent_marker") {
    const agent = m.agent_id || "agent";
    const marker = m.marker;
    const label = marker === "handoff"
      ? `⇄ handoff → ${agent}`
      : marker === "joined"
        ? `▶ ${agent} joined`
        : `⇄ switched to ${agent}`;
    return (
      <div style={{ marginLeft: 60, marginTop: 6, marginBottom: 6 }}>
        <span className="muted text-sm mono" style={{ color: "var(--accent)" }}>
          {label}
        </span>
      </div>
    );
  }

  if (kind === "compaction_marker") {
    return <CompactionMarker m={m} />;
  }

  // Coalesced agent reply (the streaming tokens collapsed into one
  // bubble by window.chatCoalesce). Renders as markdown — LLMs
  // routinely emit headings, lists, bold, and code blocks; raw text
  // is borderline unreadable for any non-trivial response.
  if (kind === "assistant_message") {
    return (
      <div style={{ display: "flex", gap: 12, marginBottom: 14 }}>
        <CT_Attribution
          label={m.agent_id || "agent"}
          isUser={false}
          time={CT_formatTime(m.created_at)}
        />
        <div className="md-body" style={{
          flex: 1, minWidth: 0, fontSize: 13, lineHeight: 1.55, color: "var(--text)",
          borderLeft: "2px solid var(--accent)", paddingLeft: 12,
        }}>
          <CT_MarkdownBody text={m.text} />
        </div>
      </div>
    );
  }

  const role = CT_roleForKind(kind);
  const isUser = role === "user";
  // Pull attachment parts (image/document) out of the user_message
  // payload so they render under the text. Non-user messages don't
  // currently carry attachments through this surface.
  const attachmentParts = (isUser && Array.isArray(m.parts))
    ? m.parts.filter((p) => p && (p.type === "image" || p.type === "document"))
    : [];
  const attribLabel = isUser ? "user" : (m.agent_id || "agent");
  // Optimistic echo (Task C2): <Conversation> pushes a synthetic
  // user_message row (marked `pending: true`, carrying a `clientId`) the
  // instant the operator hits Send, before the WS round-trip confirms
  // it's persisted. This tick is the only visual difference from a real
  // row — it disappears the moment the persisted row (same text, real
  // seq) reconciles it in place.
  const isPending = isUser && m.pending === true;
  // Task F3 (R4): the rewind icon only ever appears on a persisted (not
  // still-optimistic) user_message strictly after the compaction
  // boundary — see CT_RewindButton above for the full rationale.
  const canRewind = isUser && !isPending
    && typeof m.seq === "number" && m.seq > (compactionBoundarySeq || 0);
  return (
    <div style={{ display: "flex", gap: 12, marginBottom: 14 }}>
      <CT_Attribution
        label={attribLabel}
        isUser={isUser}
        time={CT_formatTime(m.created_at)}
      />
      <div style={{ flex: 1, minWidth: 0, fontSize: 13, lineHeight: 1.55, color: "var(--text)", borderLeft: `2px solid ${isUser ? "var(--border)" : "var(--accent)"}`, paddingLeft: 12 }}>
        {CT_textOf(m) && <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{CT_textOf(m)}</div>}
        {attachmentParts.length > 0 && (
          <div style={{ marginTop: 6, display: "flex", flexWrap: "wrap", gap: 6 }}>
            {attachmentParts.map((p, i) => <CT_AttachmentPart key={i} part={p} chatId={chatId} />)}
          </div>
        )}
        {isPending && (
          <div className="pill pill-created" style={{ marginTop: 6, width: "fit-content" }}>
            <Icon name="clock" size={9} />
            sending
          </div>
        )}
      </div>
      {canRewind && (
        <CT_RewindButton seq={m.seq} disabled={rewindDisabled} onRewind={onRewind} />
      )}
    </div>
  );
}

// ============================================================================
// Inline artifact previews (Task E1) — CT_AttachmentPart + helpers
// ============================================================================
//
// A media part is either an inline base64 blob (`part.data`, the composer's
// pre-A8 attach path) or an artifact-backed reference (`part.artifact_id`,
// no `data` — the shape tool-produced media always takes; see
// primer/chat/executor.py::_tool_media_parts and
// tests/e2e/test_chat_artifact_fetch.py). The A8 route
// (`GET /v1/chats/{chat_id}/artifacts/{artifact_id}`) serves the latter's
// bytes directly, so an artifact-only part's `src` is that raw versioned
// path — not the base-relative data-fetch helper the rest of the console
// uses, since this becomes a real `<img src>` / `<a href>` / `<embed
// src>` attribute, not a fetch call.

function CT_artifactUrl(chatId, artifactId) {
  if (!chatId || !artifactId) return null;
  return "/v1/chats/" + chatId + "/artifacts/" + artifactId;
}

// Best-effort human size label. A persisted artifact-only part doesn't
// carry a byte count (see the shape above), so this only has something to
// show when the part still carries inline base64 `data` (or an explicit
// `size`/`bytes` field some future caller sets) — otherwise it degrades to
// "" and the chip just omits the size, same precedent as CT_formatTime's
// empty-string degrade for a missing created_at.
function CT_formatBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function CT_partSizeLabel(part) {
  if (Number.isFinite(part.size)) return CT_formatBytes(part.size);
  if (Number.isFinite(part.bytes)) return CT_formatBytes(part.bytes);
  if (typeof part.data === "string" && part.data.length > 0) {
    const len = part.data.length;
    const padding = part.data.endsWith("==") ? 2 : part.data.endsWith("=") ? 1 : 0;
    return CT_formatBytes(Math.floor((len * 3) / 4) - padding);
  }
  return "";
}

// Thumb -> click-to-expand image preview: starts small, click toggles a
// larger inline view instead of leaving the page to see the full image.
function CT_ImagePreview({ src, filename }) {
  const [expanded, setExpanded] = React.useState(false);
  const toggle = () => setExpanded((v) => !v);
  return (
    <div style={{ display: "inline-flex", flexDirection: "column", gap: 4, alignItems: "flex-start" }}>
      <img
        src={src}
        alt={filename || "image"}
        onClick={toggle}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } }}
        style={{
          maxHeight: expanded ? 480 : 160,
          maxWidth: expanded ? 480 : 240,
          borderRadius: 4,
          border: "1px solid var(--border)",
          display: "block",
          cursor: "zoom-in",
        }}
      />
      <a href={src} download={filename || undefined} target="_blank" rel="noreferrer" className="muted text-sm">
        open ↗
      </a>
    </div>
  );
}

// PDFs render inline via <embed> at a compact size that expands on click —
// same thumb -> click-to-expand pattern as CT_ImagePreview.
function CT_PdfPreview({ src, filename }) {
  const [expanded, setExpanded] = React.useState(false);
  const toggle = () => setExpanded((v) => !v);
  return (
    <div style={{ display: "inline-flex", flexDirection: "column", gap: 4, alignItems: "flex-start" }}>
      <div
        onClick={toggle}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } }}
        title={expanded ? "Collapse preview" : "Expand preview"}
        style={{ cursor: "pointer" }}
      >
        <embed
          src={src}
          type="application/pdf"
          style={{
            width: expanded ? 480 : 200,
            height: expanded ? 620 : 150,
            border: "1px solid var(--border)",
            borderRadius: 4,
            display: "block",
          }}
        />
      </div>
      <a href={src} download={filename || undefined} target="_blank" rel="noreferrer" className="muted text-sm">
        {filename || "document.pdf"} · open ↗
      </a>
    </div>
  );
}

// Anything that isn't inline-previewable (a document that isn't a PDF, or
// a preview-able type with no usable src) renders as a compact chip:
// filename + mime type + a best-effort size, plus open/download.
function CT_FileChip({ part, mime, src }) {
  const filename = part.filename || (part.type === "image" ? "image" : "document");
  const sizeLabel = CT_partSizeLabel(part);
  return (
    <div style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      padding: "4px 8px", border: "1px solid var(--border)",
      borderRadius: 4, background: "var(--bg-0)",
    }}>
      <Icon name="file" size={12} className="muted" />
      <span className="mono text-sm">{filename}</span>
      <span className="muted text-sm" style={{ fontSize: 10.5 }}>
        {mime}{sizeLabel ? ` · ${sizeLabel}` : ""}
      </span>
      {src && (
        <a href={src} download={filename} target="_blank" rel="noreferrer" className="muted text-sm" title="Open / download">
          <Icon name="external" size={11} />
        </a>
      )}
    </div>
  );
}

// Inline-render one attachment Part as it appears inside a user_message
// bubble or under a tool_result's media (Task E1 extends this to the
// latter — see CT_ToolMedia below). Image/PDF parts get a thumb ->
// click-to-expand inline preview; anything else renders as a chip.
// `chatId` builds the A8 artifact-fetch src for a part that carries only
// `artifact_id` (no inline `data`); the pre-existing inline base64
// (`part.data`) and public `part.url` paths keep working unchanged.
function CT_AttachmentPart({ part, chatId }) {
  if (!part) return null;
  const mime = part.mime_type || (part.type === "image" ? "image/png" : "application/octet-stream");
  const src = part.url
    ? part.url
    : (part.data
      ? `data:${mime};base64,${part.data}`
      : CT_artifactUrl(chatId, part.artifact_id));

  if (part.type === "image" && src) {
    return <CT_ImagePreview src={src} filename={part.filename} />;
  }
  if (part.type === "document" && mime === "application/pdf" && src) {
    return <CT_PdfPreview src={src} filename={part.filename} />;
  }
  if (part.type === "image" || part.type === "document" || part.type === "audio" || part.type === "video") {
    return <CT_FileChip part={part} mime={mime} src={src} />;
  }
  return null;
}

// A tool_result's payload.media (flattened onto the row — see
// primer/chat/executor.py::_tool_media_parts) renders as a row of
// CT_AttachmentPart previews right under the tool's collapsed result chip.
function CT_ToolMedia({ media, chatId }) {
  if (!Array.isArray(media) || media.length === 0) return null;
  return (
    <div style={{ marginLeft: 60, marginTop: -2, marginBottom: 6, display: "flex", flexWrap: "wrap", gap: 6 }}>
      {media.map((p, i) => <CT_AttachmentPart key={i} part={p} chatId={chatId} />)}
    </div>
  );
}

// ============================================================================
// CompactionMarker — in-stream divider rendered for `kind: "compaction_marker"`
// rows. The marker is synthesised client-side when a `"compaction"` envelope
// arrives over the WS so the operator sees where context was summarised.
// ============================================================================

function CompactionMarker({ m }) {
  // The marker row can arrive two ways:
  // - From the WS `compaction` envelope (lifted to the top-level row).
  // - From REST history replay (server's compaction_marker ChatMessage
  //   with the token counts nested under `payload`).
  // Read both shapes; whichever has a value wins.
  const payload = m.payload || {};
  const before = Number(m.tokens_before ?? payload.tokens_before) || 0;
  const after = Number(m.tokens_after ?? payload.tokens_after) || 0;
  const saved = before > 0 ? Math.max(0, before - after) : 0;
  return (
    <div
      className="compaction-marker"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        margin: "16px 0",
        padding: "8px 12px",
        borderTop: "2px solid var(--accent)",
        borderBottom: "2px solid var(--accent)",
        background: "var(--bg-2)",
        fontSize: 12,
        color: "var(--text-2)",
        fontFamily: "IBM Plex Mono, monospace",
      }}
      title={m.reason || "Conversation was compacted to fit the context window."}
    >
      <Icon name="compress" size={13} className="muted" />
      <span style={{ fontWeight: 600, color: "var(--accent)" }}>
        Conversation compacted
      </span>
      {before > 0 && (
        <span className="muted">
          · {before.toLocaleString()} → {after.toLocaleString()} tokens
          {saved > 0 ? ` (-${saved.toLocaleString()})` : ""}
        </span>
      )}
      {m.reason && <span className="muted">· {m.reason}</span>}
    </div>
  );
}

// ============================================================================
// CT_ConnectionStatus — connection + turn-status indicator (Task G1 / §4.5)
// ============================================================================
//
// <Transcript> is the pure core Studio embeds directly (no ChatDetail page
// chrome) — the WS badge that already lives in the /chats page host
// (ui/components/chats.jsx's wsBadge) isn't guaranteed to be visible to
// every host. This renders the SAME wsState (open/connecting/closed) pill
// styling inline in the transcript itself, plus a `turn_status`
// (idle/claimable/running) pill right alongside it — "surface turn_status
// ... as a real indicator alongside the WS badge" per the plan — so
// connection + turn legibility travels with the component wherever it's
// embedded, not just the /chats page's own header.
function CT_ConnectionStatus({ wsState, turnStatus }) {
  const wsPillClass = wsState === "open"
    ? "pill pill-running"
    : wsState === "connecting"
      ? "pill pill-paused"
      : "pill pill-ended";
  const wsLabel = wsState === "open" ? "live" : wsState === "connecting" ? "connecting" : "offline";
  const turn = turnStatus || "idle";
  const turnPillClass = turn === "running"
    ? "pill pill-running"
    : turn === "claimable"
      ? "pill pill-claimed"
      : "pill pill-created";
  return (
    <div
      className="chat-connection-status"
      data-testid="chat-connection-status"
      style={{
        display: "flex", alignItems: "center", gap: 8, flex: "0 0 auto",
        padding: "6px 24px", fontSize: 11,
        borderBottom: "1px solid var(--border)",
      }}
    >
      <span className={wsPillClass} title={`WebSocket ${wsState}`}>
        <span className="dot"></span>{wsLabel}
      </span>
      <span className={turnPillClass} data-testid="chat-turn-status" title={`Turn status: ${turn}`}>
        <span className="dot"></span>{turn}
      </span>
    </div>
  );
}

// ============================================================================
// Transcript — the timeline itself (Task B3)
// ============================================================================
//
// Rows that mean "agent is currently producing visible output, no
// thinking placeholder needed" or "turn closed out, definitely no
// placeholder." NOTE this set is checked against the COALESCED last
// row (Transcript receives `messages` already run through
// window.chatCoalesce by <Conversation>), so "assistant_message"
// stands in for the raw "assistant_token" kind the original inline
// check used — a still-streaming reply is coalesced into an
// "assistant_message" entry before it ever reaches this component, and
// that entry is exactly the case the original check meant to treat as
// "already visibly rendered, don't also show Thinking…".
const _QUIET_LAST_KINDS = new Set([
  "assistant_message", // agent reply bubble already visible (mid-stream or done)
  "done",
  "error",
  "cancelled",
  "yielded",
]);

function Transcript({
  messages, chatId, agentId, wsState, waitingForReply, turnStatus,
  pendingToolCall, sendMessage, onRewind, compactionBoundarySeq,
  scrollRef, onScroll, loadingOlder, hasMoreOlder,
}) {
  const turnInFlight = turnStatus === "claimable" || turnStatus === "running";
  // Task F3 (R4): mirrors A7's own 409 guard exactly — `turn_status ===
  // "running"` — rather than the broader claimable-or-running
  // `turnInFlight` used for Send/Stop above, since a claimable (not yet
  // picked up) turn isn't actually racing the rewind's own DB writes.
  const rewindDisabled = turnStatus === "running";
  // Task C4: an approval-mode gate (see CT_ApprovalGate above) renders
  // inline Approve/Deny right after the timeline — it never coexists with
  // the Thinking bubble in practice (the gate holds turn_status idle until
  // the human's yes/no resumes the turn).
  const gateAwaitingApproval = !!(pendingToolCall && pendingToolCall.mode === "approval");
  const lastRow = messages.length > 0 ? messages[messages.length - 1] : null;
  const lastIsQuiet = lastRow && _QUIET_LAST_KINDS.has(lastRow.kind);
  const showThinking = waitingForReply || (turnInFlight && !lastIsQuiet);
  // Task C2: a still-running tool_call is exactly the last coalesced row
  // that hasn't yet been followed by its tool_result (pairing the two by
  // id to also handle a *completed* tool as the last row is Task C3's
  // job) — label the live state with what the agent is doing instead of
  // the generic "Thinking…".
  const runningToolName = lastRow && lastRow.kind === "tool_call"
    ? (lastRow.name || lastRow.tool_name || "tool")
    : null;
  const thinkingLabel = runningToolName ? `running ${runningToolName}…` : null;

  // Task C3: pair tool_call + tool_result rows by shared `id` so a
  // completed tool renders as ONE hybrid row instead of two separate
  // one-line dumps. toolCallIdsPresent lets a tool_result whose call has
  // scrolled out of the loaded window still fall back to its own row.
  const toolResultsById = new Map();
  const toolCallIdsPresent = new Set();
  for (const row of messages) {
    if (row.kind === "tool_result" && row.id) toolResultsById.set(row.id, row);
    if (row.kind === "tool_call" && row.id) toolCallIdsPresent.add(row.id);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0, minWidth: 0 }}>
      {/* Task G1 (§4.5): connection + turn-status legibility that travels
          with the component wherever it's embedded — see
          CT_ConnectionStatus above. */}
      <CT_ConnectionStatus wsState={wsState} turnStatus={turnStatus} />
      <div
        ref={scrollRef}
        onScroll={onScroll}
        // Task G1 (§4.5): the timeline is a live region — streaming
        // assistant text (coalesced into assistant_message rows) and new
        // tool rows (tool_call/tool_result) are appended into this same
        // subtree, so role="log" + aria-live="polite" is enough for a
        // screen reader to announce each addition without re-reading the
        // whole transcript.
        role="log"
        aria-live="polite"
        style={{ flex: 1, overflow: "auto", padding: "18px 24px", minHeight: 0, minWidth: 0 }}
      >
        {(loadingOlder || hasMoreOlder) && messages.length > 0 && (
          <div
            className="muted text-sm"
            style={{ textAlign: "center", padding: "6px 0 12px", fontSize: 11 }}
            data-testid="chat-load-older"
          >
            {loadingOlder ? "Loading older…" : "Scroll up to load older"}
          </div>
        )}
        {messages.length === 0 && (
          <div className="muted text-sm" style={{ textAlign: "center", padding: 24 }}>
            {wsState === "connecting" ? "Connecting…" : "No messages yet. Say hello to the agent."}
          </div>
        )}
        {messages.map((m) => {
          // A tool_result that pairs with a tool_call present in this same
          // window (Task C3) folds into that call's row above — it doesn't
          // get a row of its own.
          if (m.kind === "tool_result" && m.id && toolCallIdsPresent.has(m.id)) {
            return null;
          }
          const pairedResult = m.kind === "tool_call" && m.id
            ? toolResultsById.get(m.id)
            : undefined;
          // A pending optimistic echo (Task C2) has no `seq` yet — key off
          // its `clientId` instead so it doesn't collide with (or get
          // confused for) a persisted row while it's still in flight. A
          // tool_call's key additionally folds in run/done state (Task C3)
          // so the row remounts — and so its default-open state resets from
          // "expanded while running" to "collapsed once done" — the instant
          // its tool_result pairs up, rather than staying stuck open.
          const key = m.kind === "assistant_message"
            ? `am-${m.startSeq}-${m.endSeq}`
            : m.clientId
              ? `pending-${m.clientId}`
              : m.kind === "tool_call"
                ? `${m.seq}-${m.kind}-${pairedResult ? "done" : "running"}`
                : `${m.seq}-${m.kind}`;
          return (
            <Message
              key={key}
              m={m}
              pairedResult={pairedResult}
              chatId={chatId}
              onRewind={onRewind}
              rewindDisabled={rewindDisabled}
              compactionBoundarySeq={compactionBoundarySeq}
            />
          );
        })}

        {/* Thinking indicator — see _QUIET_LAST_KINDS above for why this
            checks the coalesced last row rather than the raw one; see
            thinkingLabel above (Task C2) for the tool-labeled live state. */}
        {showThinking ? <CT_ThinkingBubble label={thinkingLabel} /> : null}

        {/* Task C4: inline Approve/Deny under the gating assistant message —
            see CT_ApprovalGate above for why rendering it here (right after
            the last row) satisfies "under the gating assistant message". */}
        {gateAwaitingApproval ? <CT_ApprovalGate sendMessage={sendMessage} /> : null}
      </div>
    </div>
  );
}

window.Transcript = Transcript;
// CompactionMarker was window.CompactionMarker-exported from chats.jsx
// before Task B3; preserved here now that it lives in this file.
window.CompactionMarker = CompactionMarker;
