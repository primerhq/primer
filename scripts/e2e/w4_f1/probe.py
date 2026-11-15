"""Manual-only F1 driver. No network, processes or signals occur on import."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import asyncpg
import httpx
import uvicorn

from .contracts import (
    BASE, CASE, MODEL, NEUTRAL, digest, postgres_config, prerequisites,
    require_evidence, source_hashes, utc_now,
)
from .relay import Relay

REPO = Path(__file__).resolve().parents[3]


def process_identity(pid: int) -> dict:
    path = Path('/proc') / str(pid)
    # comm can contain spaces and parentheses; fields follow the LAST ')'.
    fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'ppid': int(fields[1]), 'pgid': int(fields[2]), 'sid': int(fields[3]),
            'starttime': fields[19], 'exe': os.readlink(path / 'exe'),
            'cmdline': (path / 'cmdline').read_bytes().split(b'\0')[:-1]}


def live_group_members(pgid: int) -> list[int]:
    members = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != 'Z':
                members.append(int(path.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return members


class OwnedApp:
    """Signal only a direct, separately sessioned child with unchanged identity."""
    def __init__(self, root: Path, config: Path, log):
        self.root, self.config, self.log = root, config, log
        self.proc = None
        self.identity = None
        self.groups = []
        self.members: dict[int, dict] = {}
        self.expected_executable = os.readlink('/proc/self/exe')
        self.expected_command = [os.fsencode(v) for v in (sys.executable, '-m', 'scripts.e2e.w4_f1.app', '--config', str(config))]

    def start(self):
        if self.proc is not None and self.proc.poll() is None:
            raise ValueError('owned app is already alive')
        env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONPATH': str(REPO),
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
               'PRIMER_WIKI_DIR': str(self.root / 'wiki')}
        self.proc = subprocess.Popen([sys.executable, '-m', 'scripts.e2e.w4_f1.app', '--config', str(self.config)],
                                     cwd=self.root, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        self.identity = process_identity(self.proc.pid)
        self.groups.append(self.identity["pgid"])
        self.check()
        return self.public_identity()

    def public_identity(self):
        return {k: v for k, v in self.identity.items() if k != 'cmdline'} | {'command': [os.fsdecode(v) for v in self.identity['cmdline']]}

    def check(self):
        if self.proc is None or self.proc.poll() is not None:
            raise ValueError('owned child is not live')
        current = process_identity(self.proc.pid)
        if (current != self.identity or current['ppid'] != os.getpid()
                or current['pgid'] != current['pid'] or current['sid'] != current['pid']
                or current['pgid'] == os.getpgrp() or os.fsencode(self.config) not in current['cmdline']
                or current['exe'] != self.expected_executable or current['cmdline'] != self.expected_command
                or self.config.parent != self.root):
            raise ValueError('owned process identity changed or group is shared')
        # A proven live direct-child session leader owns this kernel session.
        # Capture descendants NOW; a dead leader cannot vouch for new identities.
        for pid in live_group_members(current['pgid']):
            try:
                member = process_identity(pid)
            except (FileNotFoundError, ProcessLookupError):
                continue
            if member['pgid'] != current['pgid'] or member['sid'] != current['sid']:
                raise ValueError('group membership changed during ownership capture')
            self.members[pid] = member
        return current

    def recorded_identities(self):
        return [{k: v for k, v in row.items() if k != 'cmdline'} | {'command': [os.fsdecode(x) for x in row['cmdline']]}
                for row in self.members.values()]

    async def _stop_recorded_descendants(self):
        current_members = {pid for group in self.groups for pid in live_group_members(group)}
        if not current_members:
            return
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise ValueError('safe dead-leader cleanup needs Linux pidfds')
        descriptors = []
        try:
            for pid in current_members:
                expected = self.members.get(pid)
                if expected is None:
                    raise ValueError('uncaptured descendant: retain root for resource owner')
                try:
                    fd = os.pidfd_open(pid, 0)
                except ProcessLookupError:
                    continue
                descriptors.append(fd)
                actual = process_identity(pid)
                # A parent can die/reparent a child; PID/start/exe/command/session
                # remain the recorded identity. The pidfd prevents reuse races.
                for key in ('pid', 'starttime', 'exe', 'cmdline', 'pgid', 'sid'):
                    if actual[key] != expected[key]:
                        raise ValueError('recorded descendant identity changed')
            for fd in descriptors:
                try:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            async def wait_empty(seconds):
                async with asyncio.timeout(seconds):
                    while any(live_group_members(g) for g in self.groups):
                        await asyncio.sleep(.05)
            try:
                await wait_empty(1)
            except TimeoutError:
                remaining = {pid for group in self.groups for pid in live_group_members(group)}
                if not remaining <= current_members:
                    raise ValueError('new unproven descendant after leader exit')
                for fd in descriptors:
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await wait_empty(2)
        finally:
            for fd in descriptors:
                os.close(fd)

    async def stop(self, *, crash=False):
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            if crash:
                raise ValueError('crash window missed: leader was already dead')
            await self._stop_recorded_descendants()
            return
        identity = self.check()
        try:
            os.killpg(identity['pgid'], signal.SIGKILL if crash else signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.to_thread(self.proc.wait, timeout=3)
        except subprocess.TimeoutExpired:
            self.check()
            os.killpg(identity['pgid'], signal.SIGKILL)
            await asyncio.to_thread(self.proc.wait, timeout=3)
        await self._stop_recorded_descendants()
        if crash and self.proc.returncode != -signal.SIGKILL:
            raise ValueError('child did not die from the intended crash signal')


def app_config(root: Path, port: int, dsn: str) -> dict:
    from primer.api.config import AppConfig
    value = {'host': '127.0.0.1', 'port': port, 'auto_bootstrap': False,
             'db': {'provider': 'postgres', 'config': postgres_config(dsn)},
             'auth': {'enabled': True}, 'scheduler': {'provider': 'postgres', 'config': {}},
             'worker': {'concurrency': 1, 'claim_batch_size': 1, 'heartbeat_interval_seconds': 10,
                        'lease_ttl_seconds': 30, 'poll_interval_seconds': .1, 'max_attempts': 2,
                        'drain_timeout_seconds': 3}, 'log_level': 'warning'}
    AppConfig.model_validate(value)
    return value


def require_process_handles():
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ValueError('selected probe Python must expose Linux pidfd APIs')
    descriptor = os.pidfd_open(os.getpid(), 0)
    os.close(descriptor)  # Verify kernel support on self; no signal is sent.


def listening_socket():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    return sock


async def api(client: httpx.AsyncClient, method: str, path: str, *, status=200, **kwargs):
    response = await client.request(method, path, **kwargs)
    if response.status_code != status:
        raise ValueError(f'private API {method} {path}: HTTP {response.status_code}')
    return response.json() if response.content else None


async def wait_for(check, *, seconds, relay: Relay):
    async with asyncio.timeout(seconds):
        while True:
            if relay.failure:
                raise ValueError(relay.failure)
            value = await check()
            if value:
                return value
            await asyncio.sleep(.1)


async def ready(client, app, relay):
    async def check():
        if app.proc.poll() is not None:
            raise ValueError('private app exited before health')
        try:
            return (await client.get('/v1/health')).status_code == 200
        except httpx.HTTPError:
            return False
    await wait_for(check, seconds=30, relay=relay)


async def grant_public(admin, plural, identifier):
    path = f'/v1/{plural}/{identifier}/access'
    view = await api(admin, 'GET', path)
    await api(admin, 'PUT', path, json={'owners': [o['id'] for o in view.get('owners', [])],
                                     'grants': {}, 'public': True, 'rev': view['rev']})


async def snapshot(conn, sid, app):
    # This observer NEVER writes a session/lease, including after the crash.
    data = await conn.fetchval('SELECT data FROM sessions WHERE id=$1', sid)
    if not data:
        raise ValueError('private durable session absent')
    row = json.loads(data) if isinstance(data, str) else data
    row = {**row, 'id': sid}
    lease = await conn.fetchrow("SELECT *, now() AS observed_at FROM leases WHERE kind='session' AND entity_id=$1", sid)
    observed = await conn.fetchval('SELECT now()')
    return {'observed_at': observed.isoformat(), 'session': row,
            'lease': {k: v.isoformat() if isinstance(v, datetime) else v for k, v in dict(lease).items() if k != 'observed_at'} if lease else None,
            'process_alive': app.proc is not None and app.proc.poll() is None}


def normalized_records(items):
    from primer.model.chat import Message, ToolResultPart
    from primer.model.workspace_session import SessionMessageRecord
    result = []
    for row in items:
        if 'kind' in row:
            result.append(SessionMessageRecord.model_validate(row).model_dump(mode='json'))
        else:
            message = Message.model_validate(row)
            if any(p.error for p in message.parts if isinstance(p, ToolResultPart)):
                raise ValueError('failed native tool result')
    return result


async def lifecycle(root, output, app, relay, conn, client, t, relay_url):
    password = secrets.token_urlsafe(24)
    for verb in ('register', 'login'):
        await api(client, 'POST', '/v1/auth/' + verb, status=200,
                  json={'username': 'f1-admin', 'password': password})
    admin = await api(client, 'GET', '/v1/auth/status')
    if admin['role'] != 'admin':
        raise ValueError('private first principal is not admin')
    t['admin_id'] = admin['principal']['user_id']
    for plural, body in [
        ('workspace_providers', {'id': 'f1-local', 'provider': 'local', 'config': {'kind': 'local', 'root_path': str(root / 'workspaces')}}),
        ('workspace_templates', {'id': 'f1-template', 'description': 'Disposable F1 local', 'provider_id': 'f1-local', 'backend': {'kind': 'local'}}),
        ('llm_providers', {'id': 'f1-relay', 'provider': 'openchat', 'config': {'url': relay_url, 'flavor': 'vllm'},
                           'limits': {'max_concurrency': 1, 'connect_timeout_seconds': 5, 'request_timeout_seconds': 20,
                                      'total_timeout_seconds': 30, 'max_retries': 0}}),
        ('model_profiles', {'id': 'f1-model', 'description': 'Disposable F1 model', 'provider_id': 'f1-relay', 'model_name': MODEL, 'context_length': 16384}),
    ]:
        await api(client, 'POST', '/v1/' + plural, status=201, json=body)
        await grant_public(client, plural, body['id'])
    await api(client, 'POST', '/v1/setup/seed')
    await api(client, 'POST', '/v1/admin/users', status=201, json={'username': 'f1-user', 'password': password, 'role': 'user'})
    async with httpx.AsyncClient(base_url=client.base_url, timeout=10, trust_env=False) as user:
        await api(user, 'POST', '/v1/auth/login', json={'username': 'f1-user', 'password': password})
        identity = await api(user, 'GET', '/v1/auth/status')
        t.update(role=identity['role'], user_id=identity['principal']['user_id'])
        await api(user, 'POST', '/v1/agents', status=201, json={'id': 'f1-agent', 'description': 'Disposable F1 ordinary agent',
                  'model': {'profile_id': 'f1-model'}, 'system_prompt': [], 'tools': ['workspace_ext__watch_files'],
                  'max_tool_turns': 4, 'max_output_tokens': 1024})
        workspace = await api(user, 'POST', '/v1/workspaces', status=201, json={'template_id': 'f1-template'})
        wid = t['workspace_id'] = workspace['id']
        await api(user, 'PUT', f'/v1/workspaces/{wid}/files', status=204, params={'path': 'task.json'}, json={'content': '{}'})
        relay.stage = 'cycle1'
        launched = await api(user, 'POST', f'/v1/workspaces/{wid}/sessions', status=201,
                             json={'binding': {'kind': 'agent', 'agent_id': 'f1-agent'}, 'initial_instructions': NEUTRAL,
                                   'base_prompt': BASE, 'auto_start': True})
        sid = t['session_id'] = launched['id']
        t.update(initial_instruction=NEUTRAL, steers=[])
        async def parked():
            s = await snapshot(conn, sid, app)
            return s if s['session'].get('parked_status') == 'parked' and s['lease'] is None else None
        async def idle():
            s = await snapshot(conn, sid, app)
            return s if s['session'].get('turn_status') == 'idle' and s['session'].get('parked_status') is None and s['lease'] is None else None
        t['watches'] = []
        t['nonces'] = {}
        for cycle in (1, 2):
            if cycle == 2:
                relay.stage = 'cycle2'
                relay.arm_crash = True
                await api(user, 'POST', f'/v1/workspaces/{wid}/sessions/{sid}/steer', json={'instruction': NEUTRAL})
                t['steers'].append(NEUTRAL)
            t['watches'].append(await wait_for(parked, seconds=45, relay=relay))
            nonce = secrets.token_hex(12)
            task = {'nonce': nonce, 'value': secrets.token_hex(24)}
            t['nonces'][nonce] = task
            await api(user, 'PUT', f'/v1/workspaces/{wid}/files', status=204, params={'path': 'task.json'}, json={'content': json.dumps(task)})
            if cycle == 1:
                t['snapshots']['idle1'] = await wait_for(idle, seconds=150, relay=relay)
                relay.stage = 'compact'
                t['compaction'] = await api(user, 'POST', f'/v1/workspaces/{wid}/sessions/{sid}/compact', timeout=45)
                t['snapshots']['compacted'] = await snapshot(conn, sid, app)
            else:
                await asyncio.wait_for(relay.held.wait(), timeout=90)
                t['snapshots']['before_kill'] = await snapshot(conn, sid, app)
                from .contracts import live_lease
                live_lease(t['snapshots']['before_kill']['lease'], sid, t['snapshots']['before_kill']['observed_at'])
                app.check()
                t['old_process'] = app.public_identity()
                await app.stop(crash=True)
                t['owned_process_kill_verified'] = True
                t['snapshots']['dead_live'] = await snapshot(conn, sid, app)
                relay.discard.set()
                async def expired():
                    s = await snapshot(conn, sid, app)
                    return s if s['lease'] and datetime.fromisoformat(s['lease']['expires_at']) < datetime.fromisoformat(s['observed_at']) else None
                t['snapshots']['dead_expired'] = await wait_for(expired, seconds=40, relay=relay)
                relay.stage = 'recovery'
                t['new_process'] = app.start()
                await asyncio.wait_for(relay.recovery_held.wait(), timeout=40)
                t['snapshots']['reclaimed'] = await snapshot(conn, sid, app)
                relay.release_recovery.set()
                t['snapshots']['idle2'] = await wait_for(idle, seconds=150, relay=relay)
        info = await api(user, 'GET', f'/v1/sessions/{sid}')
        expected = t['snapshots']['idle2']['session']
        for key in ('id', 'workspace_id', 'base_prompt', 'binding', 'status', 'turn_status', 'turn_no', 'last_seq', 'parked_status', 'last_worker_id'):
            if info.get(key) != expected.get(key):
                raise ValueError('REST/durable session mismatch: ' + key)
        t['rest_idle2'] = info
        messages = await api(user, 'GET', f'/v1/sessions/{sid}/messages', params={'limit': 1000})
        if messages['total'] > 1000:
            raise ValueError('transcript unexpectedly exceeds evidence cap')
        t['rest_records'] = normalized_records(messages['items'])
        slot = root / 'workspaces' / wid / '.state' / 'sessions' / sid
        from primer.workspace.segments import ordered
        disk = []
        for _, name in ordered(p.name for p in slot.iterdir()):
            raw = (slot / name).read_bytes()
            (output / name).write_bytes(raw)
            disk.extend(json.loads(line) for line in raw.splitlines() if line)
        t['disk_records'] = normalized_records(disk)
        t['outputs'] = {}
        for nonce in t['nonces']:
            path = f'answers/{nonce}.json'
            body = await api(user, 'GET', f'/v1/workspaces/{wid}/files/read', params={'path': path})
            data = (root / 'workspaces' / wid / path).read_text()
            if data != body['content']:
                raise ValueError('REST/file output mismatch')
            t['outputs'][nonce] = data
        t['runtime'] = [json.loads(line) for line in (root / 'runtime.jsonl').read_text().splitlines()]


def preserve_native(root: Path, output: Path) -> list[dict]:
    """Bounded whitelist, no private config/password dump, including early failures."""
    candidates = [root / 'runtime.jsonl']
    candidates.extend(sorted((root / 'workspaces').glob('*/.state/sessions/*/messages*.jsonl')))
    copied = []
    for path in candidates:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('unsafe private evidence path')
        if len(copied) >= 32 or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('private native evidence exceeds bound; retain root')
        with path.open('rb') as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError('growing private evidence exceeded byte bound')
        relative = path.relative_to(root)
        target = output / 'private-native' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        copied.append({'path': str(Path('private-native') / relative), 'sha256': digest(data), 'bytes': len(data)})
    return copied


async def _finish_probe(output, root, t, app, relay, server, relay_task, relay_socket, conn, log, started):
    """Always preserve partial evidence, including cleanup/unknown-upstream failures."""
    errors = []
    try:
        if app is not None:
            await app.stop()
        t['cleanup']['processes_gone'] = app is None or (app.proc.poll() is not None and all(not live_group_members(g) for g in app.groups))
    except Exception as exc:
        errors.append('owned_app:' + type(exc).__name__)
        t['cleanup']['processes_gone'] = False
    if conn is not None and t.get('session_id') and app is not None:
        try:
            t['cleanup']['last_durable_observation'] = await asyncio.wait_for(snapshot(conn, t['session_id'], app), timeout=2)
        except Exception as exc:
            errors.append('final_durable:' + type(exc).__name__)
    relay.discard.set()
    relay.release_recovery.set()
    server.should_exit = True
    if relay_task is not None:
        try:
            await asyncio.wait_for(relay_task, timeout=3)
        except Exception as exc:
            errors.append('relay:' + type(exc).__name__)
            relay_task.cancel()
            await asyncio.gather(relay_task, return_exceptions=True)
    # Preserve uncertainty BEFORE local cancellation; closing a socket is not
    # proof that an external generation stopped. Never submit another attempt.
    unknown = relay.unknown_upstream
    pending = list(relay.in_progress)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    try:
        relay_socket.close()
        if conn is not None:
            await conn.close(timeout=3)
        if log is not None:
            log.close()
    except Exception as exc:
        errors.append('handles:' + type(exc).__name__)
    t['cleanup']['unknown_upstream'] = unknown
    t['packets'] = relay.packets
    t['cleanup']['recorded_processes'] = app.recorded_identities() if app is not None and hasattr(app, 'recorded_identities') else []
    try:
        t['cleanup']['native_evidence'] = await asyncio.wait_for(asyncio.to_thread(preserve_native, root, output), timeout=5)
    except Exception as exc:
        errors.append('native_evidence:' + type(exc).__name__)
    if not errors and not unknown and t['cleanup']['processes_gone']:
        try:
            await asyncio.wait_for(asyncio.to_thread(shutil.rmtree, root), timeout=5)
            t['cleanup']['owned_private_root_removed'] = not root.exists()
        except Exception as exc:
            errors.append('private_root:' + type(exc).__name__)
    if root.exists():
        t['cleanup'].update(owned_private_root_removed=False, retained_root=str(root))
    t['elapsed_seconds'] = round(time.monotonic() - started, 6)
    t['cleanup']['errors'] = errors
    if errors or unknown or not t['cleanup']['processes_gone']:
        t['outcome'] = 'failed'



async def finish_probe(output, root, t, app, relay, server, relay_task, relay_socket, conn, log, started):
    try:
        async with asyncio.timeout(25):
            await _finish_probe(output, root, t, app, relay, server, relay_task, relay_socket, conn, log, started)
    except TimeoutError:
        t['outcome'] = 'failed'
        t['cleanup'].update(processes_gone=False, unknown_upstream=True, errors=['cleanup_deadline'],
                            owned_private_root_removed=False, retained_root=str(root))
    finally:
        t['packets'] = relay.packets
        t['elapsed_seconds'] = round(time.monotonic() - started, 6)
        (output / 'trace.json').write_text(json.dumps(t, indent=2) + '\n')


async def run(output: Path):
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    reservation = prerequisites(dict(os.environ), head, time.time())
    require_process_handles()
    if subprocess.check_output(['git', 'status', '--porcelain=v1', '--untracked-files=normal'], cwd=REPO):
        raise ValueError('probe checkout must be clean')
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix='w4-f1-owned-', dir=os.environ['RUNNER_TEMP']))
    t = {'case': CASE, 'source_sha': head, 'driver_hashes': source_hashes(REPO), 'reservation': reservation,
         'base': BASE, 'base_sha256': digest(BASE.encode()), 'storage': 'private-postgres',
         'outcome': 'running', 'snapshots': {}, 'packets': [], 'cleanup': {},
         'controller': {'pid': os.getpid(), 'pgid': os.getpgrp()},
         'run': {k: os.environ[k] for k in ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB', 'RUNNER_ENVIRONMENT', 'RUNNER_OS', 'RUNNER_NAME')},
         'interpreter': {'executable': sys.executable, 'real_executable': os.readlink('/proc/self/exe')}}
    relay = Relay(reservation['endpoint'])
    relay_socket = listening_socket()
    app_socket = listening_socket()
    app_port = app_socket.getsockname()[1]
    app_socket.close()
    server = uvicorn.Server(uvicorn.Config(relay.app, log_level='warning', access_log=False))
    server.install_signal_handlers = lambda: None
    relay_task = None
    app = None
    conn = None
    log = None
    try:
        async with asyncio.timeout(570):
            dsn = os.environ['W4_F1_POSTGRES_DSN']
            conn = await asyncpg.connect(dsn, timeout=5, command_timeout=5)
            # Fresh dedicated database only. No destructive cleanup or accepting stale probe data.
            tables = await conn.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            if tables:
                raise ValueError('private F1 service is not fresh')
            (root / 'wiki').mkdir()
            (root / 'wiki/index.md').write_text('---\ntitle: Private F1 probe\nsummary: Disposable evidence fixture.\n---\n')
            config = root / 'config.json'
            config.write_text(json.dumps(app_config(root, app_port, dsn)))
            config.chmod(0o600)
            t['config_sha256'] = digest(config.read_bytes())
            t['config_path'] = str(config)
            relay_task = asyncio.create_task(server.serve(sockets=[relay_socket]))
            async def relay_ready():
                return server.started
            await wait_for(relay_ready, seconds=5, relay=relay)
            relay_url = f'http://127.0.0.1:{relay_socket.getsockname()[1]}/v1'
            async with httpx.AsyncClient(timeout=40, trust_env=False) as model:
                r = await model.get(reservation['endpoint'] + '/models', timeout=5)
                r.raise_for_status()
                ids = [m['id'] for m in r.json()['data']]
                if MODEL not in ids:
                    raise ValueError('reviewed upstream model absent')
                t['model_ids'] = ids
                capability = {'model': MODEL, 'stream': True, 'max_tokens': 128,
                              'messages': [{'role': 'user', 'content': 'Call w4_probe_capability with ok true.'}],
                              'tools': [{'type': 'function', 'function': {'name': 'w4_probe_capability', 'description': 'A no-effect capability check.',
                                        'parameters': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}}}],
                              'tool_choice': {'type': 'function', 'function': {'name': 'w4_probe_capability'}}}
                r = await model.post(relay_url + '/chat/completions', json=capability)
                r.raise_for_status()
            log = (output / 'app.log').open('wb')
            app = OwnedApp(root, config, log)
            app.start()
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{app_port}', timeout=45, trust_env=False) as client:
                await ready(client, app, relay)
                await lifecycle(root, output, app, relay, conn, client, t, relay_url)
            t['outcome'] = 'passed'
    except BaseException as exc:
        t['outcome'] = 'failed'
        # Do not serialize URLs/credentials from arbitrary exception strings.
        t['failure_type'] = type(exc).__name__
        raise
    finally:
        await finish_probe(output, root, t, app, relay, server, relay_task, relay_socket, conn, log, started)
    try:
        require_evidence(t, head)
    except BaseException as exc:
        t.update(outcome='failed', failure_type=type(exc).__name__, failure_stage='raw_evidence_validation')
        (output / 'trace.json').write_text(json.dumps(t, indent=2) + '\n')
        raise
    (output / 'accepted.json').write_text(json.dumps({'source_sha': head, 'trace_sha256': digest((output / 'trace.json').read_bytes())}) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.output))


if __name__ == '__main__':
    main()
