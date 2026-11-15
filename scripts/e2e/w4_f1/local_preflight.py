"""Pure HOST owner-reservation gate, required before any VM boot or model request."""
import argparse
import json
from pathlib import Path
import time

from .contracts import digest, source_hashes
from .local_authority import canonical, finite, read_packet, source_state, validate_owner_reservation, verify_hold_files, read_host_signing_key
from .probe import REPO


def verify(packet: dict, source: str, *, owner_key: bytes, owner_key_sha256: str,
           host_key: bytes, host_key_sha256: str, run_id: str, vm_uuid: str, starts_at: float, expires_at: float, hold_evidence: Path):
    now = time.time()
    if digest(owner_key) != owner_key_sha256 or digest(host_key) != host_key_sha256 or host_key==owner_key or source_state(REPO) != source:
        raise ValueError('actual host source/pinned owner identity differs')
    if not finite(starts_at) <= now <= starts_at + 300 or not now + 900 <= finite(expires_at) <= starts_at + 1800:
        raise ValueError('fresh bounded real reservation is required before VM boot')
    r = validate_owner_reservation(packet, owner_key, source=source, run_id=run_id, vm_uuid=vm_uuid,
                                   start=starts_at, end=expires_at)
    originals = verify_hold_files(r, hold_evidence)
    return {'mode': 'local-vm-owner-preflight', 'source_sha': source, 'driver_hashes':source_hashes(REPO),'host_key_sha256':host_key_sha256,'run_id': run_id, 'vm_uuid': vm_uuid,
            'checked_at': now, 'starts_at': starts_at, 'expires_at': expires_at,
            'owner_key_sha256': owner_key_sha256, 'signed_reservation_sha256': digest(canonical(packet)),
            'original_host_holds': originals, 'does_not_establish_or_infer_a_hold': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reservation', type=Path, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--vm-uuid', required=True)
    parser.add_argument('--starts-at', type=float, required=True)
    parser.add_argument('--expires-at', type=float, required=True)
    parser.add_argument('--owner-public-key', type=Path, required=True)
    parser.add_argument('--owner-key-sha256', required=True)
    parser.add_argument('--host-public-key',type=Path,required=True)
    parser.add_argument('--host-key-sha256',required=True)
    parser.add_argument('--host-signing-key',type=Path,required=True)
    parser.add_argument('--hold-evidence', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    result = verify(read_packet(args.reservation), args.source, owner_key=args.owner_public_key.read_bytes(),
                    owner_key_sha256=args.owner_key_sha256,host_key=args.host_public_key.read_bytes(),host_key_sha256=args.host_key_sha256,run_id=args.run_id, vm_uuid=args.vm_uuid,
                    starts_at=args.starts_at, expires_at=args.expires_at, hold_evidence=args.hold_evidence)
    from .local_verify import seal_gate
    sealed=seal_gate(result,read_host_signing_key(args.host_signing_key),args.host_public_key.read_bytes())
    with args.receipt.open('xb') as stream:
        stream.write(canonical(sealed)+b'\n')


if __name__ == '__main__':
    main()
