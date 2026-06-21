"""Capture real API responses as embed fixtures.
# NOTE: the docs corpus + fixtures now live in the primerhq.github.io repo.
# Repoint the fixture/registry paths (and embed_harness serving paths) at that
# checkout (docs_source/_fixtures) before regenerating fixtures or embeds.

  PRIMER_BASE=http://127.0.0.1:8000 uv run python scripts/docs/capture_fixtures.py

Writes primer/user_docs/_fixtures/<embed>.json keyed by "<METHOD> <path>"
(no /v1 prefix -- matching how the UI calls apiFetch, e.g. apiFetch("GET", "/workers", ...)).
"""
import json
import os
import tempfile

import httpx

BASE = os.environ.get("PRIMER_BASE", "http://127.0.0.1:8000")
# Credentials: override via PRIMER_DOCS_USER / PRIMER_DOCS_PASS if the dev
# server was bootstrapped with a different account. Default values are used
# for first-boot or a fresh test database (register is idempotent when no
# user exists yet; it returns 409 when registration is already locked --
# in that case only the login attempt matters).
USER = {
    "username": os.environ.get("PRIMER_DOCS_USER", "docs"),
    "password": os.environ.get("PRIMER_DOCS_PASS", "docs-password-123"),
}
OUT = "primer/user_docs/_fixtures"
os.makedirs(OUT, exist_ok=True)


def client():
    c = httpx.Client(base_url=BASE, timeout=30)
    # register is idempotent: 201 on first boot, 409 when already locked
    c.post("/v1/auth/register", json=USER)
    r = c.post("/v1/auth/login", json=USER)
    if r.status_code != 200:
        raise RuntimeError(
            f"login failed ({r.status_code}): {r.text}\n"
            "Hint: if the server was bootstrapped with a different account, "
            "set PRIMER_DOCS_USER and PRIMER_DOCS_PASS to the existing credentials."
        )
    return c


def save(name, pairs):
    with open(f"{OUT}/{name}.json", "w") as f:
        json.dump(pairs, f, indent=2)
    print(f"  wrote {name}.json ({len(pairs)} keys)")


def _ensure_llm_provider(c, pid="demo-openai"):
    r = c.post(
        "/v1/llm_providers",
        json={
            "id": pid,
            "provider": "openai",
            "models": [{"name": "gpt-4o", "context_length": 128000}],
            "config": {"api_key": "sk-demo"},
            "limits": {"max_concurrency": 4},
        },
    )
    # 201=created, 409=already exists -- both are fine
    return pid


def _ensure_agent(c, agent_id="weekly-digest", provider_id="demo-openai"):
    c.post(
        "/v1/agents",
        json={
            "id": agent_id,
            "description": "Summarises the week",
            "model": {"provider_id": provider_id, "model_name": "gpt-4o"},
            "system_prompt": ["You write concise weekly digests."],
            "tools": [],
        },
    )
    return agent_id


# ---------------------------------------------------------------------------
# agents-page
# ---------------------------------------------------------------------------

def capture_agents(c):
    pid = _ensure_llm_provider(c)
    aid = _ensure_agent(c, provider_id=pid)
    save("agents-page", {
        "GET /agents?limit=200&offset=0": c.get("/v1/agents?limit=200&offset=0").json(),
        "GET /agents/weekly-digest": c.get(f"/v1/agents/{aid}").json(),
        "GET /llm_providers?limit=200": c.get("/v1/llm_providers?limit=200").json(),
    })


# ---------------------------------------------------------------------------
# sessions-list  (agent session, no auto_start -> CREATED state, no real LLM needed)
# ---------------------------------------------------------------------------

def capture_sessions(c):
    pid = _ensure_llm_provider(c)
    aid = _ensure_agent(c, provider_id=pid)

    # Need a workspace to create a session
    with tempfile.TemporaryDirectory() as tmp:
        wp_id = "docs-wp-local"
        tpl_id = "docs-tpl-local"
        c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "root_path": tmp},
        })
        c.post("/v1/workspace_templates", json={
            "id": tpl_id, "description": "docs fixture template",
            "provider_id": wp_id, "backend": {"kind": "local"},
        })
        ws_r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        if ws_r.status_code not in (200, 201):
            # workspace may already exist from a previous run -- list to find it
            ws_list = c.get("/v1/workspaces?limit=200").json()
            items = ws_list.get("items", [])
            wid = items[0]["id"] if items else None
        else:
            wid = ws_r.json()["id"]

        sid = None
        if wid:
            sess_r = c.post(f"/v1/workspaces/{wid}/sessions", json={
                "binding": {"kind": "agent", "agent_id": aid},
                "auto_start": False,
            })
            if sess_r.status_code in (200, 201):
                sid = sess_r.json()["id"]

    sessions_list = c.get("/v1/sessions?limit=200").json()
    session_detail = c.get(f"/v1/sessions/{sid}").json() if sid else {"_note": "no session seeded"}

    save("sessions-list", {
        "GET /sessions?limit=200": sessions_list,
        "GET /agents?limit=200": c.get("/v1/agents?limit=200").json(),
        "GET /workspaces?limit=200": c.get("/v1/workspaces?limit=200").json(),
    })
    save("session-detail", {
        "GET /sessions/{id}": session_detail,
    })


# ---------------------------------------------------------------------------
# chat-stream  (empty-valid: POST /chats requires a live LLM to stream)
# ---------------------------------------------------------------------------

def capture_chat_stream(c):
    pid = _ensure_llm_provider(c)
    aid = _ensure_agent(c, provider_id=pid)

    chats = c.get("/v1/chats?limit=200").json()
    # Try to create a chat (won't stream without a real LLM, but the row is created)
    chat_r = c.post("/v1/chats", json={"agent_id": aid})
    if chat_r.status_code in (200, 201):
        cid = chat_r.json()["id"]
        chat_detail = c.get(f"/v1/chats/{cid}").json()
        messages = c.get(f"/v1/chats/{cid}/messages").json()
    else:
        cid = None
        chat_detail = {"_note": "chat creation failed without real LLM", "status": chat_r.status_code}
        messages = {"items": [], "_note": "empty -- no real LLM configured"}

    save("chat-stream", {
        "GET /chats?limit=200": chats,
        "GET /chats/{id}": chat_detail,
        "GET /chats/{id}/messages": messages,
        "GET /agents?limit=200": c.get("/v1/agents?limit=200").json(),
    })


# ---------------------------------------------------------------------------
# workspaces
# ---------------------------------------------------------------------------

def capture_workspaces(c):
    workspaces = c.get("/v1/workspaces?limit=200").json()
    templates = c.get("/v1/workspace_templates?limit=200").json()
    providers = c.get("/v1/workspace_providers?limit=200").json()

    # workspace-template-form needs provider list
    save("workspaces", {
        "GET /workspaces?limit=200": workspaces,
        "GET /workspace_templates?limit=200": templates,
        "GET /workspace_providers?limit=200": providers,
    })
    save("workspace-template-form", {
        "GET /workspace_templates?limit=200": templates,
        "GET /workspace_providers?limit=200": providers,
    })


# ---------------------------------------------------------------------------
# trigger-create
# ---------------------------------------------------------------------------

def capture_triggers(c):
    # Seed a demo trigger (delayed, far future -- no agent required)
    slug = "docs-demo-trigger"
    c.post("/v1/triggers", json={
        "slug": slug,
        "name": "Demo digest trigger",
        "config": {"kind": "delayed", "fire_at": "2099-12-31T09:00:00Z"},
    })

    triggers = c.get("/v1/triggers").json()

    save("trigger-create", {
        "GET /triggers": triggers,
        "GET /agents?limit=200": c.get("/v1/agents?limit=200").json(),
        "GET /graphs?limit=200": c.get("/v1/graphs?limit=200").json(),
        "GET /workspaces?limit=200": c.get("/v1/workspaces?limit=200").json(),
        "GET /chats?limit=200": c.get("/v1/chats?limit=200").json(),
    })


# ---------------------------------------------------------------------------
# channels  (channel_providers need real credentials to connect; capture empty-valid)
# ---------------------------------------------------------------------------

def capture_channels(c):
    providers = c.get("/v1/channel_providers?limit=200").json()
    channels = c.get("/v1/channels?limit=200").json()
    workspaces = c.get("/v1/workspaces?limit=200").json()

    # NOTE: seeding a real channel provider requires live Slack/Telegram/Discord
    # credentials. Captured with empty-but-valid envelopes.
    # Channel.config.chats carries chat enablement (enabled, default_agent,
    # allowed_agents, relay_mode). Workspace.channel_association carries the
    # single channel a workspace's session gates forward to (set/clear via
    # PUT/DELETE /v1/workspaces/{id}/channel_association).
    save("channels", {
        "GET /channel_providers?limit=200": providers,
        "GET /channels?limit=200": channels,
        "GET /workspaces?limit=200": workspaces,
        "_note": "channel_providers empty-valid: seeding requires live platform credentials",
    })


# ---------------------------------------------------------------------------
# graph-canvas
# ---------------------------------------------------------------------------

def capture_graphs(c):
    pid = _ensure_llm_provider(c)
    aid = _ensure_agent(c, provider_id=pid)

    # Create a minimal graph (begin -> agent -> end)
    gid = "docs-demo-graph"
    c.post("/v1/graphs", json={
        "id": gid,
        "description": "Demo weekly digest graph",
        "nodes": [
            {"kind": "begin", "id": "start"},
            {"kind": "agent", "id": "run", "agent_id": aid, "input_template": "{{ instructions }}"},
            {"kind": "end", "id": "done", "output_template": "{{ nodes.run.text }}"},
        ],
        "edges": [
            {"kind": "static", "from_node": "start", "to_node": "run"},
            {"kind": "static", "from_node": "run", "to_node": "done"},
        ],
    })

    graphs = c.get("/v1/graphs?limit=200").json()
    graph_detail = c.get(f"/v1/graphs/{gid}").json()

    save("graph-canvas", {
        "GET /graphs?limit=200": graphs,
        "GET /graphs/{id}": graph_detail,
        "GET /agents?limit=200": c.get("/v1/agents?limit=200").json(),
        "GET /tools/catalogue": c.get("/v1/tools/catalogue").json(),
    })


# ---------------------------------------------------------------------------
# workers-stats
# ---------------------------------------------------------------------------

def capture_workers(c):
    workers = c.get("/v1/workers").json()
    health = c.get("/v1/health").json()

    save("workers-stats", {
        "GET /workers": workers,
        "GET /health": health,
    })


# ---------------------------------------------------------------------------
# collection-list  (embedding providers needed to ingest; capture empty-valid)
# ---------------------------------------------------------------------------

def capture_collections(c):
    collections = c.get("/v1/collections?limit=200").json()
    embedding_providers = c.get("/v1/embedding_providers?limit=200").json()
    ssp = c.get("/v1/ssp?limit=200").json()

    # NOTE: creating a populated collection requires a live embedding provider.
    # Captured with empty-but-valid envelopes.
    save("collection-list", {
        "GET /collections?limit=200": collections,
        "GET /embedding_providers?limit=200": embedding_providers,
        "GET /ssp?limit=200": ssp,
        "_note": "collections empty-valid: ingestion requires live embedding provider",
    })


# ---------------------------------------------------------------------------
# api-token-create
# ---------------------------------------------------------------------------

def capture_api_tokens(c):
    # Seed a demo token
    token_r = c.post("/v1/auth/tokens", json={
        "name": "docs-demo-token",
        "scopes": [],
        "expires_at": None,
    })
    token_list = c.get("/v1/auth/tokens").json()

    created = token_r.json() if token_r.status_code in (200, 201) else {
        "_note": "token create failed", "status": token_r.status_code,
    }

    save("api-token-create", {
        "POST /auth/tokens": created,
        "GET /auth/tokens": token_list,
    })


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"connecting to {BASE} ...")
    c = client()
    print("authenticated")

    tasks = [
        ("agents-page", capture_agents),
        ("sessions", capture_sessions),
        ("chat-stream", capture_chat_stream),
        ("workspaces", capture_workspaces),
        ("trigger-create", capture_triggers),
        ("channels", capture_channels),
        ("graph-canvas", capture_graphs),
        ("workers-stats", capture_workers),
        ("collection-list", capture_collections),
        ("api-token-create", capture_api_tokens),
    ]

    succeeded = []
    failed = []
    for label, fn in tasks:
        try:
            fn(c)
            succeeded.append(label)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED {label}: {exc}")
            failed.append((label, exc))

    print(f"\nDone: {len(succeeded)} succeeded, {len(failed)} failed")
    if failed:
        for label, exc in failed:
            print(f"  - {label}: {exc}")
