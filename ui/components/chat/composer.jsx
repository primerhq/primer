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
// Task D2: slash commands. Typing `/` at the very start of the
// message opens a discoverable command menu, filtered as the operator
// keeps typing the command word. The registry itself is NOT owned
// here — it arrives via the `slashCommands` prop (already reserved
// since Task B4) as a plain `{name, hint, run, takesArg}` array, kept
// external so this component stays a pure, non-fetching shell (no
// network calls or WebSocket code live here — see conversation.jsx,
// which builds the registry against its own existing REST actions:
// /compact, /agent, chat creation, chat deletion). Selecting/confirming a
// no-arg command runs it immediately and clears the composer;
// argument-taking commands (e.g. `/agent <name>`) fill in the command
// word plus a trailing space so the operator can type the argument —
// Enter then matches the typed word against the registry and runs it
// with the typed argument. Non-matching text (including a stray
// leading "/") sends as a normal message. @-mention autocomplete
// (`mentionSources`) is still Task D3's job — unused until then.
//
// Task D1 (R2): the attach control is folded INTO the chatbox — the
// icon is anchored at the right end of the textarea's own box
// (position: relative wrapper + an absolutely-positioned button),
// not a standalone bottom-left column. Drag-and-drop and paste onto
// the textarea reuse the same `onAttach` callback the file input
// already calls — <Conversation> still owns `handleFilesPicked` (the
// 8 MiB cap + base64 encoding), passed down unchanged as a prop.
//
// `schemaInvalid` is a gating hook for Task F2's <SchemaPanel>
// validity wiring — `disabled || schemaInvalid` already disables Send
// so F2 only needs to start passing a real value.
//
// The Send/Stop control is context-aware per the plan: `running`
// swaps the affordance to Stop (calling `onStop`) instead of Send.
// <Conversation> wires the real turn-running signal (turn_status
// claimable/running) and the `/chats/{id}/cancel` POST behind `onStop`
// (Task C2) — this component just renders the correct control for
// whatever it's given.

// Splits a composer draft like "/agent claude-x" into the command
// word ("agent") and the rest of the text as its argument
// ("claude-x"). Returns null for anything that doesn't start with
// "/" — plain messages never enter slash-command territory.
function CT_parseSlashDraft(text) {
  const raw = String(text || "");
  if (!raw.startsWith("/")) return null;
  const spaceIdx = raw.indexOf(" ");
  const word = spaceIdx === -1 ? raw.slice(1) : raw.slice(1, spaceIdx);
  const arg = spaceIdx === -1 ? "" : raw.slice(spaceIdx + 1);
  return { word, arg, hasSpace: spaceIdx !== -1 };
}

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

  // ---- Slash commands (Task D2) -----------------------------------------
  // `slashDismissedFor` remembers the exact draft string an Escape was
  // pressed against, so the menu stays closed for that draft but pops
  // back open the moment the operator edits it further.
  const [slashDismissedFor, setSlashDismissedFor] = React.useState(null);
  const [slashActiveIndex, setSlashActiveIndex] = React.useState(0);

  const slashRegistry = Array.isArray(slashCommands) ? slashCommands : [];
  const slashDraft = CT_parseSlashDraft(value);
  const slashMenuOpen =
    !!slashDraft && !slashDraft.hasSpace && !disabled &&
    slashDismissedFor !== value && slashRegistry.length > 0;
  const slashMatches = slashMenuOpen
    ? slashRegistry.filter((c) => c.name.toLowerCase().startsWith(slashDraft.word.toLowerCase()))
    : [];

  // Keep the highlighted row in range as the filtered list narrows/widens
  // while typing (e.g. "/c" -> "/co" narrows from several matches to one).
  React.useEffect(() => {
    setSlashActiveIndex(0);
  }, [value]);

  const runSlashCommand = (cmd, arg) => {
    setSlashDismissedFor(null);
    if (typeof onChange === "function") onChange("");
    if (cmd && typeof cmd.run === "function") cmd.run(arg);
  };

  // Argument-taking commands (e.g. `/agent`) fill in the word + a
  // trailing space instead of running immediately — the operator still
  // has to type the argument, and Enter (below) runs it once they do.
  const pickSlashCommand = (cmd) => {
    if (!cmd) return;
    if (cmd.takesArg) {
      if (typeof onChange === "function") onChange(`/${cmd.name} `);
    } else {
      runSlashCommand(cmd, "");
    }
  };

  const handleComposerKeyDown = (e) => {
    if (slashMenuOpen && slashMatches.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashActiveIndex((i) => (i + 1) % slashMatches.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashActiveIndex((i) => (i - 1 + slashMatches.length) % slashMatches.length);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setSlashDismissedFor(value);
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        pickSlashCommand(slashMatches[slashActiveIndex] || slashMatches[0]);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (running) return;
      // Non-command text (including a stray leading "/" that doesn't
      // match a registered command word) sends as a normal message.
      const draft = CT_parseSlashDraft(value);
      const match = draft && slashRegistry.find(
        (c) => c.name.toLowerCase() === draft.word.toLowerCase()
      );
      if (draft && match) {
        runSlashCommand(match, draft.arg.trim());
        return;
      }
      if (typeof onSend === "function") onSend();
    }
  };

  // Drag-and-drop onto the textarea forwards the dropped files through
  // the same `onAttach` callback the hidden file input uses — the cap +
  // encoding logic (handleFilesPicked, <Conversation>) is unchanged.
  const handleDrop = (e) => {
    e.preventDefault();
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length && typeof onAttach === "function") onAttach(files);
  };

  // Pasting an image/file onto the textarea attaches it the same way.
  const handlePaste = (e) => {
    const files = e.clipboardData && e.clipboardData.files;
    if (files && files.length && typeof onAttach === "function") onAttach(files);
  };

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
        {/* Task D1 (R2): the attach control is folded into the chatbox
            itself — this wrapper gives the textarea a `position:
            relative` box so the attach icon can be anchored at its
            right end (`position: absolute`) instead of rendering as a
            separate bottom-left column. */}
        <div style={{ position: "relative", flex: 1, display: "flex" }}>
          <textarea
            className="textarea"
            value={value}
            onChange={(e) => typeof onChange === "function" && onChange(e.target.value)}
            placeholder={disabled ? "This chat has ended." : "Send a message…"}
            rows={2}
            style={{ flex: 1, resize: "none", paddingRight: 34 }}
            disabled={disabled}
            onKeyDown={handleComposerKeyDown}
            onPaste={handlePaste}
            onDrop={handleDrop}
            onDragOver={(e) => e.preventDefault()}
          />
          <button
            type="button"
            title="Attach files (images, PDFs)"
            data-testid="chat-attach-btn"
            onClick={() => fileInputRef.current && fileInputRef.current.click()}
            disabled={disabled}
            style={{
              position: "absolute",
              right: 6,
              bottom: 6,
              background: "transparent",
              border: "none",
              borderRadius: 6,
              padding: 4,
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
          {/* Slash-command menu (Task D2) — anchored above the textarea,
              same popover pattern as CT_AgentSwitcher's picker. Only the
              commands whose name starts with what's typed so far show up,
              so the list narrows as the operator keeps typing. */}
          {slashMenuOpen && slashMatches.length > 0 && (
            <div
              data-testid="chat-slash-menu"
              className="popover"
              style={{
                position: "absolute",
                left: 0,
                bottom: "100%",
                marginBottom: 6,
                zIndex: 50,
                width: 280,
                maxHeight: 220,
                overflow: "auto",
                background: "var(--bg-1)",
                border: "1px solid var(--border)",
                borderRadius: 8,
                padding: 4,
                boxShadow: "0 6px 24px rgba(0,0,0,.3)",
              }}
            >
              {slashMatches.map((c, i) => (
                <button
                  key={c.name}
                  type="button"
                  data-testid={`chat-slash-item-${c.name}`}
                  className="menu-item"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => pickSlashCommand(c)}
                  style={{
                    display: "block",
                    width: "100%",
                    textAlign: "left",
                    padding: "6px 8px",
                    borderRadius: 6,
                    background: i === slashActiveIndex ? "var(--bg-active, rgba(255,255,255,.08))" : "transparent",
                  }}
                >
                  <div className="mono">/{c.name}</div>
                  {c.hint ? <div className="muted text-sm">{c.hint}</div> : null}
                </button>
              ))}
            </div>
          )}
        </div>
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
