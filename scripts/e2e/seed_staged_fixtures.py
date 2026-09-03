"""Seed a live e2e stack with one instance of every fixture-gated entity
kind the console UI can render: an approval-gated session in all four
decision states (approved/rejected/cancelled/timed-out, plus one left
genuinely pending), an ask_user park (with a response_schema enum, so
the radio-button UI path is exercised, not just free text), one trigger
per kind (delayed/scheduled/webhook/channel) with a subscription, a
channel + provider, a harness, a service with 2 published versions, a
Python toolset, a non-system collection + document, a graph (with a
bound run session), a minted API token, an SSO/OIDC provider, and a
disabled + a non-admin user. Originally written for the uiv2 mockup-vs-
actual reconciliation pass (01a06494); promoted here (01a064cd) because
the staging need — a live stack with every "empty state" view populated
— recurs for any UI/visual work, not just that one pass.

Run against a live local e2e-style bringup (scripts/e2e/bringup.sh).
Self-contained: registers its own admin user, starts an in-process
multi-scenario mock LLM (see MockLLMServer below, built on
tests._support.mock_llm's ScriptRegistry — the same DSL
scripts/e2e/run_mock_llm.py uses, just with several scenarios
registered instead of one).

Two hard-won lessons baked in as fixes, not just comments, so a future
caller doesn't have to rediscover them:

* Workspace tool ids are SHORT and singular-toolset-scoped —
  ``workspace__write`` / ``workspace__read``, not ``workspace__write_file``
  / ``workspace__read_file`` (the mockup's own illustrative fixture text
  uses the long form, which doesn't exist as a real registered tool —
  see ``primer/agent/tool_manager.py``'s ``_workspace_scoped`` map and
  ``primer/workspace/local/tools/{write,read}.py``'s ``id: ClassVar``).
  An approval-policy ``tool_name`` and a scripted LLM's ``emit_tool``
  both need the short form or the call is rejected as an unknown tool
  before any approval gate even runs.
* Custom-seeded entities do NOT satisfy ``GET /v1/auth/status``'s
  ``setup_complete`` — that flag checks for the RESERVED default
  workspace / operator+builder agents / system collection specifically
  (``primer/bootstrap/setup_state.py``), not just "any" LLM provider or
  model profile row. Without completing that bootstrap ensure-pass
  (``POST /v1/setup/seed``, called below right after a provider+profile
  exist), the console renders the first-boot ``SetupWizardGate`` for
  EVERY hash route instead of the real app — every subsequent capture
  or click silently no-ops against the wizard, not the page you think
  you're on.

Idempotency: every entity below has a fixed id (or, for triggers/users,
a fixed slug/username) under a "staged-" namespace. A create that 409s
because the row already exists is treated as success — this script is
safe to run repeatedly against an already-seeded stack. The exceptions
are inherently instance-scoped resources with no natural identity to
dedupe against (sessions, the workspace instance, the token, the
harness, the service, the collection document): each run creates a
FRESH one of these. That's intentional, not a limitation — a stale
session's transcript wouldn't be a representative fixture anyway, and
harness/service/doc creation is cheap. If you need the OLD instances
gone too, tear down and bring the stack back up first (bringup.sh
resets the database).

Known, accepted gap: the workspace instance staged here is LOCAL-backed
only, not a "non-local" (container/k8s) one — no container/k8s runtime
is assumed available in a bare e2e bringup.

Usage:
    PRIMER_E2E_BASE_URL=http://127.0.0.1:8765 \
        uv run python scripts/e2e/seed_staged_fixtures.py

Prints a JSON summary of every entity's id to stdout, and writes the
same to tests/.e2e/seed_summary.json (gitignored) for a companion
script (e.g. a Playwright capture pass) to read.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests._support.mock_llm import Rule, ScriptRegistry, build_app  # noqa: E402

BASE_URL = os.environ.get("PRIMER_E2E_BASE_URL", "http://127.0.0.1:8765")
MOCK_LLM_PORT = int(os.environ.get("SEED_MOCK_LLM_PORT", "8899"))
MOCK_LLM_URL = f"http://127.0.0.1:{MOCK_LLM_PORT}/v1"
SUMMARY_PATH = ROOT / "tests" / ".e2e" / "seed_summary.json"

ADMIN_USER = "staged-admin"
ADMIN_PASSWORD = "staged-admin-password-1"

# ---------------------------------------------------------------------------
# In-process multi-scenario mock LLM
# ---------------------------------------------------------------------------

MODEL_CHAT = "scripted:chat"          # populated transcript + trace-bearing tool call
MODEL_ASK_USER = "scripted:ask-user"  # parks on ask_user
MODEL_APPROVAL = "scripted:approval"  # parks on an approval-gated tool (workspace__write)
MODEL_APPROVAL_TIMEOUT = "scripted:approval-timeout"  # parks on a SHORT-timeout gate (workspace__read)


def _build_registry() -> ScriptRegistry:
    registry = ScriptRegistry()
    registry.register(MODEL_CHAT, [
        Rule(
            when_tool_result=False,
            emit_tool="misc__uuid_v4",
            emit_args={},
            emit_tool_call_id="call_0",
        ),
        Rule(
            when_tool_result=True,
            emit_text=(
                "I generated a fresh identifier using the uuid tool and "
                "verified it looks well-formed. Here is a longer, more "
                "detailed explanation of what a UUID v4 is, how it is "
                "derived from random bits, and why it is suitable as a "
                "collision-resistant identifier for this kind of task. "
                "This extra length is deliberate: it gives the transcript "
                "and trace panel something substantial to render."
            ),
        ),
    ])
    registry.register(MODEL_ASK_USER, [
        Rule(
            when_tool_result=False,
            emit_tool="system__ask_user",
            # response_schema exercises the radio-button UI path
            # (SH_askOptionsOf normalizes {enum: [...]}) instead of the
            # free-text textarea fallback.
            emit_args={
                "prompt": "Which environment should I target: staging or prod?",
                "response_schema": {"enum": ["staging", "prod"]},
            },
            emit_tool_call_id="call_0",
        ),
        Rule(
            when_tool_result=True,
            emit_text="Thanks, proceeding with that environment.",
        ),
    ])
    registry.register(MODEL_APPROVAL, [
        Rule(
            when_tool_result=False,
            emit_tool="workspace__write",  # NOT workspace__write_file - see module docstring
            emit_args={"path": "release-notes.txt", "content": "staged fixture"},
            emit_tool_call_id="call_0",
        ),
        Rule(
            when_tool_result=True,
            emit_text="Wrote the file as requested.",
        ),
    ])
    registry.register(MODEL_APPROVAL_TIMEOUT, [
        Rule(
            when_tool_result=False,
            emit_tool="workspace__read",  # NOT workspace__read_file - see module docstring
            emit_args={"path": "release-notes.txt"},
            emit_tool_call_id="call_0",
        ),
        Rule(
            when_tool_result=True,
            emit_text="Read the file as requested.",
        ),
    ])
    return registry


class MockLLMServer:
    """Runs tests._support.mock_llm's Starlette app in a background thread."""

    def __init__(self, registry: ScriptRegistry, port: int) -> None:
        app = build_app(registry)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if getattr(self._server, "started", False):
                return
            time.sleep(0.1)
        raise RuntimeError("mock LLM server did not start within 10s")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.c = client

    async def post(self, path: str, **kw: Any) -> httpx.Response:
        return await self.c.post(path, **kw)

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.c.get(path, **kw)

    async def patch(self, path: str, **kw: Any) -> httpx.Response:
        return await self.c.patch(path, **kw)

    async def ensure(self, resp: httpx.Response, *, ok: tuple[int, ...] = (200, 201)) -> Any:
        if resp.status_code not in ok:
            raise RuntimeError(
                f"{resp.request.method} {resp.request.url} -> "
                f"{resp.status_code}: {resp.text[:2000]}"
            )
        try:
            return resp.json()
        except ValueError:
            return None

    async def create_singleton(self, path: str, body: dict) -> Any:
        """POST body to path; a 409 (the fixed-id row already exists
        from a prior run) is treated as success, not an error - the
        caller already knows the id, it's in body["id"]. Makes this
        script safe to re-run against an already-seeded stack instead
        of crashing on the second create of every entity."""
        resp = await self.post(path, json=body)
        if resp.status_code == 409:
            return None
        return await self.ensure(resp)


async def _login_or_register(api: Api) -> None:
    status = await api.ensure(await api.get("/v1/auth/status"))
    if status.get("has_user"):
        r = await api.post(
            "/v1/auth/login",
            json={"username": ADMIN_USER, "password": ADMIN_PASSWORD, "remember": True},
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"login failed ({r.status_code}): {r.text[:500]} -- "
                "the stack already has a DIFFERENT first user; this script "
                "expects either a freshly-reset database or one it seeded "
                "itself before (bringup.sh resets the database)."
            )
    else:
        await api.ensure(await api.post(
            "/v1/auth/register",
            json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        ))


async def _ensure_trigger(api: Api, body: dict) -> str:
    """Create a trigger by slug; on a slug conflict (409, a prior run
    already created it), there's no by-slug GET (only by-id), so list
    by kind and find the matching slug instead."""
    resp = await api.post("/v1/triggers", json=body)
    if resp.status_code == 409:
        kind = body["config"]["kind"]
        listing = await api.ensure(await api.get("/v1/triggers", params={"kind": kind}))
        for item in listing["items"]:
            if item["slug"] == body["slug"]:
                return item["id"]
        raise RuntimeError(
            f"trigger slug {body['slug']!r} conflicted but wasn't found "
            f"among kind={kind!r} triggers"
        )
    return (await api.ensure(resp))["id"]


async def _ensure_harness(api: Api, body: dict) -> str:
    """Create a harness by slug; slugs ARE enforced unique (live finding
    - a re-run 409s), unlike the "not required unique" guess this
    script originally shipped with. GET /v1/harnesses?slug=... exists,
    so the conflict path is a straight lookup, no listing+filtering
    needed."""
    resp = await api.post("/v1/harnesses", json=body)
    if resp.status_code == 409:
        listing = await api.ensure(await api.get(
            "/v1/harnesses", params={"slug": body["slug"]},
        ))
        items = listing["items"]
        if not items:
            raise RuntimeError(
                f"harness slug {body['slug']!r} conflicted but wasn't "
                "found by the slug filter"
            )
        return items[0]["id"]
    return (await api.ensure(resp))["id"]


async def _ensure_user(api: Api, body: dict) -> str:
    """Create a user by username; on a username conflict (409), list +
    filter client-side (no by-username GET exists) and reuse the
    existing id."""
    resp = await api.post("/v1/admin/users", json=body)
    if resp.status_code == 409:
        listing = await api.ensure(await api.get("/v1/admin/users"))
        for item in listing["items"]:
            if item["username"] == body["username"]:
                return item["id"]
        raise RuntimeError(
            f"username {body['username']!r} conflicted but wasn't found "
            "in the user list"
        )
    return (await api.ensure(resp))["id"]


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _wait_for(predicate, *, timeout: float, interval: float = 1.0, label: str = "") -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    print(f"[seed] WARNING: timed out waiting for: {label}", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def seed(api: Api) -> dict[str, Any]:
    summary: dict[str, Any] = {}

    # ---- Provider + profile + agents (idempotent: fixed ids) -------------
    provider_id = "staged-llm"
    await api.create_singleton("/v1/llm_providers", {
        "id": provider_id,
        "provider": "openchat",
        "models": [
            {"name": MODEL_CHAT, "context_length": 8192},
            {"name": MODEL_ASK_USER, "context_length": 8192},
            {"name": MODEL_APPROVAL, "context_length": 8192},
            {"name": MODEL_APPROVAL_TIMEOUT, "context_length": 8192},
        ],
        "config": {"url": MOCK_LLM_URL, "flavor": "lmstudio"},
        "limits": {"max_concurrency": 4},
    })
    summary["llm_provider"] = provider_id

    profiles = {}
    for slug, model in (
        ("chat", MODEL_CHAT), ("ask-user", MODEL_ASK_USER),
        ("approval", MODEL_APPROVAL), ("approval-timeout", MODEL_APPROVAL_TIMEOUT),
    ):
        pid = f"staged-profile-{slug}"
        await api.create_singleton("/v1/model_profiles", {
            "id": pid,
            "description": f"staged fixture profile ({slug})",
            "provider_id": provider_id,
            "model_name": model,
            "context_length": 8192,
        })
        profiles[slug] = pid
    summary["model_profiles"] = profiles

    # Complete the S5 bootstrap ensure-pass now that a provider + profile
    # exist. See the module docstring's second hard-won lesson - without
    # this, GET /v1/auth/status.setup_complete stays False and the
    # console renders window.SetupWizardGate for EVERY hash route
    # instead of the real app. Already idempotent on its own (its own
    # docstring: "Idempotently ensure the seeded world").
    setup_result = await api.ensure(await api.post("/v1/setup/seed"))
    summary["setup_complete"] = setup_result.get("setup_complete")
    summary["setup_missing"] = setup_result.get("setup_missing")

    # tools: [] means genuinely NO extra tools (Agent.tools' own
    # docstring: "Empty list means the agent has NO tools registered").
    # Workspace tools (workspace__write/workspace__read) are auto-
    # composed onto any workspace-bound session regardless of this list,
    # so the approval agents don't need an entry - but misc__uuid_v4 and
    # system__ask_user are NOT workspace tools and must be listed
    # explicitly or the scripted tool call the mock LLM emits would
    # never be offered to begin with.
    agent_tools = {
        "chat": ["misc__uuid_v4"],
        "ask-user": ["system__ask_user"],
        "approval": [],
        "approval-timeout": [],
    }
    agents = {}
    for slug, profile_slug in (
        ("chat", "chat"), ("ask-user", "ask-user"),
        ("approval", "approval"), ("approval-timeout", "approval-timeout"),
    ):
        aid = f"staged-agent-{slug}"
        await api.create_singleton("/v1/agents", {
            "id": aid,
            "description": f"staged fixture agent ({slug})",
            "model": {"profile_id": profiles[profile_slug]},
            "tools": agent_tools[slug],
            "system_prompt": ["You are a terse assistant used for a staged fixture."],
        })
        agents[slug] = aid
    summary["agents"] = agents

    # ---- Workspace (local) -------------------------------------------------
    # Provider + template are idempotent (fixed ids); the workspace
    # INSTANCE itself is not - every run creates a fresh one (server-
    # generated id, no natural identity to dedupe against).
    ws_provider_id = "staged-ws-provider"
    root = Path("/tmp/primer-staged-fixtures-workspace")
    root.mkdir(parents=True, exist_ok=True)
    await api.create_singleton("/v1/workspace_providers", {
        "id": ws_provider_id,
        "provider": "local",
        "config": {"kind": "local", "root_path": str(root)},
    })
    ws_template_id = "staged-ws-template"
    await api.create_singleton("/v1/workspace_templates", {
        "id": ws_template_id,
        "description": "staged fixture template",
        "provider_id": ws_provider_id,
        "backend": {"kind": "local"},
    })
    ws = await api.ensure(await api.post("/v1/workspaces", json={"template_id": ws_template_id}))
    workspace_id = ws["id"]
    summary["workspace"] = {
        "provider": ws_provider_id, "template": ws_template_id, "instance": workspace_id,
    }

    # local-default template existence check (is it a first-class
    # template entity or a string default? confirmed here live: a real,
    # auto-bootstrapped entity - primer/bootstrap/defaults.py's
    # RESERVED_WORKSPACE_TEMPLATES).
    templates = await api.ensure(await api.get("/v1/workspace_templates?limit=200"))
    template_ids = [t["id"] for t in templates.get("items", [])]
    summary["local_default_is_registered_template"] = "local-default" in template_ids
    summary["all_template_ids"] = template_ids

    # ---- Approval policy -----------------------------------------------
    # timeout_seconds=None falls back to the 60-minute global yield cap
    # (primer/toolset/_system_tools.py) rather than a short fixed window:
    # the 'pending' scenario below is deliberately left undecided so its
    # screenshot/inspection shows a genuinely parked session, and a run
    # of this script (plus whatever work happens around it) can easily
    # outlast a short timeout - a short fixed timeout here previously
    # let the session end with an APIConnectionError once the in-process
    # mock LLM (tied to THIS script's own lifetime) was long gone.
    # approved/rejected/cancelled resolve within seconds regardless, so
    # the long fallback doesn't change their behaviour.
    policy_id = "staged-approval-policy"
    await api.create_singleton("/v1/tool_approval_policies", {
        "id": policy_id,
        "toolset_id": "workspace",
        "tool_name": "write",  # NOT write_file - see module docstring
        "approval": {"type": "required"},
        "enabled": True,
        "timeout_seconds": None,
    })
    summary["approval_policy"] = policy_id

    # ---- Session 1: populated transcript + trace ------------------------
    s1 = await api.ensure(await api.post(
        f"/v1/workspaces/{workspace_id}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": agents["chat"]},
            "name": "Populated transcript fixture",
            "initial_instructions": "Generate a fresh UUID and explain what it is.",
            "auto_start": True,
        },
    ))
    session_chat_id = s1["id"]
    summary["session_chat"] = session_chat_id

    # ---- Session 2: ask_user park (inbox row) ----------------------------
    s2 = await api.ensure(await api.post(
        f"/v1/workspaces/{workspace_id}/sessions",
        json={
            "binding": {"kind": "agent", "agent_id": agents["ask-user"]},
            "name": "Ask-user fixture",
            "initial_instructions": "Ask me which environment to target.",
            "auto_start": True,
        },
    ))
    session_ask_user_id = s2["id"]
    summary["session_ask_user"] = session_ask_user_id

    # ---- Sessions 3-6: approval decisions in all 4 states ----------------
    # 'timeout' uses the approval-timeout agent (reads a file, matching
    # the short-timeout policy created below) so it doesn't share the
    # long write policy the other 3 park against.
    short_policy_id = "staged-approval-policy-short-timeout"
    await api.create_singleton("/v1/tool_approval_policies", {
        "id": short_policy_id,
        "toolset_id": "workspace",
        "tool_name": "read",  # NOT read_file - see module docstring
        "approval": {"type": "required"},
        "enabled": True,
        "timeout_seconds": 2,
    })
    summary["approval_timeout_policy"] = short_policy_id

    approval_sessions: dict[str, str] = {}
    for outcome, agent_slug, instruction in (
        # 'pending' is deliberately never resolved below - a genuinely
        # open approval is a different fixture than an already-resolved
        # one (a "needs a human" inbox row / mid-flight decision card).
        ("pending", "approval", "Write the release notes file."),
        ("approved", "approval", "Write the release notes file."),
        ("rejected", "approval", "Write the release notes file."),
        ("cancelled", "approval", "Write the release notes file."),
        ("timeout", "approval-timeout", "Read the release notes file."),
    ):
        s = await api.ensure(await api.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": agents[agent_slug]},
                "name": f"Approval fixture ({outcome})",
                "initial_instructions": instruction,
                "auto_start": True,
            },
        ))
        approval_sessions[outcome] = s["id"]
    summary["approval_sessions"] = approval_sessions

    # Wait for each to actually park on the approval gate, then drive it.
    async def _is_parked(sid: str) -> bool:
        detail = await api.ensure(await api.get(f"/v1/sessions/{sid}"))
        return detail.get("parked_status") in ("parked", "resumable")

    for outcome, sid in approval_sessions.items():
        await _wait_for(
            lambda sid=sid: _is_parked(sid), timeout=20, interval=1,
            label=f"session {sid} ({outcome}) to park on approval",
        )

    pending = await api.ensure(await api.get(
        f"/v1/sessions/{approval_sessions['approved']}/tool_approval/pending"
    ))
    tcid = pending["tool_call_id"]
    await api.ensure(await api.post(
        f"/v1/sessions/{approval_sessions['approved']}/tool_approval/respond",
        json={"tool_call_id": tcid, "decision": "approved"},
    ), ok=(200, 202))

    pending_r = await api.ensure(await api.get(
        f"/v1/sessions/{approval_sessions['rejected']}/tool_approval/pending"
    ))
    tcid_r = pending_r["tool_call_id"]
    await api.ensure(await api.post(
        f"/v1/sessions/{approval_sessions['rejected']}/tool_approval/respond",
        json={"tool_call_id": tcid_r, "decision": "rejected", "reason": "not needed"},
    ), ok=(200, 202))

    pending_c = await api.ensure(await api.get(
        f"/v1/sessions/{approval_sessions['cancelled']}/tool_approval/pending"
    ))
    tcid_c = pending_c["tool_call_id"]
    await api.ensure(await api.post(
        f"/v1/sessions/{approval_sessions['cancelled']}/yields/{tcid_c}/cancel",
        json={"reason": "staged fixture cancel"},
    ), ok=(200, 202, 204))

    # 'timeout' parked against the 2s policy above; let the sweeper
    # (polls every 30s) trip it rather than mutating the shared policy
    # the other 3 already-parked sessions depend on.
    #
    # classify_approval_payload (primer/worker/yield_runtime.py) collapses
    # BOTH a sweeper timeout and an explicit cancel into decision="rejected"
    # with a distinguishing `reason` ("timed-out" / the cancel reason) on
    # this session-resume path -- a literal decision="timeout" is never
    # written here (only /v1/tool_approval/records?status=timeout would
    # ever see one, and it never will from this path), so poll the
    # session's own parked_status clearing instead of that filter.
    print("[seed] waiting up to 50s for the timeout sweeper to trip the "
          "'timeout' approval session...", file=sys.stderr)

    async def _timeout_session_resolved() -> bool:
        return not await _is_parked(approval_sessions["timeout"])

    await _wait_for(
        _timeout_session_resolved,
        timeout=50, interval=3, label="the timeout session's park to clear",
    )

    # ---- Triggers (one per kind) + subscriptions -------------------------
    trigger_ids: dict[str, str] = {}
    trigger_ids["delayed"] = await _ensure_trigger(api, {
        "slug": "staged-delayed",
        "name": "Staged delayed trigger",
        "config": {"kind": "delayed", "fire_at": "2030-01-01T00:00:00Z"},
        "enabled": True,
    })
    trigger_ids["scheduled"] = await _ensure_trigger(api, {
        "slug": "staged-scheduled",
        "name": "Staged scheduled trigger",
        "config": {"kind": "scheduled", "cron": "0 2 * * *", "timezone": "UTC", "catchup": "one"},
        "enabled": True,
    })
    trigger_ids["webhook"] = await _ensure_trigger(api, {
        "slug": "staged-webhook",
        "name": "Staged webhook trigger",
        "config": {"kind": "webhook", "interactive": False, "wait_timeout_seconds": 60},
        "enabled": True,
    })

    channel_provider_id = "staged-channel-provider"
    await api.create_singleton("/v1/channel_providers", {
        "id": channel_provider_id,
        "provider": "discord",
        "config": {"bot_token": "staged-fake-bot-token-0123456789", "enable_dms": True},
    })
    channel_id = "staged-channel"
    await api.create_singleton("/v1/channels", {
        "id": channel_id,
        "provider_id": channel_provider_id,
        "provider": "discord",
        "external_id": "staged-room-1",
        "config": {"chats": {"enabled": True, "relay_mode": "final"}},
    })
    trigger_ids["channel"] = await _ensure_trigger(api, {
        "slug": "staged-channel-trigger",
        "name": "Staged channel trigger",
        "config": {"kind": "channel", "provider_id": channel_provider_id, "channel_id": channel_id, "interactive": True},
        "enabled": True,
    })

    # Subscription on the channel trigger = "the rule". Not deduped
    # (subscriptions have no caller-supplied id); a re-run adds another
    # one, which is harmless - the trigger just gets a second identical
    # rule, not a crash.
    await api.ensure(await api.post(
        f"/v1/triggers/{trigger_ids['channel']}/subscriptions",
        json={
            "config": {"kind": "agent_fresh_session", "workspace_id": workspace_id, "agent_id": agents["chat"]},
            "parallelism": "skip",
            "enabled": True,
            "event_matcher": {"event_type": "command.invoked", "command_name": "run"},
            "reply_target": "source_thread",
        },
    ))

    summary["triggers"] = trigger_ids
    summary["channel_provider"] = channel_provider_id
    summary["channel"] = channel_id

    # ---- Harness (idempotent via slug lookup) -----------------------------
    harness_id_slug = "staged-harness"
    summary["harness"] = await _ensure_harness(api, {
        "name": "Staged Harness",
        "slug": harness_id_slug,
        "git_url": "https://github.com/example/staged-harness-fixture",
        "ref": "main",
    })
    summary["harness_slug"] = harness_id_slug

    # ---- Service + 2 published versions (fresh each run) -----------------
    svc = await api.ensure(await api.post("/v1/services", json={
        "name": "staged-service",
        "description": "staged fixture service",
    }))
    service_id = svc["id"]
    await api.ensure(await api.post(
        f"/v1/services/{service_id}/versions",
        content=_tar_bytes({"index.html": b"<h1>v1</h1>"}),
        headers={"content-type": "application/gzip"},
    ))
    await api.ensure(await api.post(
        f"/v1/services/{service_id}/versions",
        params={"activate": "true"},
        content=_tar_bytes({"index.html": b"<h1>v2</h1>"}),
        headers={"content-type": "application/gzip"},
    ))
    summary["service"] = service_id

    # ---- Python toolset ---------------------------------------------------
    source = (
        "@primer_tool()\n"
        "def greet(name: str) -> str:\n"
        "    \"\"\"Greet a person.\n\n"
        "    Use when greeting someone by name.\n\n"
        "    Args:\n"
        "        name: Who to greet.\n"
        "    \"\"\"\n"
        "    return 'hi ' + name\n"
    )
    await api.create_singleton("/v1/toolsets", {
        "id": "staged-python-toolset",
        "provider": "python",
        "config": {
            "source": source, "source_version": 1,
            "default_timeout_seconds": 30.0, "env": {}, "allow_network": False,
        },
    })
    summary["python_toolset"] = "staged-python-toolset"

    # ---- Collection + document ------------------------------------------
    # Collection is idempotent (fixed id); the document is not (no
    # upsert-by-slug attempted here) - a re-run 409s on the doc create,
    # which is fine, the original document is still there.
    await api.create_singleton("/v1/collections", {
        "id": "staged-collection",
        "description": "Staged fixture collection",
    })
    collection_id = "staged-collection"
    doc_resp = await api.post(
        f"/v1/collections/{collection_id}/docs",
        json={"parent": "", "slug": "guide", "title": "Guide", "body": "# Guide\n\nHello, staged fixture."},
    )
    doc = None if doc_resp.status_code == 409 else await api.ensure(doc_resp)
    summary["collection"] = collection_id
    # "guide" is the slug this script itself requested above, so it's
    # always a safe fallback regardless of what the response body
    # actually names the field (observed empty on a real run - the
    # create response apparently doesn't echo slug/path back).
    summary["collection_document"] = (
        (doc and (doc.get("slug") or doc.get("path"))) or "guide"
    )

    # ---- Graph (idempotent) + a bound run session (fresh each run) -------
    graph_id = "staged-graph"
    await api.create_singleton("/v1/graphs", {
        "id": graph_id,
        "description": "Staged fixture graph",
        "nodes": [
            {"kind": "begin", "id": "start"},
            {"kind": "agent", "id": "worker", "agent_id": agents["chat"],
             "input_template": "Say hello."},
            {"kind": "end", "id": "finish", "output_template": "{{ nodes.worker.text }}"},
        ],
        "edges": [
            {"kind": "static", "from_node": "start", "to_node": "worker"},
            {"kind": "static", "from_node": "worker", "to_node": "finish"},
        ],
    })
    gs = await api.ensure(await api.post(
        f"/v1/workspaces/{workspace_id}/sessions",
        json={
            "binding": {"kind": "graph", "graph_id": graph_id},
            "name": "Staged graph run",
            "auto_start": True,
        },
    ))
    summary["graph"] = graph_id
    summary["graph_run_session"] = gs["id"]

    # ---- API token (fresh each run) ---------------------------------------
    # Token names are enforced unique (live finding - a fixed name 409s
    # on re-run) - not id-lookup-on-conflict like harness/triggers above,
    # since a token's plaintext value is only ever returned once at
    # creation. A re-run couldn't retrieve a prior run's value anyway,
    # so mint a genuinely fresh, uniquely-named one every time instead.
    token = await api.ensure(await api.post("/v1/auth/tokens", json={
        "name": f"staged-fixture-token-{int(time.time())}",
        "scopes": [], "expires_at": None,
    }))
    summary["api_token_id"] = token["id"]

    # ---- SSO / OIDC provider (idempotent) ---------------------------------
    await api.create_singleton("/v1/admin/oidc-providers", {
        "id": "staged-oidc",
        "name": "Staged OIDC",
        "discovery_url": "https://sso.example.com/.well-known/openid-configuration",
        "client_id": "staged-client-id",
        "client_secret": "staged-client-secret",
        "scopes": ["openid", "email", "profile"],
        "enabled": True,
    })
    summary["sso_provider"] = "staged-oidc"

    # ---- Non-admin + disabled user (idempotent via username lookup) ------
    summary["non_admin_user"] = await _ensure_user(api, {
        "username": "staged-user", "password": "staged-user-password-1",
        "role": "user",
    })

    disabled_user_id = await _ensure_user(api, {
        "username": "staged-disabled-user", "password": "staged-disabled-password-1",
        "role": "restricted",
    })
    await api.ensure(await api.patch(
        f"/v1/admin/users/{disabled_user_id}", json={"disabled": True},
    ))
    summary["disabled_user"] = disabled_user_id

    return summary


async def main() -> None:
    registry = _build_registry()
    mock = MockLLMServer(registry, MOCK_LLM_PORT)
    mock.start()
    print(f"[seed] mock LLM up on {MOCK_LLM_URL}", file=sys.stderr)
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
            api = Api(client)
            await _login_or_register(api)
            summary = await seed(api)
        text = json.dumps(summary, indent=2)
        print(text)
        SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        SUMMARY_PATH.write_text(text)
        print(f"[seed] wrote {SUMMARY_PATH}", file=sys.stderr)
    finally:
        mock.stop()


if __name__ == "__main__":
    asyncio.run(main())
