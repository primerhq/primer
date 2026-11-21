"""Offline filesystem/crypto/socket proofs, NOT cross-user provisioning evidence."""
import base64
import hashlib
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from primer.llm.codex.boundary import BoundaryError
from primer.llm.codex.broker import host
from primer.llm.codex.broker.security import PrivateStore, SignedPolicy
from primer.llm.codex.request import compact


@pytest.fixture(autouse=True)
def independent_interpreter_fence(monkeypatch):
    # Each test models its own interpreter; recompositions within a test share
    # the real process-wide set. Temporary inode recycling must not couple cases.
    monkeypatch.setattr(host, '_UNCERTAIN_STORES', set())


@pytest.fixture
def ledger(tmp_path):
    tmp_path.chmod(0o700)
    store = PrivateStore(tmp_path)
    key = Ed25519PrivateKey.generate()
    keys = {'admin': key.public_key().public_bytes_raw()}
    doc = {'audience': 'test', 'revision': '1', 'expires_at': time.time() + 3600,
           'accounts': ['account'], 'models': ['model'], 'required_native_effects': [],
           'suspended': False, 'enrollment': True}
    def sign():
        raw = compact(doc).encode()
        return {'admin': {'payload': raw.decode(), 'signature': base64.b64encode(key.sign(raw)).decode()}}
    bundle = sign()
    state = {'identity': 'host', 'conflicts': [], 'revisions': {'admin': {'revision': '1',
             'sha256': hashlib.sha256(bundle['admin']['payload'].encode()).hexdigest()}}}
    store.write('policy_ledger', state)
    store.write('policy_session', {'identity': 'host', 'state': 'ready'})
    def reopen():
        return host.PolicyLedger(store, identity='host', keys=keys, audience='test',
                                 floors={'admin': '1'}, load=lambda: bundle)
    subject = reopen()
    policy = SignedPolicy(keys, 'test', subject)
    yield SimpleNamespace(store=store, doc=doc, bundle=bundle, sign=sign, subject=subject,
                          policy=policy, reopen=reopen)
    store.close()


def advance(world, **changes):
    world.doc.update(changes)
    world.bundle.update(world.sign())


def test_verified_revision_survives_reconstruction(ledger):
    assert ledger.policy.grant('account')[2] == {'model'}
    old = dict(ledger.bundle)
    advance(ledger, revision='2')
    ledger.policy.grant('account')
    ledger.bundle.update(old)
    with pytest.raises(BoundaryError):
        ledger.reopen()()


def test_denial_is_recorded_before_grant_and_cannot_be_rolled_back(ledger):
    old = dict(ledger.bundle)
    advance(ledger, revision='2', suspended=True)
    with pytest.raises(BoundaryError):
        ledger.policy.grant('account')
    assert ledger.store.read('policy_ledger')['revisions']['admin']['revision'] == '2'
    ledger.bundle.update(old)
    with pytest.raises(BoundaryError):
        ledger.policy.grant('account')


def test_same_revision_different_payload_refused(ledger):
    advance(ledger, models=['other'])
    with pytest.raises(BoundaryError):
        ledger.policy.grant('account')


@pytest.mark.parametrize('kind', ['bad_signature', 'wrong_audience', 'extra_authority', 'bad_revision'])
def test_invalid_bundle_cannot_poison_high_water(ledger, kind):
    before = ledger.store.read('policy_ledger')
    advance(ledger, revision='2')
    if kind == 'bad_signature':
        ledger.bundle['admin']['signature'] = base64.b64encode(b'x' * 64).decode()
    elif kind == 'wrong_audience':
        advance(ledger, audience='other')
    elif kind == 'extra_authority':
        ledger.bundle['extra'] = ledger.bundle['admin']
    else:
        advance(ledger, revision='02')
    with pytest.raises(Exception):
        ledger.subject()
    assert ledger.store.read('policy_ledger') == before


@pytest.mark.parametrize('after', [False, True])
def test_uncertain_persistence_fences_even_newer_grant(ledger, monkeypatch, after):
    original = ledger.store.write
    def fail(name, value):
        if after:
            original(name, value)
        raise OSError('synthetic fsync uncertainty')
    monkeypatch.setattr(ledger.store, 'write', fail)
    advance(ledger, revision='2')
    with pytest.raises(BoundaryError):
        ledger.policy.grant('account')
    monkeypatch.setattr(ledger.store, 'write', original)
    advance(ledger, revision='3')
    with pytest.raises(BoundaryError):
        ledger.policy.grant('account')


def test_missing_ledger_never_bootstraps(ledger):
    os.unlink('policy_ledger.json', dir_fd=ledger.store._fd)
    with pytest.raises(BoundaryError):
        ledger.reopen()


def test_manifest_identity_change_requires_explicit_reprovisioning(ledger):
    state = ledger.store.read('policy_ledger')
    state['identity'] = 'other host'
    ledger.store.write('policy_ledger', state)
    with pytest.raises(BoundaryError):
        ledger.reopen()


@pytest.mark.parametrize('hazard', ['symlink', 'writable', 'wrong_owner', 'safe'])
def test_descriptor_walk_checks_every_component(tmp_path, monkeypatch, hazard):
    # Simulate root ownership only. Real directory descriptors, symlinks, modes
    # and relative openat traversal exercise the production implementation.
    root = tmp_path / 'root'
    root.mkdir(mode=0o755)
    child = root / 'child'
    child.mkdir(mode=0o755)
    (child / 'leaf').mkdir(mode=0o755)
    if hazard == 'symlink':
        (root / 'alias').symlink_to(child, target_is_directory=True)
    if hazard == 'writable':
        child.chmod(0o777)
    original_open, original_stat = os.open, os.fstat
    def open_(path, flags, *args, **kwargs):
        return original_open(root if path == '/' else path, flags, *args, **kwargs)
    inode = child.stat().st_ino
    def fstat(fd):
        info = original_stat(fd)
        fields = list(info)
        fields[4] = 123 if hazard == 'wrong_owner' and info.st_ino == inode else 0
        return os.stat_result(fields)
    monkeypatch.setattr(host.os, 'open', open_)
    monkeypatch.setattr(host.os, 'fstat', fstat)
    path = Path('/alias/leaf' if hazard == 'symlink' else '/child/leaf')
    if hazard == 'safe':
        fd = host._root_directory(path)
        os.close(fd)
    else:
        with pytest.raises((BoundaryError, OSError)):
            host._root_directory(path)


@pytest.mark.parametrize('hazard', ['symlink', 'hardlink', 'writable', 'fifo', 'oversize'])
def test_root_record_refuses_unsafe_objects(tmp_path, monkeypatch, hazard):
    path = tmp_path / 'host.json'
    path.write_text('{}')
    if hazard == 'symlink':
        path.rename(tmp_path / 'target')
        path.symlink_to(tmp_path / 'target')
    elif hazard == 'hardlink':
        os.link(path, tmp_path / 'link')
    elif hazard == 'writable':
        path.chmod(0o666)
    elif hazard == 'fifo':
        path.unlink()
        os.mkfifo(path)
    else:
        path.write_bytes(b'x' * (host.MAX_RECORD + 1))
    original = os.fstat
    def fstat(fd):
        i = original(fd)
        fields = list(i)
        fields[4] = 0
        return os.stat_result(fields)
    monkeypatch.setattr(host.os, 'fstat', fstat)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises((BoundaryError, OSError)):
            host._root_json(fd, 'host.json')
    finally:
        os.close(fd)


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    uid = os.geteuid()
    doc = {'audience': 'test', 'broker_uid': uid, 'primer_uid': uid + 1,
           'account': 'a' * 64, 'store': str(tmp_path / 'private'),
           'socket': str(tmp_path / 'model.sock'),
           'authorities': {'admin': {'key': base64.b64encode(key.public_key().public_bytes_raw()).decode(),
                                     'minimum_revision': '1'}}}
    # Trusted-owner fixture for composition only; provenance walked separately.
    monkeypatch.setattr(host, '_root_directory', lambda p: os.open(p, os.O_RDONLY | os.O_DIRECTORY))
    monkeypatch.setattr(host, '_root_json', lambda fd, name: doc.copy())
    return doc


def test_distinct_principals_required_before_store_construction(manifest, tmp_path):
    manifest['primer_uid'] = manifest['broker_uid']
    with pytest.raises(BoundaryError):
        host.HostTrust(tmp_path)
    assert not (tmp_path / 'private').exists()


def test_wrong_process_identity_cannot_open_credentials(manifest, tmp_path, monkeypatch):
    trust = host.HostTrust(tmp_path)
    monkeypatch.setattr(host.os, 'geteuid', lambda: manifest['primer_uid'])
    try:
        with pytest.raises(BoundaryError):
            trust.open_policy_store()
        assert not (tmp_path / 'private').exists()
    finally:
        trust.close()


@pytest.mark.parametrize('wrong_peer', [False, True])
def test_real_socket_peer_checked_before_any_request(manifest, tmp_path, monkeypatch, wrong_peer):
    actual_uid = os.geteuid()
    if wrong_peer:
        manifest['broker_uid'] = actual_uid + 2
    trust = host.HostTrust(tmp_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(manifest['socket'])
    os.chmod(manifest['socket'], 0o660)
    listener.listen(1)
    original_stat = os.stat
    def stat_(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if str(path) == 'model.sock':
            fields = list(info)
            fields[4] = manifest['broker_uid']
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(host.os, 'stat', stat_)
    monkeypatch.setattr(host.os, 'geteuid', lambda: manifest['primer_uid'])
    client = None
    try:
        if wrong_peer:
            with pytest.raises(BoundaryError):
                trust.connect_model_socket()
        else:
            client = trust.connect_model_socket()
            assert not client.getblocking()
        accepted, _ = listener.accept()
        with accepted:
            accepted.settimeout(0.05)
            if wrong_peer:
                assert accepted.recv(1) == b''
            else:
                with pytest.raises(TimeoutError):
                    accepted.recv(1)
    finally:
        if client:
            client.close()
        listener.close()
        trust.close()


@pytest.mark.asyncio
async def test_host_composition_refuses_before_http_or_auth(manifest, tmp_path, monkeypatch):
    from primer.llm.codex.broker import http
    constructed = []
    monkeypatch.setattr(http, 'FixedHTTP', lambda: constructed.append(True))
    monkeypatch.setattr(host.os, 'geteuid', lambda: manifest['primer_uid'])
    with pytest.raises(BoundaryError):
        async with host.host_broker(tmp_path):
            pytest.fail('untrusted process admitted')
    assert constructed == []


@pytest.mark.asyncio
async def test_host_composition_wires_live_policy_and_releases_store(manifest, tmp_path, monkeypatch):
    # Actual store, signature validation, SignIn, FixedHTTP and BrokerServer.
    # Only root-owned provisioning is simulated; no HTTP request is made.
    key = Ed25519PrivateKey.generate()
    manifest['authorities']['admin']['key'] = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    document = {'audience': 'test', 'revision': '1', 'expires_at': time.time() + 3600,
                'accounts': [manifest['account']], 'models': ['model'], 'required_native_effects': [],
                'suspended': False, 'enrollment': False}
    def sign():
        raw = compact(document).encode()
        return {'admin': {'payload': raw.decode(), 'signature': base64.b64encode(key.sign(raw)).decode()}}
    bundle = sign()
    monkeypatch.setattr(host, '_root_json', lambda fd, name: manifest.copy() if name == 'host.json' else bundle)
    directory = Path(manifest['store'])
    directory.mkdir(mode=0o700)
    store = PrivateStore(directory)
    store.write('policy_ledger', {'identity': host.manifest_identity(manifest), 'conflicts': [],
                'revisions': {'admin': {'revision': '1', 'sha256': hashlib.sha256(bundle['admin']['payload'].encode()).hexdigest()}}})
    store.write('policy_session', {'identity': host.manifest_identity(manifest), 'state': 'ready'})
    store.close()
    async with host.host_broker(tmp_path) as server:
        assert server._uid == manifest['primer_uid']
        assert server._account == manifest['account']
        policy = server._broker._auth._policy
        assert policy.grant(manifest['account'])[2] == {'model'}
        document.update(revision='2', suspended=True)
        bundle.update(sign())
        with pytest.raises(BoundaryError):
            policy.grant(manifest['account'])
    reopened = PrivateStore(directory)
    try:
        assert reopened.read('policy_ledger')['revisions']['admin']['revision'] == '2'
    finally:
        reopened.close()


def test_duplicate_authority_key_refused_before_store(manifest, tmp_path):
    manifest['authorities']['second'] = dict(manifest['authorities']['admin'])
    with pytest.raises(BoundaryError):
        host.HostTrust(tmp_path)
    assert not Path(manifest['store']).exists()


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('tamper', [False, True])
def test_mixed_revision_retains_new_denial_after_full_authentication(tmp_path, reverse, tamper):
    tmp_path.chmod(0o700)
    store = PrivateStore(tmp_path)
    signers = {name: Ed25519PrivateKey.generate() for name in ('a', 'b')}
    keys = {name: signers[name].public_key().public_bytes_raw() for name in (('b', 'a') if reverse else ('a', 'b'))}
    def envelope(name, revision, *, suspended=False, models=None):
        doc = {'audience': 'test', 'revision': revision, 'expires_at': expiry,
               'accounts': ['account'], 'models': models or ['common', name],
               'required_native_effects': [], 'suspended': suspended, 'enrollment': False}
        raw = compact(doc).encode()
        return {'payload': raw.decode(), 'signature': base64.b64encode(signers[name].sign(raw)).decode()}
    expiry = time.time() + 3600
    initial = {name: envelope(name, '2') for name in keys}
    state = {'identity': 'host', 'conflicts': [], 'revisions': {name: {
        'revision': '2', 'sha256': hashlib.sha256(item['payload'].encode()).hexdigest()}
        for name, item in initial.items()}}
    store.write('policy_ledger', state)
    store.write('policy_session', {'identity': 'host', 'state': 'ready'})
    bundle = dict(initial)
    def ledger_():
        return host.PolicyLedger(store, identity='host', keys=keys, audience='test',
                                 floors={name: '1' for name in keys}, load=lambda: bundle)
    try:
        subject = ledger_()
        policy = SignedPolicy(keys, 'test', subject)
        assert policy.grant('account')[2] == {'common'}
        # A second, already constructed handle must also observe durable updates.
        other = SignedPolicy(keys, 'test', ledger_())
        bundle.update(a=envelope('a', '1'), b=envelope('b', '3', suspended=True))
        if tamper:
            bundle['a']['signature'] = base64.b64encode(b'x' * 64).decode()
        with pytest.raises(BoundaryError):
            policy.grant('account')
        persisted = store.read('policy_ledger')
        assert persisted['revisions']['a'] == state['revisions']['a']
        assert persisted['revisions']['b']['revision'] == ('2' if tamper else '3')
        bundle.update(initial)
        reconstructed = SignedPolicy(keys, 'test', ledger_())
        for retained in (policy, other, reconstructed):
            if tamper:
                assert retained.grant('account')[2] == {'common'}
            else:
                with pytest.raises(BoundaryError):
                    retained.grant('account')
    finally:
        store.close()


def test_equivocation_is_durable_refusal(ledger):
    old = dict(ledger.bundle)
    advance(ledger, models=['other'])
    with pytest.raises(BoundaryError):
        ledger.policy.grant('account')
    assert ledger.store.read('policy_ledger')['conflicts'] == ['admin']
    ledger.bundle.update(old)
    with pytest.raises(BoundaryError):
        ledger.reopen()


def test_manifest_reorder_has_same_live_and_restart_identity(manifest, tmp_path):
    first = host.HostTrust(tmp_path)
    expected = first.identity
    reordered = dict(reversed(list(manifest.items())))
    manifest.clear()
    manifest.update(reordered)
    try:
        first._manifest_current()
        second = host.HostTrust(tmp_path)
        try:
            assert second.identity == expected
        finally:
            second.close()
        # Dictionary equality would accept 1000.0 == 1000. Strict reload must not.
        manifest['broker_uid'] = float(manifest['broker_uid'])
        with pytest.raises(BoundaryError):
            first._manifest_current()
        with pytest.raises(BoundaryError):
            host.HostTrust(tmp_path)
    finally:
        first.close()


def test_closed_handles_refuse_before_io(manifest, tmp_path, ledger, monkeypatch):
    trust = host.HostTrust(tmp_path)
    trust.close()
    ledger.store.close()
    def forbidden(*args, **kwargs):
        pytest.fail('closed handle attempted filesystem/socket IO')
    # Restore process-global functions before pytest capture/reporting runs.
    with monkeypatch.context() as patch:
        patch.setattr(host, '_root_json', forbidden)
        patch.setattr(host, '_root_directory', forbidden)
        patch.setattr(os, 'open', forbidden)
        patch.setattr(os, 'fstat', forbidden)
        patch.setattr(socket, 'socket', forbidden)
        for operation in (trust._policy_bundle, trust.open_policy_store, trust.connect_model_socket,
                          lambda: trust.account, lambda: ledger.policy.grant('account'),
                          lambda: ledger.store.read('registration'),
                          lambda: ledger.store.write('registration', {})):
            with pytest.raises(BoundaryError):
                operation()


def test_private_fifo_refusal_in_bounded_child(tmp_path):
    import subprocess
    import sys
    tmp_path.chmod(0o700)
    os.mkfifo(tmp_path / 'policy_ledger.json', mode=0o600)
    script = '''
import sys
from pathlib import Path
from primer.llm.codex.broker.security import PrivateStore
from primer.llm.codex.boundary import BoundaryError
store = PrivateStore(Path(sys.argv[1]))
try:
    try:
        store.read('policy_ledger')
    except BoundaryError:
        pass
    else:
        raise AssertionError('FIFO accepted')
finally:
    store.close()
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], timeout=3,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def composed(manifest, tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    manifest['authorities']['admin']['key'] = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    doc = {'audience': 'test', 'revision': '1', 'expires_at': time.time() + 3600,
           'accounts': [manifest['account']], 'models': ['model'], 'required_native_effects': [],
           'suspended': False, 'enrollment': False}
    def sign():
        raw = compact(doc).encode()
        return {'admin': {'payload': raw.decode(), 'signature': base64.b64encode(key.sign(raw)).decode()}}
    bundle = sign()
    monkeypatch.setattr(host, '_root_json', lambda fd, name: manifest.copy() if name == 'host.json' else bundle)
    directory = Path(manifest['store'])
    directory.mkdir(mode=0o700)
    identity = host.manifest_identity(manifest)
    store = PrivateStore(directory)
    store.write('policy_ledger', {'identity': identity, 'conflicts': [], 'revisions': {'admin': {
        'revision': '1', 'sha256': hashlib.sha256(bundle['admin']['payload'].encode()).hexdigest()}}})
    store.write('policy_session', {'identity': identity, 'state': 'ready'})
    store.close()
    return SimpleNamespace(manifest=manifest, directory=directory, identity=identity,
                           doc=doc, bundle=bundle, sign=sign, root=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['initial', 'during_use'])
@pytest.mark.parametrize('where', ['file', 'directory'])
async def test_composed_fsync_uncertainty_survives_close_reopen(composed, monkeypatch, phase, where):
    import stat
    from primer.llm.codex.broker import http
    world = composed
    original_write, original_sync = PrivateStore.write, os.fsync
    calls = []
    def faulty_write(store, name, value):
        if name != 'policy_ledger':
            return original_write(store, name, value)
        def sync(fd):
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            if directory == (where == 'directory'):
                calls.append(where)
                raise OSError('synthetic fsync failure')
            original_sync(fd)
        with monkeypatch.context() as patch:
            patch.setattr(os, 'fsync', sync)
            return original_write(store, name, value)
    monkeypatch.setattr(PrivateStore, 'write', faulty_write)
    if phase == 'initial':
        advance(world, revision='2')
        with pytest.raises(BoundaryError):
            async with host.host_broker(world.root):
                pytest.fail('initial uncertainty authorized construction')
    else:
        async with host.host_broker(world.root) as server:
            policy = server._broker._auth._policy
            advance(world, revision='2')
            with pytest.raises(BoundaryError):
                policy.grant(world.manifest['account'])
            with pytest.raises(BoundaryError):
                policy.grant(world.manifest['account'])
    assert calls == [where]
    monkeypatch.setattr(PrivateStore, 'write', original_write)
    # Even restoring a ready marker cannot clear this interpreter's uncertainty.
    store = PrivateStore(world.directory)
    try:
        store.write('policy_session', {'identity': world.identity, 'state': 'ready'})
    finally:
        store.close()
    def forbidden():
        pytest.fail('recomposed uncertain store constructed HTTP')
    monkeypatch.setattr(http, 'FixedHTTP', forbidden)
    with pytest.raises(BoundaryError):
        async with host.host_broker(world.root):
            pytest.fail('recomposition cleared uncertainty')


@pytest.mark.asyncio
async def test_closed_host_requires_explicit_recovery_even_in_fresh_process(composed):
    import subprocess
    import sys
    world = composed
    async with host.host_broker(world.root):
        pass
    with pytest.raises(BoundaryError):
        async with host.host_broker(world.root):
            pytest.fail('closed session silently recovered')
    script = """
import sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from primer.llm.codex.broker.host import PolicyLedger
from primer.llm.codex.broker.security import PrivateStore
from primer.llm.codex.boundary import BoundaryError
store = PrivateStore(Path(sys.argv[1]))
key = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
try:
    assert store.read('policy_session')['state'] == 'active'
    try:
        PolicyLedger(store, identity=sys.argv[2], keys={'admin': key}, audience='test',
                     floors={'admin': '1'}, load=lambda: (_ for _ in ()).throw(AssertionError('policy observed')))
    except BoundaryError:
        pass
    else:
        raise AssertionError('active session admitted in new process')
finally:
    store.close()
"""
    result = subprocess.run([sys.executable, '-c', script, str(world.directory), world.identity],
                            timeout=3, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_initial_session_marker_failure_prevents_observation_and_recomposition(composed, monkeypatch):
    world = composed
    original = PrivateStore.write
    observed = []
    def failed_marker(store, name, value):
        if name == 'policy_session':
            raise OSError('marker never durably written')
        return original(store, name, value)
    monkeypatch.setattr(PrivateStore, 'write', failed_marker)
    monkeypatch.setattr(host.HostTrust, '_policy_bundle', lambda self: observed.append(True))
    with pytest.raises(OSError):
        async with host.host_broker(world.root):
            pytest.fail('failed marker admitted')
    monkeypatch.setattr(PrivateStore, 'write', original)
    with pytest.raises(BoundaryError):
        async with host.host_broker(world.root):
            pytest.fail('same-process marker retry admitted')
    assert observed == []


def test_distinct_manifest_keys_are_admitted(manifest, tmp_path):
    second = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    manifest['authorities']['second'] = {'key': base64.b64encode(second).decode(), 'minimum_revision': '1'}
    trust = host.HostTrust(tmp_path)
    try:
        assert set(trust._keys) == {'admin', 'second'}
    finally:
        trust.close()
