"""Safe F1 wiring checks; no model, process, signal, database or live server."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.e2e.w4_f1 import contracts as c
from scripts.e2e.w4_f1.app import runtime_identity
from scripts.e2e.w4_f1.probe import OwnedApp, app_config


def wire(name=None, args=None, *, model=c.MODEL, finish=None):
    delta = {'tool_calls': [{'index': 0, 'id': 'native-call', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]} if name else {'content': 'Done.'}
    rows = [{'model': model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
            {'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish or ('tool_calls' if name else 'stop')}]}]
    return (''.join('data: ' + json.dumps(r) + '\n\n' for r in rows) + 'data: [DONE]\n\n').encode()


def control_proof(now=100):
    from scripts.e2e.w4_f1.control_plane import ENVIRONMENTS, REPOSITORY, runner_label
    return {'repository': REPOSITORY, 'source_sha': 'a' * 40, 'run_id': '12345', 'run_attempt': '1', 'observed_at': now - 10,
            'runner': {'id': 77, 'name': 'f1-isolated-77', 'os': 'linux', 'status': 'online', 'busy': False, 'ephemeral': True,
                       'labels': [{'name': n} for n in ['self-hosted', 'Linux', 'X64', 'w4-f1-isolated', runner_label(77)]]},
            'environments': {n: {'id': i + 1, 'name': n, 'protection_rules': [
                {'type': 'required_reviewers', 'prevent_self_review': True, 'reviewers': [{'id': 1}]}]} for i, n in enumerate(ENVIRONMENTS)},
            'calls': [{'method': 'GET', 'path': path, 'http': 200} for path in [
                '/git/ref/heads/main', *('/environments/' + n for n in ENVIRONMENTS), '/actions/runners/77']]}


def execution_proof(now=100):
    from scripts.e2e.w4_f1.control_plane import JOB_NAME
    return {'source_sha': 'a' * 40, 'run_id': '12345', 'run_attempt': '1', 'observed_at': now - 1,
            'job': {'id': 88, 'name': JOB_NAME, 'status': 'in_progress', 'run_id': 12345, 'head_sha': 'a' * 40,
                    'runner_id': 77, 'runner_name': 'f1-isolated-77', 'runner_group_name': 'w4-f1-isolated', 'runner_group_id': 3,
                    'labels': ['self-hosted', 'linux', 'x64', 'w4-f1-runner-77']}}


def valid_env(now=100):
    from scripts.e2e.w4_f1.control_plane import canonical
    proof = canonical(control_proof(now))
    execution = canonical(execution_proof(now))
    return {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_SHA': 'a' * 40, 'W4_F1_REVIEWED_SHA': 'a' * 40, 'W4_F1_OPT_IN': 'true',
            'GITHUB_REPOSITORY': 'primerhq/primer', 'GITHUB_RUN_ID': '12345', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_JOB': 'f1',
            'RUNNER_ENVIRONMENT': 'self-hosted', 'RUNNER_OS': 'Linux', 'RUNNER_NAME': 'f1-isolated-77',
            'W4_F1_EXECUTION_PROOF': base64.b64encode(execution).decode(), 'W4_F1_EXECUTION_SHA256': c.digest(execution),
            'W4_F1_CONTROL_PLANE_PROOF': base64.b64encode(proof).decode(), 'W4_F1_CONTROL_PLANE_SHA256': c.digest(proof),
            'W4_F1_ISOLATED_RUNNER': 'ephemeral-private', 'W4_F1_NETWORK_AUTHORIZED': 'true',
            'W4_F1_MODEL_URL': 'http://asus-gx10.piranha-ph.ts.net:8000/v1',
            'W4_F1_RESERVATION_ID': 'owner-issued-window-01', 'W4_F1_RESERVATION_OWNER': 'model-owner',
            'W4_F1_RESERVATION_EXPIRES': str(now + 1900),
            'W4_F1_POSTGRES_DSN': 'postgresql://probe:private@127.0.0.1:55491/primer_w4_f1'}


def test_valid_prerequisites_and_production_pg_schema(tmp_path):
    env = valid_env()
    assert c.prerequisites(env, 'a' * 40, 100)['id'] == env['W4_F1_RESERVATION_ID']
    config = app_config(tmp_path, 18792, env['W4_F1_POSTGRES_DSN'])
    assert config['db']['provider'] == config['scheduler']['provider'] == 'postgres'
    assert config['worker']['lease_ttl_seconds'] >= 2 * config['worker']['heartbeat_interval_seconds']


@pytest.mark.parametrize('field', list(valid_env()))
def test_absent_execution_prerequisite_refuses_before_io(field):
    env = valid_env()
    env.pop(field)
    with pytest.raises((ValueError, KeyError)):
        c.prerequisites(env, 'a' * 40, 100)


@pytest.mark.parametrize('dsn', [
    'postgresql://x:y@127.0.0.1/primer_w4_f1', 'postgresql://x:y@localhost:55512/primer_w4_f1',
    'postgresql://x:y@127.0.0.1:5432/primer_e2e', 'postgresql://x:y@127.0.0.1:55512/primer',
    'sqlite:///tmp/data.sqlite', 'postgresql://x:y@127.0.0.1:55512/primer_w4_f1?schema=shared',
])
def test_shared_ambiguous_or_volatile_storage_refused(dsn):
    with pytest.raises(ValueError):
        c.postgres_config(dsn)


@pytest.mark.parametrize('patch', [{'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_REF': 'refs/heads/evil'},
                                   {'W4_F1_RESERVATION_EXPIRES': '999'}, {'W4_F1_MODEL_URL': 'http://elsewhere/v1'},
                                   {'W4_F1_REVIEWED_SHA': 'b' * 40}, {'W4_F1_OPT_IN': 'false'}])
def test_incorrect_lane_or_expired_reservation_refuses(patch):
    env = valid_env() | patch
    with pytest.raises(ValueError):
        c.prerequisites(env, 'a' * 40, 100)


@pytest.mark.parametrize('raw', [wire(model='wrong-model'), wire(finish='length'), wire()[:-16], b'data: {"error":"bad"}\n\n', b'event: error\n'])
def test_incomplete_or_wrong_native_response_refuses(raw):
    with pytest.raises((ValueError, json.JSONDecodeError)):
        c.parse_response(raw)


def test_native_sse_comments_and_split_tool_arguments_are_supported():
    rows = [
        {'model': c.MODEL, 'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'real-id', 'function': {'name': 'workspace__read', 'arguments': '{"path":'}}]}}]},
        {'model': c.MODEL, 'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '"task.json"}'}}]}, 'finish_reason': 'tool_calls'}]},
    ]
    raw = b': waiting\n\n' + ''.join('data: ' + json.dumps(r) + '\n\n' for r in rows).encode() + b'data: [DONE]\n\n'
    parsed = c.parse_response(raw)
    c.inspect_effects(parsed)
    assert parsed['calls'] == [{'id': 'real-id', 'name': 'workspace__read', 'arguments': {'path': 'task.json'}}]


@pytest.mark.parametrize('name,args', [
    ('workspace__exec', {'command': 'touch shared'}), ('workspace__edit', {'path': 'task.json'}),
    ('workspace__read', {'path': '/etc/passwd'}), ('workspace__read', {'path': 'task.json', 'limit': None}),
    ('workspace_ext__watch_files', {'paths': ['task.json'], 'timeout_seconds': 300}),
    ('workspace__write', {'path': '../escape', 'content': '{}'}),
    ('workspace__write', {'path': 'answers/' + 'a' * 24 + '.json', 'content': '{}', 'force': True}),
])
def test_unsafe_effects_rejected_before_forward(name, args):
    with pytest.raises((ValueError, KeyError)):
        c.inspect_effects(c.parse_response(wire(name, args)))


def test_runtime_assertion_rejects_pg_storage_with_memory_scheduler_or_bus():
    from primer.storage.postgres import PostgresStorageProvider
    app = SimpleNamespace(state=SimpleNamespace(storage_provider=object.__new__(PostgresStorageProvider),
                                                scheduler=object(), event_bus=object(), claim_engine=object()))
    with pytest.raises(ValueError, match='scheduler'):
        runtime_identity(app)


@pytest.mark.parametrize('field,value', [('starttime', 'changed'), ('ppid', -1), ('pgid', -1), ('sid', -1), ('cmdline', [b'shared'])])
def test_process_identity_fence_refuses_signals(monkeypatch, tmp_path, field, value):
    from scripts.e2e.w4_f1 import probe
    config = tmp_path / 'config.json'
    own = {'pid': 999999, 'ppid': probe.os.getpid(), 'pgid': 999999, 'sid': 999999,
           'starttime': 'one', 'exe': '/usr/bin/python', 'cmdline': [b'python', bytes(config)]}
    app = OwnedApp(tmp_path, config, None)
    app.proc = SimpleNamespace(pid=999999, poll=lambda: None)
    own.update(exe=app.expected_executable, cmdline=app.expected_command)
    app.identity = deepcopy(own)
    monkeypatch.setattr(probe, 'live_group_members', lambda group: [])
    monkeypatch.setattr(probe, 'process_identity', lambda pid: own)
    assert app.check() == own
    changed = own | {field: value}
    monkeypatch.setattr(probe, 'process_identity', lambda pid: changed)
    monkeypatch.setattr(probe.os, 'killpg', lambda *args: pytest.fail('must refuse before signaling'))
    with pytest.raises(ValueError, match='identity'):
        app.check()


def make_trace():
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    sid, wid = 'f1-session', 'f1-workspace'
    session = {'id': sid, 'workspace_id': wid, 'base_prompt': c.BASE, 'turn_status': 'idle', 'parked_status': None}
    lease = {'last_heartbeat_at': now.isoformat(), 'kind': 'session', 'entity_id': sid, 'claimed_by': 'old-worker', 'claimed_at': now.isoformat(), 'expires_at': (now + timedelta(seconds=30)).isoformat()}
    def snap(at, owner=None, alive=True):
        return {'observed_at': at.isoformat(), 'session': deepcopy(session), 'lease': deepcopy(owner), 'process_alive': alive}
    snapshots = {n: snap(now) for n in ('idle1', 'compacted', 'idle2')}
    snapshots['before_kill'] = snap(now, lease)
    snapshots['dead_live'] = snap(now + timedelta(seconds=1), lease, False)
    snapshots['dead_expired'] = snap(now + timedelta(seconds=31), lease, False)
    new = lease | {'claimed_by': 'new-worker', 'claimed_at': (now + timedelta(seconds=32)).isoformat(), 'last_heartbeat_at': (now + timedelta(seconds=32)).isoformat(), 'expires_at': (now + timedelta(seconds=62)).isoformat()}
    snapshots['reclaimed'] = snap(now + timedelta(seconds=32), new)
    snapshots['reclaimed']['session']['last_worker_id'] = 'new-worker'
    t = {'case': c.CASE, 'source_sha': 'a' * 40, 'base': c.BASE, 'base_sha256': c.digest(c.BASE.encode()), 'outcome': 'passed',
         'session_id': sid, 'workspace_id': wid, 'role': 'user', 'user_id': 'reader', 'admin_id': 'admin',
         'storage': 'private-postgres', 'reservation': {'id': 'owner-reservation', 'checked_at': now.timestamp(), 'expires': now.timestamp() + 1900, 'control_plane': control_proof(now.timestamp()), 'execution': execution_proof(now.timestamp())},
         'run': {k: valid_env()[k] for k in ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB', 'RUNNER_ENVIRONMENT', 'RUNNER_OS', 'RUNNER_NAME')}, 'snapshots': snapshots,
         'controller': {'pid': 123450, 'pgid': 123450},
         'old_process': {'pid': 123456, 'pgid': 123456, 'sid': 123456, 'ppid': 123450, 'starttime': '100', 'command': ['python', '-m', 'scripts.e2e.w4_f1.app', '--config', '/owned/config.json']},
         'new_process': {'pid': 123457, 'pgid': 123457, 'sid': 123457, 'ppid': 123450, 'starttime': '200', 'command': ['python', '-m', 'scripts.e2e.w4_f1.app', '--config', '/owned/config.json']}, 'owned_process_kill_verified': True,
         'compaction': {'summary': 'Done.', 'compaction_marker_seq': 8}, 'packets': [], 'nonces': {}, 'outputs': {},
         'initial_instruction': c.NEUTRAL, 'steers': [c.NEUTRAL], 'elapsed_seconds': 60,
         'cleanup': {'processes_gone': True, 'unknown_upstream': False, 'owned_private_root_removed': True, 'errors': []},
         'runtime': [dict(storage_provider='PostgresStorageProvider', scheduler='PostgresScheduler', event_bus='PostgresEventBus',
                          claim_engine='PostgresClaimEngine', pid=p, worker_id=w) for p, w in [(123456, 'old-worker'), (123457, 'new-worker')]],
         'watches': [snap(now), snap(now)]}
    for watched in t['watches']:
        watched['session']['parked_status'] = 'parked'
    t['interpreter'] = {'executable': 'python', 'real_executable': '/usr/bin/python3.12'}
    t['config_path'], t['config_sha256'] = '/owned/config.json', 'c' * 64
    for name in ('old_process', 'new_process'):
        t[name]['exe'] = t['interpreter']['real_executable']
    for proof, process in zip(t['runtime'], (t['old_process'], t['new_process']), strict=True):
        proof.update(exe=process['exe'], starttime=process['starttime'], command=deepcopy(process['command']),
                     config_path=t['config_path'], config_sha256=t['config_sha256'])
    t['watches'][0]['observed_at'] = (now - timedelta(seconds=4)).isoformat()
    t['watches'][1]['observed_at'] = (now - timedelta(seconds=1)).isoformat()
    snapshots['idle1']['observed_at'] = (now - timedelta(seconds=3)).isoformat()
    snapshots['compacted']['observed_at'] = (now - timedelta(seconds=2)).isoformat()
    snapshots['idle2']['observed_at'] = (now + timedelta(seconds=33)).isoformat()
    for name, row in snapshots.items():
        row['session']['last_worker_id'] = 'new-worker' if name in ('reclaimed', 'idle2') else 'old-worker'
    records = []
    def packet(stage, name=None, args=None, discarded=False):
        raw = wire(name, args)
        request = {'model': c.MODEL, 'messages': [{'role': 'system', 'content': c.BASE + '\n\nworkspace'}], 'max_tokens': 1024,
                   'tools': [{'function': {'name': n}} for n in c.TOOLS]}
        if stage == 'capability':
            request.update(max_tokens=128, tools=[{'function': {'name': 'w4_probe_capability'}}], messages=[])
        if stage == 'compact':
            request.update(max_tokens=4096, tools=[], messages=[{'role': 'user', 'content': 'History'}])
        t['packets'].append({'index': len(t['packets']) + 1, 'stage': stage, 'request': request, 'response_hex': raw.hex(),
                             'response_sha256': c.digest(raw), 'upstream_complete': True, 'elapsed_seconds': 1,
                             'disposition': 'discarded_at_crash' if discarded else 'forwarded'})
    packet('capability', 'w4_probe_capability', {'ok': True})
    for cycle in (1, 2):
        stage = 'cycle1' if cycle == 1 else 'recovery'
        nonce = str(cycle) * 24
        task = {'nonce': nonce, 'value': str(cycle) * 48}
        t['nonces'][nonce] = task
        t['outputs'][nonce] = json.dumps(task)
        if cycle == 2:
            records.append({'seq': len(records) + 1, 'kind': 'compaction_marker', 'payload': {'summary': 'Done.'}, 'created_at': now.isoformat()})
            packet('compact')
        actions = [('workspace_ext__watch_files', {'paths': ['task.json'], 'timeout_seconds': 30}, json.dumps({'timed_out': False, 'changes': [{'path': 'task.json'}]})),
                   ('workspace__read', {'path': 'task.json'}, '1: ' + json.dumps(task)),
                   ('workspace__write', {'path': f'answers/{nonce}.json', 'content': json.dumps(task)}, 'wrote bytes')]
        for i, (name, args, result) in enumerate(actions):
            packet('cycle2' if cycle == 2 and i == 0 else stage, name, args)
            if cycle == 2 and i == 0:
                packet('cycle2', 'workspace__read', {'path': 'task.json'}, discarded=True)
            call_id = f'call-{cycle}-{i}'
            for kind, payload in [('tool_call', {'id': call_id, 'raw_id': 'native-call', 'name': name, 'arguments': args}),
                                  ('tool_result', {'call_id': call_id, 'output': result, 'error': False})]:
                records.append({'seq': len(records) + 1, 'kind': kind, 'payload': payload, 'created_at': now.isoformat()})
        packet(stage)
        records.append({'seq': len(records) + 1, 'kind': 'done', 'payload': {'stop_reason': 'stop'}, 'created_at': now.isoformat()})
    t['rest_idle2'] = deepcopy(snapshots['idle2']['session'])
    from scripts.e2e.w4_f1.probe import normalized_records
    t['disk_records'] = normalized_records(records)
    t['rest_records'] = deepcopy(t['disk_records'])
    t['cleanup']['native_evidence'] = native_fixture_rows(t)
    return t


def native_fixture_bytes(trace):
    return {'private-native/runtime.jsonl': ''.join(json.dumps(r) + '\n' for r in trace['runtime']).encode(),
            f"private-native/workspaces/{trace['workspace_id']}/.state/sessions/{trace['session_id']}/messages.jsonl":
            ''.join(json.dumps(r) + '\n' for r in trace['disk_records']).encode()}


def native_fixture_rows(trace):
    return [{'path': path, 'sha256': c.digest(data), 'bytes': len(data)} for path, data in native_fixture_bytes(trace).items()]


def write_native_fixture(output, trace):
    for path, data in native_fixture_bytes(trace).items():
        target = output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def test_connected_synthetic_fixture_exercises_full_evidence_validator():
    # Wiring fixture only. This is deliberately not an F1 acceptance artifact.
    c.require_evidence(make_trace(), 'a' * 40)


@pytest.mark.parametrize('mutation', [
    lambda t: t.update(source_sha='b' * 40), lambda t: t.update(outcome='running'),
    lambda t: t.update(storage='sqlite'), lambda t: t['runtime'][0].update(claim_engine='InMemoryClaimEngine'),
    lambda t: t['snapshots']['before_kill'].update(lease=None),
    lambda t: t['snapshots']['dead_live'].update(process_alive=True),
    lambda t: t['snapshots']['dead_expired']['lease'].update(expires_at='2099-01-01T00:00:00+00:00'),
    lambda t: t['snapshots']['reclaimed']['lease'].update(claimed_by='old-worker'),
    lambda t: t['snapshots']['compacted']['session'].update(base_prompt='changed'),
    lambda t: t.update(steers=[c.BASE]), lambda t: t['cleanup'].update(unknown_upstream=True),
    lambda t: t['cleanup'].update(processes_gone=False), lambda t: t.update(elapsed_seconds=601),
    lambda t: t['packets'][0].update(upstream_complete=False),
    lambda t: t['packets'][0].update(response_sha256='tampered'),
    lambda t: next(p for p in t['packets'] if p['disposition'] == 'discarded_at_crash').update(disposition='forwarded'),
    lambda t: t['watches'][0]['session'].update(parked_status=None),
    lambda t: t['rest_records'].pop(), lambda t: t['disk_records'][1]['payload'].update(error=True),
    lambda t: t['outputs'].update({'1' * 24: '{}'}),
])
def test_invalid_connected_evidence_is_not_acceptance(mutation):
    trace = make_trace()
    mutation(trace)
    with pytest.raises((AssertionError, ValueError, KeyError, TypeError)):
        c.require_evidence(trace, 'a' * 40)


def test_actual_pg_runtime_types_are_recorded_and_each_memory_substitution_refuses():
    from primer.bus.postgres import PostgresEventBus
    from primer.claim.postgres import PostgresClaimEngine
    from primer.scheduler.postgres import PostgresScheduler
    from primer.storage.postgres import PostgresStorageProvider
    # No constructors/DB pools/startup. Exercise the real type assertion only.
    values = {name: object.__new__(kind) for name, kind in {
        'storage_provider': PostgresStorageProvider, 'event_bus': PostgresEventBus,
        'claim_engine': PostgresClaimEngine, 'scheduler': PostgresScheduler}.items()}
    app = SimpleNamespace(state=SimpleNamespace(**values, worker_pool=SimpleNamespace(worker_id='private-worker')))
    proof = runtime_identity(app)
    assert proof['claim_engine'] == 'PostgresClaimEngine' and proof['worker_id'] == 'private-worker'
    for name in values:
        original = getattr(app.state, name)
        setattr(app.state, name, object())
        with pytest.raises(ValueError, match=name):
            runtime_identity(app)
        setattr(app.state, name, original)


@pytest.mark.parametrize('mutation', [
    lambda t: t['runtime'][0].update(worker_id='unrelated-worker'),
    lambda t: t['snapshots']['before_kill'].update(process_alive=False),
    lambda t: t['snapshots']['before_kill']['lease'].update(expires_at='2099-01-01T00:00:00+00:00'),
    lambda t: t['old_process'].update(exe='/bin/sh'),
    lambda t: t['snapshots']['dead_live'].update(observed_at='2020-01-01T00:00:00+00:00'),
    lambda t: t['snapshots']['reclaimed'].update(process_alive=False),
    lambda t: t['snapshots']['dead_expired']['lease'].update(claimed_at='2020-01-01T00:00:00+00:00'),
    lambda t: t['runtime'][1].update(config_sha256='wrong-configuration'),
    lambda t: t['snapshots']['reclaimed']['session'].update(last_worker_id='unrelated-worker'),
])
def test_independent_review_chain_mutations_are_rejected(mutation):
    trace = make_trace()
    c.require_evidence(trace, 'a' * 40)  # Calibrate the unchanged raw packet first.
    mutation(trace)
    with pytest.raises((AssertionError, ValueError, KeyError, TypeError)):
        c.require_evidence(trace, 'a' * 40)


@pytest.mark.parametrize('path', [
    ('old_process', 'exe'), ('new_process', 'starttime'), ('runtime', 0, 'config_sha256'),
    ('snapshots', 'before_kill', 'process_alive'), ('snapshots', 'dead_live', 'observed_at'),
    ('snapshots', 'dead_expired', 'lease', 'last_heartbeat_at'), ('interpreter', 'real_executable'),
])
def test_missing_chain_identity_is_not_optional(path):
    trace = make_trace()
    target = trace
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    with pytest.raises((AssertionError, ValueError, KeyError, TypeError)):
        c.require_evidence(trace, 'a' * 40)


def test_legitimate_final_heartbeat_before_death_is_preserved():
    trace = make_trace()
    before = trace['snapshots']['before_kill']
    heartbeat = datetime.fromisoformat(before['observed_at']) + timedelta(seconds=.5)
    for name in ('dead_live', 'dead_expired'):
        trace['snapshots'][name]['lease'].update(last_heartbeat_at=heartbeat.isoformat(),
                                               expires_at=(heartbeat + timedelta(seconds=30)).isoformat())
    c.require_evidence(trace, 'a' * 40)


@pytest.mark.parametrize('expiry', ['nan', 'NaN', '+inf', 'inf', '-inf', 'not-a-time', '', '999'])
def test_nonfinite_malformed_or_expired_reservation_refused(expiry):
    env = valid_env() | {'W4_F1_RESERVATION_EXPIRES': expiry}
    with pytest.raises(ValueError):
        c.prerequisites(env, 'a' * 40, 100)
