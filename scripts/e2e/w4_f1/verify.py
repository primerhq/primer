"""Require the actual named JUnit and source/digest-bound connected trace."""
import argparse
import json
import os
from pathlib import Path
from xml.etree import ElementTree as ET

from .contracts import CASE, digest, require_evidence, source_hashes
from .probe import REPO


RUN_CONTEXT = ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB', 'RUNNER_ENVIRONMENT', 'RUNNER_OS', 'RUNNER_NAME')


def verify(output: Path, source: str, junit: Path, *, expected_run: dict):
    raw = (output / 'trace.json').read_bytes()
    accepted = json.loads((output / 'accepted.json').read_text())
    if accepted != {'source_sha': source, 'trace_sha256': digest(raw)}:
        raise ValueError('acceptance receipt does not bind actual raw trace/source')
    trace = json.loads(raw)
    if set(expected_run) != set(RUN_CONTEXT) or trace['run'] != expected_run:
        raise ValueError('raw evidence belongs to another actual job/run/runner')
    if trace['driver_hashes'] != source_hashes(REPO):
        raise ValueError('probe source bytes changed')
    require_evidence(trace, source)
    for row in trace['cleanup']['native_evidence']:
        relative = Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('unsafe native evidence path')
        path = output / relative
        if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()):
            raise ValueError('native evidence escaped artifact root')
        data = path.read_bytes()
        if len(data) != row['bytes'] or digest(data) != row['sha256']:
            raise ValueError('copied native evidence digest mismatch')
    runtime = output / 'private-native/runtime.jsonl'
    if [json.loads(line) for line in runtime.read_text().splitlines()] != trace['runtime']:
        raise ValueError('copied runtime proof differs from claimed identities')
    slot = output / f"private-native/workspaces/{trace['workspace_id']}/.state/sessions/{trace['session_id']}"
    from primer.workspace.segments import ordered
    from .probe import normalized_records
    segments = ordered(p.name for p in slot.iterdir())
    if [index for index, _ in segments] != list(range(len(segments))):
        raise ValueError('native session segments have a gap')
    rows = [json.loads(line) for _, name in segments for line in (slot / name).read_bytes().splitlines() if line]
    if normalized_records(rows) != trace['disk_records']:
        raise ValueError('copied native transcript differs from claimed records')
    cases = [c for c in ET.parse(junit).iter('testcase') if c.get('name') == CASE and c.get('classname') == 'scripts.e2e.w4_f1.acceptance']
    if len(cases) != 1 or any(cases[0].find(k) is not None for k in ('skipped', 'failure', 'error')):
        raise ValueError('actual named lifecycle did not execute successfully exactly once')
    return {'source_sha': source, 'trace_sha256': digest(raw), 'junit_sha256': digest(junit.read_bytes())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--junit', type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.output, args.source, args.junit, expected_run={k: os.environ[k] for k in RUN_CONTEXT})
    (args.output / 'gate.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
