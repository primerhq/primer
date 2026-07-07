/* global React, Icon, Transcript, Composer, SchemaPanel */
//
// <Conversation> — the embeddable core of the chat feature (Task B2 of
// the chat-refactor plan). Owns ALL WS/data lifecycle + optimistic
// echo that used to live inline in ChatDetail (ui/components/chats.jsx):
// initial REST tail-load, WS connect/reconnect/backoff + cursor resume,
// usage/compaction envelope handling, lazy-load-older, auto-scroll,
// sendMessage, and the file-attachment read/encode pipeline that feeds
// the composer.
//
// ChatDetail is now a thin host: it renders page chrome (title/back
// button/status pills/TokenMeter) and mounts this component for the
// scrollable transcript + composer. Status that the chrome needs but
// that now lives in here (wsState, usage, compact-in-flight, a
// compact-trigger, and a 404 history error) is bubbled up via the
// `onStatus` callback rather than duplicated.
//
// Task B3 moved the single-column timeline (the row renderers Message,
// CT_ExpandableToolRow, CT_AttachmentPart, CompactionMarker,
// CT_ThinkingBubble, plus the scrollable container itself) out of this
// file into the pure <Transcript> renderer
// (ui/components/chat/transcript.jsx). This component still owns
// coalescing (window.chatCoalesce) and hands the result to <Transcript>
// as props — no data fetching or WS in that file.
//
// Task B4 moved the input surface (textarea + attachment strip + send
// control) out of this file into the pure <Composer> shell
// (ui/components/chat/composer.jsx) and added the collapsible
// <SchemaPanel> shell (ui/components/chat/schema-panel.jsx, R3) as an
// optional right-hand sibling, gated by `showSchemaPanel`. This
// component still owns the composer text/attachments state and the
// send/attach handlers — <Composer> is a controlled, pure-rendering
// shell. Real Send/Stop cancel wiring (turn_status-driven `running` +
// POST /cancel) lands in Task C2; for now `running`/`onStop` are inert
// placeholders so the shell's interface is locked in ahead of that
// phase. Task F2 filled in the Schema Builder/JSON bodies
// (schema-panel.jsx) and wired persistent/ephemeral application here:
// Persistent ON PUTs Chat.response_format (A3); OFF rides the next
// send frame only (see sendMessage's `frame.response_format`).
//
// No viewport-relative (vh/dvh) height in this file (per §3) — the
// component fills its flex parent (height:100%/flex:1); the host owns
// viewport-relative sizing.

function Conversation({ chatId, headerSlot, rightChromeSlot, showSchemaPanel, onCloseSchemaPanel, onStatus, pushToast }) {
  const { useResource, useViewport, apiFetch, useRouter } = window.primerApi;
  const { isMobile } = useViewport();
  const { navigate } = useRouter();
  const cid = chatId;

  // Chat row (status, agent_id) — small one-shot fetch + light polling
  // so the composer's ended-state gating and the agent switcher's
  // label mirror a server-driven "ended" state. Shares the useResource
  // cache entry with the host's own `chat-detail:${cid}` fetch (same
  // key), so this does NOT double the network traffic.
  const chat = useResource(
    `chat-detail:${cid}`,
    (s) => apiFetch("GET", `/chats/${encodeURIComponent(cid)}`, null, { signal: s }),
    { pollMs: 10000, deps: [cid] }
  );
  const chatRow = chat.data;
  const chatStatus = chatRow?.status || "active";
  const chatAgent = chatRow?.agent_id || "—";
  // Drives the composer's Send/Stop control (Task C2) — mirrors
  // <Transcript>'s own turnInFlight calc (turn_status claimable/running)
  // since both need the same "a turn is in flight" signal.
  const turnInFlight = chatRow?.turn_status === "claimable" || chatRow?.turn_status === "running";

  // studio-ux fix 3: a chat with no `response_format` of its own still
  // effectively runs under the AGENT's build-time response_format (per-turn
  // precedence is agent default -> chat override -> ephemeral send-frame).
  // Fetched only while the schema panel is open and the chat is bound to a
  // real agent id, mirroring the guarded-useResource convention in
  // agents.jsx's AG_ReferencesPanel (cache key/fetcher both fall back to a
  // no-op "none" entry rather than skipping the hook). SchemaPanel decides
  // whether to actually SHOW this — it only applies while the chat has no
  // override of its own (`schemaValue == null`).
  const agentIdForSchema = chatRow?.agent_id || null;
  const agentDetail = useResource(
    showSchemaPanel && agentIdForSchema ? `agent-detail:${agentIdForSchema}` : "agent-detail:none",
    (s) =>
      showSchemaPanel && agentIdForSchema
        ? apiFetch("GET", `/agents/${encodeURIComponent(agentIdForSchema)}`, null, { signal: s })
        : Promise.resolve(null),
    { deps: [showSchemaPanel, agentIdForSchema] }
  );
  const agentResponseFormat =
    agentDetail.data && agentDetail.data.response_format != null ? agentDetail.data.response_format : null;

  const [messages, setMessages] = React.useState([]);
  const [lastSeq, setLastSeq] = React.useState(0);
  // Oldest seq currently loaded; null until the initial tail fetch
  // completes. Drives the scroll-up lazy-loader's `before_seq` cursor.
  const [oldestSeq, setOldestSeq] = React.useState(null);
  // false once the tail fetch (or a later older-page fetch) returns
  // fewer rows than asked for — we've hit the top of history.
  const [hasMoreOlder, setHasMoreOlder] = React.useState(false);
  const [loadingOlder, setLoadingOlder] = React.useState(false);
  // Set by the initial-load effect when the first batch is in the
  // store; gates the WebSocket open so the WS cursor can be set to
  // the highest already-loaded seq and skip a redundant full replay.
  const [initialLoadedSeq, setInitialLoadedSeq] = React.useState(null);
  const [wsState, setWsState] = React.useState("connecting");
  const [composer, setComposer] = React.useState("");
  const [historyError, setHistoryError] = React.useState(null);
  // Live token-usage snapshot — driven by `"usage"` WS envelopes the
  // worker emits after each turn. Drives the header TokenMeter pill
  // (rendered by the host, fed via onStatus); values stay at 0 until
  // the first envelope lands so the meter renders dimmed but present.
  const [usage, setUsage] = React.useState({ input_tokens: 0, output_tokens: 0, context_length: 0 });
  const [compactInFlight, setCompactInFlight] = React.useState(false);
  // Set true the moment the user sends a frame; cleared when any
  // assistant_token / tool_call / done / error row arrives. Drives
  // the "Thinking..." placeholder so the operator sees the system
  // is responding before the first delta lands.
  const [waitingForReply, setWaitingForReply] = React.useState(false);
  // Pending file attachments. Each entry: {id, file, name, mime, kind, dataB64, preview}.
  const [attachments, setAttachments] = React.useState([]);
  const wsRef = React.useRef(null);
  const scrollRef = React.useRef(null);
  // Task G1 (§4.5): queue-on-reconnect — outbound frames buffered here
  // while the socket is not open (connecting/closed) and flushed in
  // order the instant it reopens (see ws.onopen below), instead of
  // hard-rejecting the send. Reset on chat switch (the tail-load effect
  // below) — a queued frame belongs to the PREVIOUS chat's socket, not
  // the new one.
  const outboxRef = React.useRef([]);
  // <SchemaPanel> (R3, Task F2) local state. `schemaValue`/`schemaValid`
  // are driven by <SchemaPanel>'s Builder/JSON tabs (JSON is the source
  // of truth there); this component owns applying them: persistent ON
  // pushes to the server via PUT (below), OFF rides the next send frame
  // only (see sendMessage's `frame.response_format`).
  const [schemaValue, setSchemaValue] = React.useState(null);
  const [schemaPersistent, setSchemaPersistent] = React.useState(false);
  const [schemaValid, setSchemaValid] = React.useState(true);
  // studio-ux fix 2: the panel used to ALSO start collapsed internally
  // (a second, redundant expand step behind the "⚙ schema" chip's own
  // mount/unmount toggle — see showSchemaPanel above). <SchemaPanel> is now
  // always rendered fully open the moment it's mounted; its own header
  // chevron (still useful as a quick "hide" control) closes the panel
  // outright via onCloseSchemaPanel instead of re-collapsing in place.

  // One-time hydration: once the chat row's own fetch resolves, seed
  // the panel from any existing per-chat persistent schema (A1's
  // `Chat.response_format`) so reopening a chat that already has one
  // shows the toggle ON with its JSON populated, instead of looking
  // reset to empty/OFF. Guarded by a ref (not state) so the chat row's
  // 10s poll (above) never stomps on an operator's in-progress edit.
  const schemaHydratedRef = React.useRef(false);
  React.useEffect(() => {
    if (!showSchemaPanel) return;
    if (schemaHydratedRef.current || !chatRow) return;
    schemaHydratedRef.current = true;
    if (chatRow.response_format != null) {
      setSchemaValue(chatRow.response_format);
      setSchemaPersistent(true);
    }
  }, [showSchemaPanel, chatRow]);

  // Persistent toggle ON (§8.3/R3): PUT the current schema so it
  // constrains every subsequent turn on this chat (A3). OFF: clear the
  // per-chat override immediately (PUT {schema: null}) — it reverts to
  // next-turn-only, applied instead via the ephemeral send-frame path
  // in sendMessage below.
  const handleSchemaPersistentChange = React.useCallback((next) => {
    setSchemaPersistent(next);
    if (next && !schemaValid) {
      if (typeof pushToast === "function") {
        pushToast({ kind: "error", title: "Cannot persist an invalid schema" });
      }
      return;
    }
    apiFetch("PUT", `/chats/${encodeURIComponent(cid)}/response_format`, {
      schema: next ? schemaValue : null,
    }).catch((err) => {
      if (typeof pushToast === "function") {
        pushToast({
          kind: "error",
          title: err?.title || "Schema update failed",
          detail: err?.detail || err?.message,
          requestId: err?.requestId,
        });
      }
    });
  }, [cid, apiFetch, pushToast, schemaValid, schemaValue]);

  // Keeps the persisted schema in sync with further Builder/JSON edits
  // made while Persistent is already ON (debounced so keystrokes in the
  // JSON tab don't spam the endpoint). The toggle flip itself is
  // handled immediately above; this only covers subsequent edits.
  const schemaPersistTimerRef = React.useRef(null);
  React.useEffect(() => {
    if (!showSchemaPanel) return undefined;
    if (!schemaPersistent || !schemaValid) return undefined;
    if (schemaPersistTimerRef.current) clearTimeout(schemaPersistTimerRef.current);
    schemaPersistTimerRef.current = setTimeout(() => {
      apiFetch("PUT", `/chats/${encodeURIComponent(cid)}/response_format`, { schema: schemaValue }).catch((err) => {
        if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err?.title || "Schema update failed",
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      });
    }, 500);
    return () => { if (schemaPersistTimerRef.current) clearTimeout(schemaPersistTimerRef.current); };
  }, [showSchemaPanel, schemaValue, schemaPersistent, schemaValid, cid, apiFetch, pushToast]);

  // Initial REST load — fetches the TAIL of the history so the
  // renderer can scroll straight to the bottom without dragging
  // through thousands of older rows. The WS then opens with
  // cursor=<lastSeq> below so it streams only new messages, not a
  // redundant full replay. Older rows lazy-load on scroll-up.
  //
  // Page size is 200 (the server pagination cap) and we keep paging
  // BACKWARDS until either:
  //   - we've crossed at least one `user_message` (so the operator
  //     sees the prior turn, not just the tail of one long response —
  //     each LLM token streams as its own assistant_token row, so a
  //     single reply can span 100+ rows and a naive single-page
  //     tail-load returns only fragments of that one message); OR
  //   - we've exhausted the chat; OR
  //   - we've hit the safety cap of TAIL_MAX_PAGES iterations.
  const TAIL_PAGE_SIZE = 200;
  const TAIL_MAX_PAGES = 6; // ~1200 rows max on initial load
  // Pagination ceiling is 2**53 - 1; the server caps int Query at the
  // 64-bit boundary, but Number.MAX_SAFE_INTEGER is unambiguous and
  // matches every persisted seq.
  const SENTINEL_TAIL_SEQ = Number.MAX_SAFE_INTEGER;
  React.useEffect(() => {
    let cancelled = false;
    setMessages([]);
    setLastSeq(0);
    setOldestSeq(null);
    setHasMoreOlder(false);
    setInitialLoadedSeq(null);
    outboxRef.current = [];
    (async () => {
      try {
        let collected = []; // prepended each iteration so it stays ASC
        let cursor = SENTINEL_TAIL_SEQ;
        let hasMore = true;
        let foundUserMsg = false;
        for (let i = 0; i < TAIL_MAX_PAGES; i++) {
          const data = await apiFetch(
            "GET",
            `/chats/${encodeURIComponent(cid)}/messages?before_seq=${cursor}&limit=${TAIL_PAGE_SIZE}`,
          );
          if (cancelled) return;
          const items = (data && data.items) || [];
          if (items.length === 0) {
            hasMore = false;
            break;
          }
          // REST returns ChatMessage rows with kind-specific fields
          // nested under `payload`. WS frames spread payload into the
          // top-level (see chats router _message_to_wire). Flatten on
          // load to keep both sources homogeneous.
          const flat = window.chatFlatten(items);
          collected = [...flat, ...collected];
          foundUserMsg = foundUserMsg || flat.some((r) => r.kind === "user_message");
          if (items.length < TAIL_PAGE_SIZE) {
            hasMore = false;
            break;
          }
          if (foundUserMsg) {
            break; // We've reached at least one full turn — operator can see context.
          }
          cursor = flat[0].seq || cursor;
        }
        setMessages(collected);
        if (collected.length > 0) {
          const last = collected[collected.length - 1].seq || 0;
          const first = collected[0].seq || 0;
          setLastSeq(last);
          setOldestSeq(first);
          setHasMoreOlder(hasMore);
          setInitialLoadedSeq(last);
        } else {
          // Empty chat — open WS with cursor 0 to catch the very
          // first message that may land between mount and any send.
          setInitialLoadedSeq(0);
        }
      } catch (err) {
        if (cancelled) return;
        setHistoryError(err);
        // Fall through to opening the WS at cursor 0 so the user can
        // still send and see new messages even if history failed.
        setInitialLoadedSeq(0);
      }
    })();
    return () => { cancelled = true; };
  }, [cid]); // eslint-disable-line react-hooks/exhaustive-deps

  // Lazy-load older messages on scroll-up. Captures scroll geometry
  // before the prepend; a useLayoutEffect (below) restores the
  // visible position synchronously between React's commit and the
  // browser's paint, so the user never sees the intermediate
  // (wrong-scroll) frame.
  const loadingOlderRef = React.useRef(false);
  // When non-null, the next layout effect should snap scrollTop so
  // the previously-visible content stays put. Shape:
  // { oldScrollHeight: number, oldScrollTop: number }.
  const pendingPrependRef = React.useRef(null);
  const loadOlder = React.useCallback(async () => {
    if (loadingOlderRef.current) return;
    if (!hasMoreOlder || oldestSeq == null || oldestSeq <= 1) return;
    loadingOlderRef.current = true;
    setLoadingOlder(true);
    try {
      const data = await apiFetch(
        "GET",
        `/chats/${encodeURIComponent(cid)}/messages?before_seq=${oldestSeq}&limit=${TAIL_PAGE_SIZE}`,
      );
      const items = (data && data.items) || [];
      if (items.length === 0) {
        setHasMoreOlder(false);
        return;
      }
      const flat = window.chatFlatten(items);
      // Capture scroll geometry RIGHT before triggering the prepend
      // re-render. The layout effect (keyed on messages.length) reads
      // this ref synchronously after React commits and adjusts
      // scrollTop before the browser paints — no visible jump.
      const el = scrollRef.current;
      if (el) {
        pendingPrependRef.current = {
          oldScrollHeight: el.scrollHeight,
          oldScrollTop: el.scrollTop,
        };
      }
      setMessages((prev) => [...flat, ...prev]);
      setOldestSeq(flat[0].seq || oldestSeq);
      setHasMoreOlder(items.length === TAIL_PAGE_SIZE);
    } catch (err) {
      if (typeof pushToast === "function") {
        pushToast({ kind: "error", title: "Load older failed", detail: err?.message || "" });
      }
    } finally {
      setLoadingOlder(false);
      loadingOlderRef.current = false;
    }
  }, [cid, oldestSeq, hasMoreOlder, apiFetch, pushToast]);

  // Synchronous scroll-position restoration after a prepend. Runs
  // post-commit, pre-paint — so the browser paints exactly once with
  // the correct scrollTop, and the previously-visible content stays
  // pinned to the same on-screen pixel row.
  React.useLayoutEffect(() => {
    const pending = pendingPrependRef.current;
    if (pending == null) return;
    pendingPrependRef.current = null;
    const el = scrollRef.current;
    if (!el) return;
    const delta = el.scrollHeight - pending.oldScrollHeight;
    el.scrollTop = pending.oldScrollTop + delta;
  }, [messages.length]);

  // WS lifecycle — opens once the initial REST tail-load has settled,
  // using the tail's highest seq as the cursor so the server only
  // streams NEW frames (no redundant full-history replay). The init
  // gate (`initialLoadedSeq != null`) tolerates an empty-chat or
  // failed-load fallback to cursor 0.
  //
  // Reconnect: an unexpected close (network blip, server restart)
  // triggers exponential-backoff reconnect (1s -> 2s -> 4s ... 30s cap).
  // Reconnection reuses the last received seq as the cursor so no
  // frames are missed or duplicated. Terminal close codes (4404, 4410)
  // do not reconnect.
  React.useEffect(() => {
    if (initialLoadedSeq == null) return;
    let intentional = false;
    let backoffMs = 1000;
    const MAX_BACKOFF_MS = 30000;
    let reconnectTimer = null;
    // Track the highest seq received in this effect's lifetime.
    // Starts at initialLoadedSeq so the first connection opens with the
    // correct cursor; updated by onmessage so reconnects resume from the
    // last received frame.
    let latestSeq = initialLoadedSeq;

    function connect() {
      if (intentional) return;
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${window.location.host}/v1/chats/${encodeURIComponent(cid)}/ws?cursor=${latestSeq}`;
      let ws;
      try {
        ws = new WebSocket(url);
      } catch (e) {
        setWsState("closed");
        return;
      }
      wsRef.current = ws;
      setWsState("connecting");

      ws.onopen = () => {
        setWsState("open");
        backoffMs = 1000; // reset on successful connect
        // Task G1 (§4.5): flush any frames queued while the socket was
        // not open (queue-on-reconnect) — in order, exactly once, on
        // this exact socket instance (the one that just opened).
        if (outboxRef.current.length > 0) {
          const queued = outboxRef.current;
          outboxRef.current = [];
          for (const frame of queued) {
            try { ws.send(JSON.stringify(frame)); } catch { /* dropped; nothing to reconcile against a frame that never reached the server */ }
          }
          setWaitingForReply(true);
        }
      };

      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (!msg || typeof msg !== "object") return;

        if (msg.kind === "error" && typeof msg.seq !== "number") {
          // Protocol-level error frame (not a persisted row).
          if (typeof pushToast === "function") {
            pushToast({
              kind: "error",
              title: msg.code || "WebSocket error",
              detail: msg.message || "",
            });
          }
          return;
        }
        if (msg.kind === "pong") return;

        // Token usage envelope (no seq). Emitted by the worker after each
        // assistant turn. Snapshot drives the header TokenMeter pill.
        if (msg.kind === "usage" && typeof msg.seq !== "number") {
          setUsage({
            input_tokens: Number(msg.input_tokens) || 0,
            output_tokens: Number(msg.output_tokens) || 0,
            context_length: Number(msg.context_length) || 0,
          });
          return;
        }

        // Compaction envelope (no seq). Server tells us a compaction
        // pass just happened; surface in three places so the operator
        // sees it: an in-stream marker row, a success toast, and an
        // immediate TokenMeter update reflecting the new prompt size.
        //
        // Field names mirror the server envelope shape
        // (tokens_before, tokens_after) per
        // primer/api/routers/chats.py::_compaction_envelope.
        //
        // tokens_after IS the post-compaction context size - per
        // primer/agent/compaction.py::_full_compact + _estimate_tokens,
        // it is computed over the FULL new history (summary + retained
        // tail), not just the summary payload. Pinning the meter to it
        // is correct: that's the prompt the next assistant turn carries.
        // Compaction envelope. The server translates the persisted
        // compaction_marker row into this 'compaction' envelope and sends
        // it WITH the row's seq (primer/api/routers/chats.py::
        // _compaction_envelope). Handle it regardless of whether a seq is
        // present so the marker, the TokenMeter update, and the toast all
        // fire. Earlier code required a missing seq and so silently
        // dropped every server-sent compaction (the meter never moved and
        // no completion marker appeared).
        if (msg.kind === "compaction") {
          const beforeT = Number(msg.tokens_before) || 0;
          const afterT = Number(msg.tokens_after) || 0;
          const markerSeq = typeof msg.seq === "number"
            ? msg.seq
            : `compaction-${Date.now()}`;
          // Clear the in-progress flag and append the completion marker
          // (de-duped by seq against the cursor replay).
          setCompactInFlight(false);
          setMessages((prev) => prev.some((m) => m.seq === markerSeq)
            ? prev
            : [...prev, {
                kind: "compaction_marker",
                seq: markerSeq,
                tokens_before: beforeT,
                tokens_after: afterT,
                reason: msg.reason || "",
              }]);
          // Update the context meter to the post-compaction prompt size so
          // the top-right indicator reflects the smaller window immediately.
          if (afterT > 0) {
            setUsage((prev) => ({
              ...prev,
              input_tokens: afterT,
            }));
          }
          if (typeof pushToast === "function") {
            const saved = beforeT > 0 ? Math.max(0, beforeT - afterT) : 0;
            pushToast({
              kind: "success",
              title: "Compaction complete",
              detail: beforeT > 0
                ? `${beforeT.toLocaleString()} -> ${afterT.toLocaleString()} tokens`
                  + (saved > 0 ? ` (saved ${saved.toLocaleString()})` : "")
                : null,
            });
          }
          return;
        }

        // Any persisted row carries seq → append + advance cursor. We
        // de-dupe against the initial REST replay because both sources
        // may overlap (REST loaded seqs 1..N, WS replays seq>0).
        if (typeof msg.seq === "number") {
          if (msg.seq > latestSeq) latestSeq = msg.seq;
          setMessages((prev) => {
            if (prev.some((p) => p.seq === msg.seq)) return prev;
            // Reconcile the optimistic echo (Task C2): the persisted
            // user_message row for what we just sent has arrived. Swap
            // the pending placeholder for the real row IN PLACE (same
            // text wins; the oldest still-pending row is the fallback so
            // one straggler doesn't leave a phantom bubble behind)
            // rather than appending a duplicate — storage stays truth,
            // this only changes which row stands in for it on screen.
            if (msg.kind === "user_message") {
              let pendingIdx = prev.findIndex(
                (p) => p.pending && p.kind === "user_message" && (p.content || "") === (msg.content || "")
              );
              if (pendingIdx === -1) {
                pendingIdx = prev.findIndex((p) => p.pending && p.kind === "user_message");
              }
              if (pendingIdx !== -1) {
                const next = prev.slice();
                next[pendingIdx] = msg;
                return next;
              }
            }
            return [...prev, msg];
          });
          setLastSeq((prev) => (msg.seq > prev ? msg.seq : prev));
          // The agent is now producing output (or finished); drop the
          // thinking placeholder. user_message echoes from a previous
          // turn don't reach here because we only get rows after the
          // server processes our outbound frame.
          if (msg.kind !== "user_message") {
            setWaitingForReply(false);
          }
        }
      };

      ws.onclose = (ev) => {
        wsRef.current = null;
        setWsState("closed");
        // Terminal codes: do not reconnect.
        if (ev.code === 4404) {
          if (typeof pushToast === "function") {
            pushToast({ kind: "error", title: "Chat not found", detail: ev.reason || cid });
          }
          return;
        }
        if (ev.code === 4410) {
          if (typeof pushToast === "function") {
            pushToast({ kind: "warning", title: "Chat ended", detail: ev.reason || cid });
          }
          return;
        }
        // Unexpected close - reconnect with exponential backoff.
        if (!intentional) {
          reconnectTimer = setTimeout(() => {
            backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
            connect();
          }, backoffMs);
        }
      };

      ws.onerror = () => {
        // Browsers report a generic ErrorEvent and then close - onclose
        // handles user-facing messaging and reconnect scheduling.
      };
    }

    connect();

    return () => {
      intentional = true;
      if (reconnectTimer != null) clearTimeout(reconnectTimer);
      try { wsRef.current && wsRef.current.close(); } catch { /* no-op */ }
      wsRef.current = null;
    };
  }, [cid, initialLoadedSeq]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-scroll to bottom only when the tail grows (initial load or
  // a live frame). `lastSeq` is monotone-increasing on appends, so
  // depending on it instead of `messages` means scroll-up prepends
  // (which keep lastSeq unchanged) don't yank the user back down.
  const stickToBottomRef = React.useRef(true);
  const onScroll = React.useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distance < 80;
    // Near the top → fetch the next older page. The 100px threshold
    // gives a small buffer so the user sees the loading indicator
    // before they bottom-out the scroll.
    if (el.scrollTop < 100) {
      loadOlder();
    }
  }, [loadOlder]);
  React.useEffect(() => {
    if (!scrollRef.current || !stickToBottomRef.current) return;
    const el = scrollRef.current;
    const raf = requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(raf);
  }, [lastSeq, waitingForReply]);

  // Build the WS frame's `parts` list from the composer's pending
  // attachments. Shared by sendMessage (the real frame) and
  // onSubmitComposer's optimistic echo (Task C2) so the synthetic
  // preview row renders identically to what's actually on the wire.
  const partsForAttachments = (atts) => atts.map((a) => ({
    type: a.kind,           // "image" or "document"
    data: a.dataB64,
    mime_type: a.mime,
    ...(a.kind === "document" && a.name ? { filename: a.name } : {}),
  }));

  // Returns true if the frame was accepted — either sent live or queued
  // for the next reconnect (Task G1, queue-on-reconnect). The composer
  // no longer needs to hard-reject solely because the socket is
  // momentarily not open; a brief reconnect is exactly what the WS
  // effect's own backoff loop above is for. `false` is reserved for a
  // caller precondition failure (none currently — kept for API
  // symmetry with onSubmitComposer's `if (sent)` gate).
  const sendMessage = (text, atts) => {
    const ws = wsRef.current;
    const frame = { kind: "user_message" };
    if (text) frame.content = text;
    if (atts && atts.length > 0) {
      frame.parts = partsForAttachments(atts);
    }
    // R3 ephemeral structured-output override (A3): only this one turn
    // — Persistent ON is already enforced server-side via
    // Chat.response_format (handleSchemaPersistentChange /
    // the debounced PUT effect above), so it needn't ride every frame.
    if (showSchemaPanel && !schemaPersistent && schemaValid && schemaValue) {
      frame.response_format = schemaValue;
    }
    if (!ws || ws.readyState !== 1) {
      // Task G1 (§4.5): queue instead of hard-rejecting — buffered here
      // and flushed in ws.onopen the instant the socket reopens, rather
      // than dropping the message and forcing the operator to notice a
      // toast and retype/resend.
      outboxRef.current.push(frame);
      if (typeof pushToast === "function") {
        pushToast({
          kind: "info",
          title: "Message queued",
          detail: "Will send automatically once reconnected.",
        });
      }
      return true;
    }
    ws.send(JSON.stringify(frame));
    setWaitingForReply(true);
    return true;
  };

  // Clear the composer and attachments only after a successful send so a
  // failed send (WS not open) leaves the user's text intact. On success,
  // also push an optimistic echo (Task C2): a synthetic user_message row
  // with a client id + a "sending" tick (rendered by <Transcript>'s
  // Message), so the operator sees their own message the instant they
  // hit Send instead of waiting on the WS round-trip. It's reconciled/
  // deduped against the persisted row (same text, real seq) once that
  // arrives — see the onmessage handler above; storage stays truth.
  const onSubmitComposer = () => {
    const text = composer.trim();
    if (!text && attachments.length === 0) return;
    const atts = attachments;
    const sent = sendMessage(text, atts);
    if (sent) {
      const clientId = `client-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      setMessages((prev) => [...prev, {
        kind: "user_message",
        content: text || undefined,
        parts: atts.length > 0 ? partsForAttachments(atts) : undefined,
        pending: true,
        clientId,
        created_at: new Date().toISOString(),
      }]);
      setComposer("");
      setAttachments([]);
    }
  };

  // Operator-triggered compaction. POSTs to the chat's compact
  // endpoint; the server runs the configured compaction prompt
  // against the conversation and emits a `"compaction"` envelope on
  // the WS once finished — that envelope flows through onmessage
  // above and appends a `compaction_marker` row to the timeline.
  //
  // Exposed to the host (which renders the TokenMeter's Compact
  // button as page chrome) via the `requestCompact` field of the
  // onStatus bag below, rather than as a named prop, since the
  // compact-in-flight state and the WS envelope that clears it both
  // live in here.
  const handleCompact = React.useCallback(async () => {
    if (compactInFlight) return;
    setCompactInFlight(true);
    try {
      await apiFetch("POST", `/chats/${encodeURIComponent(cid)}/compact`);
      if (typeof pushToast === "function") {
        pushToast({ kind: "success", title: "Compaction started" });
      }
    } catch (err) {
      if (typeof pushToast === "function") {
        pushToast({
          kind: "error",
          title: err?.title || "Compact failed",
          detail: err?.detail || err?.message,
          requestId: err?.requestId,
        });
      }
    } finally {
      setCompactInFlight(false);
    }
  }, [cid, compactInFlight, apiFetch, pushToast]);

  // Task F3 (R4): compaction guard for the rewind affordance — the
  // highest `seq` among loaded `compaction_marker` rows. <Transcript>
  // gates the rewind icon per-message against this boundary (only
  // rendered on a user_message whose seq is AFTER it) rather than
  // re-scanning `messages` itself, mirroring the server's own guard
  // (A7 422s a rewind at/behind this same seq — rewinding into
  // compacted history would desync the marker's summary from the
  // history it replaced).
  const compactionBoundarySeq = React.useMemo(() => {
    let boundary = 0;
    for (const m of messages) {
      if (m.kind === "compaction_marker" && typeof m.seq === "number" && m.seq > boundary) {
        boundary = m.seq;
      }
    }
    return boundary;
  }, [messages]);

  // Task F3 (R4): rewind to an earlier user message. <Transcript>'s
  // CT_RewindButton already confirmed (window.confirm) and gated on the
  // compaction boundary + turn_status before calling this — this just
  // POSTs A7's truncation endpoint and drops the locally-held rows after
  // the kept seq (cheaper than a full REST refetch; the server's success
  // response already tells us exactly what was kept/deleted, and A7's
  // pre-flight guards mean there's nothing to reconcile on failure).
  const handleRewind = React.useCallback(async (seq) => {
    try {
      const result = await apiFetch("POST", `/chats/${encodeURIComponent(cid)}/rewind`, { seq });
      const keptSeq = (result && typeof result.truncated_to_seq === "number") ? result.truncated_to_seq : seq;
      setMessages((prev) => prev.filter((m) => typeof m.seq !== "number" || m.seq <= keptSeq));
      setLastSeq(keptSeq);
      if (typeof pushToast === "function") {
        pushToast({
          kind: "success",
          title: "Rewound",
          detail: `Discarded ${result?.deleted ?? 0} message(s)`,
        });
      }
    } catch (err) {
      if (typeof pushToast === "function") {
        pushToast({
          kind: "error",
          title: err?.title || "Rewind failed",
          detail: err?.detail || err?.message,
          requestId: err?.requestId,
        });
      }
    }
  }, [cid, apiFetch, pushToast]);

  // Stop button (Task C2, backend A6) — POSTs the REST cancel endpoint
  // rather than sending a WS `interrupt` frame so it works even if the
  // socket has dropped. A 409 means the turn already finished/idled
  // between the click and the request landing (a race with the worker
  // heartbeat) — nothing to surface, the composer flips back to Send on
  // the next chat-row poll regardless. Any other failure gets a toast.
  const handleStop = React.useCallback(async () => {
    try {
      await apiFetch("POST", `/chats/${encodeURIComponent(cid)}/cancel`);
    } catch (err) {
      if (err?.status === 409) return;
      if (typeof pushToast === "function") {
        pushToast({
          kind: "error",
          title: err?.title || "Stop failed",
          detail: err?.detail || err?.message,
          requestId: err?.requestId,
        });
      }
    }
  }, [cid, apiFetch, pushToast]);

  // ---- File picking + base64 encoding ----------------------------------
  // The chat protocol's user_message.parts list expects each binary part
  // to carry `data` (base64), `mime_type`, and (for documents) a
  // filename hint. Browsers read files as a Blob; FileReader gives us
  // a data URL we can split into the b64 tail.
  const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024; // 8 MiB — keeps the WS frame sane.
  const handleFilesPicked = async (fileList) => {
    if (!fileList || fileList.length === 0) return;
    const next = [...attachments];
    for (const file of Array.from(fileList)) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: "File too large",
            detail: `${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MiB; limit is 8 MiB.`,
          });
        }
        continue;
      }
      const mime = file.type || "application/octet-stream";
      const isImage = mime.startsWith("image/");
      const dataB64 = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const result = String(reader.result || "");
          const comma = result.indexOf(",");
          resolve(comma >= 0 ? result.slice(comma + 1) : result);
        };
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      next.push({
        id: `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        name: file.name,
        mime,
        kind: isImage ? "image" : "document",
        dataB64,
        preview: isImage ? `data:${mime};base64,${dataB64}` : null,
        size: file.size,
      });
    }
    setAttachments(next);
  };

  const removeAttachment = (id) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  };

  // Task D2: slash-command registry, handed to <Composer> via the
  // `slashCommands` prop it's accepted since Task B4. <Composer> only
  // renders the menu, filters by prefix, and matches on Enter — it
  // stays a pure, non-fetching shell (no apiFetch/WebSocket import
  // there); the real REST actions live here, next to the ones they
  // reuse (handleCompact above; /agent, chat creation, chat deletion
  // mirror CT_AgentSwitcher / ChatsPage in chats.jsx). Kept as a plain
  // {name, hint, run, takesArg} array — not a component — so a future
  // embedder can extend it by concatenating more entries.
  const slashCommands = React.useMemo(() => [
    {
      name: "compact",
      hint: "Compact this conversation's history",
      run: () => handleCompact(),
    },
    {
      name: "agent",
      hint: "<agent-id> — switch this chat's agent",
      takesArg: true,
      run: async (arg) => {
        const agentId = String(arg || "").trim();
        if (!agentId) {
          if (typeof pushToast === "function") {
            pushToast({ kind: "error", title: "Usage: /agent <agent-id>" });
          }
          return;
        }
        try {
          const row = await apiFetch("POST", `/chats/${encodeURIComponent(cid)}/agent`, { agent_id: agentId });
          if (typeof pushToast === "function") {
            pushToast({ kind: "success", title: "Agent switched", detail: row?.agent_id || agentId });
          }
        } catch (err) {
          if (typeof pushToast === "function") {
            pushToast({
              kind: "error",
              title: err?.title || "Switch failed",
              detail: err?.detail || err?.message,
              requestId: err?.requestId,
            });
          }
        }
      },
    },
    {
      name: "new",
      hint: "Start a new chat",
      run: () => navigate("/chats"),
    },
    {
      name: "end",
      hint: "End (delete) this chat",
      run: async () => {
        try {
          await apiFetch("DELETE", `/chats/${encodeURIComponent(cid)}?force=true`);
          if (typeof pushToast === "function") {
            pushToast({ kind: "success", title: "Chat ended", detail: cid });
          }
          navigate("/chats");
        } catch (err) {
          if (typeof pushToast === "function") {
            pushToast({
              kind: "error",
              title: err?.title || "End failed",
              detail: err?.detail || err?.message,
              requestId: err?.requestId,
            });
          }
        }
      },
    },
  ], [cid, apiFetch, pushToast, handleCompact, navigate]);

  // Task D3: @-mention sources, handed to <Composer> via the
  // `mentionSources` prop it's accepted since Task B4. Same split as
  // the slash registry above — <Composer> only filters/renders/inserts
  // (no apiFetch/WebSocket there); this component owns the actual
  // fetching: agents reuse the same `GET /agents?limit=200` call +
  // cache key `CT_AgentSwitcher` (chats.jsx) already uses, sessions
  // reuse the chats list `ChatsPage` already fetches (`chats:list`),
  // and files need no fetch at all — they come from this chat's own
  // attachments already in memory (the draft's pending ones, plus any
  // already sent earlier in the transcript's `parts`).
  const agentsForMentions = useResource(
    "agent-switcher:agents",
    (s) => apiFetch("GET", "/agents?limit=200", null, { signal: s }),
    {}
  );
  const sessionsForMentions = useResource(
    "chats:list",
    (s) => apiFetch("GET", "/chats?limit=200", null, { signal: s }),
    {}
  );
  const mentionSources = React.useMemo(() => {
    const agentItems = (agentsForMentions.data?.items ?? []).map((a) => ({
      type: "agent",
      id: a.id,
      label: a.id,
      hint: a.description || "",
    }));
    const sessionItems = (sessionsForMentions.data?.items ?? [])
      .filter((s) => s.id !== cid)
      .map((s) => ({
        type: "session",
        id: s.id,
        label: s.title || s.id,
        hint: s.agent_id ? `agent ${s.agent_id}` : "",
      }));
    // Files: de-dupe by name across the draft's own pending attachments
    // and filenames already seen on persisted user_message parts, so a
    // resend/re-attach doesn't show up twice.
    const seenNames = new Set();
    const fileItems = [];
    for (const a of attachments) {
      if (!a.name || seenNames.has(a.name)) continue;
      seenNames.add(a.name);
      fileItems.push({ type: "file", id: a.name, label: a.name, hint: "attached to this draft" });
    }
    for (const m of messages) {
      if (!Array.isArray(m.parts)) continue;
      for (const p of m.parts) {
        if (!p || (p.type !== "image" && p.type !== "document") || !p.filename) continue;
        if (seenNames.has(p.filename)) continue;
        seenNames.add(p.filename);
        fileItems.push({ type: "file", id: p.filename, label: p.filename, hint: "shared earlier in this chat" });
      }
    }
    return [...agentItems, ...sessionItems, ...fileItems];
  }, [agentsForMentions.data, sessionsForMentions.data, attachments, messages, cid]);

  // Bubble status the host's page chrome needs (TokenMeter, connection
  // badge, status pill, "chat not found" gate) up to the parent —
  // rather than each of those living inside this embeddable core.
  React.useEffect(() => {
    if (typeof onStatus !== "function") return;
    onStatus({ wsState, usage, compactInFlight, historyError, requestCompact: handleCompact });
  }, [onStatus, wsState, usage, compactInFlight, historyError, handleCompact]);

  return (
    <div
      className="chat-conversation"
      style={{
        display: "flex",
        flexDirection: "row",
        flex: 1,
        height: "100%",
        minHeight: 0,
        minWidth: 0,
      }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          flex: 1,
          minHeight: 0,
          minWidth: 0,
        }}
      >
        {/* Top-of-panel host chrome row (R1): the agent selector moved
            out of the bottom-left composer row up here — "top-right,
            next to the back button" per the chat-refactor plan's R1
            (docs/superpowers/reqs/chat-refactor.md §3/§5). `headerSlot`
            and `rightChromeSlot` are opaque host-supplied nodes — this
            component doesn't know or care what's inside them. ChatDetail
            (chats.jsx) fills `rightChromeSlot` with <CT_AgentSwitcher>;
            a Studio host embedding <Conversation> directly (no back
            button of its own) can fill either slot with its own
            top-right chrome instead. Rendered only when the host
            actually supplies something, so a bare embed with both
            slots null doesn't grow an empty bordered row. */}
        {(headerSlot || rightChromeSlot) && (
          <div
            className="chat-conversation-header"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "10px 14px",
              borderBottom: "1px solid var(--border)",
              flex: "0 0 auto",
            }}
          >
            <div style={{ flex: 1, minWidth: 0 }}>{headerSlot}</div>
            <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
              {rightChromeSlot}
            </div>
          </div>
        )}
        <Transcript
          messages={window.chatCoalesce(messages)}
          chatId={cid}
          agentId={chatAgent}
          wsState={wsState}
          waitingForReply={waitingForReply}
          turnStatus={chatRow?.turn_status}
          chatStatus={chatStatus}
          pendingToolCall={chatRow?.pending_tool_call}
          sendMessage={sendMessage}
          onRewind={handleRewind}
          compactionBoundarySeq={compactionBoundarySeq}
          scrollRef={scrollRef}
          onScroll={onScroll}
          loadingOlder={loadingOlder}
          hasMoreOlder={hasMoreOlder}
        />

        {/* Compaction in-progress indicator. Shown from the moment the
            operator clicks Compact until the server's compaction
            envelope lands (which clears compactInFlight and appends the
            completion marker). Without it the only feedback was the
            disabled button in the header. Rendered as a sibling below
            the (now self-contained) <Transcript> scroll area rather than
            as its last child — <Transcript>'s prop surface (Task B3) is
            the coalesced timeline only, not this live compaction flag. */}
        {compactInFlight && (
          <div style={{
            display: "flex", alignItems: "center", gap: 8,
            margin: "12px auto", padding: "6px 14px",
            border: "1px dashed var(--border)", borderRadius: 14,
            width: "fit-content", fontSize: 12,
            color: "var(--text-3)", background: "var(--bg-1, var(--bg))",
          }}>
            <Icon name="compress" size={12} className="muted" />
            <span>Compacting conversation history…</span>
          </div>
        )}

        <div
          className={isMobile ? "composer-sticky" : ""}
          style={
            isMobile
              ? {
                  display: "flex",
                  gap: 8,
                  alignItems: "stretch",
                }
              : {
                  borderTop: "1px solid var(--border)",
                  padding: 14,
                  display: "flex",
                  gap: 8,
                  alignItems: "stretch",
                }
          }
        >
          {/* Task B4 shell: the input surface (attachment strip,
              attach control, textarea, Send/Stop) now lives in
              <Composer>. This component keeps the composer text +
              attachments state and the send/attach handlers — the
              wsState-not-open case is handled by queueing (Task G1,
              sendMessage's outboxRef buffer, flushed in ws.onopen)
              rather than a hard reject, so <Composer>'s `disabled`
              prop still maps 1:1 to "chat ended" only, never to
              connection state; `wsState` is passed through purely for
              the queue-on-reconnect legibility hint. `running` (Task
              C2) mirrors turn_status (claimable/running) and `onStop`
              POSTs /cancel (A6) — see turnInFlight/handleStop above. */}
          <Composer
            value={composer}
            onChange={setComposer}
            onSend={onSubmitComposer}
            onStop={handleStop}
            running={turnInFlight}
            disabled={chatStatus === "ended"}
            attachments={attachments}
            onAttach={handleFilesPicked}
            onRemoveAttachment={removeAttachment}
            slashCommands={slashCommands}
            mentionSources={mentionSources}
            schemaInvalid={showSchemaPanel ? !schemaValid : false}
            wsState={wsState}
          />
        </div>
      </div>

      {/* <SchemaPanel> (R3) — right-hand sibling of the timeline + composer
          column, per §3's layout. Builder/JSON bodies + persistent/ephemeral
          application (Task F2) are wired above (handleSchemaPersistentChange,
          the debounced persist effect, sendMessage's ephemeral
          frame.response_format, and the one-time chatRow.response_format
          hydration). studio-ux fix 2: always mounted fully OPEN (never the
          internal collapsed-rail state) — the "⚙ schema" chip is now the
          single toggle for the whole panel (mount === visible); its own
          header chevron closes it outright via onCloseSchemaPanel instead of
          re-collapsing in place. studio-ux fix 3: inheritedSchema hands it
          the agent's build-time response_format so a chat with no override
          of its own still shows the EFFECTIVE schema instead of looking
          schema-less. */}
      {showSchemaPanel && (
        <SchemaPanel
          value={schemaValue}
          onChange={setSchemaValue}
          persistent={schemaPersistent}
          onPersistentChange={handleSchemaPersistentChange}
          valid={schemaValid}
          onValidityChange={setSchemaValid}
          collapsed={false}
          onToggle={onCloseSchemaPanel}
          inheritedSchema={agentResponseFormat}
        />
      )}
    </div>
  );
}

window.Conversation = Conversation;
