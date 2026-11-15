"""Fail-closed limits and raw evidence checks for the opt-in F1 probe."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

CASE = "test_real_model_watch_survives_compaction_and_active_lease_crash"
MODEL = "qwen3.8-27b"
AUTO_TOOLS = {"workspace__" + n for n in ("edit", "exec", "glob", "grep", "ls", "read", "write")}
TOOLS = AUTO_TOOLS | {"workspace_ext__watch_files"}
EFFECTS = {"workspace__read", "workspace__write", "workspace_ext__watch_files"}
MAX_REQUESTS = 11
MAX_BYTES = 2 * 1024 * 1024
BASE = (
    "On every turn, first call workspace_ext__watch_files with paths=['task.json'], "
    "timeout_seconds=30 and batch_window_ms=250. Wait for a real change; a timeout is failure. "
    "Then use workspace__read to read task.json. It contains a JSON object with nonce and value. "
    "Use workspace__write to create answers/<nonce>.json containing exactly that JSON object. "
    "Do not invent either field or reuse earlier values. Do not execute shell commands or other tools. "
    "After that single write, give a brief final answer and end the turn. "
    "Follow this procedure again on a later neutral Continue message."
)
NEUTRAL = "Continue."


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def postgres_config(dsn: str) -> dict:
    """Only the disposable loopback service, never a host/shared default DB."""
    u = urlsplit(dsn)
    if (u.scheme not in {"postgres", "postgresql"} or u.hostname != "127.0.0.1"
            or u.port is None or u.path != "/primer_w4_f1" or u.query or u.fragment
            or not u.username or not u.password):
        raise ValueError("F1 requires explicit loopback port and private primer_w4_f1 database")
    from urllib.parse import unquote
    return {"hostname": u.hostname, "port": u.port, "database": "primer_w4_f1",
            "username": unquote(u.username), "password": unquote(u.password), "db_schema": "public"}


def prerequisites(env: dict[str, str], head: str, now: float) -> dict:
    """All conditions precede network, DB and process creation."""
    if not (env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and env.get("GITHUB_REF") == "refs/heads/main" and env.get("W4_F1_OPT_IN") == "true"
            and env.get("W4_F1_ISOLATED_RUNNER") == "ephemeral-private"
            and env.get("W4_F1_NETWORK_AUTHORIZED") == "true"):
        raise ValueError("manual trusted-main isolated-runner prerequisites are absent")
    expected = env.get("W4_F1_REVIEWED_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", expected) or expected != head or env.get("GITHUB_SHA") != head:
        raise ValueError("reviewed checkout does not equal dispatched main")
    reservation = env.get("W4_F1_RESERVATION_ID", "")
    owner = env.get("W4_F1_RESERVATION_OWNER", "")
    expiry = float(env.get("W4_F1_RESERVATION_EXPIRES", "0"))
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", reservation) or not owner or not math.isfinite(expiry) or not math.isfinite(now) or expiry < now + 900:
        raise ValueError("model-owner reservation must cover the full job")
    endpoint = env.get("W4_F1_MODEL_URL", "")
    # The destination is reviewed runner configuration, not an arbitrary dispatch input.
    if endpoint != "http://asus-gx10.piranha-ph.ts.net:8000/v1":
        raise ValueError("unreviewed model endpoint")
    postgres_config(env.get("W4_F1_POSTGRES_DSN", ""))
    from .control_plane import proof_from_env, execution_from_env
    proof = proof_from_env(env, head, now)
    execution = execution_from_env(env, proof, head, now)
    return {"id": reservation, "owner": owner, "checked_at": now, "expires": expiry, "endpoint": endpoint, "control_plane": proof, "execution": execution}


def parse_response(raw: bytes, model: str = MODEL) -> dict:
    """Reconstruct an actual complete OpenChat SSE response, preserving errors."""
    if len(raw) > MAX_BYTES:
        raise ValueError("response exceeds evidence bound")
    calls: dict[int, dict] = {}
    text = ""
    finished = None
    done = False
    for line in raw.decode("utf-8").splitlines():
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data: "):
            raise ValueError("unexpected SSE framing")
        data = line[6:]
        if data == "[DONE]":
            done = True
            continue
        if done:
            raise ValueError("data after completion")
        chunk = json.loads(data)
        if "error" in chunk or chunk.get("model") != model:
            raise ValueError("upstream model identity/error")
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                raise ValueError("multiple choices")
            delta = choice.get("delta", {})
            text += delta.get("content") or ""
            for c in delta.get("tool_calls", []):
                row = calls.setdefault(c["index"], {"id": "", "name": "", "arguments": ""})
                row["id"] += c.get("id", "")
                f = c.get("function", {})
                row["name"] += f.get("name", "")
                row["arguments"] += f.get("arguments", "")
            if choice.get("finish_reason") is not None:
                if finished is not None:
                    raise ValueError("duplicate finish")
                finished = choice["finish_reason"]
    if not done or finished not in {"stop", "tool_calls"}:
        raise ValueError("incomplete, capped or failed completion")
    result = []
    for _, c in sorted(calls.items()):
        if not c["id"] or not c["name"]:
            raise ValueError("missing native call identity")
        result.append({**c, "arguments": json.loads(c["arguments"])})
    if bool(result) != (finished == "tool_calls") or len({c["id"] for c in result}) != len(result):
        raise ValueError("tool completion/identity mismatch")
    return {"calls": result, "text": text, "finish_reason": finished}


def inspect_effects(parsed: dict) -> None:
    """Inspect buffered genuine calls BEFORE any response reaches the app."""
    for c in parsed["calls"]:
        name, args = c["name"], c["arguments"]
        if name not in EFFECTS or not isinstance(args, dict):
            raise ValueError("unexpected effect, including shell execution")
        if name == "workspace_ext__watch_files":
            if args.get("paths") != ["task.json"] or args.get("timeout_seconds") != 30 or args.get("batch_window_ms", 250) != 250:
                raise ValueError("unbounded or different watch")
        elif name == "workspace__read":
            if args.get("path") != "task.json" or args.get("offset", 0) != 0 or args.get("limit", 2000) is None:
                raise ValueError("unexpected file read")
        else:
            if (not re.fullmatch(r"answers/[0-9a-f]{24}\.json", args.get("path", ""))
                    or args.get("force", False) or args.get("mode") is not None):
                raise ValueError("unexpected output path/mode")
            content = json.loads(args["content"])
            if set(content) != {"nonce", "value"} or args["path"] != f"answers/{content['nonce']}.json":
                raise ValueError("output does not identify its nonce")


def system_head(body: dict) -> str:
    messages = body["messages"]
    return next((m["content"] for m in messages if m.get("role") == "system"), "")


def live_lease(row: dict, session: str, now: str) -> None:
    if not (row and row["kind"] == "session" and row["entity_id"] == session and row["claimed_by"]
            and datetime.fromisoformat(row["expires_at"]) > datetime.fromisoformat(now)):
        raise ValueError("missing or expired active lease")


def utc_time(value: str) -> datetime:
    time = datetime.fromisoformat(value)
    if time.utcoffset() != timedelta(0):
        raise ValueError("observation must have an explicit UTC offset")
    return time


def lease_clock(lease: dict, observed_at: str) -> None:
    claimed = utc_time(lease["claimed_at"])
    heartbeat = utc_time(lease["last_heartbeat_at"])
    observed = utc_time(observed_at)
    expires = utc_time(lease["expires_at"])
    assert claimed <= heartbeat <= observed
    assert expires == heartbeat + timedelta(seconds=30)


def require_evidence(t: dict, source: str) -> None:
    """Accept one connected raw source-bound run, not a collection or PASS flag."""
    assert t["case"] == CASE and t["outcome"] == "passed" and t["source_sha"] == source
    assert re.fullmatch(r"[0-9a-f]{40}", source)
    assert t["base"] == BASE and t["base_sha256"] == digest(BASE.encode())
    sid = t["session_id"]
    assert sid and t["role"] == "user" and t["user_id"] != t["admin_id"]
    assert t["storage"] == "private-postgres" and t["reservation"]["id"]
    reservation = t["reservation"]
    assert math.isfinite(reservation["checked_at"]) and math.isfinite(reservation["expires"])
    assert reservation["expires"] >= reservation["checked_at"] + 900
    from .control_plane import validate_proof, validate_execution
    validate_proof(reservation["control_plane"], t["run"], source, reservation["checked_at"])
    validate_execution(reservation["execution"], reservation["control_plane"], t["run"], source, reservation["checked_at"])
    runtime = t["runtime"]
    assert len(runtime) == 2 and {r["pid"] for r in runtime} == {t["old_process"]["pid"], t["new_process"]["pid"]}
    for r in runtime:
        assert r["storage_provider"] == "PostgresStorageProvider" and r["scheduler"] == "PostgresScheduler"
        assert r["event_bus"] == "PostgresEventBus" and r["claim_engine"] == "PostgresClaimEngine"
    assert runtime[0]["worker_id"] != runtime[1]["worker_id"]
    by_pid = {r["pid"]: r for r in runtime}
    old_runtime, new_runtime = (by_pid[t[name]["pid"]] for name in ("old_process", "new_process"))
    for name, proof in (("old_process", old_runtime), ("new_process", new_runtime)):
        process = t[name]
        assert proof["exe"] == process["exe"] == t["interpreter"]["real_executable"]
        assert proof["starttime"] == process["starttime"]
        assert proof["command"] == process["command"]
        assert proof["config_path"] == t["config_path"] and proof["config_sha256"] == t["config_sha256"]
    packets = t["packets"]
    assert 1 <= len(packets) <= MAX_REQUESTS and [p["index"] for p in packets] == list(range(1, len(packets) + 1))
    assert all(p["upstream_complete"] and p["request"]["model"] == MODEL for p in packets)
    for p in packets:
        raw = bytes.fromhex(p["response_hex"])
        assert digest(raw) == p["response_sha256"]
        parsed = parse_response(raw)
        if p["stage"] == "capability":
            assert p["index"] == 1 and len(parsed["calls"]) == 1
            assert parsed["calls"][0]["name"] == "w4_probe_capability" and parsed["calls"][0]["arguments"] == {"ok": True}
            assert p["request"]["max_tokens"] == 128
        elif p["stage"] == "compact":
            assert BASE not in json.dumps(p["request"]["messages"])
            assert not p["request"].get("tools") and p["request"]["max_tokens"] == 4096
        else:
            assert p["stage"] in {"cycle1", "cycle2", "recovery"}
            assert system_head(p["request"]).startswith(BASE + "\n\n")
            assert {x["function"]["name"] for x in p["request"]["tools"]} == TOOLS
            assert p["request"]["max_tokens"] == 1024
            inspect_effects(parsed)
        assert p["disposition"] in {"forwarded", "discarded_at_crash"}
        assert 0 <= p["elapsed_seconds"] <= 35
    assert len([p for p in packets if p["stage"] == "capability"]) == 1
    compact = [p for p in packets if p["stage"] == "compact"]
    discarded = [p for p in packets if p["disposition"] == "discarded_at_crash"]
    assert len(compact) == len(discarded) == 1
    assert discarded[0]["stage"] == "cycle2" and [c["name"] for c in parse_response(bytes.fromhex(discarded[0]["response_hex"]))["calls"]] == ["workspace__read"]
    assert compact[0]["index"] < discarded[0]["index"] < min(p["index"] for p in packets if p["stage"] == "recovery")
    stages = t["snapshots"]
    assert set(stages) == {"idle1", "compacted", "before_kill", "dead_live", "dead_expired", "reclaimed", "idle2"}
    order = ("idle1", "compacted", "before_kill", "dead_live", "dead_expired", "reclaimed", "idle2")
    observed = [utc_time(stages[name]["observed_at"]) for name in order]
    assert observed == sorted(observed) and observed[2] < observed[3] < observed[4] < observed[5]
    for row in stages.values():
        assert row["session"]["id"] == sid and row["session"]["workspace_id"] == t["workspace_id"]
        assert row["session"]["base_prompt"] == BASE
    for name in ("idle1", "compacted", "idle2"):
        assert stages[name]["session"]["turn_status"] == "idle" and stages[name]["session"]["parked_status"] is None
    assert len(t["watches"]) == 2
    for watched in t["watches"]:
        assert watched["session"]["id"] == sid and watched["session"]["base_prompt"] == BASE
        assert watched["session"]["parked_status"] == "parked" and watched["lease"] is None
    assert utc_time(t["watches"][0]["observed_at"]) < utc_time(stages["idle1"]["observed_at"])
    assert utc_time(stages["compacted"]["observed_at"]) < utc_time(t["watches"][1]["observed_at"]) < utc_time(stages["before_kill"]["observed_at"])
    marker = t["compaction"]
    assert marker["summary"] and marker["compaction_marker_seq"] >= 0
    assert parse_response(bytes.fromhex(compact[0]["response_hex"]))["text"].strip() == marker["summary"]
    markers = [r for r in t["disk_records"] if r.get("kind") == "compaction_marker"]
    assert len(markers) == 1 and markers[0]["seq"] == marker["compaction_marker_seq"]
    assert markers[0]["payload"]["summary"] == marker["summary"]
    before, dead, expired, reclaimed = (stages[k] for k in ("before_kill", "dead_live", "dead_expired", "reclaimed"))
    live_lease(before["lease"], sid, before["observed_at"])
    live_lease(dead["lease"], sid, dead["observed_at"])
    assert before["process_alive"] is True and reclaimed["process_alive"] is True
    assert dead["process_alive"] is False and expired["process_alive"] is False
    for row in (before, dead, expired, reclaimed):
        lease_clock(row["lease"], row["observed_at"])
    assert before["lease"]["claimed_by"] == old_runtime["worker_id"] == before["session"]["last_worker_id"]
    assert reclaimed["lease"]["claimed_by"] == new_runtime["worker_id"]
    assert dead["lease"]["claimed_at"] == before["lease"]["claimed_at"]
    assert utc_time(dead["lease"]["last_heartbeat_at"]) >= utc_time(before["lease"]["last_heartbeat_at"])
    assert utc_time(dead["lease"]["expires_at"]) >= utc_time(before["lease"]["expires_at"])
    assert dead["lease"] == expired["lease"]
    assert dead["lease"]["claimed_by"] == before["lease"]["claimed_by"] == expired["lease"]["claimed_by"]
    assert expired["lease"]["expires_at"] == dead["lease"]["expires_at"]
    assert datetime.fromisoformat(expired["lease"]["expires_at"]) < datetime.fromisoformat(expired["observed_at"])
    live_lease(reclaimed["lease"], sid, reclaimed["observed_at"])
    assert reclaimed["lease"]["claimed_by"] != before["lease"]["claimed_by"]
    assert datetime.fromisoformat(reclaimed["lease"]["claimed_at"]) > datetime.fromisoformat(expired["lease"]["expires_at"])
    assert reclaimed["lease"]["claimed_at"] != before["lease"]["claimed_at"]
    assert reclaimed["session"]["last_worker_id"] == reclaimed["lease"]["claimed_by"]
    assert t["old_process"]["pid"] != t["new_process"]["pid"]
    for proc in (t["old_process"], t["new_process"]):
        assert proc["ppid"] == t["controller"]["pid"] and proc["pgid"] != t["controller"]["pgid"]
        assert proc["pid"] == proc["pgid"] == proc["sid"] and proc["starttime"]
        assert proc["command"] == [t["interpreter"]["executable"], "-m", "scripts.e2e.w4_f1.app", "--config", t["config_path"]]
        assert proc["exe"] == t["interpreter"]["real_executable"]
        assert proc["pid"] > 0 and str(proc["starttime"]).isdecimal()
    assert t["old_process"]["command"] == t["new_process"]["command"]
    assert t["old_process"]["pgid"] == t["old_process"]["pid"] and t["owned_process_kill_verified"]
    assert stages["idle2"]["lease"] is None
    for key in ("id", "workspace_id", "base_prompt", "binding", "status", "turn_status", "turn_no", "last_seq", "parked_status", "last_worker_id"):
        assert t["rest_idle2"].get(key) == stages["idle2"]["session"].get(key)
    # Native committed tool events, not relay requests, prove exactly-once effects.
    records = t["disk_records"]
    from primer.model.workspace_session import SessionMessageRecord
    for r in records:
        SessionMessageRecord.model_validate(r)
    assert records == t["rest_records"] and not any(r["kind"] in {"error", "cancelled"} for r in records)
    assert [r["seq"] for r in records] == sorted({r["seq"] for r in records})
    from primer.session.terminals import closes_turn
    assert len([r for r in records if r["kind"] == "done" and closes_turn(r)]) == 2
    calls = [r for r in records if r["kind"] == "tool_call"]
    wire_calls = [c for p in packets if p["stage"] not in {"compact", "capability"} and p["disposition"] == "forwarded"
                  for c in parse_response(bytes.fromhex(p["response_hex"]))["calls"]]
    assert len(calls) == len(wire_calls) == 6
    results = [r for r in records if r["kind"] == "tool_result"]
    assert len(results) == 6
    for native, wire in zip(calls, wire_calls, strict=True):
        assert native["payload"]["raw_id"] == wire["id"]
        assert native["payload"]["name"] == wire["name"] and native["payload"]["arguments"] == wire["arguments"]
        paired = [r for r in results if r["payload"]["call_id"] == native["payload"]["id"]]
        assert len(paired) == 1 and paired[0]["payload"]["error"] is False
        if wire["name"] == "workspace_ext__watch_files":
            event = json.loads(paired[0]["payload"]["output"])
            assert event["timed_out"] is False and any(c["path"] == "task.json" for c in event["changes"])
    writes = [r for r in calls if r["payload"].get("name") == "workspace__write"]
    watches = [r for r in calls if r["payload"].get("name") == "workspace_ext__watch_files"]
    assert len(watches) == 2 and len(writes) == 2
    assert len(t["nonces"]) == len(set(t["nonces"])) == 2
    for nonce, expected in t["nonces"].items():
        assert re.fullmatch(r"[0-9a-f]{24}", nonce) and expected["nonce"] == nonce
        content = t["outputs"][nonce]
        assert json.loads(content) == expected
        matches = [r for r in writes if r["payload"].get("arguments", {}).get("path") == f"answers/{nonce}.json"]
        assert len(matches) == 1 and json.loads(matches[0]["payload"]["arguments"]["content"]) == expected
        read_matches = [r for r in calls if r["payload"]["name"] == "workspace__read" and r["seq"] < matches[0]["seq"]]
        assert any(nonce in result["payload"]["output"] and expected["value"] in result["payload"]["output"]
                   for read in read_matches for result in results if result["payload"]["call_id"] == read["payload"]["id"])
    assert t["initial_instruction"] == NEUTRAL and t["steers"] == [NEUTRAL]
    assert t["cleanup"]["processes_gone"] and not t["cleanup"]["unknown_upstream"]
    assert t["cleanup"]["owned_private_root_removed"] and t["cleanup"]["errors"] == []
    native = t["cleanup"]["native_evidence"]
    paths = [row["path"] for row in native]
    assert len(paths) == len(set(paths))
    assert "private-native/runtime.jsonl" in paths
    prefix = f"private-native/workspaces/{t['workspace_id']}/.state/sessions/{sid}/"
    assert any(p.startswith(prefix + "messages") and p.endswith(".jsonl") for p in paths)
    assert t["elapsed_seconds"] <= 600


def source_hashes(repo: Path) -> dict:
    return {str(p.relative_to(repo)): digest(p.read_bytes()) for p in sorted((repo / "scripts/e2e/w4_f1").glob("*.py"))}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
