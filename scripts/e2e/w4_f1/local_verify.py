"""Trusted HOST final gate. A key or VM observation supplied by a guest is never trusted."""
from pathlib import Path
import argparse
import json
import time
from xml.etree import ElementTree as ET

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .local_authority import canonical, read_host_signing_key, read_packet, finite
from .contracts import digest, source_hashes
from .local_authority import host_vm_observation, source_state, verify_hold_files
from .local_vm import require_local_evidence
from .probe import REPO
from .verify import verify_native, verify_named_junit


def verify(output: Path, source: str, junit: Path, *, host_key: bytes, host_key_sha256: str, owner_key: bytes, owner_key_sha256: str, hold_evidence: Path):
    if digest(host_key) != host_key_sha256 or digest(owner_key) != owner_key_sha256 or source_state(REPO) != source:
        raise ValueError('host-side pinned trust key or exact clean reviewed source differs')
    raw = (output / 'trace.json').read_bytes()
    if json.loads((output / 'local-accepted.json').read_text()) != {'source_sha': source, 'trace_sha256': digest(raw)}:
        raise ValueError('local receipt does not bind actual raw evidence/source')
    t = json.loads(raw)
    if t['driver_hashes'] != source_hashes(REPO):
        raise ValueError('actual local driver source bytes changed')
    require_local_evidence(t, source, host_key, owner_key)
    original_preboot=(output/'owner-preboot.json').read_bytes()
    if digest(original_preboot)!=t['local_vm']['authority']['payload']['preboot_sha256'] or read_packet(output/'owner-preboot.json')!=t['local_vm']['authority']['payload']['preboot']:
        raise ValueError('original host preflight receipt missing/substituted')
    if junit!=output/'local-junit.xml' or junit.is_symlink():
        raise ValueError('retain the actual local JUnit at its fixed artifact path')
    # This command executes on the trusted host, outside the guest and its claims.
    holds = verify_hold_files(t['local_vm']['authority']['payload']['reservation']['payload'], hold_evidence)
    actual_vm = host_vm_observation(t['local_vm']['authority']['payload']['vm'])
    verify_native(output, t)
    if len(list(ET.parse(junit).iter('testcase'))) != 1:
        raise ValueError('local gate requires only the single executed bounded case')
    verify_named_junit(junit, 'scripts.e2e.w4_f1.local_acceptance')
    result={'mode':'local-vm','run_id':t['run']['run_id'],'vm_uuid':t['run']['vm_uuid'],
            'reservation_id':t['reservation']['id'],'verified_at':time.time(),'source_sha':source,
            'trace_sha256':digest(raw),'junit_sha256':digest(junit.read_bytes()),'junit_file':'local-junit.xml',
            'driver_hashes':t['driver_hashes'],'host_key_sha256':host_key_sha256,'owner_key_sha256':owner_key_sha256,
            'actual_host_vm':actual_vm,'host_verified_original_holds':holds,'native_evidence':t['cleanup']['native_evidence'],
            'preboot_sha256':digest(original_preboot)}
    validate_host_gate(result,t,source,digest(raw),host_key_sha256,owner_key_sha256)
    return result


HOST_GATE_FIELDS={'mode','run_id','vm_uuid','reservation_id','verified_at','source_sha','trace_sha256','junit_sha256',
                  'junit_file','driver_hashes','host_key_sha256','owner_key_sha256','actual_host_vm',
                  'host_verified_original_holds','native_evidence','preboot_sha256'}
HOST_VM_FIELDS={'uuid','xml_sha256','image_sha256','seed_sha256','disk_cap_bytes','state','host_process','host_paths','observed_at'}


def validate_host_gate(g: dict,t: dict,source: str,trace_hash: str,host_fingerprint: str,owner_fingerprint: str):
    if set(g)!=HOST_GATE_FIELDS:raise ValueError('complete closed actual host-gate schema required')
    p=t['local_vm']['authority']['payload'];vm=p['vm'];r=p['reservation']['payload']
    expected={'mode':'local-vm','source_sha':source,'trace_sha256':trace_hash,'run_id':t['run']['run_id'],
              'vm_uuid':vm['uuid'],'reservation_id':r['id'],'driver_hashes':t['driver_hashes'],
              'host_key_sha256':host_fingerprint,'owner_key_sha256':owner_fingerprint,'junit_file':'local-junit.xml',
              'native_evidence':t['cleanup']['native_evidence'],'preboot_sha256':p['preboot_sha256']}
    if any(g[k]!=v for k,v in expected.items()):raise ValueError('host gate is disconnected from this exact source/run/VM/evidence')
    verified=finite(g['verified_at'])
    if not t['local_vm']['timeline']['finished_at']-2<=verified<=p['expires_at']:raise ValueError('actual host gate outside signed phase chronology')
    a=g['actual_host_vm']
    if set(a)!=HOST_VM_FIELDS:raise ValueError('complete closed actual host VM observation required')
    expected_vm={'uuid':vm['uuid'],'xml_sha256':vm['xml_sha256'],'image_sha256':vm['image_sha256'],
                 'seed_sha256':None if vm['seed'] is None else vm['seed']['sha256'],'disk_cap_bytes':vm['disk_cap_bytes'],
                 'state':'running','host_process':vm['host_process']}
    if any(a[k]!=v for k,v in expected_vm.items()):raise ValueError('actual host VM identity differs from signed execution')
    if not t['local_vm']['timeline']['finished_at']-2<=finite(a['observed_at'])<=verified+2:raise ValueError('host VM observation clock disconnected')
    paths=[vm['disk_path'],vm['image_path']]+([] if vm['seed'] is None else [vm['seed']['path']])
    if len(a['host_paths'])!=len(paths):raise ValueError('actual host path identities incomplete')
    for row,path in zip(a['host_paths'],paths,strict=True):
        if set(row)!={'path','uid','gid','mode','device','inode'} or row['path']!=path or any(isinstance(row[k],bool) or not isinstance(row[k],int) or row[k]<0 for k in ('uid','gid','mode','device','inode')) or row['mode']&0o022:
            raise ValueError('actual host path identity/permissions inconsistent')
    expected_holds=[{'consumer':a['consumer'],'name':a['evidence_name'],'sha256':a['evidence_sha256']} for a in r['acknowledgements']]
    if g['host_verified_original_holds']!=expected_holds:raise ValueError('host verified holds are incomplete/swapped')


def verify_retained(output: Path,t: dict,g: dict):
    junit=output/g['junit_file']
    if junit.is_symlink() or digest(junit.read_bytes())!=g['junit_sha256']:raise ValueError('retained actual JUnit digest differs')
    if len(list(ET.parse(junit).iter('testcase')))!=1:raise ValueError('retained JUnit is not the single actual local case')
    verify_named_junit(junit,'scripts.e2e.w4_f1.local_acceptance');verify_native(output,t)
    preboot=output/'owner-preboot.json'
    if digest(preboot.read_bytes())!=g['preboot_sha256'] or read_packet(preboot)!=t['local_vm']['authority']['payload']['preboot']:
        raise ValueError('retained original pre-boot evidence differs')


def seal_gate(result: dict, signing_key: bytes, expected_public_key: bytes) -> dict:
    key = Ed25519PrivateKey.from_private_bytes(signing_key)
    if key.public_key().public_bytes_raw() != expected_public_key:
        raise ValueError('host gate signing key differs from the pinned execution authority')
    return {'payload': result, 'signature': key.sign(canonical(result)).hex()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source', required=True)
    parser.add_argument('--junit', required=True, type=Path)
    parser.add_argument('--host-public-key', required=True, type=Path)
    parser.add_argument('--host-key-sha256', required=True)
    parser.add_argument('--host-signing-key', required=True, type=Path)
    parser.add_argument('--owner-public-key', required=True, type=Path)
    parser.add_argument('--owner-key-sha256', required=True)
    parser.add_argument('--hold-evidence', required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.output, args.source, args.junit, host_key=args.host_public_key.read_bytes(),
                    host_key_sha256=args.host_key_sha256, owner_key=args.owner_public_key.read_bytes(),
                    owner_key_sha256=args.owner_key_sha256, hold_evidence=args.hold_evidence)
    sealed = seal_gate(result, read_host_signing_key(args.host_signing_key), args.host_public_key.read_bytes())
    with (args.output / 'local-gate.json').open('x') as stream:
        stream.write(json.dumps(sealed, indent=2) + '\n')


if __name__ == '__main__':
    main()
