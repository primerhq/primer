"""Hosted-job-only, read-only runner/protection preflight. Never a model secret."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPOSITORY = 'primerhq/primer'
ENVIRONMENTS = ('w4-f1-control-plane', 'w4-f1-model-reservation')


def canonical(proof: dict) -> bytes:
    return json.dumps(proof, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def runner_label(identifier: int) -> str:
    return 'w4-f1-runner-' + str(identifier)


def assert_protection(value: dict, name: str):
    if value.get('name') != name or not any(r.get('type') == 'required_reviewers' and r.get('reviewers')
                                             and r.get('prevent_self_review') is True
                                             for r in value.get('protection_rules', [])):
        raise ValueError('required independent environment approval absent: ' + name)


def assert_runner(value: dict, identifier: int):
    required = {'self-hosted', 'linux', 'x64', 'w4-f1-isolated', runner_label(identifier)}
    if (value.get('id') != identifier or not value.get('name') or value.get('os') != 'linux'
            or value.get('status') != 'online' or value.get('busy') is not False
            or value.get('ephemeral') is not True
            or not required <= {v['name'].casefold() for v in value.get('labels', [])}):
        raise ValueError('selected runner is not the approved idle ephemeral target')


def execute(env: dict[str, str], *, opener=urlopen, clock=time.time) -> dict:
    """Only an owner-provisioned restricted identity can call runner REST."""
    if (env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or env.get('GITHUB_REF') != 'refs/heads/main'
            or env.get('GITHUB_REPOSITORY') != REPOSITORY or env.get('OPT_IN') != 'true'
            or env.get('RUNNER_ENABLED') != 'true'):
        raise ValueError('trusted manual source/resource authorization absent')
    source = env.get('REVIEWED_SHA', '')
    if not re.fullmatch('[0-9a-f]{40}', source) or source != env.get('GITHUB_SHA'):
        raise ValueError('reviewed source mismatch')
    token = env.get('W4_F1_CONTROL_PLANE_READ_TOKEN', '')
    if not token:
        raise ValueError('dedicated control-plane credential absent; no default-token fallback')
    identifier = env.get('APPROVED_RUNNER_ID', '')
    if not re.fullmatch('[1-9][0-9]*', identifier):
        raise ValueError('owner-approved runner id absent')
    identifier = int(identifier)
    run_id, attempt = env.get('GITHUB_RUN_ID', ''), env.get('GITHUB_RUN_ATTEMPT', '')
    if not run_id.isdecimal() or not attempt.isdecimal():
        raise ValueError('actual run identity absent')
    calls = []
    def get(path):
        request = Request('https://api.github.com/repos/' + REPOSITORY + path,
                          headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                                   'X-GitHub-Api-Version': '2026-03-10'}, method='GET')
        try:
            with opener(request, timeout=10) as response:
                calls.append({'method': 'GET', 'path': path, 'http': response.status})
                if response.status != 200:
                    raise ValueError('control-plane HTTP access refused')
                return json.load(response)
        except HTTPError as exc:
            raise ValueError(f'control-plane HTTP {exc.code}; no fallback or retry') from None
    ref = get('/git/ref/heads/main')
    if ref['object']['sha'] != source:
        raise ValueError('current main differs from reviewed source')
    environments = {}
    for name in ENVIRONMENTS:
        value = get('/environments/' + name)
        assert_protection(value, name)
        environments[name] = {'name': name, 'id': value['id'], 'protection_rules': value['protection_rules']}
    runner = get('/actions/runners/' + str(identifier))
    assert_runner(runner, identifier)
    return {'repository': REPOSITORY, 'source_sha': source, 'run_id': run_id, 'run_attempt': attempt,
            'observed_at': clock(), 'calls': calls, 'environments': environments,
            'runner': {k: runner[k] for k in ('id', 'name', 'os', 'status', 'busy', 'ephemeral', 'labels')}}


def validate_proof(proof: dict, env: dict[str, str], source: str, now: float):
    """Bind the real selected metadata to THIS private job before any I/O."""
    import math
    if (proof['repository'] != REPOSITORY or env.get('GITHUB_REPOSITORY') != REPOSITORY
            or proof['source_sha'] != source or proof['run_id'] != env.get('GITHUB_RUN_ID')
            or proof['run_attempt'] != env.get('GITHUB_RUN_ATTEMPT')
            or env.get('RUNNER_ENVIRONMENT') != 'self-hosted' or env.get('RUNNER_OS') != 'Linux'
            or not math.isfinite(proof['observed_at']) or not 0 <= now - proof['observed_at'] <= 180):
        raise ValueError('control-plane proof is stale or belongs to another source/run/runner environment')
    runner = proof['runner']
    assert_runner(runner, runner['id'])
    if runner['name'] != env.get('RUNNER_NAME'):
        raise ValueError('executing runner differs from actual approved target')
    for name in ENVIRONMENTS:
        assert_protection(proof['environments'][name], name)
    expected = ['/git/ref/heads/main', *('/environments/' + n for n in ENVIRONMENTS), '/actions/runners/' + str(runner['id'])]
    if proof['calls'] != [{'method': 'GET', 'path': p, 'http': 200} for p in expected]:
        raise ValueError('actual successful read-only control-plane calls absent')


def proof_from_env(env: dict[str, str], source: str, now: float) -> dict:
    encoded = env.get('W4_F1_CONTROL_PLANE_PROOF', '')
    if not encoded or len(encoded) > 32768:
        raise ValueError('control-plane receipt absent/oversized')
    raw = base64.b64decode(encoded, validate=True)
    if hashlib.sha256(raw).hexdigest() != env.get('W4_F1_CONTROL_PLANE_SHA256'):
        raise ValueError('control-plane receipt digest mismatch')
    proof = json.loads(raw)
    validate_proof(proof, env, source, now)
    return proof


JOB_NAME = 'W4 F1 protected probe'


def validate_execution(execution: dict, proof: dict, env: dict[str, str], source: str, now: float):
    import math
    job = execution['job']
    runner = proof['runner']
    required_labels = {'self-hosted', 'linux', 'x64', runner_label(runner['id'])}
    if (env.get('GITHUB_JOB') != 'f1' or execution['source_sha'] != source
            or execution['run_id'] != env.get('GITHUB_RUN_ID') or execution['run_attempt'] != env.get('GITHUB_RUN_ATTEMPT')
            or not math.isfinite(execution['observed_at']) or not 0 <= now - execution['observed_at'] <= 30
            or job['name'] != JOB_NAME or job['status'] != 'in_progress' or job['head_sha'] != source
            or str(job['run_id']) != env.get('GITHUB_RUN_ID') or job['runner_id'] != runner['id']
            or job['runner_name'] != runner['name'] or job['runner_name'] != env.get('RUNNER_NAME')
            or job['runner_group_name'] != 'w4-f1-isolated' or not job['runner_group_id']
            or not required_labels <= {x.casefold() for x in job['labels']}):
        raise ValueError('actual running job does not use the approved ephemeral runner/group/source')


def bind_execution(env: dict[str, str], *, opener=urlopen, clock=time.time) -> dict:
    """Actions(read) identity only; the administrative identity is never here."""
    now = clock()
    source = env['GITHUB_SHA']
    proof = proof_from_env(env, source, now)
    token = env.get('W4_F1_EXECUTION_READ_TOKEN', '')
    if not token:
        raise ValueError('read-only Actions identity absent')
    path = '/actions/runs/' + proof['run_id'] + '/attempts/' + proof['run_attempt'] + '/jobs?per_page=100'
    request = Request('https://api.github.com/repos/' + REPOSITORY + path,
                      headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                               'X-GitHub-Api-Version': '2026-03-10'}, method='GET')
    try:
        with opener(request, timeout=10) as response:
            if response.status != 200:
                raise ValueError('running-job metadata refused')
            data = json.load(response)
    except HTTPError as exc:
        raise ValueError(f'Actions job metadata HTTP {exc.code}; no fallback/retry') from None
    if data['total_count'] != len(data['jobs']) or data['total_count'] > 100:
        raise ValueError('incomplete/paginated running-job metadata')
    jobs = [j for j in data['jobs'] if j.get('name') == JOB_NAME]
    if len(jobs) != 1:
        raise ValueError('running protected job is absent or ambiguous')
    job = jobs[0]
    selected = {k: job[k] for k in ('id', 'name', 'status', 'run_id', 'head_sha', 'runner_id', 'runner_name',
                                   'runner_group_id', 'runner_group_name', 'labels')}
    result = {'source_sha': source, 'run_id': proof['run_id'], 'run_attempt': proof['run_attempt'],
              'observed_at': now, 'job': selected}
    validate_execution(result, proof, env, source, now)
    return result


def execution_from_env(env, proof, source, now):
    encoded = env.get('W4_F1_EXECUTION_PROOF', '')
    if not encoded or len(encoded) > 32768:
        raise ValueError('executing-job receipt absent/oversized')
    raw = base64.b64decode(encoded, validate=True)
    if hashlib.sha256(raw).hexdigest() != env.get('W4_F1_EXECUTION_SHA256'):
        raise ValueError('executing-job receipt digest mismatch')
    result = json.loads(raw)
    validate_execution(result, proof, env, source, now)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--bind-execution', action='store_true')
    args = parser.parse_args()
    try:
        proof = bind_execution(dict(os.environ)) if args.bind_execution else execute(dict(os.environ))
    except Exception as exc:
        args.output.write_text(json.dumps({'outcome': 'failed', 'failure_type': type(exc).__name__}) + '\n')
        raise
    raw = canonical(proof)
    args.output.write_bytes(raw)
    outputs = {'proof': base64.b64encode(raw).decode(), 'proof_sha256': hashlib.sha256(raw).hexdigest()}
    if not args.bind_execution:
        outputs['runner_label'] = runner_label(proof['runner']['id'])
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:
        for key, value in outputs.items():
            stream.write(key + '=' + value + '\n')


if __name__ == '__main__':
    main()
