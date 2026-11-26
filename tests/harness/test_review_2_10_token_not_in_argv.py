"""Review 2.10: the harness git credential reaches git through its environment, never its argv (``ps``) or a repo config on disk, and only
ever answers a challenge from the remote's own origin (local servers only)."""
from __future__ import annotations

import base64
import functools
import http.server
import subprocess
import threading

import pytest

from primer.harness import git as harness_git
from primer.harness.git import _credential_env, _prepare_remote, clone_at_ref, ls_remote, push_bundle

TOKEN = "glpat-SECRET-TOKEN-123"
PAT = "ghp_URLCREDENTIAL456"
URL = "https://git.example.com:8443/org/repo.git"
PAT_URL = f"https://{PAT}@git.example.com:8443/org/repo.git"


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def communicate(self):
        return b"0123456789012345678901234567890123456789\trefs/heads/main\n", b""


@pytest.fixture
def spawned(monkeypatch, request):
    calls: list[tuple[tuple[str, ...], dict]] = []
    fail_clone = request.node.name.startswith("test_push")

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeProc(1 if fail_clone and "clone" in args else 0)       # a failed clone sends push_bundle down its init + ``remote add`` path

    monkeypatch.setattr(harness_git.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def _assert_no_secret_in_argv(calls, *secrets):
    assert calls, "git was not spawned"
    for args, _ in calls:
        for secret in secrets:
            assert not any(secret in a for a in args), f"{secret} in argv: {args}"
        assert not any("oauth2" in a for a in args), args


def _assert_credential_reaches_git(calls, username, password, url):
    """Ask a real git, with the exact environment the subprocess got, which helper it would run for the remote, and run it."""
    _, kwargs = calls[0]
    helpers = subprocess.run(
        ["git", "config", "--get-urlmatch", "credential.helper", url], env=kwargs["env"], capture_output=True, text=True, check=True,
    ).stdout
    assert helpers.strip().splitlines()[-1].startswith("!f()")
    asked = subprocess.run(
        ["git", "credential", "fill"], env={**kwargs["env"], "GIT_TERMINAL_PROMPT": "0"},
        input=f"protocol=https\nhost={url.split('//')[1].split('/')[0]}\npath=org/repo.git\n\n", capture_output=True, text=True, check=True,
    ).stdout
    assert f"username={username}" in asked and f"password={password}" in asked


async def test_ls_remote_keeps_the_token_out_of_argv(spawned):
    await ls_remote(URL, token=TOKEN, ref="main")
    _assert_no_secret_in_argv(spawned, TOKEN)
    _assert_credential_reaches_git(spawned, "oauth2", TOKEN, URL)


async def test_clone_keeps_the_token_out_of_argv(spawned, tmp_path):
    await clone_at_ref(URL, token=TOKEN, ref="main", dest=str(tmp_path / "d"))
    _assert_no_secret_in_argv(spawned, TOKEN)
    _assert_credential_reaches_git(spawned, "oauth2", TOKEN, URL)


async def test_sha_fetch_keeps_the_token_out_of_argv(spawned, tmp_path):
    await clone_at_ref(URL, token=TOKEN, ref="0" * 40, dest=str(tmp_path / "d"))
    _assert_no_secret_in_argv(spawned, TOKEN)
    _assert_credential_reaches_git(spawned[1:], "oauth2", TOKEN, URL)      # [0] is the credential-less ``git init``


async def test_a_credential_kept_in_the_git_url_stays_out_of_argv_and_reaches_git(spawned, tmp_path):
    await ls_remote(PAT_URL, token=None, ref="main")
    await clone_at_ref(PAT_URL, token=None, ref="main", dest=str(tmp_path / "d"))
    _assert_no_secret_in_argv(spawned, PAT)
    _assert_credential_reaches_git(spawned, PAT, "", URL)


async def test_the_stored_token_wins_over_a_url_credential(spawned):
    await ls_remote(PAT_URL, token=TOKEN, ref="main")
    _assert_no_secret_in_argv(spawned, TOKEN, PAT)
    _assert_credential_reaches_git(spawned, "oauth2", TOKEN, URL)


async def test_push_keeps_the_credential_out_of_argv_and_the_clone_config(spawned):
    try:
        await push_bundle(
            url=PAT_URL, token=TOKEN, ref="main", files=[("a.txt", b"x")], subpath=None, commit_message="m", expected_remote_sha=None,
        )
    except Exception:  # noqa: BLE001 - the fake git has no repo to commit in; only the spawned commands matter
        pass
    _assert_no_secret_in_argv(spawned, TOKEN, PAT)
    # ``remote add origin`` records the clean URL, so nothing secret is stored in .git/config.
    adds = [args for args, _ in spawned if "remote" in args and "add" in args]
    assert adds, "the init + remote add path was not exercised"
    for args in adds:
        assert args[-1] == URL


# ---- the credential answers a challenge from its own origin only (real git, two local servers) ------------------------------


class _Repo(http.server.SimpleHTTPRequestHandler):
    """A dumb-http git repository server. ``challenge`` makes it answer 401 until a Basic credential arrives; ``redirect_to`` makes it 301
    every request to another origin. Every request's Authorization header is recorded."""

    challenge = False
    redirect_to: str | None = None

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        if self.redirect_to:
            self.send_response(301)
            self.send_header("Location", self.redirect_to + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.challenge and not self.headers.get("Authorization"):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="x"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_GET()

    def log_message(self, *a):
        pass


def _serve(directory, **attrs):
    handler = type("H", (_Repo,), attrs)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(handler, directory=str(directory)))
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def dumb_repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=work, check=True)
    (work / "f").write_text("x")
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=work, check=True)
    site = tmp_path / "site"
    site.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(site / "repo.git")], check=True)
    subprocess.run(["git", "update-server-info"], cwd=site / "repo.git", check=True)
    return site


def _ls_remote(url, env):
    return subprocess.run(
        ["git", "-c", "protocol.allow=always", "ls-remote", "--", url], env={**harness_git.os.environ, "GIT_TERMINAL_PROMPT": "0", **env},
        capture_output=True, text=True,
    )


def _auth(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_the_credential_answers_a_challenge_from_its_own_origin(dumb_repo):
    server = _serve(dumb_repo, challenge=True)
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        done = _ls_remote(f"{origin}/repo.git", _credential_env(origin, "oauth2", TOKEN))
        assert done.returncode == 0, done.stderr
        assert any(auth == _auth("oauth2", TOKEN) for _, auth in server.seen)
    finally:
        server.shutdown()


def test_a_cross_origin_redirect_never_receives_the_credential(dumb_repo):
    target = _serve(dumb_repo, challenge=True)            # B: would accept the token if it were ever offered
    origin_server = _serve(dumb_repo, redirect_to=f"http://127.0.0.1:{target.server_port}")      # A: the remote, which redirects to B
    try:
        origin = f"http://127.0.0.1:{origin_server.server_port}"
        done = _ls_remote(f"{origin}/repo.git", _credential_env(origin, "oauth2", TOKEN))
        assert target.seen, "git did follow the redirect (the probe is meaningful)"
        assert all(auth is None for _, auth in target.seen), target.seen
        assert all(auth is None for _, auth in origin_server.seen), "A never challenged, so nothing was offered to it either"
        assert done.returncode != 0
    finally:
        origin_server.shutdown()
        target.shutdown()


def test_a_public_repository_is_still_read_anonymously_with_a_stale_token(dumb_repo):
    server = _serve(dumb_repo)          # no challenge: a public repository
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        done = _ls_remote(f"{origin}/repo.git", _credential_env(origin, "oauth2", "stale-token"))
        assert done.returncode == 0, done.stderr
        assert all(auth is None for _, auth in server.seen)
    finally:
        server.shutdown()


def test_the_helper_is_not_consulted_for_another_origin():
    _, env = _prepare_remote(URL, TOKEN)
    other = subprocess.run(
        ["git", "config", "--get-urlmatch", "credential.helper", "https://evil.example.org/x"], env={**harness_git.os.environ, **env},
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    assert not any(line.startswith("!f()") for line in other)
