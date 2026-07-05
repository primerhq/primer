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
// chatId / agentId / pendingToolCall / onRewind are accepted now so
// the prop surface is stable for the phases that fill them in
// (attribution + rewind icon in C1, inline Approve/Deny in C4) —
// mirrors the showSchemaPanel precedent in <Conversation> (B2).

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
function CT_ThinkingBubble() {
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
        Thinking
        <span className="thinking-dots" style={{ marginLeft: 2 }}>
          <span>.</span><span>.</span><span>.</span>
        </span>
      </div>
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
}) {
  const [open, setOpen] = React.useState(false);
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
          wordBreak: "break-all",
          maxHeight: 360,
          overflow: "auto",
        }}>{fullText}</pre>
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
// Message — one row in the conversation
// ============================================================================

function Message({ m }) {
  const kind = m.kind;

  if (kind === "tool_call") {
    const name = m.name || m.tool_name || "tool";
    const args = m.args || m.arguments || {};
    const argsFull = (() => { try { return JSON.stringify(args, null, 2); } catch { return ""; } })();
    const argsPreview = (() => { try { return JSON.stringify(args); } catch { return ""; } })();
    return (
      <CT_ExpandableToolRow
        icon="play"
        iconColor={m.pending_approval ? "var(--amber)" : "var(--text-3)"}
        borderColor="var(--border)"
        name={name}
        separator="("
        previewText={argsPreview}
        fullText={argsFull}
        endBadge={m.pending_approval ? (
          <span className="pill pill-paused"><span className="dot"></span>awaiting approval</span>
        ) : null}
      />
    );
  }

  if (kind === "tool_result") {
    const name = m.name || m.tool_name || "tool";
    const isError = !!m.error;
    const fullStr = typeof m.result === "string"
      ? m.result
      : (m.result != null ? JSON.stringify(m.result, null, 2) : "");
    const previewStr = typeof m.result === "string"
      ? m.result
      : (m.result != null ? JSON.stringify(m.result) : "");
    return (
      <CT_ExpandableToolRow
        icon={isError ? "x-circle" : "check"}
        iconColor={isError ? "var(--red)" : "var(--green)"}
        borderColor={isError ? "var(--red)" : "var(--green)"}
        name={name}
        separator="→"
        previewText={previewStr}
        fullText={fullStr}
      />
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
          {typeof window.renderMarkdown === "function"
            ? window.renderMarkdown(m.text)
            : <div style={{ whiteSpace: "pre-wrap" }}>{m.text}</div>}
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
            {attachmentParts.map((p, i) => <CT_AttachmentPart key={i} part={p} />)}
          </div>
        )}
      </div>
    </div>
  );
}

// Inline-render one attachment Part as it appears inside a user_message
// bubble. Image parts show a small thumbnail; document parts show a
// filename + mime badge. The persisted ChatMessage row keeps the full
// base64 payload, so thumbnails work from cursor-replay without a
// follow-up fetch.
function CT_AttachmentPart({ part }) {
  if (part.type === "image") {
    const src = part.url
      ? part.url
      : (part.data ? `data:${part.mime_type || "image/png"};base64,${part.data}` : null);
    if (!src) return null;
    return (
      <a href={src} target="_blank" rel="noreferrer" style={{ display: "inline-block" }}>
        <img
          src={src}
          alt={part.filename || "image"}
          style={{
            maxHeight: 160, maxWidth: 240, borderRadius: 4,
            border: "1px solid var(--border)", display: "block",
          }}
        />
      </a>
    );
  }
  if (part.type === "document") {
    const filename = part.filename || "document";
    const mime = part.mime_type || "application/octet-stream";
    return (
      <div style={{
        display: "inline-flex", alignItems: "center", gap: 6,
        padding: "4px 8px", border: "1px solid var(--border)",
        borderRadius: 4, background: "var(--bg-0)",
      }}>
        <Icon name="file" size={12} className="muted" />
        <span className="mono text-sm">{filename}</span>
        <span className="muted text-sm" style={{ fontSize: 10.5 }}>{mime}</span>
      </div>
    );
  }
  return null;
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
  pendingToolCall, onRewind, scrollRef, onScroll, loadingOlder, hasMoreOlder,
}) {
  const turnInFlight = turnStatus === "claimable" || turnStatus === "running";
  const lastRow = messages.length > 0 ? messages[messages.length - 1] : null;
  const lastIsQuiet = lastRow && _QUIET_LAST_KINDS.has(lastRow.kind);
  const showThinking = waitingForReply || (turnInFlight && !lastIsQuiet);

  return (
    <div ref={scrollRef} onScroll={onScroll} style={{ flex: 1, overflow: "auto", padding: "18px 24px", minHeight: 0, minWidth: 0 }}>
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
      {messages.map((m) =>
        m.kind === "assistant_message" ? (
          <Message key={`am-${m.startSeq}-${m.endSeq}`} m={m} />
        ) : (
          <Message key={`${m.seq}-${m.kind}`} m={m} />
        )
      )}

      {/* Thinking indicator — see _QUIET_LAST_KINDS above for why this
          checks the coalesced last row rather than the raw one. */}
      {showThinking ? <CT_ThinkingBubble /> : null}
    </div>
  );
}

window.Transcript = Transcript;
// CompactionMarker was window.CompactionMarker-exported from chats.jsx
// before Task B3; preserved here now that it lives in this file.
window.CompactionMarker = CompactionMarker;
