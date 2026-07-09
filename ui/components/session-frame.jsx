/* global React, Icon */
// Shared live-stream frame renderer + coalescing helpers, extracted
// verbatim from session-detail.jsx so the graph node inspector
// (SD_NodeInspector) renders node-attributed records identically to the
// agent live stream. No behavior change vs the pre-extraction file.

const SESSION_TERMINAL = new Set(["ended", "completed", "failed", "cancelled"]);

// Coalesce consecutive assistant_token rows into a single message.
// If any token in the run carries a `parsed` payload (graph End nodes
// emit one such record with the structured output), preserve it on the
// coalesced blob so the renderer can surface a "Structured output" panel.
function _SLS_coalesceMessages(messages) {
  const out = [];
  let buf = null;
  const flush = () => { if (buf) { out.push(buf); buf = null; } };
  for (const m of messages) {
    if (m.kind === "assistant_token") {
      // Payload may carry `text` (coalesced by backend) or `delta` (raw token).
      const delta = typeof m.text === "string" ? m.text
                  : typeof m.delta === "string" ? m.delta : "";
      if (!buf) {
        buf = { kind: "_assistant_message", text: delta, startSeq: m.seq, endSeq: m.seq };
      } else {
        buf.text += delta;
        buf.endSeq = m.seq;
      }
      // Graph End nodes emit an assistant_token whose payload includes
      // `parsed` (the validated structured output). Carry it through so
      // the renderer can show a collapsible JSON panel.
      if (m.parsed != null && buf.parsed == null) {
        buf.parsed = m.parsed;
      }
      if (typeof m.end_node_id === "string" && !buf.end_node_id) {
        buf.end_node_id = m.end_node_id;
      }
      continue;
    }
    flush();
    out.push(m);
  }
  flush();
  return out;
}

// Red "ERROR" chip + wrapped <pre> for NodeOutput.error, with an optional
// subtler grey chip for NodeOutput.ended_detail (the structured failure code)
// when distinct from the message. Spec B §5 — operator-facing failure surface.
//
// The session WS flattens `payload` onto the top-level frame (see ws.onmessage
// in SessionDetail), so a graph node-failure record exposes its NodeOutput
// fields as both `m.payload?.error` and `m.error`. We accept both shapes.
function _SLS_NodeErrorBadge({ error, code }) {
  if (!error && !code) return null;
  const showCode = code && code !== error;
  return (
    <div style={{ marginBottom: 8 }}>
      <span style={{
        display: "inline-block",
        background: "var(--red-dim)",
        color: "var(--red)",
        padding: "2px 8px",
        borderRadius: 4,
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: "0.04em",
      }}>ERROR</span>
      {error && (
        <pre className="mono" style={{
          marginTop: 6,
          padding: 8,
          fontSize: 12,
          background: "var(--bg-2)",
          border: "1px solid var(--red-dim)",
          borderRadius: 4,
          color: "var(--text)",
          whiteSpace: "pre-wrap",
          overflow: "auto",
        }}>{error}</pre>
      )}
      {showCode && (
        <span style={{
          display: "inline-block",
          marginTop: 4,
          background: "var(--bg-2)",
          color: "var(--text-2)",
          border: "1px solid var(--border)",
          padding: "1px 6px",
          borderRadius: 4,
          fontSize: 11,
          fontFamily: "IBM Plex Mono, monospace",
        }}>code: {code}</span>
      )}
    </div>
  );
}

// One row in the live-stream timeline.
function _SLS_Frame({ m }) {
  const kind = m.kind;

  // Coalesced assistant blob.
  if (kind === "_assistant_message") {
    return (
      <div style={{ display: "flex", gap: 12, marginBottom: 14 }}>
        <div style={{
          width: 52, flexShrink: 0,
          fontFamily: "IBM Plex Mono, monospace", fontSize: 10.5,
          textTransform: "uppercase", letterSpacing: "0.06em",
          color: "var(--accent)", fontWeight: 600, paddingTop: 2,
        }}>agent</div>
        <div style={{
          flex: 1, fontSize: 13, lineHeight: 1.55, color: "var(--text)",
          borderLeft: "2px solid var(--accent)", paddingLeft: 12,
          whiteSpace: "pre-wrap",
        }}>
          <_SLS_NodeErrorBadge
            error={m.payload?.error || m.error}
            code={m.payload?.ended_detail || m.ended_detail}
          />
          {typeof window.renderMarkdown === "function"
            ? window.renderMarkdown(m.text)
            : m.text}
          {m.parsed != null && (
            <details className="structured-output" style={{ marginTop: 8 }}>
              <summary style={{ cursor: "pointer", fontSize: 11, color: "var(--text-2)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
                Structured output
              </summary>
              <pre className="mono" style={{
                marginTop: 6,
                padding: 8,
                fontSize: 12,
                background: "var(--bg-2)",
                border: "1px solid var(--border)",
                borderRadius: 4,
                whiteSpace: "pre-wrap",
                overflow: "auto",
              }}>{JSON.stringify(m.parsed, null, 2)}</pre>
            </details>
          )}
        </div>
      </div>
    );
  }

  // Raw assistant_token (uncoalesced) — render the same way for
  // defense in depth: if a graph End record arrives outside a token
  // run, still surface its parsed payload.
  if (kind === "assistant_token" && m.payload?.parsed != null) {
    const text = typeof m.text === "string" ? m.text
               : typeof m.delta === "string" ? m.delta : "";
    return (
      <div style={{ display: "flex", gap: 12, marginBottom: 14 }}>
        <div style={{
          width: 52, flexShrink: 0,
          fontFamily: "IBM Plex Mono, monospace", fontSize: 10.5,
          textTransform: "uppercase", letterSpacing: "0.06em",
          color: "var(--accent)", fontWeight: 600, paddingTop: 2,
        }}>agent</div>
        <div style={{
          flex: 1, fontSize: 13, lineHeight: 1.55, color: "var(--text)",
          borderLeft: "2px solid var(--accent)", paddingLeft: 12,
          whiteSpace: "pre-wrap",
        }}>
          {text}
          <details className="structured-output" style={{ marginTop: 8 }}>
            <summary style={{ cursor: "pointer", fontSize: 11, color: "var(--text-2)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
              Structured output
            </summary>
            <pre className="mono" style={{
              marginTop: 6,
              padding: 8,
              fontSize: 12,
              background: "var(--bg-2)",
              border: "1px solid var(--border)",
              borderRadius: 4,
              whiteSpace: "pre-wrap",
              overflow: "auto",
            }}>{JSON.stringify(m.payload.parsed, null, 2)}</pre>
          </details>
        </div>
      </div>
    );
  }

  // User input echo.
  if (kind === "user_input") {
    const text = m.text || m.content || m.message || "";
    return (
      <div style={{ display: "flex", gap: 12, marginBottom: 14 }}>
        <div style={{
          width: 52, flexShrink: 0,
          fontFamily: "IBM Plex Mono, monospace", fontSize: 10.5,
          textTransform: "uppercase", letterSpacing: "0.06em",
          color: "var(--text-2)", fontWeight: 600, paddingTop: 2,
        }}>user</div>
        <div style={{
          flex: 1, fontSize: 13, lineHeight: 1.55, color: "var(--text)",
          borderLeft: "2px solid var(--border)", paddingLeft: 12,
          whiteSpace: "pre-wrap",
        }}>{text}</div>
      </div>
    );
  }

  // Tool call card — expandable if args are large.
  if (kind === "tool_call") {
    const name = m.name || m.tool_name || "tool";
    const args = m.args || m.arguments || {};
    const argsFull = (() => { try { return JSON.stringify(args, null, 2); } catch { return ""; } })();
    const argsPreview = (() => { try { return JSON.stringify(args); } catch { return ""; } })();
    return <_SLS_ExpandableRow
      icon="play" iconColor="var(--text-3)" borderColor="var(--border)"
      name={name} separator="(" previewText={argsPreview} fullText={argsFull} />;
  }

  // Tool result card — expandable.
  if (kind === "tool_result") {
    const name = m.name || m.tool_name || "tool";
    const isErr = !!m.error;
    const fullStr = typeof m.result === "string" ? m.result
                  : (m.result != null ? JSON.stringify(m.result, null, 2) : "");
    const previewStr = typeof m.result === "string" ? m.result
                     : (m.result != null ? JSON.stringify(m.result) : "");
    return <_SLS_ExpandableRow
      icon={isErr ? "x-circle" : "check"}
      iconColor={isErr ? "var(--red)" : "var(--green)"}
      borderColor={isErr ? "var(--red)" : "var(--green)"}
      name={name} separator="→" previewText={previewStr} fullText={fullStr} />;
  }

  // Error banner. Graph node failures arrive here via _GraphErrorEvent, whose
  // payload carries the structured ``code`` (NodeOutput.ended_detail) and the
  // human-readable ``message`` (NodeOutput.error). Older / non-graph error
  // records may carry only ``message`` / ``error`` / ``detail`` — fall back
  // to the original banner shape in that case.
  if (kind === "error") {
    const msg = m.payload?.error || m.error || m.message || m.detail || "error";
    const code = m.payload?.ended_detail || m.ended_detail || m.code;
    const nodeId = m.payload?.node_id || m.node_id;
    return (
      <div style={{ marginLeft: 64, marginTop: 6, marginBottom: 6 }}>
        <_SLS_NodeErrorBadge error={msg} code={code} />
        {nodeId && (
          <div className="muted text-sm mono" style={{ marginTop: 2 }}>
            node: {nodeId}
          </div>
        )}
      </div>
    );
  }

  // Event markers: done, cancelled, yielded, resumed.
  if (kind === "done" || kind === "cancelled" || kind === "yielded" || kind === "resumed") {
    const stopReason = m.stop_reason || m.reason || "";
    return (
      <div style={{ marginLeft: 64, marginTop: 4, marginBottom: 8 }}>
        <span
          className="muted text-sm mono"
          style={{
            color: kind === "cancelled" ? "var(--red)"
                 : kind === "done" ? "var(--green)"
                 : "var(--amber)",
          }}
        >· {kind}{stopReason ? ` (${stopReason})` : ""}</span>
      </div>
    );
  }

  // Unknown / future frame kinds — render a dim mono line.
  return (
    <div style={{ marginLeft: 64, marginTop: 2, marginBottom: 2 }}>
      <span className="muted text-sm mono">· {kind}</span>
    </div>
  );
}

const _SLS_PREVIEW_CHARS = 80;

function _SLS_ExpandableRow({ icon, iconColor, borderColor, name, separator, previewText, fullText }) {
  const [open, setOpen] = React.useState(false);
  const preview = (previewText || "").replace(/\s+/g, " ");
  const truncated = preview.length > _SLS_PREVIEW_CHARS;
  const previewShown = truncated ? preview.slice(0, _SLS_PREVIEW_CHARS) + "…" : preview;
  const hasExpand = (fullText || "").length > _SLS_PREVIEW_CHARS;
  const toggle = () => { if (hasExpand) setOpen((o) => !o); };
  return (
    <div style={{ marginLeft: 64, marginTop: 2, marginBottom: 6 }}>
      <div
        className="tool-call"
        style={{ borderLeft: `2px solid ${borderColor}`, cursor: hasExpand ? "pointer" : "default" }}
        onClick={toggle}
      >
        {hasExpand && <Icon name={open ? "chevron-down" : "chevron-right"} size={10} style={{ color: "var(--text-3)" }} />}
        <Icon name={icon} size={10} style={{ color: iconColor }} />
        <span className="name">{name}</span>
        <span className="arrow">{separator}</span>
        <span className="muted" style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", flex: 1, minWidth: 0 }}>{previewShown}</span>
      </div>
      {open && (
        <pre style={{
          marginTop: 6, padding: "10px 12px",
          background: "var(--bg)", border: "1px solid var(--border)",
          borderRadius: 6, fontSize: 11.5, lineHeight: 1.5,
          fontFamily: "IBM Plex Mono, monospace", color: "var(--text-2)",
          whiteSpace: "pre-wrap", wordBreak: "break-all",
          maxHeight: 300, overflow: "auto",
        }}>{fullText}</pre>
      )}
    </div>
  );
}

Object.assign(window, {
  SESSION_TERMINAL,
  _SLS_coalesceMessages,
  _SLS_NodeErrorBadge,
  _SLS_Frame,
  _SLS_ExpandableRow,
});
