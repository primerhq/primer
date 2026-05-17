/* global React */
// Mock data + live tick

const AGENTS = [
  { id: "support-triage", desc: "Routes inbound support tickets to the right queue", provider: "openai" },
  { id: "stripe-refunds", desc: "Issues partial refunds against Stripe based on policy", provider: "anthropic" },
  { id: "pr-reviewer", desc: "Reviews pull requests and posts inline comments", provider: "anthropic" },
  { id: "code-explainer", desc: "Walks junior engineers through unfamiliar code paths", provider: "anthropic" },
  { id: "doc-ingestion", desc: "Ingests new docs into the knowledge base", provider: "openai" },
  { id: "sql-helper", desc: "Translates English into safe read-only SQL", provider: "openai" },
  { id: "release-notes", desc: "Drafts release notes from a range of commits", provider: "anthropic" },
  { id: "agent-broken-llm", desc: "Test fixture with a deleted LLM provider", provider: null },
];

const WORKSPACES = [
  "ws-3f8a9bc1d4e2",
  "ws-7c2d4e9a8b15",
  "ws-1a5e7d3f9c80",
  "ws-9b4c8e1d5a72",
  "ws-2d8f4a7c3b91",
];

const WORKSPACE_DETAILS = {
  "ws-3f8a9bc1d4e2": { template: "python-3.11-slim", provider: "container", created_at_ago: 184 * 1000 },
  "ws-7c2d4e9a8b15": { template: "node-22", provider: "container", created_at_ago: 920 * 1000 },
  "ws-1a5e7d3f9c80": { template: "stripe-toolkit", provider: "container", created_at_ago: 38 * 1000 },
  "ws-9b4c8e1d5a72": { template: "python-3.11-slim", provider: "local", created_at_ago: 3600 * 1000 * 2 },
  "ws-2d8f4a7c3b91": { template: "node-22", provider: "k8s", created_at_ago: 600 * 1000 },
};

// File tree fixture for ws detail
const FILE_TREE = {
  "/": {
    type: "dir",
    children: ["src", "scripts", "tests", "README.md", "pyproject.toml", ".env.example", ".state", ".tmp"],
  },
  "/src": {
    type: "dir",
    children: ["main.py", "handlers", "models.py", "config.py", "__init__.py"],
  },
  "/src/handlers": {
    type: "dir",
    children: ["webhook.py", "queue.py", "__init__.py"],
  },
  "/scripts": { type: "dir", children: ["bootstrap.sh", "seed_db.py"] },
  "/tests": { type: "dir", children: ["test_handlers.py", "conftest.py"] },
  "/.state": { type: "dir", system: true, children: ["history.jsonl", "checkpoints", "session-meta.json"] },
  "/.state/checkpoints": { type: "dir", system: true, children: ["turn-001.json", "turn-002.json"] },
  "/.tmp": { type: "dir", system: true, children: [] },
  "/README.md": { type: "file", size: 1842, lang: "markdown" },
  "/pyproject.toml": { type: "file", size: 412, lang: "toml" },
  "/.env.example": { type: "file", size: 184, lang: "env" },
  "/src/main.py": { type: "file", size: 2841, lang: "python", preview: true },
  "/src/models.py": { type: "file", size: 1582, lang: "python" },
  "/src/config.py": { type: "file", size: 412, lang: "python" },
  "/src/__init__.py": { type: "file", size: 0, lang: "python" },
  "/src/handlers/webhook.py": { type: "file", size: 1924, lang: "python" },
  "/src/handlers/queue.py": { type: "file", size: 1108, lang: "python" },
  "/src/handlers/__init__.py": { type: "file", size: 0, lang: "python" },
  "/scripts/bootstrap.sh": { type: "file", size: 412, lang: "shell" },
  "/scripts/seed_db.py": { type: "file", size: 1284, lang: "python" },
  "/tests/test_handlers.py": { type: "file", size: 2840, lang: "python" },
  "/tests/conftest.py": { type: "file", size: 412, lang: "python" },
};

const FILE_PREVIEWS = {
  "/src/main.py": `"""Webhook ingestion service for Stripe events."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from .config import Settings
from .handlers.webhook import handle_event
from .handlers.queue import worker_loop


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Spin up the worker loop alongside the API."""
    settings = Settings.from_env()
    task = asyncio.create_task(worker_loop(settings))
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.post("/stripe/webhook")
async def stripe_webhook(req: Request) -> dict:
    body = await req.body()
    event = await handle_event(body, req.headers["stripe-signature"])
    return {"received": True, "id": event["id"]}
`,
  "/README.md": `# Webhook service

Ingests Stripe webhook events and dispatches them to the Matrix queue.

## Quickstart

\`\`\`shell
./scripts/bootstrap.sh
uvicorn src.main:app --reload
\`\`\`

## Environment

See \`.env.example\` for required variables. The session runtime injects
\`MATRIX_SESSION_ID\` automatically — do not hardcode it.
`,
  "/pyproject.toml": `[project]
name = "webhook-svc"
version = "0.4.2"
requires-python = ">=3.11"

dependencies = [
  "fastapi>=0.110",
  "httpx>=0.27",
  "stripe>=7.0",
]
`,
};

const GIT_LOG = [
  { sha: "7f3a9c2", at: 3, msg: "session sess-7f3a9c2b8d14 turn 3" },
  { sha: "b2d8e1f", at: 28, msg: "session sess-7f3a9c2b8d14 turn 2" },
  { sha: "a4c0e8d", at: 92, msg: "session sess-7f3a9c2b8d14 turn 1" },
  { sha: "c8e2f1a", at: 188, msg: "session sess-7f3a9c2b8d14 turn 0 — init" },
  { sha: "f1d4a3b", at: 920, msg: "session sess-9a4f2c8b1e75 turn 3" },
  { sha: "8d2b9c1", at: 1100, msg: "session sess-9a4f2c8b1e75 turn 2" },
  { sha: "e7c4a8f", at: 1420, msg: "session sess-9a4f2c8b1e75 turn 1" },
  { sha: "2a8e1d5", at: 1680, msg: "session sess-9a4f2c8b1e75 turn 0 — init" },
  { sha: "5b3f9c8", at: 7600, msg: "session sess-6c3f9a2b7d18 turn 4" },
  { sha: "1e8a4d2", at: 7820, msg: "session sess-6c3f9a2b7d18 turn 3" },
  { sha: "9c4f2b8", at: 8100, msg: "session sess-6c3f9a2b7d18 turn 2" },
  { sha: "3f8c1a9", at: 8420, msg: "session sess-6c3f9a2b7d18 turn 1" },
  { sha: "6d2e8c4", at: 8720, msg: "session sess-6c3f9a2b7d18 turn 0 — init" },
  { sha: "init0000", at: 8800, msg: "init" },
];

const GRAPHS = ["graph-tier1-escalation", "graph-onboarding-wizard"];

// Build a session
let _sid = 0;
function nextSid() { _sid += 1; return _sid.toString(36).padStart(6, "0") + Math.random().toString(36).slice(2, 8); }

function buildSessions(now) {
  const items = [
    // running, multi-turn
    {
      id: "sess-7f3a9c2b8d14",
      status: "running",
      binding_kind: "agent",
      agent_id: "support-triage",
      workspace_id: "ws-3f8a9bc1d4e2",
      graph_id: null,
      created_at: new Date(now - 1000 * 184),
      started_at: new Date(now - 1000 * 182),
      last_turn_at: new Date(now - 1000 * 3),
      turn_count: 3,
      worker_id: "wrk-3a8e",
      attempt: 1,
      instructions: "Triage this incoming customer email and assign it to the right Zendesk queue. If it's a refund request under $50, draft a response and mark resolved.",
      error: null,
    },
    {
      id: "sess-1c4d8b7e9a36",
      status: "running",
      binding_kind: "agent",
      agent_id: "pr-reviewer",
      workspace_id: "ws-7c2d4e9a8b15",
      graph_id: null,
      created_at: new Date(now - 1000 * 62),
      started_at: new Date(now - 1000 * 60),
      last_turn_at: new Date(now - 1000 * 1),
      turn_count: 2,
      worker_id: "wrk-9d2f",
      attempt: 1,
      instructions: "Review PR #4218 — focus on the migration in db/0017_add_billing_index.sql and the new retry logic in workers/scheduler.py.",
      error: null,
    },
    {
      id: "sess-9b2e6f1a4c87",
      status: "running",
      binding_kind: "agent",
      agent_id: "stripe-refunds",
      workspace_id: "ws-1a5e7d3f9c80",
      graph_id: null,
      created_at: new Date(now - 1000 * 38),
      started_at: new Date(now - 1000 * 36),
      last_turn_at: new Date(now - 1000 * 6),
      turn_count: 1,
      worker_id: "wrk-7c1b",
      attempt: 1,
      instructions: "Process the partial refund on charge ch_3OZ4mQ — customer was double-billed for two seats.",
      error: null,
    },
    {
      id: "sess-4e8c2a1d6f53",
      status: "paused",
      binding_kind: "agent",
      agent_id: "code-explainer",
      workspace_id: "ws-9b4c8e1d5a72",
      graph_id: null,
      created_at: new Date(now - 1000 * 540),
      started_at: new Date(now - 1000 * 538),
      last_turn_at: new Date(now - 1000 * 412),
      turn_count: 5,
      worker_id: "wrk-3a8e",
      attempt: 1,
      instructions: "Walk me through how session state propagates from scheduler.claim() through worker.run_turn() and back to storage.",
      error: null,
    },
    {
      id: "sess-2a6d4f8b1c95",
      status: "created",
      binding_kind: "agent",
      agent_id: "release-notes",
      workspace_id: "ws-2d8f4a7c3b91",
      graph_id: null,
      created_at: new Date(now - 1000 * 14),
      started_at: null,
      last_turn_at: null,
      turn_count: 0,
      worker_id: null,
      attempt: 0,
      instructions: "Draft release notes for v2.14.0 covering commits between abc123 and def456.",
      error: null,
    },
    {
      id: "sess-6c3f9a2b7d18",
      status: "ended",
      binding_kind: "agent",
      agent_id: "doc-ingestion",
      workspace_id: "ws-3f8a9bc1d4e2",
      graph_id: null,
      created_at: new Date(now - 1000 * 1820),
      started_at: new Date(now - 1000 * 1818),
      last_turn_at: new Date(now - 1000 * 1612),
      turn_count: 4,
      worker_id: "wrk-9d2f",
      attempt: 1,
      instructions: "Ingest the latest copy of the API reference into the docs collection. Use chunk size 800.",
      error: null,
    },
    {
      id: "sess-8d4a1f3b6e29",
      status: "failed",
      binding_kind: "agent",
      agent_id: "sql-helper",
      workspace_id: "ws-7c2d4e9a8b15",
      graph_id: null,
      created_at: new Date(now - 1000 * 2640),
      started_at: new Date(now - 1000 * 2638),
      last_turn_at: new Date(now - 1000 * 2620),
      turn_count: 1,
      worker_id: "wrk-7c1b",
      attempt: 1,
      instructions: "Query the events table for last week's signups grouped by country.",
      error: {
        type: "/errors/provider-server-error",
        title: "LLM provider returned 502",
        status: 502,
        detail: "Upstream provider 'openai-1' returned 502 Bad Gateway after 3 retries. Last request_id: req_8d4a1f3b",
        instance: "/v1/sessions/sess-8d4a1f3b6e29/turns",
        extensions: { request_id: "req_a92e8c4b1d76", provider_kind: "openai", upstream_status: 502 },
      },
    },
    {
      id: "sess-5b9e3c8a2d47",
      status: "ended",
      binding_kind: "agent",
      agent_id: "support-triage",
      workspace_id: "ws-1a5e7d3f9c80",
      graph_id: null,
      created_at: new Date(now - 1000 * 3600 * 2),
      started_at: new Date(now - 1000 * (3600 * 2 - 2)),
      last_turn_at: new Date(now - 1000 * (3600 * 2 - 240)),
      turn_count: 6,
      worker_id: "wrk-3a8e",
      attempt: 1,
      instructions: "Process the next 10 items from the unassigned queue.",
      error: null,
    },
    {
      id: "sess-3e7f1a9b4c62",
      status: "cancelled",
      binding_kind: "agent",
      agent_id: "release-notes",
      workspace_id: "ws-2d8f4a7c3b91",
      graph_id: null,
      created_at: new Date(now - 1000 * 3600 * 5),
      started_at: new Date(now - 1000 * (3600 * 5 - 2)),
      last_turn_at: new Date(now - 1000 * (3600 * 5 - 40)),
      turn_count: 1,
      worker_id: "wrk-7c1b",
      attempt: 1,
      instructions: "Auto-cancelled by retention policy after 48h pause.",
      error: null,
    },
    {
      id: "sess-1f8c4a2e6b39",
      status: "failed",
      binding_kind: "graph",
      agent_id: null,
      workspace_id: "ws-9b4c8e1d5a72",
      graph_id: "graph-tier1-escalation",
      created_at: new Date(now - 1000 * 720),
      started_at: new Date(now - 1000 * 718),
      last_turn_at: new Date(now - 1000 * 714),
      turn_count: 0,
      worker_id: "wrk-3a8e",
      attempt: 1,
      instructions: "Route this escalation through the tier-1 → tier-2 graph.",
      error: {
        type: "/errors/internal",
        title: "NotImplementedError: graph executor",
        status: 500,
        detail: "Graph executor is not implemented (T0612). The session was claimed but failed on the first turn.",
        instance: "/v1/sessions/sess-1f8c4a2e6b39/turns",
        extensions: { request_id: "req_4d8e1f2a9c63" },
      },
    },
    {
      id: "sess-9a4f2c8b1e75",
      status: "ended",
      binding_kind: "agent",
      agent_id: "pr-reviewer",
      workspace_id: "ws-7c2d4e9a8b15",
      graph_id: null,
      created_at: new Date(now - 1000 * 3600 * 8),
      started_at: new Date(now - 1000 * (3600 * 8 - 2)),
      last_turn_at: new Date(now - 1000 * (3600 * 8 - 312)),
      turn_count: 3,
      worker_id: "wrk-9d2f",
      attempt: 1,
      instructions: "Review PR #4205.",
      error: null,
    },
    {
      id: "sess-7b3d9e1a5c84",
      status: "ended",
      binding_kind: "agent",
      agent_id: "stripe-refunds",
      workspace_id: "ws-1a5e7d3f9c80",
      graph_id: null,
      created_at: new Date(now - 1000 * 3600 * 12),
      started_at: new Date(now - 1000 * (3600 * 12 - 2)),
      last_turn_at: new Date(now - 1000 * (3600 * 12 - 88)),
      turn_count: 2,
      worker_id: "wrk-3a8e",
      attempt: 1,
      instructions: "Issue refund for charge ch_3NY2vT.",
      error: null,
    },
    {
      id: "sess-4c1a8f2d6b93",
      status: "ended",
      binding_kind: "agent",
      agent_id: "doc-ingestion",
      workspace_id: "ws-3f8a9bc1d4e2",
      graph_id: null,
      created_at: new Date(now - 1000 * 3600 * 18),
      started_at: new Date(now - 1000 * (3600 * 18 - 2)),
      last_turn_at: new Date(now - 1000 * (3600 * 18 - 540)),
      turn_count: 8,
      worker_id: "wrk-7c1b",
      attempt: 1,
      instructions: "Re-ingest the changelog directory.",
      error: null,
    },
  ];
  return items;
}

const WORKERS = [
  { id: "wrk-3a8e", host: "matrix-w1.local", pid: 28401, status: "active", capacity: 4, in_flight: 2, started_at: 8400, heartbeat: 1.2 },
  { id: "wrk-9d2f", host: "matrix-w2.local", pid: 28402, status: "active", capacity: 4, in_flight: 1, started_at: 8400, heartbeat: 0.8 },
  { id: "wrk-7c1b", host: "matrix-w3.local", pid: 28403, status: "active", capacity: 4, in_flight: 1, started_at: 8400, heartbeat: 2.1 },
  { id: "wrk-1e5d", host: "matrix-w4.local", pid: 28404, status: "draining", capacity: 4, in_flight: 0, started_at: 8400, heartbeat: 1.5 },
];

window.MOCK = { AGENTS, WORKSPACES, WORKSPACE_DETAILS, FILE_TREE, FILE_PREVIEWS, GIT_LOG, GRAPHS, WORKERS, buildSessions };
