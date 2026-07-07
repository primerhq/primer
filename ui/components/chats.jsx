/* global React, Icon, Btn, Modal, Banner, BottomSheet, relativeTime, fmtDate */
//
// Chats list + detail. Live updates via WebSocket (`/v1/chats/{id}/ws`)
// and initial history via REST (`GET /v1/chats/{id}/messages`). Tool
// approval is conversational: when the agent hits an approval gate it
// ends its turn with a normal assistant message asking for a yes/no,
// and the human replies via the normal composer like any other turn.
//
// CT_roleForKind / CT_textOf moved to ui/components/chat/transcript.jsx
// (Task B3) — they're private helpers of the Message row renderer,
// which lives there now.

// ============================================================================
// ChatsPage — list view
// ============================================================================

function ChatsPage({ onOpen, pushToast }) {
  const { useMutation, useRouter, apiFetch, usePagedList, Pager } = window.primerApi;
  const { navigate } = useRouter();
  const [showNew, setShowNew] = React.useState(false);
  const [textQuery, setTextQuery] = React.useState("");
  const [agentFilter, setAgentFilter] = React.useState("");
  const [pendingDelete, setPendingDelete] = React.useState(null);
  const filterFocused = React.useRef(false);

  // Server-side offset pagination (bug #19) — replaces the old hard
  // limit=200 fetch + client-side page slicing. The text + agent filters
  // narrow the current page client-side, so changing either resets to page 0.
  const list = usePagedList({
    key: "chats:list",
    path: "/chats",
    pageSize: 50,
    pollMs: 5000,
    pauseWhile: () => filterFocused.current,
    resetKey: textQuery + "|" + agentFilter,
  });

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

  const items = list.items;

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
        (c.agent_id || "").toLowerCase().includes(q) ||
        (c.title || "").toLowerCase().includes(q)
      );
    }
    if (agentFilter) arr = arr.filter((c) => c.agent_id === agentFilter);
    return arr;
  }, [items, textQuery, agentFilter]);

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
            onChange={(e) => setTextQuery(e.target.value)}
            onFocus={() => { filterFocused.current = true; }}
            onBlur={() => { filterFocused.current = false; }}
          />
        </div>
        <div className="sep-v" />
        <select
          className="select"
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
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
              <tr><th>Chat</th><th>Agent</th><th>Status</th><th style={{ textAlign: "right" }}>Messages</th><th>Created</th><th></th></tr>
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
                <th>Chat</th>
                <th>Agent</th>
                <th>Status</th>
                <th style={{ textAlign: "right" }}>Messages</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr><td colSpan={6} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                  No chats match the current filter{textQuery ? ` "${textQuery}"` : ""}.
                  {" · "}<a
                    onClick={() => { setTextQuery(""); setAgentFilter(""); }}
                    style={{ cursor: "pointer", color: "var(--accent)" }}
                  >Clear filters</a>
                </td></tr>
              ) : filtered.map((c) => {
                const createdSec = ageSec(c.created_at);
                const status = c.status || "active";
                return (
                  <tr key={c.id} onClick={() => openRow(c.id)} style={{ cursor: "pointer" }} title={c.id}>
                    <td>
                      <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
                        <span
                          style={{
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                            maxWidth: 360,
                            color: c.title ? "var(--text)" : "var(--text-3)",
                          }}
                        >{c.title || c.id}</span>
                        {c.title && (
                          <span
                            className="mono muted text-sm"
                            style={{ fontSize: 10.5, marginTop: 1 }}
                          >{c.id}</span>
                        )}
                      </div>
                    </td>
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
        </div>
      )}

      <Pager pager={list} label="chats" />

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
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
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
// CT_AgentSwitcher - clickable header dropdown to switch a chat's agent
// (POST /v1/chats/{id}/agent), paginated + searchable picker.
// ============================================================================

function CT_AgentSwitcher({ chatId, currentAgentId, pushToast, placement = "down", disabled = false, triggerStyle = null }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const [open, setOpen] = React.useState(false);
  const [q, setQ] = React.useState("");
  const agents = useResource(
    "agent-switcher:agents",
    (s) => apiFetch("GET", "/agents?limit=200", null, { signal: s }),
    {}
  );
  const items = agents.data?.items ?? [];
  const filtered = q
    ? items.filter((a) =>
        (a.id + " " + (a.description || "")).toLowerCase().includes(q.toLowerCase()))
    : items;
  const PAGE = 8;
  const [page, setPage] = React.useState(0);
  React.useEffect(() => setPage(0), [q]);
  const shown = filtered.slice(page * PAGE, page * PAGE + PAGE);
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE));

  const switchAgent = useMutation(
    (agentId) => apiFetch("POST", `/chats/${chatId}/agent`, { agent_id: agentId }),
    {
      invalidates: [`chat-detail:${chatId}`],
      onSuccess: (row) => {
        setOpen(false); setQ("");
        if (typeof pushToast === "function")
          pushToast({ kind: "success", title: "Agent switched", detail: row.agent_id });
      },
      onError: (err) => {
        if (typeof pushToast === "function")
          pushToast({ kind: "error", title: err?.title || "Switch failed",
                      detail: err?.detail || err?.message, requestId: err?.requestId });
      },
    }
  );

  return (
    <span className="agent-switcher" style={{ position: "relative", display: "inline-flex", alignItems: "stretch" }}>
      <button
        className="chip"
        onClick={() => !disabled && setOpen((v) => !v)}
        title="Switch agent"
        disabled={disabled}
        style={{ display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap", ...(triggerStyle || {}) }}
      >
        agent <span className="mono">{currentAgentId}</span>
        <Icon name={placement === "up" ? "chevron-up" : "chevron-down"} size={11} />
      </button>
      {open && (
        <div className="popover" style={{ position: "absolute",
              ...(placement === "up" ? { bottom: "100%", marginBottom: 6 } : { top: "100%", marginTop: 6 }),
              left: 0, zIndex: 50,
              width: 300, background: "var(--bg-1)", border: "1px solid var(--border)",
              borderRadius: 8, padding: 8, boxShadow: "0 6px 24px rgba(0,0,0,.3)" }}>
          <input className="input" placeholder="Search agents…" value={q}
                 onChange={(e) => setQ(e.target.value)} style={{ width: "100%", marginBottom: 6 }} />
          <div style={{ display: "flex", flexDirection: "column", gap: 2, maxHeight: 260, overflow: "auto" }}>
            {shown.map((a) => (
              <button key={a.id} className="menu-item"
                      disabled={a.id === currentAgentId || switchAgent.loading}
                      onClick={() => switchAgent.mutate(a.id)}
                      style={{ textAlign: "left", padding: "6px 8px", borderRadius: 6 }}>
                <div className="mono">{a.id}{a.id === currentAgentId ? " (current)" : ""}</div>
                {a.description ? <div className="muted text-sm">{a.description}</div> : null}
              </button>
            ))}
            {shown.length === 0 ? <div className="muted text-sm">No agents match.</div> : null}
          </div>
          {pages > 1 && (
            <div style={{ display: "flex", justifyContent: "space-between", marginTop: 6 }}>
              <button className="chip" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Prev</button>
              <span className="muted text-sm">{page + 1}/{pages}</span>
              <button className="chip" disabled={page >= pages - 1} onClick={() => setPage((p) => p + 1)}>Next</button>
            </div>
          )}
        </div>
      )}
    </span>
  );
}

// ============================================================================
// ChatDetail — conversation view (REST replay + live WS stream)
// ============================================================================

function ChatDetail({ chatId, onBack, pushToast }) {
  const { useResource, useViewport, apiFetch } = window.primerApi;
  const { isMobile } = useViewport();
  const cid = chatId;
  // Mobile-only kebab actions sheet (collapses Token meter + Compact +
  // status pill that don't fit in the slim mobile header).
  const [actionsOpen, setActionsOpen] = React.useState(false);
  // R3 (Task F2) built the structured-output <SchemaPanel> shell but
  // ChatDetail hard-coded it permanently hidden with no way for an
  // operator to reach it. This toggle lives in the same top-right
  // chrome slot as the agent selector (R1) so flipping it on shows the
  // panel as <Conversation>'s collapsible right-hand sibling.
  const [showSchemaPanel, setShowSchemaPanel] = React.useState(false);

  // Status bag reported by <Conversation> (Task B2 moved the WS/data
  // lifecycle wholesale into that component) — this host only needs
  // enough of it to drive its own page chrome: the TokenMeter, the
  // connection badge, the status pill, the mobile kebab sheet, and
  // the "chat not found" gate below.
  const [convStatus, setConvStatus] = React.useState({
    wsState: "connecting",
    usage: { input_tokens: 0, output_tokens: 0, context_length: 0 },
    compactInFlight: false,
    historyError: null,
    requestCompact: null,
  });

  // Chat row (status, agent_id) — small one-shot fetch + light polling
  // so the header pill mirrors a server-driven "ended" state. Shares
  // the useResource cache entry with <Conversation>'s own fetch of
  // the same key (see foundation/use-resource.js), so this does NOT
  // double the network traffic.
  const chat = useResource(
    `chat-detail:${cid}`,
    (s) => apiFetch("GET", `/chats/${encodeURIComponent(cid)}`, null, { signal: s }),
    { pollMs: 10000, deps: [cid] }
  );

  const wsBadge = convStatus.wsState === "open"
    ? <span className="pill pill-running" title="WebSocket open"><span className="dot"></span>live</span>
    : convStatus.wsState === "connecting"
      ? <span className="pill pill-paused" title="WebSocket connecting"><span className="dot"></span>connecting</span>
      : <span className="pill pill-ended" title="WebSocket closed"><span className="dot"></span>offline</span>;

  if (convStatus.historyError && convStatus.historyError.status === 404) {
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
  const chatTitle = chatRow?.title || null;

  return (
    <div
      className="col"
      style={{
        gap: 14,
        // On mobile use 100dvh (dynamic viewport height) so the
        // container shrinks when the browser address bar slides in;
        // 100vh on iOS/Android Chrome is the *large* viewport that
        // ignores the address bar, which makes the inner scroll
        // container overflow the visible area and the BODY scrolls
        // instead of the transcript's own scroll container — breaking
        // pull-up-to-load-older and lazy-prepend.
        //
        // Mobile deduction is just the global topbar (48px) plus a
        // few px of breathing room; the chat panel renders its own
        // mobile header inside this container so there's no separate
        // page-header above us. Desktop keeps the original 180px
        // (topbar + page-header chrome).
        height: isMobile ? "calc(100dvh - 56px)" : "calc(100vh - 180px)",
        display: "flex",
        flexDirection: "column",
        minWidth: 0,
        overflowX: "hidden",
      }}
    >
      <div className="panel" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0, minWidth: 0 }}>
        {isMobile ? (
          <div className="chat-mobile-header">
            <button
              className="icon-btn touch-target"
              aria-label="Back"
              onClick={() => (typeof onBack === "function" ? onBack() : window.history.back())}
            >
              <Icon name="chevron-left" size={16} />
            </button>
            <span className="title" title={chatTitle || cid}>
              {chatTitle || cid}
            </span>
            <button
              className="icon-btn touch-target"
              aria-label="More actions"
              data-testid="chat-mobile-kebab"
              onClick={() => setActionsOpen(true)}
            >
              <Icon name="more-vertical" size={16} />
            </button>
          </div>
        ) : (
        <div className="panel-h">
          <Icon name="send" size={13} style={{ color: "var(--accent)" }} />
          {chatTitle ? (
            <>
              <span title={cid} style={{ maxWidth: 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{chatTitle}</span>
              <span className="sub mono" style={{ fontSize: 11 }}>· <span style={{ color: "var(--text-3)" }}>{cid}</span></span>
            </>
          ) : (
            <span className="mono">{cid}</span>
          )}
          <div className="right" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <window.TokenMeter
              inputTokens={convStatus.usage.input_tokens}
              contextLength={convStatus.usage.context_length}
              onCompact={chatStatus === "ended" ? null : convStatus.requestCompact}
              compactDisabled={convStatus.compactInFlight || convStatus.wsState !== "open"}
              compactTooltip={
                convStatus.compactInFlight
                  ? "Compaction in progress…"
                  : convStatus.wsState !== "open"
                    ? "WebSocket offline"
                    : ""
              }
            />
            {wsBadge}
            <span className={chatStatus === "active" ? "pill pill-running" : "pill pill-ended"}>
              <span className="dot"></span>{chatStatus}
            </span>
          </div>
        </div>
        )}
        <Conversation
          chatId={cid}
          pushToast={pushToast}
          onStatus={setConvStatus}
          headerSlot={null}
          // R1: the agent selector lives in the host's top-right chrome
          // slot now, next to the back/title chrome above, instead of
          // the composer row — <Conversation> just renders whatever
          // node this host hands it (see conversation.jsx). `placement`
          // flips to "down" (was "up" in the old bottom-composer spot)
          // since the popover now opens below the trigger.
          rightChromeSlot={
            <>
              <CT_AgentSwitcher
                chatId={cid}
                currentAgentId={chatAgent}
                pushToast={pushToast}
                placement="down"
                disabled={chatStatus === "ended"}
              />
              <button
                type="button"
                className={"chip" + (showSchemaPanel ? " active" : "")}
                onClick={() => setShowSchemaPanel((v) => !v)}
                title={showSchemaPanel ? "Hide structured output schema panel" : "Show structured output schema panel"}
                data-testid="chat-schema-panel-toggle"
                style={{ display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap" }}
              >
                <Icon name="settings" size={12} />
                schema
              </button>
            </>
          }
          showSchemaPanel={showSchemaPanel}
          onCloseSchemaPanel={() => setShowSchemaPanel(false)}
        />
      </div>

      {/* Mobile-only kebab actions sheet — collapses TokenMeter,
          Compact action, connection badge, and chat status pill that
          don't fit in the slim mobile header. */}
      {isMobile && (
        <BottomSheet
          open={actionsOpen}
          onClose={() => setActionsOpen(false)}
          title="Chat actions"
        >
          <div className="col" style={{ gap: 12 }}>
            <window.TokenMeter
              inputTokens={convStatus.usage.input_tokens}
              contextLength={convStatus.usage.context_length}
              onCompact={chatStatus === "ended" ? null : () => {
                setActionsOpen(false);
                convStatus.requestCompact && convStatus.requestCompact();
              }}
              compactDisabled={convStatus.compactInFlight || convStatus.wsState !== "open"}
              compactTooltip={
                convStatus.compactInFlight
                  ? "Compaction in progress…"
                  : convStatus.wsState !== "open"
                    ? "WebSocket offline"
                    : ""
              }
            />
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {wsBadge}
              <span className={chatStatus === "active" ? "pill pill-running" : "pill pill-ended"}>
                <span className="dot"></span>{chatStatus}
              </span>
            </div>
            <div className="muted text-sm mono">agent: {chatAgent}</div>
            <div className="muted text-sm mono" style={{ wordBreak: "break-all" }}>{cid}</div>
          </div>
        </BottomSheet>
      )}
    </div>
  );
}

// ============================================================================
// Helpers: attachment chip
// ============================================================================
//
// Assistant-token coalescing moved to
// ui/components/chat/use-transcript.js (window.chatCoalesce) as part
// of Task B2 — it's now shared with <Conversation> and unit-testable
// on its own. CT_ThinkingBubble (and the rest of the row renderers)
// moved to ui/components/chat/transcript.jsx as part of Task B3.

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

// CT_ExpandableToolRow, Message, CT_AttachmentPart, and CompactionMarker
// moved to ui/components/chat/transcript.jsx as part of Task B3
// (chat-refactor plan) — they are the transcript's row renderers now.
window.ChatsPage = ChatsPage;
window.ChatDetail = ChatDetail;
