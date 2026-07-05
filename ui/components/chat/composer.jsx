/* global React, Icon, Btn, CT_AttachmentChip */
//
// <Composer> — the chat input surface shell (Task B4 of the
// chat-refactor plan). Moved out of <Conversation>
// (ui/components/chat/conversation.jsx): the pending-attachments
// strip, the attach-file control, the message textarea, and the
// context-aware Send/Stop control. <Conversation> still OWNS all the
// state and handlers (composer text, attachments list, send/attach
// logic) — this component is a controlled, pure-rendering shell that
// forwards events, so this move is structural, not a behavior change.
//
// Slash-command detection, @-mention autocomplete, and folding the
// attach control into the chatbox are Task D1-D3's job — the
// `slashCommands`/`mentionSources` props are accepted now so the
// prop surface is stable ahead of that phase, but are otherwise
// unused until then.
//
// `schemaInvalid` is a gating hook for Task F2's <SchemaPanel>
// validity wiring — `disabled || schemaInvalid` already disables Send
// so F2 only needs to start passing a real value.
//
// The Send/Stop control is context-aware per the plan: `running`
// swaps the affordance to Stop (calling `onStop`) instead of Send.
// <Conversation> wires the real turn-running signal + the
// `/chats/{id}/cancel` POST behind `onStop` starting in Task C2 — for
// now the shell just renders the correct control for whatever it's
// given.

function Composer({
  value,
  onChange,
  onSend,
  onStop,
  running,
  disabled,
  attachments,
  onAttach,
  onRemoveAttachment,
  slashCommands,
  mentionSources,
  schemaInvalid,
}) {
  const fileInputRef = React.useRef(null);
  const hasAttachments = Array.isArray(attachments) && attachments.length > 0;
  const sendDisabled = disabled || schemaInvalid || (!String(value || "").trim() && !hasAttachments);

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minWidth: 0, gap: 8 }}>
      {/* Pending-attachments strip — visible only when the composer has
          files queued. Each chip carries an image thumbnail or a
          document icon + filename + size; clicking x drops it. */}
      {hasAttachments && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {attachments.map((a) => (
            <CT_AttachmentChip
              key={a.id}
              attachment={a}
              onRemove={() => onRemoveAttachment(a.id)}
            />
          ))}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, alignItems: "stretch" }}>
        <button
          type="button"
          title="Attach files (images, PDFs)"
          data-testid="chat-attach-btn"
          onClick={() => fileInputRef.current && fileInputRef.current.click()}
          disabled={disabled}
          style={{
            background: "transparent",
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: "0 10px",
            color: "var(--text-2)",
            cursor: disabled ? "not-allowed" : "pointer",
            display: "flex",
            alignItems: "center",
            opacity: disabled ? 0.5 : 1,
          }}
        >
          <Icon name="paperclip" size={14} />
        </button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept="image/*,application/pdf"
          style={{ display: "none" }}
          onChange={(e) => {
            if (typeof onAttach === "function") onAttach(e.target.files);
            if (fileInputRef.current) fileInputRef.current.value = "";
          }}
        />
        <textarea
          className="textarea"
          value={value}
          onChange={(e) => typeof onChange === "function" && onChange(e.target.value)}
          placeholder={disabled ? "This chat has ended." : "Send a message…"}
          rows={2}
          style={{ flex: 1, resize: "none" }}
          disabled={disabled}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (!running && typeof onSend === "function") onSend();
            }
          }}
        />
        {running ? (
          <Btn
            kind="danger"
            icon="stop"
            data-testid="chat-stop-btn"
            onClick={onStop}
            style={{ alignSelf: "stretch", paddingLeft: 16, paddingRight: 16 }}
          >Stop</Btn>
        ) : (
          <Btn
            kind="primary"
            icon="send"
            data-testid="chat-send-btn"
            disabled={sendDisabled}
            onClick={onSend}
            style={{ alignSelf: "stretch", paddingLeft: 16, paddingRight: 16 }}
          >Send</Btn>
        )}
      </div>
    </div>
  );
}

window.Composer = Composer;
