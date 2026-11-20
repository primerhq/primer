"""Bounded, read-only state Git inspection for runtime protocol 1.6."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re

from primer_runtime.protocol import ErrorCode

# Refuse oversized responses rather than silently reporting a partial diff.
MAX_INSPECT_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"[0-9a-fA-F]{4,64}\Z")


def _bounded_result(result: dict) -> dict:
    from primer_runtime.ops import OpError

    # JSON escaping and per-file metadata expand Git's byte output. Bound the
    # whole encoded result too, well below the WebSocket's 256 MiB frame cap.
    if len(json.dumps(result).encode("utf-8")) > MAX_RESULT_BYTES:
        raise OpError(ErrorCode.EPROTOCOL, "State inspection exceeds the 8 MiB JSON response limit")
    return result


def _repo(workspace_root: str) -> Path:
    from primer_runtime.ops import OpError

    root = Path(workspace_root).resolve()
    state = (root / ".state").resolve()
    git = (state / ".git").resolve()
    if not state.is_relative_to(root) or not git.is_relative_to(state):
        raise OpError(ErrorCode.EACCES, "State repository escapes its workspace")
    if not git.is_dir():
        raise OpError(ErrorCode.ENOENT, "State repository does not exist")
    return state


async def _git(state: Path, *args: str) -> tuple[int, str, str]:
    from primer_runtime.ops import OpError, _stop_git, _subprocess_timeout

    # No inherited git-dir, alternate object directory, or global config may
    # redirect this read. Explicit git-dir also prevents parent-repo discovery.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_LAZY_FETCH="1", LC_ALL="C")
    proc = await asyncio.create_subprocess_exec(
        "git", "--no-pager", f"--git-dir={state / '.git'}", f"--work-tree={state}",
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    remaining = MAX_INSPECT_BYTES

    async def read(stream):
        nonlocal remaining
        chunks = []
        while chunk := await stream.read(65536):
            remaining -= len(chunk)
            if remaining < 0:
                raise OpError(ErrorCode.EPROTOCOL, "State inspection exceeds the 4 MiB output limit")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    tasks = [asyncio.create_task(read(proc.stdout)), asyncio.create_task(read(proc.stderr))]
    try:
        async with asyncio.timeout(_subprocess_timeout()):
            stdout, stderr = await asyncio.gather(*tasks)
            await proc.wait()
        return proc.returncode, stdout, stderr
    except TimeoutError as exc:
        raise OpError(ErrorCode.ETIMEDOUT, "State inspection timed out") from exc
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if proc.returncode is None:
            await asyncio.gather(_stop_git(proc), proc.communicate())


async def _checked(state: Path, *args: str) -> str:
    from primer_runtime.ops import OpError

    code, out, err = await _git(state, *args)
    if code:
        raise OpError(ErrorCode.EINTERNAL, f"State inspection failed: {err.strip()}")
    return out


async def state_show_commit(args: dict, workspace_root: str, locks=None) -> dict:
    """Return a commit header and per-file patches, including root commits."""
    from primer_runtime.ops import OpError

    sha = args.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise OpError(ErrorCode.EPROTOCOL, "Commit must be 4 to 64 hexadecimal digits")
    state = _repo(workspace_root)
    code, resolved, error = await _git(state, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if code == 1:
        raise OpError(ErrorCode.ENOENT, f"Commit {sha!r} not found")
    if code:
        raise OpError(ErrorCode.EINTERNAL, f"Commit lookup failed: {error.strip()}")
    resolved = resolved.strip()
    header = await _checked(state, "show", "--no-patch", "--format=%P%x00%s%x00%b", resolved, "--")
    parents, subject, body = header.rstrip("\n").split("\0", 2)
    parent = parents.split(" ")[0] or None
    # Explicit first parent makes merge commits obey the same parent/diff contract.
    revisions = [parent, resolved] if parent else [resolved]
    common = ["diff-tree", "--root", "--no-commit-id", "-r", "--no-renames", "--no-ext-diff", "--no-textconv"]
    names = await _checked(state, *common, "--name-status", "-z", *revisions, "--")
    tokens = names.rstrip("\0").split("\0") if names else []
    files = [{"status": tokens[i], "path": tokens[i + 1], "patch": ""} for i in range(0, len(tokens), 2)]
    patches = await _checked(state, *common, "--patch", "--no-color", *revisions, "--")
    # Both diff-tree invocations walk the same tree in the same order. Pair by
    # position, not by parsing quoted Git headers (paths may contain tabs/newlines).
    blocks = re.split(r"(?m)(?=^diff --git )", patches)
    blocks = [block.rstrip("\n") for block in blocks if block]
    if len(files) != len(blocks):
        raise OpError(ErrorCode.EINTERNAL, "State diff file/patch count mismatch")
    for file, patch in zip(files, blocks, strict=True):
        file["patch"] = patch
    return _bounded_result({"sha": sha, "subject": subject, "body": body, "parent": parent, "files": files})


async def state_history_files(args: dict, workspace_root: str) -> dict:
    """One bounded git-log process; NUL framing preserves unusual file names."""
    from primer_runtime.ops import OpError, _parse_trailers

    limit = args.get("limit", 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
        raise OpError(ErrorCode.EPROTOCOL, "History limit must be between 1 and 10000")
    state = _repo(workspace_root)
    command = ["log", f"--max-count={limit}", "--format=%x00%H%x00%s%x00%cI%x00%(trailers:only,unfold)%x00", "--numstat", "-z", "--diff-merges=first-parent", "--no-renames", "--no-ext-diff", "--no-textconv"]
    for field, trailer in (("session_id", "Session"), ("agent_id", "Agent")):
        if args.get(field) is not None:
            literal = re.sub(r"([\\.\[\]*^$])", r"\\\1", str(args[field]))
            command.extend(["--grep", f"^X-Primer-{trailer}: {literal}$"])
    if args.get("session_id") is not None and args.get("agent_id") is not None:
        command.append("--all-match")
    code, output, error = await _git(state, *command)
    if code:
        if "does not have any commits yet" in error:
            return {"commits": []}
        raise OpError(ErrorCode.EINTERNAL, f"State history failed: {error.strip()}")
    tokens = output.split("\0")
    commits = []
    i = 0
    while i < len(tokens):
        token = tokens[i].lstrip("\n")
        if not token:
            i += 1
            continue
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", token):
            trailers = _parse_trailers(tokens[i + 3])
            commits.append({
                "sha": token, "subject": tokens[i + 1], "committed_at": tokens[i + 2],
                **{key: trailers.get(f"X-Primer-{label}") for key, label in (
                    ("workspace_id", "Workspace"), ("session_id", "Session"), ("agent_id", "Agent"),
                    ("op", "Op"), ("tool", "Tool"), ("call_id", "Call"),
                )}, "files": [],
            })
            i += 4
            continue
        additions, deletions, path = token.split("\t", 2)
        binary = additions == "-" or deletions == "-"
        commits[-1]["files"].append({
            "path": path, "additions": 0 if binary else int(additions),
            "deletions": 0 if binary else int(deletions), "binary": binary,
        })
        i += 1
    return _bounded_result({"commits": commits})
