"""Read-only HOST post-cleanup receipt, after the actual local gate and owner release."""
import argparse
import json
from pathlib import Path
import subprocess
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import MODEL, digest
from .local_authority import ENDPOINT, canonical, finite, read_packet, source_state, verify_hold_files
from .probe import REPO
from .contracts import source_hashes
from .local_vm import require_local_evidence
from .local_verify import validate_host_gate, verify_retained, HOST_GATE_FIELDS


def verify(output: Path, source: str, *, owner_key: bytes, owner_key_sha256: str, host_key: bytes, host_key_sha256: str,
           release: dict, restoration_evidence: Path):
    if digest(owner_key) != owner_key_sha256 or digest(host_key) != host_key_sha256 or host_key == owner_key or source_state(REPO) != source:
        raise ValueError('trusted host source/model-owner identity differs')
    gate_raw = (output / 'local-gate.json').read_bytes();sealed = json.loads(gate_raw)
    if set(sealed) != {'payload', 'signature'}:
        raise ValueError('actual host-signed gate receipt required')
    gate = sealed['payload']
    if set(gate)!=HOST_GATE_FIELDS:raise ValueError('complete closed actual host-gate schema required')
    Ed25519PublicKey.from_public_bytes(host_key).verify(bytes.fromhex(sealed['signature']), canonical(gate))
    raw = (output / 'trace.json').read_bytes();trace = json.loads(raw)
    if gate['mode'] != 'local-vm' or gate['source_sha'] != source or gate['trace_sha256'] != digest(raw) or gate['owner_key_sha256'] != owner_key_sha256 or gate['host_key_sha256'] != host_key_sha256:
        raise ValueError('post-cleanup receipt must follow the exact actual host gate')
    if trace['driver_hashes']!=source_hashes(REPO):raise ValueError('retained driver source changed')
    require_local_evidence(trace,source,host_key,owner_key)
    validate_host_gate(gate,trace,source,digest(raw),host_key_sha256,owner_key_sha256)
    verify_retained(output,trace,gate)
    if set(release) != {'payload', 'signature'}:
        raise ValueError('actual signed model-owner release required')
    p = release['payload']
    Ed25519PublicKey.from_public_bytes(owner_key).verify(bytes.fromhex(release['signature']), canonical(p))
    if set(p) != {'mode', 'run_id', 'vm_uuid', 'source_sha', 'reservation_id', 'endpoint', 'model', 'released_at', 'restoration_reference', 'evidence_name', 'evidence_sha256'}:
        raise ValueError('unknown/missing actual release/restoration fields')
    authority = trace['local_vm']['authority']['payload'];vm = authority['vm']
    if (p['mode'] != 'local-vm-release' or p['run_id'] != trace['run']['run_id'] or p['vm_uuid'] != vm['uuid']
            or p['source_sha'] != source or p['reservation_id'] != trace['reservation']['id']
            or p['endpoint'] != ENDPOINT or p['model'] != MODEL or not p['restoration_reference']):
        raise ValueError('release is disconnected from this actual run/VM/model')
    if not finite(gate['verified_at']) <= finite(p['released_at']) <= min(time.time(),trace['local_vm']['authority']['payload']['expires_at']):
        raise ValueError('release must follow the successful actual host gate')
    domains = subprocess.check_output(['virsh', '--connect', 'qemu:///system', 'list', '--all', '--uuid'], text=True, timeout=5).split()
    root = Path(vm['owned_host_root'])
    if vm['uuid'] in domains or root.exists() or root.is_symlink():
        raise ValueError('exact owned VM/private host root still present')
    verify_hold_files({'acknowledgements': [{'consumer': 'owner-restoration', 'evidence_name': p['evidence_name'],
                                           'evidence_sha256': p['evidence_sha256']}]}, restoration_evidence)
    return {'mode': 'local-vm-cleanup', 'source_sha': source, 'run_id': p['run_id'], 'vm_uuid': p['vm_uuid'],
            'actual_host_gate_sha256': digest(gate_raw), 'owner_release_sha256': digest(canonical(release)),
            'owned_domain_and_host_root_absent': True, 'restoration_evidence_sha256': p['evidence_sha256']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source', required=True)
    parser.add_argument('--owner-public-key', required=True, type=Path)
    parser.add_argument('--owner-key-sha256', required=True)
    parser.add_argument('--host-public-key', required=True, type=Path)
    parser.add_argument('--host-key-sha256', required=True)
    parser.add_argument('--owner-release', required=True, type=Path)
    parser.add_argument('--restoration-evidence', required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.output, args.source, owner_key=args.owner_public_key.read_bytes(), owner_key_sha256=args.owner_key_sha256,
                    host_key=args.host_public_key.read_bytes(), host_key_sha256=args.host_key_sha256,
                    release=read_packet(args.owner_release), restoration_evidence=args.restoration_evidence)
    with (args.output / 'local-cleanup.json').open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
