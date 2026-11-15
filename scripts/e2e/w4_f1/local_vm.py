"""Explicit isolated-VM F1 entry. It cannot impersonate or dispatch GitHub Actions."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
import time

import httpx

from . import probe
from .contracts import CASE, MODEL, digest, postgres_config, require_lifecycle_evidence, source_hashes
from .local_authority import ENDPOINT, guest_trust_key, observe_guest, read_packet, validate_authority, validate_guest


def local_context(env: dict):
    if any(k.startswith(('GITHUB_', 'RUNNER_')) or k.startswith('W4_F1_CONTROL_PLANE') or k.startswith('W4_F1_EXECUTION_') for k in env):
        raise ValueError('local execution must not contain GitHub/runner receipts')


def require_local_evidence(t: dict, source: str, key: bytes, owner_key: bytes) -> None:
    if sys.flags.optimize:
        raise ValueError('local evidence requires active raw assertions')
    if t.get('authority_mode') != 'local-vm' or set(t['run']) != {'mode', 'run_id', 'vm_uuid'}:
        raise ValueError('explicit local VM evidence required')
    evidence = t['local_vm'];checked = t['reservation']['checked_at']
    p = validate_authority(evidence['authority'], key, owner_key, source=source, hashes=t['driver_hashes'], now=checked)
    validate_guest(p, evidence['guest'])
    if t['interpreter']['real_executable'] != evidence['guest']['real_executable']:
        raise ValueError('runtime interpreter is disconnected from actual guest')
    if evidence['owner_key_sha256'] != digest(owner_key) or evidence['key_sha256'] != digest(key) or t['run'] != {'mode': 'local-vm', 'run_id': p['run_id'], 'vm_uuid': p['vm']['uuid']}:
        raise ValueError('local source/run/VM trust identity differs')
    r = p['reservation']['payload']
    if t['reservation'] != {'id': r['id'], 'owner': r['owner'], 'checked_at': checked,
                           'expires': r['expires_at'], 'endpoint': ENDPOINT}:
        raise ValueError('local reservation does not bind signed owner hold')
    network = evidence['network'];raw = bytes.fromhex(network['body_hex'])
    if len(raw) > 2 * 1024 * 1024 or digest(raw) != network['sha256'] or network['endpoint'] != ENDPOINT or network['status'] != 200:
        raise ValueError('actual guest network evidence differs')
    if not checked <= network['checked_at'] <= checked + 10 or MODEL not in [m['id'] for m in json.loads(raw)['data']]:
        raise ValueError('guest reachability is absent/stale/wrong model')
    if checked + t['elapsed_seconds'] + 10 > p['expires_at']:
        raise ValueError('attempt did not fit the authorized local window')
    require_lifecycle_evidence(t, source)
    from .local_timing import validate_timing
    validate_timing(t,p)


async def run(output: Path, authority: Path, dsn: str):
    timer_started=time.monotonic();wall_started=time.time()
    # Every guard precedes output, app, DB, socket or real model effects.
    if sys.flags.optimize:
        raise ValueError('local execution requires active raw assertions')
    local_context(dict(os.environ))
    if os.geteuid() == 0:
        raise ValueError('guest probe must run as an unprivileged principal')
    head = probe.subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=probe.REPO, text=True, timeout=5).strip()
    if probe.subprocess.check_output(['git', 'status', '--porcelain=v1', '--untracked-files=normal'], cwd=probe.REPO):
        raise ValueError('local VM source checkout must be clean')
    key = guest_trust_key();owner_key = guest_trust_key(owner=True);packet = read_packet(authority);checked = time.time()
    p = validate_authority(packet, key, owner_key, source=head, hashes=source_hashes(probe.REPO), now=checked)
    guest = observe_guest();validate_guest(p, guest)
    postgres_config(dsn);probe.require_process_handles()
    expected = Path('/var/tmp/w4-f1-local-artifacts') / p['run_id']
    if output != expected or output.exists() or output.parent.is_symlink():
        raise ValueError('only the fresh run-specific guest artifact path is permitted')
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.get(ENDPOINT + '/models');response.raise_for_status()
    raw = response.content
    if len(raw) > 2 * 1024 * 1024 or MODEL not in [m['id'] for m in response.json()['data']]:
        raise ValueError('exact reviewed model unavailable inside guest')
    local = {'timeline':{'started_at':wall_started},'authority': packet, 'guest': guest, 'key_sha256': digest(key), 'owner_key_sha256': digest(owner_key),
             'network': {'endpoint': ENDPOINT, 'status': 200, 'body_hex': raw.hex(),
                         'sha256': digest(raw), 'checked_at': time.time()}}
    r = p['reservation']['payload']
    reservation = {'id': r['id'], 'owner': r['owner'], 'checked_at': checked,
                   'expires': r['expires_at'], 'endpoint': ENDPOINT}
    from .local_timing import LocalRelay
    await probe.execute_probe(output, head=head, reservation=reservation,
                              run_context={'mode': 'local-vm', 'run_id': p['run_id'], 'vm_uuid': p['vm']['uuid']},
                              temp_root=Path('/var/tmp'), dsn=dsn,
                              evidence_validator=lambda t, source: require_local_evidence(t, source, key, owner_key),
                              accepted_name='local-accepted.json', local_vm=local, timer_started=timer_started,relay_type=LocalRelay)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--authority', required=True, type=Path)
    # Keep credentials in a private environment, never in argv or raw evidence.
    args = parser.parse_args()
    asyncio.run(run(args.output, args.authority, os.environ['W4_F1_LOCAL_POSTGRES_DSN']))


if __name__ == '__main__':
    main()
