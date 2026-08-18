// Shared transcript helpers — pulled out of the chat UI (deleted in S1 P7) (Task B2 of the
// chat-refactor plan) so they're unit-testable and reusable by
// <Conversation> (and, later, <Transcript>). Behavior is byte-identical
// to the inline versions that used to live inside ChatDetail.
//
// Loaded via <script type="text/babel"> in ui/index.html, before
// the session surfaces (see index.html for the load-order
// note). Plain helpers, no React/JSX — exported as window.chatFlatten /
// window.chatCoalesce so callers reference them via the full path,
// mirroring the window.primerVendor.highlightCode convention.

(function () {
  // REST rows carry kind-specific fields nested under `payload`; WS
  // frames spread payload into the top-level (see chats router
  // _message_to_wire). Flatten REST rows on load so both sources are
  // homogeneous. Moved verbatim from the chat UI's REST-load map.
  function chatFlatten(rows) {
    return (rows || []).map((row) => {
      const payload = row.payload && typeof row.payload === "object" ? row.payload : {};
      return { ...payload, ...row };
    });
  }

  // Walk `messages` in order and merge any run of consecutive
  // `assistant_token` rows into one synthetic "assistant_message" entry
  // whose `text` is the concatenation of the run's deltas. Any other
  // row passes through unchanged. Without this, every token from the
  // LLM renders as its own bubble — unreadable for any reply longer
  // than a word or two. Moved verbatim from CT_coalesceMessages.
  // Reasoning streams as its own run of `reasoning` rows, exactly like
  // assistant text. It is coalesced into one synthetic
  // "reasoning_block" so the transcript can render a single collapsed
  // bar rather than one bubble per thinking token. Kept as a SEPARATE
  // run from assistant_message: reasoning precedes the answer and is
  // display-only (the backend never replays it into the prompt), so
  // merging the two would put thinking text inside the answer bubble.
  function chatCoalesce(messages) {
    const out = [];
    let buffer = null;
    let reasoning = null;
    const flushReasoning = () => {
      if (reasoning && reasoning.text.trim().length > 0) {
        out.push(reasoning);
      }
      reasoning = null;
    };
    const flushBuffer = () => {
      // Skip text-only buffers whose content is whitespace-only. LLMs
      // commonly emit one or more empty/zero-delta assistant_token
      // rows alongside a tool_call (the protocol uses the token row as
      // the carrier for an empty `content` field when the model's
      // reply is *all* tool calls). Without this guard, every tool
      // call produces a blank assistant_message bubble above it.
      if (buffer && buffer.text.trim().length > 0) {
        out.push(buffer);
      }
      buffer = null;
    };
    for (const m of messages) {
      if (m.kind === "reasoning") {
        // Reasoning arrives BEFORE the answer it explains, so an open
        // assistant buffer means a new turn started; flush it first.
        flushBuffer();
        const delta = typeof m.delta === "string" ? m.delta : "";
        if (!reasoning) {
          reasoning = {
            kind: "reasoning_block",
            text: delta,
            startSeq: m.seq,
            endSeq: m.seq,
            agent_id: m.agent_id,
            created_at: m.created_at,
          };
        } else {
          reasoning.text += delta;
          reasoning.endSeq = m.seq;
        }
        continue;
      }
      flushReasoning();
      if (m.kind === "assistant_token") {
        const delta = typeof m.delta === "string" ? m.delta : "";
        if (!buffer) {
          // Stamp agent_id/created_at from the FIRST raw token of the run
          // onto the synthetic assistant_message (Task C2 fold-in fix).
          // REST-loaded history already carries these per-row (Task C1
          // reads m.agent_id/m.created_at off the persisted ChatMessage),
          // but a LIVE-STREAMED reply only has them on the raw
          // assistant_token frames that get coalesced away here — without
          // forwarding them, a live reply's attribution/timestamp went
          // missing until the next REST reload.
          buffer = {
            kind: "assistant_message",
            text: delta,
            startSeq: m.seq,
            endSeq: m.seq,
            agent_id: m.agent_id,
            created_at: m.created_at,
          };
        } else {
          buffer.text += delta;
          buffer.endSeq = m.seq;
        }
        continue;
      }
      flushBuffer();
      out.push(m);
    }
    flushReasoning();
    flushBuffer();
    return out;
  }

  window.chatFlatten = chatFlatten;
  window.chatCoalesce = chatCoalesce;
})();
