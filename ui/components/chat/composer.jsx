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
// leading "/") sends as a normal message.
//
// Task D3: @-mentions. Unlike "/", "@" can appear anywhere in the
// draft (not just at the start), so the trigger is cursor-relative —
// it looks backward from the caret for the nearest "@" that starts a
// whitespace-bounded token. `mentionSources` arrives as a flat,
// already-fetched `{type, id, label, hint}` array built in
// conversation.jsx (agents via GET /agents, sessions via the chats
// list, files via this chat's attachments) — this shell stays pure
// (no network calls or WebSocket code live here either); it only
// filters by prefix, renders a keyboard-navigable popover mirroring
// the slash menu, and on selection splices a structured `@type:id`
// ref token into the text at the trigger's position.
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
// `wsState` (Task G1, §4.5) is NOT part of the send-gate — a brief WS
// reconnect no longer hard-disables Send; <Conversation>'s
// `sendMessage` queues the frame and flushes it once the socket
// reopens. This prop only drives a legibility hint (a small line above
// the input row) so the operator understands why their message hasn't
// visibly landed yet.
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

// Finds the "@" mention token the caret currently sits inside, if any.
// Looks backward from `cursor` for the nearest "@" that starts a
// whitespace-bounded token (start-of-string or preceded by
// whitespace) and returns its position + the text typed since it —
// e.g. for "hey @cla|" (caret at "|") this returns
// { start: 4, query: "cla" }. Returns null once whitespace appears
// between the "@" and the caret (the operator has moved past the
// token) or when there's no "@" to find at all.
function CT_activeMentionQuery(text, cursor) {
  const raw = String(text || "");
  const pos = Math.max(0, Math.min(cursor || 0, raw.length));
  const upTo = raw.slice(0, pos);
  const at = upTo.lastIndexOf("@");
  if (at === -1) return null;
  const before = at === 0 ? "" : upTo[at - 1];
  if (before && !/\s/.test(before)) return null;
  const query = upTo.slice(at + 1);
  if (/\s/.test(query)) return null;
  return { start: at, query };
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
  wsState,
}) {
  const fileInputRef = React.useRef(null);
  const textareaRef = React.useRef(null);
  const hasAttachments = Array.isArray(attachments) && attachments.length > 0;
  const sendDisabled = disabled || schemaInvalid || (!String(value || "").trim() && !hasAttachments);
  // Task G1 (§4.5): connection legibility for the composer itself — a
  // brief WS reconnect no longer hard-disables Send (see sendMessage's
  // queue-on-reconnect in conversation.jsx); this hint is the only
  // visible difference, so the operator understands WHY their message
  // hasn't landed yet instead of assuming Send silently failed.
  const wsNotOpen = !!wsState && wsState !== "open";

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

  // ---- @-mentions (Task D3) ----------------------------------------------
  // `cursorPos` mirrors the textarea's own caret position — unlike the
  // slash menu (only relevant at the very start of the draft), an "@"
  // mention can be typed anywhere, so the trigger has to be computed
  // relative to where the caret actually is, not just the string as a
  // whole. Every handler that can move the caret (typing, click,
  // arrow-key navigation, selection) keeps this in sync.
  const [cursorPos, setCursorPos] = React.useState(0);
  const [mentionDismissedFor, setMentionDismissedFor] = React.useState(null);
  const [mentionActiveIndex, setMentionActiveIndex] = React.useState(0);

  const mentionSourceList = Array.isArray(mentionSources) ? mentionSources : [];
  const mentionTrigger = CT_activeMentionQuery(value, cursorPos);
  const mentionMenuOpen =
    !!mentionTrigger && !disabled &&
    mentionDismissedFor !== value && mentionSourceList.length > 0;

  // Debounced filter query (§Deliver: "debounced fetch") — mentionSources
  // itself is a pre-fetched list (conversation.jsx owns the actual
  // /agents + /chats calls), so what's debounced here is the re-filter
  // of that list against fast typing. A *new* "@" token snaps the query
  // immediately (no stale matches from whatever was typed before); only
  // further keystrokes within the same token are debounced.
  const [debouncedMentionQuery, setDebouncedMentionQuery] = React.useState(
    mentionTrigger ? mentionTrigger.query : ""
  );
  const mentionStart = mentionTrigger ? mentionTrigger.start : null;
  React.useEffect(() => {
    setDebouncedMentionQuery(mentionTrigger ? mentionTrigger.query : "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mentionStart]);
  React.useEffect(() => {
    if (mentionStart === null) return undefined;
    const t = setTimeout(() => setDebouncedMentionQuery(mentionTrigger.query), 120);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mentionTrigger ? mentionTrigger.query : null]);

  const mentionMatches = mentionMenuOpen
    ? mentionSourceList
        .filter((m) => (m.label || m.id || "").toLowerCase().startsWith(debouncedMentionQuery.toLowerCase()))
        .slice(0, 8)
    : [];

  // Keep the highlighted row in range as the filtered list narrows/widens.
  React.useEffect(() => {
    setMentionActiveIndex(0);
  }, [debouncedMentionQuery]);

  // Splices a structured `@type:id` ref token in place of the "@query"
  // the operator just typed, then parks the caret right after it (a
  // trailing space so typing continues as a normal word, same as the
  // slash menu's argument-taking commands).
  const pickMention = (item) => {
    if (!item || !mentionTrigger) return;
    setMentionDismissedFor(null);
    const token = `@${item.type}:${item.id} `;
    const raw = String(value || "");
    const nextValue = raw.slice(0, mentionTrigger.start) + token + raw.slice(cursorPos);
    const nextCursor = mentionTrigger.start + token.length;
    if (typeof onChange === "function") onChange(nextValue);
    setCursorPos(nextCursor);
    // The DOM node still holds the pre-insertion value/selection at this
    // point (onChange only updates the controlled prop) — wait a tick
    // for the re-render to land before moving the caret past the token.
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(nextCursor, nextCursor);
      }
    });
  };

  const updateCursorFromEvent = (e) => {
    const el = e && e.target;
    if (el && typeof el.selectionStart === "number") setCursorPos(el.selectionStart);
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
    if (mentionMenuOpen && mentionMatches.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMentionActiveIndex((i) => (i + 1) % mentionMatches.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMentionActiveIndex((i) => (i - 1 + mentionMatches.length) % mentionMatches.length);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setMentionDismissedFor(value);
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        pickMention(mentionMatches[mentionActiveIndex] || mentionMatches[0]);
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

      {/* Task G1 (§4.5): queue-on-reconnect legibility — Send stays
          enabled (see sendDisabled above, which never checks wsState);
          this is the only cue that a brief reconnect will queue the
          message instead of sending it immediately. */}
      {wsNotOpen && !disabled && (
        <div className="muted text-sm" data-testid="chat-queue-hint" style={{ fontSize: 11 }}>
          {wsState === "connecting" ? "Reconnecting…" : "Offline"} — messages will queue and send automatically once reconnected.
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
            ref={textareaRef}
            className="textarea"
            value={value}
            onChange={(e) => {
              if (typeof onChange === "function") onChange(e.target.value);
              updateCursorFromEvent(e);
            }}
            placeholder={disabled ? "This chat has ended." : "Send a message…"}
            rows={2}
            style={{ flex: 1, resize: "none", paddingRight: 44 }}
            disabled={disabled}
            onKeyDown={handleComposerKeyDown}
            onKeyUp={updateCursorFromEvent}
            onClick={updateCursorFromEvent}
            onSelect={updateCursorFromEvent}
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
            // Full-height attach affordance: spans the textarea box top-to-bottom
            // (anchored right) with the icon vertically centered, so it reads as
            // proportional to the chat box instead of a tiny bottom-corner glyph.
            style={{
              position: "absolute",
              right: 4,
              top: 0,
              bottom: 0,
              background: "transparent",
              border: "none",
              borderRadius: 6,
              padding: "0 8px",
              color: "var(--text-2)",
              cursor: disabled ? "not-allowed" : "pointer",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              opacity: disabled ? 0.5 : 1,
            }}
          >
            <Icon name="paperclip" size={18} />
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
          {/* @-mention menu (Task D3) — same popover pattern as the slash
              menu, but the trigger can appear anywhere in the draft (not
              just at position 0), so it's anchored + gated off the
              cursor-relative `mentionTrigger` instead of the whole
              string. Entries are prefix-filtered against `mentionSources`
              (agents/sessions/files, supplied by conversation.jsx). */}
          {mentionMenuOpen && mentionMatches.length > 0 && (
            <div
              data-testid="chat-mention-menu"
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
              {mentionMatches.map((m, i) => (
                <button
                  key={`${m.type}:${m.id}`}
                  type="button"
                  data-testid={`chat-mention-item-${i}`}
                  className="menu-item"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => pickMention(m)}
                  style={{
                    display: "block",
                    width: "100%",
                    textAlign: "left",
                    padding: "6px 8px",
                    borderRadius: 6,
                    background: i === mentionActiveIndex ? "var(--bg-active, rgba(255,255,255,.08))" : "transparent",
                  }}
                >
                  <div className="mono">@{m.type}:{m.label || m.id}</div>
                  {m.hint ? <div className="muted text-sm">{m.hint}</div> : null}
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
            title={wsNotOpen ? "Send (queues until reconnected)" : undefined}
            style={{ alignSelf: "stretch", paddingLeft: 16, paddingRight: 16 }}
          >Send</Btn>
        )}
      </div>
    </div>
  );
}

window.Composer = Composer;
