/* global React, Icon, CT_AgentSwitcher, Transcript, Composer, SchemaPanel */
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
// phase. Schema Builder/JSON bodies + persistent/ephemeral application
// land in Task F1/F2; for now the panel's value/validity state is
// local and unused by the send path unless `showSchemaPanel` is true.
//
// No viewport-relative (vh/dvh) height in this file (per §3) — the
// component fills its flex parent (height:100%/flex:1); the host owns
// viewport-relative sizing.

function Conversation({ chatId, headerSlot, rightChromeSlot, showSchemaPanel, onStatus, pushToast }) {
  const { useResource, useViewport, apiFetch } = window.primerApi;
  const { isMobile } = useViewport();
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
  // <SchemaPanel> (R3, Task B4 shell / F1-F2 behavior) local state.
  // Builder/JSON tab bodies + persistent/ephemeral application to the
  // send path are filled in by Task F1/F2 — this component just holds
  // the state so the shell's prop surface (and <Composer>'s
  // `schemaInvalid` gate) is stable ahead of that phase.
  const [schemaValue, setSchemaValue] = React.useState(null);
  const [schemaPersistent, setSchemaPersistent] = React.useState(false);
  const [schemaValid, setSchemaValid] = React.useState(true);
  const [schemaCollapsed, setSchemaCollapsed] = React.useState(true);

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

  // Returns true if the frame was enqueued; false if the socket was not
  // ready (user-facing toast fires on false so the text is preserved).
  const sendMessage = (text, atts) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== 1) {
      if (typeof pushToast === "function") {
        pushToast({ kind: "error", title: "Not connected", detail: "WebSocket is not open" });
      }
      return false;
    }
    const frame = { kind: "user_message" };
    if (text) frame.content = text;
    if (atts && atts.length > 0) {
      frame.parts = partsForAttachments(atts);
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
        {headerSlot}
        <Transcript
          messages={window.chatCoalesce(messages)}
          chatId={cid}
          agentId={chatAgent}
          wsState={wsState}
          waitingForReply={waitingForReply}
          turnStatus={chatRow?.turn_status}
          pendingToolCall={chatRow?.pending_tool_call}
          sendMessage={sendMessage}
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
          {rightChromeSlot}
          <CT_AgentSwitcher
            chatId={cid}
            currentAgentId={chatAgent}
            pushToast={pushToast}
            placement="up"
            disabled={chatStatus === "ended"}
            triggerStyle={{ padding: "0 12px", borderRadius: 6, alignSelf: "stretch" }}
          />
          {/* Task B4 shell: the input surface (attachment strip,
              attach control, textarea, Send/Stop) now lives in
              <Composer>. This component keeps the composer text +
              attachments state and the send/attach handlers — the
              wsState-not-open case is still handled (sendMessage()
              below shows a "Not connected" toast), it just no longer
              disables the Send button pre-emptively since <Composer>'s
              `disabled` prop maps 1:1 to "chat ended" the same way the
              textarea's disabled attribute always has. `running` (Task
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
            slashCommands={[]}
            mentionSources={[]}
            schemaInvalid={showSchemaPanel ? !schemaValid : false}
          />
        </div>
      </div>

      {/* <SchemaPanel> (R3) — collapsible right-hand sibling of the
          timeline + composer column, per §3's layout. Builder/JSON
          bodies + persistent/ephemeral application to the send path
          land in Task F1/F2; this mounts the Task B4 shell now that
          showSchemaPanel (accepted since Task B2) has something to
          gate. */}
      {showSchemaPanel && (
        <SchemaPanel
          value={schemaValue}
          onChange={setSchemaValue}
          persistent={schemaPersistent}
          onPersistentChange={setSchemaPersistent}
          valid={schemaValid}
          onValidityChange={setSchemaValid}
          collapsed={schemaCollapsed}
          onToggle={() => setSchemaCollapsed((c) => !c)}
        />
      )}
    </div>
  );
}

window.Conversation = Conversation;
