"""Execute clean-environment CI guards without a runner, model or database."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import io
from urllib.error import HTTPError
from urllib.parse import urlsplit
import sys
from xml.etree import ElementTree as ET

import pytest
import yaml

from scripts.e2e.w4_f1 import contracts as c
from scripts.e2e.w4_f1 import probe, verify
from tests.tooling.test_w4_f1_contracts import make_trace, control_proof, valid_env, write_native_fixture

REPO = Path(__file__).resolve().parents[2]


def workflow():
    return yaml.load((REPO / '.github/workflows/w4-f1.yml').read_text(), Loader=yaml.BaseLoader)


def test_manual_lane_keeps_existing_selection_and_failure_gates():
    value = workflow()
    assert set(value['on']) == {'workflow_dispatch'}
    assert value['on']['workflow_dispatch']['inputs']['opt_in']['default'] == 'false'
    assert value['concurrency'] == {'group': 'w4-f1-k1-qwen38-27b', 'cancel-in-progress': 'false'}
    job = value['jobs']['f1']
    assert job['needs'] == 'prerequisites' and job['environment'] == 'w4-f1-model-reservation'
    assert job['runs-on']['group'] == 'w4-f1-isolated'
    assert job['services']['postgres']['env']['POSTGRES_DB'] == 'primer_w4_f1'
    steps = job['steps']
    assert next(s for s in steps if s.get('name') == 'Upload actual or partial evidence')['if'] == 'always()'
    gate = next(s for s in steps if s.get('name') == 'Require connected raw evidence and named execution')
    assert 'if' not in gate and 'continue-on-error' not in gate
    assert 'scripts.e2e.w4_f1.verify' in gate['run']
    run = next(s for s in steps if s.get('name') == 'Run only the bounded genuine F1 case')
    assert '-o addopts= -n0' in run['run'] and 'acceptance.py' in run['run']
    # Neither a normal tests/ sweep nor ordinary PR live CI can accidentally opt in.
    assert 'not requires_llm' in (REPO / '.github/workflows/e2e.yml').read_text()


def control_env():
    return {'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': 'a' * 40,
            'REVIEWED_SHA': 'a' * 40, 'GITHUB_REPOSITORY': 'primerhq/primer', 'GITHUB_RUN_ID': '12345',
            'GITHUB_RUN_ATTEMPT': '1', 'OPT_IN': 'true', 'RUNNER_ENABLED': 'true', 'APPROVED_RUNNER_ID': '77',
            'W4_F1_CONTROL_PLANE_READ_TOKEN': 'synthetic-owner-scoped-read-token'}


class MetadataResponse(io.BytesIO):
    status = 200


def metadata_opener(env, missing=None, calls=None):
    proof = control_proof()
    def open_metadata(request, timeout):
        if calls is not None:
            calls.append(request)
        assert request.method == 'GET' and timeout == 10
        assert request.get_header('Authorization') == 'Bearer ' + env['W4_F1_CONTROL_PLANE_READ_TOKEN']
        assert request.full_url.startswith('https://api.github.com/repos/primerhq/primer/')
        path = urlsplit(request.full_url).path.removeprefix('/repos/primerhq/primer')
        if missing == 'permission':
            raise HTTPError(request.full_url, 403, 'Forbidden', {}, None)
        if path == '/git/ref/heads/main':
            data = {'object': {'sha': ('b' if missing == 'source' else 'a') * 40}}
        elif path.startswith('/environments/'):
            name = path.rsplit('/', 1)[1]
            data = deepcopy(proof['environments'][name])
            if missing == 'protection':
                data['protection_rules'] = []
        else:
            assert path == '/actions/runners/77'
            data = deepcopy(proof['runner'])
            if missing == 'runner':
                raise HTTPError(request.full_url, 404, 'Missing runner', {}, None)
            if missing == 'ephemeral':
                data['ephemeral'] = False
            if missing == 'affinity':
                data['labels'] = [r for r in data['labels'] if r['name'] != 'w4-f1-runner-77']
        return MetadataResponse(json.dumps(data).encode())
    return open_metadata


@pytest.mark.parametrize('missing', ['authorization', 'protection', 'runner', 'source', 'permission', 'ephemeral', 'affinity', None])
def test_actual_preflight_uses_supported_restricted_identity_and_fail_closed_http(missing):
    from scripts.e2e.w4_f1.control_plane import execute, validate_proof
    env = control_env()
    if missing == 'authorization':
        env.pop('W4_F1_CONTROL_PLANE_READ_TOKEN')
        env['GH_TOKEN'] = 'must-not-fall-back-to-this'
    calls = []
    opener = metadata_opener(env, missing, calls)
    if missing is not None:
        with pytest.raises(ValueError):
            execute(env, opener=opener, clock=lambda: 90)
        if missing == 'authorization':
            assert calls == []
        if missing == 'permission':
            assert len(calls) == 1  # No retry/fallback identity.
    else:
        proof = execute(env, opener=opener, clock=lambda: 90)
        validate_proof(proof, valid_env(), 'a' * 40, 100)
        assert len(calls) == 4 and 'synthetic-owner-scoped-read-token' not in json.dumps(proof)
        assert proof['runner']['id'] == 77 and proof['runner']['ephemeral'] is True


def test_actual_basic_guard_runs_cleanly_before_credential_step():
    job = workflow()['jobs']['prerequisites']
    assert job['environment'] == 'w4-f1-control-plane'
    assert 'GH_TOKEN' not in job.get('env', {}) and 'W4_F1_CONTROL_PLANE_READ_TOKEN' not in job.get('env', {})
    control = next(s for s in job['steps'] if s.get('id') == 'control')
    assert control['env']['W4_F1_CONTROL_PLANE_READ_TOKEN'] == '${{ secrets.W4_F1_CONTROL_PLANE_READ_TOKEN }}'
    assert 'github.token' not in json.dumps(control)
    assert 'W4_F1_CONTROL_PLANE_READ_TOKEN' not in json.dumps(workflow()['jobs']['f1'])
    code = job['steps'][0]['run'].split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    env = control_env()
    env.pop('W4_F1_CONTROL_PLANE_READ_TOKEN')
    assert subprocess.run([sys.executable, '-c', code], env=env, capture_output=True).returncode == 0
    env['OPT_IN'] = 'false'
    assert subprocess.run([sys.executable, '-c', code], env=env, capture_output=True).returncode != 0


@pytest.mark.parametrize('patch', [{'RUNNER_NAME': 'unrelated-runner'}, {'GITHUB_RUN_ID': 'other-run'},
                                   {'RUNNER_ENVIRONMENT': 'github-hosted'}, {'RUNNER_OS': 'Windows'},
                                   {'W4_F1_CONTROL_PLANE_SHA256': 'tampered'}])
def test_observed_metadata_binds_to_actual_target_before_any_probe_io(patch):
    env = valid_env() | patch
    with pytest.raises(ValueError):
        c.prerequisites(env, 'a' * 40, 100)


@pytest.mark.asyncio
async def test_unapproved_driver_stops_before_app_network_database_or_output(monkeypatch, tmp_path):
    monkeypatch.setattr(probe.subprocess, 'check_output', lambda *a, **kw: 'a' * 40 + '\n')
    monkeypatch.setattr(probe.asyncpg, 'connect', lambda *a, **kw: pytest.fail('must not connect'))
    monkeypatch.setattr(probe.OwnedApp, 'start', lambda *a: pytest.fail('must not start app'))
    monkeypatch.setattr(probe, 'listening_socket', lambda: pytest.fail('must not open socket'))
    for name in ('GITHUB_ACTIONS', 'W4_F1_OPT_IN'):
        monkeypatch.delenv(name, raising=False)
    output = tmp_path / 'not-created'
    with pytest.raises(ValueError, match='prerequisites'):
        await probe.run(output)
    assert not output.exists()


def test_junit_gate_checks_actual_source_digest_and_named_case(tmp_path, monkeypatch):
    trace = make_trace()
    trace['driver_hashes'] = {'frozen.py': 'exact-bytes'}
    monkeypatch.setattr(verify, 'source_hashes', lambda repo: {'frozen.py': 'exact-bytes'})
    write_native_fixture(tmp_path, trace)
    raw = json.dumps(trace).encode()
    (tmp_path / 'trace.json').write_bytes(raw)
    receipt = {'source_sha': 'a' * 40, 'trace_sha256': c.digest(raw)}
    (tmp_path / 'accepted.json').write_text(json.dumps(receipt))
    junit = tmp_path / 'junit.xml'
    junit.write_text(f'<testsuite><testcase classname="scripts.e2e.w4_f1.acceptance" name="{c.CASE}"/></testsuite>')
    assert verify.verify(tmp_path, 'a' * 40, junit, expected_run=trace['run'])['trace_sha256'] == c.digest(raw)
    # A collector/precondition/failed/wrong-class/duplicate case cannot pass.
    for tag in ('skipped', 'failure', 'error'):
        junit.write_text(f'<testsuite><testcase classname="scripts.e2e.w4_f1.acceptance" name="{c.CASE}"><{tag}/></testcase></testsuite>')
        with pytest.raises(ValueError, match='execute'):
            verify.verify(tmp_path, 'a' * 40, junit, expected_run=trace['run'])
    for xml in ('<testsuite/>', f'<testsuite><testcase classname="other" name="{c.CASE}"/></testsuite>',
                f'<testsuite><testcase classname="scripts.e2e.w4_f1.acceptance" name="{c.CASE}"/><testcase classname="scripts.e2e.w4_f1.acceptance" name="{c.CASE}"/></testsuite>'):
        junit.write_text(xml)
        with pytest.raises(ValueError, match='execute'):
            verify.verify(tmp_path, 'a' * 40, junit, expected_run=trace['run'])
    (tmp_path / 'trace.json').write_bytes(raw + b' ')
    with pytest.raises(ValueError, match='receipt'):
        verify.verify(tmp_path, 'a' * 40, junit, expected_run=trace['run'])


def test_no_owned_process_identity_can_signal_controller_group(monkeypatch, tmp_path):
    config = tmp_path / 'config.json'
    own = {'pid': os.getpid(), 'ppid': os.getpid(), 'pgid': os.getpgrp(), 'sid': os.getpgrp(),
           'starttime': 'one', 'exe': sys.executable, 'cmdline': [bytes(config)]}
    app = probe.OwnedApp(tmp_path, config, None)
    from types import SimpleNamespace
    app.proc = SimpleNamespace(pid=own['pid'], poll=lambda: None)
    app.identity = deepcopy(own)
    monkeypatch.setattr(probe, 'process_identity', lambda pid: own)
    monkeypatch.setattr(probe.os, 'killpg', lambda *a: pytest.fail('controller must not be signaled'))
    with pytest.raises(ValueError):
        app.check()


def test_partial_raw_trace_cannot_acquire_receipt(tmp_path, monkeypatch):
    trace = make_trace()
    trace['driver_hashes'] = {}
    trace['snapshots'].pop('dead_expired')
    raw = json.dumps(trace).encode()
    (tmp_path / 'trace.json').write_bytes(raw)
    (tmp_path / 'accepted.json').write_text(json.dumps({'source_sha': 'a' * 40, 'trace_sha256': c.digest(raw)}))
    monkeypatch.setattr(verify, 'source_hashes', lambda repo: {})
    junit = tmp_path / 'junit.xml'
    root = ET.Element('testsuite')
    ET.SubElement(root, 'testcase', classname='scripts.e2e.w4_f1.acceptance', name=c.CASE)
    ET.ElementTree(root).write(junit)
    with pytest.raises(AssertionError):
        verify.verify(tmp_path, 'a' * 40, junit, expected_run=trace['run'])


@pytest.mark.asyncio
async def test_private_api_helper_accepts_actual_registration_status_metadata():
    import httpx
    from primer.api.routers.auth import auth_router
    register = next(r for r in auth_router.routes if r.path.endswith('/register'))
    login = next(r for r in auth_router.routes if r.path.endswith('/login'))
    assert (register.status_code or 200) == (login.status_code or 200) == 200
    async with httpx.AsyncClient(base_url='http://synthetic.invalid', transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'username': 'private-admin'}))) as client:
        for verb in ('register', 'login'):
            assert await probe.api(client, 'POST', '/v1/auth/' + verb, status=200, json={}) == {'username': 'private-admin'}


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_partial_raw_packet_without_acceptance(tmp_path):
    import asyncio
    import time
    from types import SimpleNamespace
    from scripts.e2e.w4_f1.relay import Relay
    root = tmp_path / 'owned'
    root.mkdir()
    (root / 'runtime.jsonl').write_text('{"private_runtime": "preserve"}\n')
    (root / 'config.json').write_text('{"password": "do-not-copy"}')
    output = tmp_path / 'evidence'
    output.mkdir()
    async def refuse():
        raise ValueError('identity changed, refuse signal')
    app = SimpleNamespace(stop=refuse)
    relay = Relay('http://synthetic.invalid/v1')
    relay.unknown_upstream = True
    relay.packets = [{'partial': 'actual bytes retained'}]
    trace = {'outcome': 'running', 'cleanup': {}}
    server = SimpleNamespace(should_exit=False)
    sock = SimpleNamespace(close=lambda: None)
    await probe.finish_probe(output, root, trace, app, relay, server, None, sock, None, None, time.monotonic())
    saved = json.loads((output / 'trace.json').read_text())
    assert saved['outcome'] == 'failed' and saved['packets'] == relay.packets
    assert not saved['cleanup']['processes_gone'] and saved['cleanup']['unknown_upstream']
    assert saved['cleanup']['errors'] == ['owned_app:ValueError']
    assert server.should_exit and root.exists() and not (output / 'accepted.json').exists()
    assert saved['cleanup']['retained_root'] == str(root)
    assert (output / 'private-native/runtime.jsonl').read_bytes() == (root / 'runtime.jsonl').read_bytes()
    assert not (output / 'private-native/config.json').exists()
    assert not relay.in_progress and isinstance(relay.discard, asyncio.Event)


@pytest.mark.asyncio
@pytest.mark.parametrize('expiry', ['nan', '+inf', '-inf', 'malformed', '0'])
async def test_invalid_window_stops_before_output_socket_db_or_process(monkeypatch, tmp_path, expiry):
    import time
    env = valid_env(time.time()) | {'W4_F1_RESERVATION_EXPIRES': expiry, 'RUNNER_TEMP': str(tmp_path)}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(probe.subprocess, 'check_output', lambda args, **kwargs: 'a' * 40 + '\n' if 'rev-parse' in args else b'')
    monkeypatch.setattr(probe.asyncpg, 'connect', lambda *a, **kw: pytest.fail('must refuse before DB'))
    monkeypatch.setattr(probe, 'listening_socket', lambda: pytest.fail('must refuse before socket/model'))
    monkeypatch.setattr(probe.OwnedApp, 'start', lambda *a: pytest.fail('must refuse before process'))
    output = tmp_path / 'not-created'
    with pytest.raises(ValueError):
        await probe.run(output)
    assert not output.exists()


@pytest.mark.asyncio
async def test_early_failure_copies_native_runtime_and_session_files_before_removal(tmp_path):
    import time
    from types import SimpleNamespace
    from scripts.e2e.w4_f1.relay import Relay
    root, output = tmp_path / 'owned', tmp_path / 'evidence'
    root.mkdir()
    output.mkdir()
    runtime = b'{"worker_id":"actual-partial-worker"}\n'
    messages = b'{"native_partial":"preserve exact bytes"}\n'
    (root / 'runtime.jsonl').write_bytes(runtime)
    (root / 'config.json').write_text('{"password":"never-upload"}')
    slot = root / 'workspaces/owned/.state/sessions/partial'
    slot.mkdir(parents=True)
    (slot / 'messages.jsonl').write_bytes(messages)
    trace = {'outcome': 'failed', 'cleanup': {}}
    relay = Relay('http://synthetic.invalid/v1')
    await probe.finish_probe(output, root, trace, None, relay, SimpleNamespace(should_exit=False), None,
                             SimpleNamespace(close=lambda: None), None, None, time.monotonic())
    assert not root.exists() and trace['cleanup']['processes_gone']
    assert (output / 'private-native/runtime.jsonl').read_bytes() == runtime
    assert (output / 'private-native/workspaces/owned/.state/sessions/partial/messages.jsonl').read_bytes() == messages
    assert not (output / 'private-native/config.json').exists() and not (output / 'accepted.json').exists()
    assert len(trace['cleanup']['native_evidence']) == 2
    for row in trace['cleanup']['native_evidence']:
        assert c.digest((output / row['path']).read_bytes()) == row['sha256']


def dead_leader_fixture(monkeypatch, tmp_path, *, reused=False, unknown=False):
    from types import SimpleNamespace
    app = probe.OwnedApp(tmp_path, tmp_path / 'config.json', None)
    app.proc = SimpleNamespace(pid=999991, poll=lambda: 0, returncode=0)
    app.groups = [999991]
    child = {'pid': 999992, 'ppid': 999991, 'pgid': 999991, 'sid': 999991,
             'starttime': '100', 'exe': '/usr/bin/git', 'cmdline': [b'git', b'commit']}
    app.members = {} if unknown else {999992: deepcopy(child)}
    live = [999992]
    events = []
    monkeypatch.setattr(probe, 'live_group_members', lambda pgid: list(live))
    current = child | {'ppid': 1, 'starttime': '200' if reused else '100'}
    monkeypatch.setattr(probe, 'process_identity', lambda pid: current)
    monkeypatch.setattr(probe.os, 'killpg', lambda *a: pytest.fail('dead leader does not authorize group signal'))
    def pidfd_open(pid, flags):
        events.append(('open', pid, flags))
        return 99999
    def pidfd_signal(fd, sig):
        events.append(('signal', fd, sig))
        live.clear()
    monkeypatch.setattr(probe.os, 'pidfd_open', pidfd_open, raising=False)
    monkeypatch.setattr(probe.signal, 'pidfd_send_signal', pidfd_signal, raising=False)
    original_close = probe.os.close
    monkeypatch.setattr(probe.os, 'close', lambda fd: events.append(('close', fd)) if fd == 99999 else original_close(fd))
    return app, events, live


@pytest.mark.asyncio
async def test_dead_leader_known_reparented_descendant_uses_verified_pidfd(monkeypatch, tmp_path):
    app, events, live = dead_leader_fixture(monkeypatch, tmp_path)
    await app.stop()
    assert not live and events == [('open', 999992, 0), ('signal', 99999, probe.signal.SIGTERM), ('close', 99999)]


@pytest.mark.asyncio
@pytest.mark.parametrize('reused,unknown', [(True, False), (False, True)])
async def test_dead_leader_reused_or_uncaptured_descendant_is_not_signaled(monkeypatch, tmp_path, reused, unknown):
    app, events, live = dead_leader_fixture(monkeypatch, tmp_path, reused=reused, unknown=unknown)
    with pytest.raises(ValueError):
        await app.stop()
    assert live == [999992] and not any(e[0] == 'signal' for e in events)
    assert events == ([] if unknown else [('open', 999992, 0), ('close', 99999)])


@pytest.mark.parametrize('field,value', [('runner_id', 999), ('runner_group_name', 'other-group'), ('runner_name', 'other-runner'),
                                        ('head_sha', 'b' * 40), ('status', 'completed')])
def test_actions_read_binding_rejects_different_actual_job_target(field, value):
    from scripts.e2e.w4_f1.control_plane import bind_execution
    from tests.tooling.test_w4_f1_contracts import execution_proof
    env = valid_env() | {'W4_F1_EXECUTION_READ_TOKEN': 'synthetic-actions-read'}
    job = execution_proof()['job'] | {field: value}
    def opener(request, timeout):
        assert request.get_header('Authorization') == 'Bearer synthetic-actions-read'
        assert request.method == 'GET' and '/actions/runs/12345/attempts/1/jobs?' in request.full_url
        return MetadataResponse(json.dumps({'total_count': 1, 'jobs': [job]}).encode())
    with pytest.raises(ValueError):
        bind_execution(env, opener=opener, clock=lambda: 100)


def test_actions_read_binding_uses_only_the_supported_jobs_endpoint_and_no_admin_secret():
    from scripts.e2e.w4_f1.control_plane import bind_execution
    from tests.tooling.test_w4_f1_contracts import execution_proof
    env = valid_env() | {'W4_F1_EXECUTION_READ_TOKEN': 'synthetic-actions-read'}
    assert 'W4_F1_CONTROL_PLANE_READ_TOKEN' not in env
    calls = []
    def opener(request, timeout):
        calls.append(request)
        assert request.get_header('Authorization') == 'Bearer synthetic-actions-read'
        assert request.method == 'GET' and request.full_url.endswith('/actions/runs/12345/attempts/1/jobs?per_page=100')
        return MetadataResponse(json.dumps({'total_count': 1, 'jobs': [execution_proof()['job']]}).encode())
    result = bind_execution(env, opener=opener, clock=lambda: 100)
    assert result['job']['runner_id'] == 77 and result['job']['runner_group_name'] == 'w4-f1-isolated' and len(calls) == 1
    def forbidden(request, timeout):
        raise HTTPError(request.full_url, 403, 'Missing Actions read', {}, None)
    with pytest.raises(ValueError, match='403'):
        bind_execution(env, opener=forbidden, clock=lambda: 100)


def test_native_handle_capability_refuses_missing_apis_without_opening_anything(monkeypatch):
    monkeypatch.delattr(probe.os, 'pidfd_open', raising=False)
    with pytest.raises(ValueError, match='pidfd'):
        probe.require_process_handles()


def test_native_handle_capability_checks_only_own_pid_and_never_signals(monkeypatch):
    calls = []
    monkeypatch.setattr(probe.os, 'pidfd_open', lambda pid, flags: calls.append(('open', pid, flags)) or 99999, raising=False)
    original_close = probe.os.close
    monkeypatch.setattr(probe.os, 'close', lambda fd: calls.append(('close', fd)) if fd == 99999 else original_close(fd))
    monkeypatch.setattr(probe.signal, 'pidfd_send_signal', lambda *a: pytest.fail('capability check sends no signal'), raising=False)
    probe.require_process_handles()
    assert calls == [('open', os.getpid(), 0), ('close', 99999)]


@pytest.mark.asyncio
async def test_owned_start_scrubs_credentials_and_records_descendants_without_real_process(monkeypatch, tmp_path):
    from types import SimpleNamespace
    app = probe.OwnedApp(tmp_path, tmp_path / 'config.json', None)
    leader = {'pid': 999991, 'ppid': os.getpid(), 'pgid': 999991, 'sid': 999991,
              'exe': app.expected_executable, 'cmdline': app.expected_command, 'starttime': '100'}
    child = {'pid': 999992, 'ppid': 999991, 'pgid': 999991, 'sid': 999991,
             'exe': '/usr/bin/git', 'cmdline': [b'git', b'commit'], 'starttime': '101'}
    calls = []
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(pid=999991, poll=lambda: None)
    monkeypatch.setenv('W4_F1_CONTROL_PLANE_READ_TOKEN', 'never-inherit-admin')
    monkeypatch.setenv('GITHUB_TOKEN', 'never-inherit-actions')
    monkeypatch.setattr(probe.subprocess, 'Popen', popen)
    monkeypatch.setattr(probe, 'process_identity', lambda pid: leader if pid == 999991 else child)
    monkeypatch.setattr(probe, 'live_group_members', lambda group: [999991, 999992])
    app.start()
    assert calls[0][1]['start_new_session'] is True and calls[0][1]['cwd'] == tmp_path
    assert not any('TOKEN' in name or name in ('HOME', 'GH_TOKEN') for name in calls[0][1]['env'])
    assert set(app.members) == {999991, 999992} and app.members[999992]['starttime'] == '101'


def test_evidence_gate_refuses_a_different_actual_run_even_with_same_source(tmp_path, monkeypatch):
    trace = make_trace()
    trace['driver_hashes'] = {}
    raw = json.dumps(trace).encode()
    (tmp_path / 'trace.json').write_bytes(raw)
    (tmp_path / 'accepted.json').write_text(json.dumps({'source_sha': 'a' * 40, 'trace_sha256': c.digest(raw)}))
    expected = trace['run'] | {'GITHUB_RUN_ID': 'another-run'}
    with pytest.raises(ValueError, match='another actual job'):
        verify.verify(tmp_path, 'a' * 40, tmp_path / 'absent.xml', expected_run=expected)
