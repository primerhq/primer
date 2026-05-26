/* global React, Icon, Btn, Modal, Banner, ApprovalBanner, relativeTime, fmtDate */
//
// Chats list + detail. Live updates via WebSocket (`/v1/chats/{id}/ws`),
// initial history via REST (`GET /v1/chats/{id}/messages`), and inline
// tool-approval card polled via REST (`GET /v1/chats/{id}/tool_approval/
// pending`). Approval decisions prefer the open WS (`tool_approval_decide`
// frame) and fall back to REST (`POST .../tool_approval/respond`) when
// the socket is not open. Sending a new message while an approval is
// pending shows an inline confirm banner — the server auto-rejects the
// park when it sees a new user_message (§10.1), so the UI mirrors that.
//
// Top-level consts are CT_-prefixed so babel-standalone's flat eval
// scope doesn't collide with sibling components (precedent: Task 3).

const CT_PAGE_SIZE = 12;

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

// ============================================================================
// ChatsPage — list view
// ============================================================================

function ChatsPage({ onOpen, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.matrixApi;
  const { navigate } = useRouter();
  const [showNew, setShowNew] = React.useState(false);
  const [textQuery, setTextQuery] = React.useState("");
  const [agentFilter, setAgentFilter] = React.useState("");
  const [page, setPage] = React.useState(1);
  const [pendingDelete, setPendingDelete] = React.useState(null);
  const filterFocused = React.useRef(false);

  const list = useResource(
    "chats:list",
    (signal) => apiFetch("GET", "/chats?limit=200", null, { signal }),
    { pollMs: 5000, pauseWhile: () => filterFocused.current }
  );

  const remove = useMutation(
    (cid) => apiFetch("DELETE", `/chats/${encodeURIComponent(cid)}?force=true`),
    {
      invalidates: ["chats:list"],
      onSuccess: () => {
        const cid = pendingDelete?.id;
        setPendingDelete(null);
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "Chat deleted", detail: cid });
        }
        list.refetch();
      },
      onError: (err) => {
        if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err?.title || "Delete failed",
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      },
    },
  );

  const items = list.data?.items ?? [];

  const agents = React.useMemo(() => {
    const set = new Set();
    for (const c of items) if (c.agent_id) set.add(c.agent_id);
    return [...set].sort();
  }, [items]);

  const filtered = React.useMemo(() => {
    let arr = items;
    if (textQuery) {
      const q = textQuery.toLowerCase();
      arr = arr.filter((c) =>
        (c.id || "").toLowerCase().includes(q) ||
        (c.agent_id || "").toLowerCase().includes(q)
      );
    }
    if (agentFilter) arr = arr.filter((c) => c.agent_id === agentFilter);
    return arr;
  }, [items, textQuery, agentFilter]);

  const total = filtered.length;
  const totalPages = Math.max(1, Math.ceil(total / CT_PAGE_SIZE));
  const pageItems = filtered.slice((page - 1) * CT_PAGE_SIZE, page * CT_PAGE_SIZE);

  const openRow = (cid) => {
    if (typeof onOpen === "function") onOpen(cid);
    else navigate("/chats/" + cid);
  };

  const ageSec = (iso) => {
    if (!iso) return null;
    return (Date.now() - new Date(iso).getTime()) / 1000;
  };

  // Error-only state (when no cached items to show).
  if (list.error && items.length === 0) {
    return (
      <Banner
        kind="error"
        title={list.error.title || "Couldn't load chats"}
        detail={list.error.detail || list.error.message}
        requestId={list.error.requestId}
        actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
      />
    );
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter chats…"
            value={textQuery}
            onChange={(e) => { setTextQuery(e.target.value); setPage(1); }}
            onFocus={() => { filterFocused.current = true; }}
            onBlur={() => { filterFocused.current = false; }}
          />
        </div>
        <div className="sep-v" />
        <select
          className="select"
          value={agentFilter}
          onChange={(e) => { setAgentFilter(e.target.value); setPage(1); }}
        >
          <option value="">all agents</option>
          {agents.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowNew(true)}>New chat</Btn>
        </div>
      </div>

      {list.loading && items.length === 0 ? (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr><th>ID</th><th>Agent</th><th>Status</th><th style={{ textAlign: "right" }}>Messages</th><th>Created</th><th></th></tr>
            </thead>
            <tbody>
              {Array.from({ length: 6 }).map((_, i) => (
                <tr key={i}>
                  {Array.from({ length: 6 }).map((__, j) => (
                    <td key={j}><span className="skel" style={{ display: "block", height: 12, width: j === 0 ? 120 : 80 }} /></td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : items.length === 0 ? (
        <div className="panel">
          <div className="empty">
            <div className="ico-wrap"><Icon name="send" size={22} /></div>
            <div className="head">No chats yet</div>
            <div className="sub">A chat is a long-running conversation with a single agent. Start one to stream tokens, tool calls, and tool results live.</div>
            <div className="actions">
              <Btn kind="primary" icon="plus" onClick={() => setShowNew(true)}>New chat</Btn>
            </div>
          </div>
        </div>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>ID</th>
                <th>Agent</th>
                <th>Status</th>
                <th style={{ textAlign: "right" }}>Messages</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {pageItems.length === 0 ? (
                <tr><td colSpan={6} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                  No chats match the current filter{textQuery ? ` "${textQuery}"` : ""}.
                  {" · "}<a
                    onClick={() => { setTextQuery(""); setAgentFilter(""); }}
                    style={{ cursor: "pointer", color: "var(--accent)" }}
                  >Clear filters</a>
                </td></tr>
              ) : pageItems.map((c) => {
                const createdSec = ageSec(c.created_at);
                const status = c.status || "active";
                return (
                  <tr key={c.id} onClick={() => openRow(c.id)} style={{ cursor: "pointer" }}>
                    <td className="mono">{c.id}</td>
                    <td className="mono">{c.agent_id}</td>
                    <td>
                      {status === "active" ? (
                        <span className="pill pill-running"><span className="dot"></span>active</span>
                      ) : (
                        <span className="pill pill-ended"><span className="dot"></span>{status}</span>
                      )}
                    </td>
                    <td className="mono num tabular">{c.last_seq ?? 0}</td>
                    <td className="mono muted">{createdSec != null ? relativeTime(createdSec) : "—"}</td>
                    <td style={{ textAlign: "right", paddingRight: 12, whiteSpace: "nowrap" }}>
                      <button
                        className="row-action"
                        title="Delete chat"
                        data-testid={`chat-row-delete-${c.id}`}
                        onClick={(e) => { e.stopPropagation(); setPendingDelete(c); }}
                        style={{
                          background: "none", border: "none", cursor: "pointer",
                          color: "var(--text-3)", padding: "2px 6px",
                          marginRight: 4,
                        }}
                      >
                        <Icon name="trash" size={13} />
                      </button>
                      <Icon name="chevron-right" size={12} className="muted" />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="tbl-foot">
            <span className="tabular">
              Showing <strong style={{ color: "var(--text)" }}>{total === 0 ? 0 : (page - 1) * CT_PAGE_SIZE + 1}</strong>–
              <strong style={{ color: "var(--text)" }}>{Math.min(page * CT_PAGE_SIZE, total)}</strong> of{" "}
              <strong style={{ color: "var(--text)" }}>{total}</strong>
            </span>
            <div className="pager">
              <button disabled={page === 1} onClick={() => setPage(page - 1)}><Icon name="chevron-left" size={12} /></button>
              <span className="muted text-sm tabular" style={{ padding: "0 8px" }}>Page {page} of {totalPages}</span>
              <button disabled={page === totalPages} onClick={() => setPage(page + 1)}><Icon name="chevron-right" size={12} /></button>
            </div>
          </div>
        </div>
      )}

      {showNew && (
        <CT_NewChatModal onClose={() => setShowNew(false)} pushToast={pushToast} />
      )}

      {pendingDelete && (
        <Modal
          title="Delete chat"
          danger
          onClose={() => { if (!remove.loading) setPendingDelete(null); }}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setPendingDelete(null)} disabled={remove.loading}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                onClick={() => remove.mutate(pendingDelete.id)}
                disabled={remove.loading}
              >
                {remove.loading ? "Deleting…" : "Delete chat"}
              </Btn>
            </>
          }
        >
          <p>
            Permanently delete <strong className="mono">{pendingDelete.id}</strong>?
            All persisted messages are removed. This cannot be undone.
          </p>
        </Modal>
      )}
    </div>
  );
}

// ============================================================================
// CT_NewChatModal — create-chat dialog (POST /v1/chats)
// ============================================================================

function CT_NewChatModal({ onClose, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.matrixApi;
  const { navigate } = useRouter();

  const agents = useResource(
    "chats-modal:agents",
    (s) => apiFetch("GET", "/agents?limit=200", null, { signal: s }),
    {}
  );
  const agentItems = agents.data?.items ?? [];

  const [agentId, setAgentId] = React.useState("");
  const [initialInstructions, setInitialInstructions] = React.useState("");
  const [fieldErrors, setFieldErrors] = React.useState({});

  React.useEffect(() => {
    if (!agentId && agentItems.length > 0) setAgentId(agentItems[0].id);
  }, [agentItems, agentId]);

  const create = useMutation(
    (body) => apiFetch("POST", "/chats", body),
    {
      invalidates: ["chats:list"],
      onSuccess: (row) => {
        onClose();
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "Chat created", detail: row.id });
        }
        navigate("/chats/" + row.id);
      },
      onError: (err) => {
        if (err && err.status === 422 && Array.isArray(err.fieldErrors)) {
          const map = {};
          for (const fe of err.fieldErrors) {
            map[(fe.loc || []).join(".")] = fe.msg;
          }
          setFieldErrors(map);
        } else if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err?.title || "Create failed",
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      },
    }
  );

  const onSubmit = () => {
    if (!agentId) return;
    setFieldErrors({});
    create.mutate({
      agent_id: agentId,
      initial_instructions: initialInstructions.trim() || undefined,
    });
  };

  return (
    <Modal
      title="New chat"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn
            kind="primary"
            icon="plus"
            disabled={!agentId || create.loading}
            onClick={onSubmit}
          >Create chat</Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">Agent</label>
        {agents.loading && agentItems.length === 0 ? (
          <div className="muted text-sm">Loading agents…</div>
        ) : agentItems.length === 0 ? (
          <div className="banner banner-warning" style={{ margin: 0, fontSize: 11.5 }}>
            <Icon name="alert" size={12} className="ico" />
            <div>No agents registered. Create one before starting a chat.</div>
          </div>
        ) : (
          <select
            className="select mono"
            style={{ width: "100%" }}
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
          >
            {agentItems.map((a) => (
              <option key={a.id} value={a.id}>{a.id}</option>
            ))}
          </select>
        )}
        {fieldErrors["body.agent_id"] && (
          <div className="muted text-sm" style={{ color: "var(--red)", marginTop: 4 }}>{fieldErrors["body.agent_id"]}</div>
        )}
      </div>
      <div className="field">
        <label className="field-label">Initial instructions <span className="hint">optional</span></label>
        <textarea
          className="textarea"
          rows={4}
          placeholder="What should the agent know before starting?"
          value={initialInstructions}
          onChange={(e) => setInitialInstructions(e.target.value)}
        />
        {fieldErrors["body.initial_instructions"] && (
          <div className="muted text-sm" style={{ color: "var(--red)", marginTop: 4 }}>{fieldErrors["body.initial_instructions"]}</div>
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// ChatDetail — conversation view (REST replay + live WS stream)
// ============================================================================

function ChatDetail({ chatId, onBack, pushToast }) {
  const { useResource, useMutation, apiFetch } = window.matrixApi;
  const cid = chatId;

  const [messages, setMessages] = React.useState([]);
  const [lastSeq, setLastSeq] = React.useState(0);
  const [wsState, setWsState] = React.useState("connecting");
  const [composer, setComposer] = React.useState("");
  const [pendingSendText, setPendingSendText] = React.useState(null);
  const [historyError, setHistoryError] = React.useState(null);
  // Set true the moment the user sends a frame; cleared when any
  // assistant_token / tool_call / done / error row arrives. Drives
  // the "Thinking..." placeholder so the operator sees the system
  // is responding before the first delta lands.
  const [waitingForReply, setWaitingForReply] = React.useState(false);
  // Pending file attachments. Each entry: {id, file, name, mime, kind, dataB64, preview}.
  const [attachments, setAttachments] = React.useState([]);
  const wsRef = React.useRef(null);
  const scrollRef = React.useRef(null);
  const fileInputRef = React.useRef(null);

  // Chat row (status, agent_id) — small one-shot fetch + light polling
  // so the header pill mirrors a server-driven "ended" state.
  const chat = useResource(
    `chat-detail:${cid}`,
    (s) => apiFetch("GET", `/chats/${encodeURIComponent(cid)}`, null, { signal: s }),
    { pollMs: 10000, deps: [cid] }
  );

  // Initial REST load — fires before WS opens. The WS handshake also
  // replays from cursor=0, but doing one REST round-trip first means
  // the timeline is visible immediately instead of waiting for the
  // (potentially slower) WS upgrade.
  const initialLoadedRef = React.useRef(false);
  React.useEffect(() => {
    let cancelled = false;
    initialLoadedRef.current = false;
    (async () => {
      try {
        const data = await apiFetch(
          "GET",
          `/chats/${encodeURIComponent(cid)}/messages?after_seq=0&limit=500`,
        );
        if (cancelled) return;
        const items = (data && data.items) || [];
        setMessages(items);
        if (items.length > 0) setLastSeq(items[items.length - 1].seq || 0);
        initialLoadedRef.current = true;
      } catch (err) {
        if (cancelled) return;
        setHistoryError(err);
      }
    })();
    return () => { cancelled = true; };
  }, [cid]); // eslint-disable-line react-hooks/exhaustive-deps

  // WS lifecycle — open once per cid. We intentionally do NOT depend on
  // lastSeq here: the cursor is read once at connect time, and live
  // appends after that come down the same socket so there is no gap.
  React.useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/v1/chats/${encodeURIComponent(cid)}/ws?cursor=0`;
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      setWsState("closed");
      return;
    }
    wsRef.current = ws;
    setWsState("connecting");

    ws.onopen = () => setWsState("open");

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

      // Any persisted row carries seq → append + advance cursor. We
      // de-dupe against the initial REST replay because both sources
      // may overlap (REST loaded seqs 1..N, WS replays seq>0).
      if (typeof msg.seq === "number") {
        setMessages((prev) => {
          if (prev.some((p) => p.seq === msg.seq)) return prev;
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
      setWsState("closed");
      if (ev.code === 4404 && typeof pushToast === "function") {
        pushToast({ kind: "error", title: "Chat not found", detail: ev.reason || cid });
      } else if (ev.code === 4410 && typeof pushToast === "function") {
        pushToast({ kind: "warning", title: "Chat ended", detail: ev.reason || cid });
      }
    };

    ws.onerror = () => {
      // Browsers report a generic ErrorEvent and then close — onclose
      // does the user-facing toasting based on the close code.
    };

    return () => {
      try { ws.close(); } catch { /* no-op */ }
      wsRef.current = null;
    };
  }, [cid]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-scroll to bottom on append.
  React.useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages]);

  // Pending tool approval (polled REST; 404 = none).
  const approval = useResource(
    `chat-approval:${cid}`,
    (s) => apiFetch("GET", `/chats/${encodeURIComponent(cid)}/tool_approval/pending`, null, { signal: s }),
    { pollMs: 2000, deps: [cid] }
  );
  const hasPendingApproval = !!(approval.data && (!approval.error || approval.error.status !== 404));
  const pendingApproval = hasPendingApproval ? approval.data : null;

  // Fallback REST mutation for approve/reject when the WS isn't open.
  const respond = useMutation(
    (body) => apiFetch(
      "POST",
      `/chats/${encodeURIComponent(cid)}/tool_approval/respond`,
      body,
    ),
    {
      invalidates: [`chat-approval:${cid}`],
      onSuccess: () => pushToast && pushToast({ kind: "success", title: "Decision sent" }),
      onError: (err) => pushToast && pushToast({
        kind: "error",
        title: err?.title || "Respond failed",
        detail: err?.detail || err?.message,
        requestId: err?.requestId,
      }),
    },
  );

  const decide = (decision, tcid, reason) => {
    const body = { tool_call_id: tcid, decision };
    if (reason) body.reason = reason;
    const ws = wsRef.current;
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ kind: "tool_approval_decide", ...body }));
    } else {
      respond.mutate(body);
    }
  };

  const sendMessage = (text, atts) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== 1) {
      if (typeof pushToast === "function") {
        pushToast({ kind: "error", title: "Not connected", detail: "WebSocket is not open" });
      }
      return;
    }
    const frame = { kind: "user_message" };
    if (text) frame.content = text;
    if (atts && atts.length > 0) {
      frame.parts = atts.map((a) => ({
        type: a.kind,           // "image" or "document"
        data: a.dataB64,
        mime_type: a.mime,
        ...(a.kind === "document" && a.name ? { filename: a.name } : {}),
      }));
    }
    ws.send(JSON.stringify(frame));
    setWaitingForReply(true);
  };

  const onSubmitComposer = () => {
    const text = composer.trim();
    if (!text && attachments.length === 0) return;
    if (hasPendingApproval) {
      setPendingSendText({ text, attachments });
      return;
    }
    sendMessage(text, attachments);
    setComposer("");
    setAttachments([]);
  };

  const confirmSendOverApproval = () => {
    if (!pendingSendText) return;
    sendMessage(pendingSendText.text, pendingSendText.attachments);
    setComposer("");
    setAttachments([]);
    setPendingSendText(null);
  };

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
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const removeAttachment = (id) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  };

  const wsBadge = wsState === "open"
    ? <span className="pill pill-running" title="WebSocket open"><span className="dot"></span>live</span>
    : wsState === "connecting"
      ? <span className="pill pill-paused" title="WebSocket connecting"><span className="dot"></span>connecting</span>
      : <span className="pill pill-ended" title="WebSocket closed"><span className="dot"></span>offline</span>;

  if (historyError && historyError.status === 404) {
    return (
      <Banner
        kind="error"
        title="Chat not found"
        detail={`No chat with id ${cid}.`}
        actions={<Btn size="sm" icon="chevron-left" onClick={onBack}>Back to chats</Btn>}
      />
    );
  }

  const chatRow = chat.data;
  const chatStatus = chatRow?.status || "active";
  const chatAgent = chatRow?.agent_id || "—";

  return (
    <div className="col" style={{ gap: 14, height: "calc(100vh - 180px)", display: "flex", flexDirection: "column" }}>
      <div className="panel" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
        <div className="panel-h">
          <Icon name="send" size={13} style={{ color: "var(--accent)" }} />
          <span className="mono">{cid}</span>
          <span className="sub">· agent <span className="mono">{chatAgent}</span></span>
          <div className="right">
            {wsBadge}
            <span className={chatStatus === "active" ? "pill pill-running" : "pill pill-ended"}>
              <span className="dot"></span>{chatStatus}
            </span>
          </div>
        </div>
        <div ref={scrollRef} style={{ flex: 1, overflow: "auto", padding: "18px 24px", minHeight: 0 }}>
          {messages.length === 0 && !historyError && (
            <div className="muted text-sm" style={{ textAlign: "center", padding: 24 }}>
              {wsState === "connecting" ? "Connecting…" : "No messages yet. Say hello to the agent."}
            </div>
          )}
          {CT_coalesceMessages(messages).map((m) =>
            m.kind === "assistant_message" ? (
              <Message key={`am-${m.startSeq}-${m.endSeq}`} m={m} />
            ) : (
              <Message key={`${m.seq}-${m.kind}`} m={m} />
            )
          )}

          {/* Thinking indicator — only when we're waiting for the
              first token of a response we just kicked off. */}
          {waitingForReply && <CT_ThinkingBubble />}

          {/* Inline approval card — sits ABOVE the composer when pending */}
          {pendingApproval && (
            <div style={{ marginLeft: 60, marginTop: 6 }}>
              <CT_InlineApproval
                data={pendingApproval}
                onApprove={() => decide("approved", pendingApproval.tool_call_id)}
                onReject={(reason) => decide("rejected", pendingApproval.tool_call_id, reason)}
                busy={respond.loading}
              />
            </div>
          )}
        </div>

        {/* Auto-reject confirm banner */}
        {pendingSendText && (
          <div style={{ borderTop: "1px solid var(--border)", padding: "10px 14px" }}>
            <Banner
              kind="warning"
              title="Sending a new message will auto-reject the pending approval."
              detail={pendingApproval ? `Tool ${pendingApproval.tool_name} will be marked rejected by the server.` : undefined}
              actions={
                <>
                  <Btn size="sm" kind="danger" icon="send" onClick={confirmSendOverApproval}>Send & reject</Btn>
                  <Btn size="sm" kind="ghost" onClick={() => setPendingSendText(null)}>Cancel</Btn>
                </>
              }
            />
          </div>
        )}

        {/* Pending-attachments strip — visible only when the composer
            has files queued. Each chip carries an image thumbnail or a
            document icon + filename + size; clicking ×  drops it. */}
        {attachments.length > 0 && (
          <div
            style={{
              borderTop: "1px solid var(--border)",
              padding: "10px 14px",
              display: "flex",
              flexWrap: "wrap",
              gap: 8,
            }}
          >
            {attachments.map((a) => (
              <CT_AttachmentChip
                key={a.id}
                attachment={a}
                onRemove={() => removeAttachment(a.id)}
              />
            ))}
          </div>
        )}

        <div
          style={{
            borderTop: "1px solid var(--border)",
            padding: 14,
            display: "flex",
            gap: 8,
            alignItems: "stretch",
          }}
        >
          <button
            type="button"
            title="Attach files (images, PDFs)"
            data-testid="chat-attach-btn"
            onClick={() => fileInputRef.current && fileInputRef.current.click()}
            disabled={chatStatus === "ended"}
            style={{
              background: "transparent",
              border: "1px solid var(--border)",
              borderRadius: 6,
              padding: "0 10px",
              color: "var(--text-2)",
              cursor: chatStatus === "ended" ? "not-allowed" : "pointer",
              display: "flex",
              alignItems: "center",
              opacity: chatStatus === "ended" ? 0.5 : 1,
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
            onChange={(e) => handleFilesPicked(e.target.files)}
          />
          <textarea
            className="textarea"
            value={composer}
            onChange={(e) => setComposer(e.target.value)}
            placeholder={chatStatus === "ended" ? "This chat has ended." : "Send a message…"}
            rows={2}
            style={{ flex: 1, resize: "none" }}
            disabled={chatStatus === "ended"}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSubmitComposer(); } }}
          />
          <Btn
            kind="primary"
            icon="send"
            disabled={(!composer.trim() && attachments.length === 0) || chatStatus === "ended" || wsState !== "open"}
            onClick={onSubmitComposer}
            style={{ alignSelf: "stretch", paddingLeft: 16, paddingRight: 16 }}
          >Send</Btn>
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Helpers: assistant-token coalescing + thinking indicator + attachment chip
// ============================================================================

// Walk `messages` in order and merge any run of consecutive
// `assistant_token` rows into one synthetic "assistant_message"
// entry whose `text` is the concatenation of the run's deltas. Any
// other row passes through unchanged. Without this, every token from
// the LLM renders as its own bubble — unreadable for any reply
// longer than a word or two.
function CT_coalesceMessages(messages) {
  const out = [];
  let buffer = null;
  const flushBuffer = () => {
    if (buffer) { out.push(buffer); buffer = null; }
  };
  for (const m of messages) {
    if (m.kind === "assistant_token") {
      const delta = typeof m.delta === "string" ? m.delta : "";
      if (!buffer) {
        buffer = {
          kind: "assistant_message",
          text: delta,
          startSeq: m.seq,
          endSeq: m.seq,
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
  flushBuffer();
  return out;
}

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

// Chip rendered in the pending-attachments strip. Image kind shows a
// 36px thumbnail; document kind shows a file icon + filename.
function CT_AttachmentChip({ attachment, onRemove }) {
  const sizeKb = attachment.size ? `${(attachment.size / 1024).toFixed(0)} KiB` : "";
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "4px 6px 4px 4px",
        border: "1px solid var(--border)",
        borderRadius: 6,
        background: "var(--bg-0)",
      }}
    >
      {attachment.kind === "image" && attachment.preview ? (
        <img
          src={attachment.preview}
          alt=""
          style={{ width: 36, height: 36, objectFit: "cover", borderRadius: 4 }}
        />
      ) : (
        <div
          style={{
            width: 36, height: 36, display: "grid", placeItems: "center",
            background: "var(--bg-1)", borderRadius: 4, color: "var(--text-3)",
          }}
        >
          <Icon name="file" size={16} />
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
        <span className="mono text-sm" style={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{attachment.name}</span>
        {sizeKb && <span className="muted text-sm" style={{ fontSize: 10.5 }}>{sizeKb}</span>}
      </div>
      <button
        type="button"
        onClick={onRemove}
        title="Remove attachment"
        style={{
          background: "none", border: "none", cursor: "pointer",
          color: "var(--text-3)", padding: 2,
        }}
      >
        <Icon name="x" size={12} />
      </button>
    </div>
  );
}

// ============================================================================
// CT_InlineApproval — render the pending tool_call + approve/reject buttons.
// Mirrors ApprovalBanner from approvals.jsx but routes decisions through the
// parent (so we can prefer WS over REST).
// ============================================================================

function CT_InlineApproval({ data, onApprove, onReject, busy }) {
  const [rejecting, setRejecting] = React.useState(false);
  const [reason, setReason] = React.useState("");
  if (!data) return null;

  const submitReject = () => {
    const r = reason.trim();
    if (!r) return;
    onReject(r);
    setRejecting(false);
    setReason("");
  };

  return (
    <div
      className="panel"
      style={{ borderColor: "var(--amber)", boxShadow: "0 0 0 3px var(--amber-dim)" }}
      data-testid="approval-banner"
    >
      <div className="panel-h" style={{ background: "var(--amber-dim)" }}>
        <Icon name="warn-circle" size={14} style={{ color: "var(--amber)" }} />
        <span style={{ color: "var(--amber)" }}>Awaiting your approval for {data.tool_name}</span>
        <span className="mono sub">· {data.tool_call_id}</span>
        <div className="right">
          <span className="muted text-sm">
            {data.policy_id && <>policy <span className="mono">{data.policy_id}</span></>}
            {data.approval_type && <> · {data.approval_type}</>}
          </span>
        </div>
      </div>
      <div className="panel-body">
        {data.gate_reason && (
          <div className="muted text-sm mb-2">
            <strong style={{ color: "var(--text)" }}>Gate:</strong> {data.gate_reason}
          </div>
        )}
        {data.arguments && Object.keys(data.arguments).length > 0 && (
          <div className="code-block" style={{ maxHeight: 140, overflow: "auto" }}>
            {JSON.stringify({ arguments: data.arguments }, null, 2)}
          </div>
        )}
        <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
          {!rejecting ? (
            <>
              <Btn
                kind="primary"
                icon="check"
                disabled={busy}
                onClick={onApprove}
                data-testid="approval-banner-approve"
              >
                Approve
              </Btn>
              <Btn
                kind="danger"
                icon="x"
                disabled={busy}
                onClick={() => setRejecting(true)}
                data-testid="approval-banner-reject"
              >
                Reject
              </Btn>
            </>
          ) : (
            <>
              <input
                className="input"
                placeholder="Reason for rejection (required)…"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                style={{ flex: 1 }}
                autoFocus
                data-testid="approval-banner-reason"
              />
              <Btn
                kind="danger"
                icon="send"
                disabled={!reason.trim() || busy}
                onClick={submitReject}
                data-testid="approval-banner-reject-submit"
              >
                Send rejection
              </Btn>
              <Btn kind="ghost" onClick={() => { setRejecting(false); setReason(""); }}>Cancel</Btn>
            </>
          )}
        </div>
      </div>
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
    const argsStr = (() => { try { return JSON.stringify(args).slice(0, 80); } catch { return ""; } })();
    return (
      <div style={{ marginLeft: 60, marginTop: 6, marginBottom: 6 }}>
        <div className="tool-call">
          <Icon name="play" size={10} style={{ color: m.pending_approval ? "var(--amber)" : "var(--text-3)" }} />
          <span className="name">{name}</span>
          <span className="arrow">(</span>
          <span className="muted">{argsStr}</span>
          <span className="arrow">)</span>
          {m.pending_approval && <span className="pill pill-paused" style={{ marginLeft: "auto" }}><span className="dot"></span>awaiting approval</span>}
        </div>
      </div>
    );
  }

  if (kind === "tool_result") {
    const name = m.name || m.tool_name || "tool";
    const result = typeof m.result === "string" ? m.result : (m.result != null ? JSON.stringify(m.result).slice(0, 80) : "");
    return (
      <div style={{ marginLeft: 60, marginTop: 2, marginBottom: 6 }}>
        <div className="tool-call" style={{ borderLeft: "2px solid var(--green)" }}>
          <Icon name="check" size={10} style={{ color: "var(--green)" }} />
          <span className="name">{name}</span>
          <span className="arrow">→</span>
          <span className="muted">{result}</span>
        </div>
      </div>
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

  // Coalesced agent reply (the streaming tokens collapsed into one
  // bubble by CT_coalesceMessages above).
  if (kind === "assistant_message") {
    return (
      <div style={{ display: "flex", gap: 12, marginBottom: 14 }}>
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
          flex: 1, fontSize: 13, lineHeight: 1.55, color: "var(--text)",
          borderLeft: "2px solid var(--accent)", paddingLeft: 12,
          whiteSpace: "pre-wrap",
        }}>
          {m.text}
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
  return (
    <div style={{ display: "flex", gap: 12, marginBottom: 14 }}>
      <div style={{
        width: 48, flexShrink: 0,
        fontFamily: "IBM Plex Mono, monospace",
        fontSize: 10.5,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: isUser ? "var(--text-2)" : "var(--accent)",
        fontWeight: 600,
        paddingTop: 2,
      }}>{isUser ? "user" : "agent"}</div>
      <div style={{ flex: 1, fontSize: 13, lineHeight: 1.55, color: "var(--text)", borderLeft: `2px solid ${isUser ? "var(--border)" : "var(--accent)"}`, paddingLeft: 12 }}>
        {CT_textOf(m) && <div style={{ whiteSpace: "pre-wrap" }}>{CT_textOf(m)}</div>}
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

window.ChatsPage = ChatsPage;
window.ChatDetail = ChatDetail;
