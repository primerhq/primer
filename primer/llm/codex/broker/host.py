"""Explicit Linux host trust composition, not a launcher or policy discovery.

Root provisions the manifest and authority inventory. No environment lookup,
credential import, identity creation, endpoint override or automatic bootstrap.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import stat
import struct
from contextlib import asynccontextmanager
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from primer.llm.codex.boundary import BoundaryError
from primer.llm.codex.request import compact, decode
from .security import MAX_RECORD, PrivateStore, SignedPolicy, require, text


def _parts(path: Path) -> tuple[str, ...]:
    raw = str(path)
    require(raw.startswith('/') and not raw.startswith('//')
            and all(p not in {'', '.', '..'} for p in raw.split('/')[1:]))
    return tuple(raw.split('/')[1:])


def _root_directory(path: Path) -> int:
    """Descriptor walk: root-owned, non-writable by group/other at every hop."""
    parts = _parts(path)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in (None, *parts):
            if part is not None:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new
            info = os.fstat(fd)
            require(info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o022)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _root_json(fd: int, name: str) -> dict:
    require(type(fd) is int and fd >= 0)
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(child)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not stat.S_IMODE(info.st_mode) & 0o022 and info.st_size <= MAX_RECORD)
        raw = bytearray()
        while len(raw) <= MAX_RECORD:
            chunk = os.read(child, min(65536, MAX_RECORD + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        require(len(raw) <= MAX_RECORD)
        value = decode(bytes(raw))
        require(type(value) is dict)
        return value
    finally:
        os.close(child)


def _revision(value) -> int:
    require(type(value) is str and re.fullmatch(r'[1-9][0-9]{0,18}', value) is not None)
    return int(value)


# No reset API: uncertain writes fence this inode for the interpreter lifetime.
_UNCERTAIN_STORES: set[tuple[int, int]] = set()


def manifest_identity(doc: dict) -> str:
    """Versioned canonical JSON, independent of object insertion order."""
    raw = json.dumps(doc, sort_keys=True, ensure_ascii=False, allow_nan=False,
                     separators=(',', ':'))
    return hashlib.sha256(('primer-codex-host-v1\n' + raw).encode()).hexdigest()


def _manifest(doc: dict) -> tuple[dict[str, bytes], dict[str, str]]:
    require(type(doc) is dict and set(doc) == {'audience', 'broker_uid', 'primer_uid', 'account',
                                             'store', 'socket', 'authorities'})
    require(type(doc['broker_uid']) is int and doc['broker_uid'] > 0
            and type(doc['primer_uid']) is int and doc['primer_uid'] > 0
            and doc['broker_uid'] != doc['primer_uid'])
    text(doc['audience'])
    require(re.fullmatch(r'[0-9a-f]{64}', text(doc['account'])) is not None)
    for field in ('store', 'socket'):
        _parts(Path(text(doc[field])))
    authorities = doc['authorities']
    require(type(authorities) is dict and 0 < len(authorities) <= 32)
    keys, floors = {}, {}
    for ident, item in authorities.items():
        text(ident)
        require(type(item) is dict and set(item) == {'key', 'minimum_revision'})
        key = base64.b64decode(text(item['key']), validate=True)
        Ed25519PublicKey.from_public_bytes(key)
        require(key not in keys.values())
        _revision(item['minimum_revision'])
        keys[ident], floors[ident] = key, item['minimum_revision']
    return keys, floors


def _policy_session(store: PrivateStore, identity: str) -> None:
    """Persist dirty marker BEFORE observing policy; never automatically clear.

    Reopening requires a trusted administrator to reconcile the ledger and
    provision a ready marker. No policy was observed if the initial marker write
    fails, but same-process uncertainty still fences every subsequent instance.
    """
    inode = store.identity
    require(inode not in _UNCERTAIN_STORES)
    if hasattr(store, '_policy_session_identity'):
        require(store._policy_session_identity == identity)
        return
    require(store.read('policy_session') == {'identity': identity, 'state': 'ready'})
    _UNCERTAIN_STORES.add(inode)
    store.write('policy_session', {'identity': identity, 'state': 'active'})
    store._policy_session_identity = identity
    _UNCERTAIN_STORES.remove(inode)


class PolicyLedger:
    """Persist each highest authenticated decision before considering a grant."""
    def __init__(self, store: PrivateStore, *, identity: str, keys: dict[str, bytes],
                 audience: str, floors: dict[str, str], load):
        require(bool(keys) and len(set(keys.values())) == len(keys))
        self._store, self._identity, self._load = store, identity, load
        self._keys = {k: Ed25519PublicKey.from_public_bytes(v) for k, v in keys.items()}
        self._audience, self._floors = audience, floors
        _policy_session(store, identity)
        self._read_state()

    def _read_state(self):
        self._state = self._store.read('policy_ledger')
        require(type(self._state) is dict and set(self._state) == {'identity', 'revisions', 'conflicts'}
                and self._state['identity'] == self._identity and self._state['conflicts'] == []
                and type(self._state['revisions']) is dict
                and set(self._state['revisions']) == set(self._keys) == set(self._floors))
        for ident, item in self._state['revisions'].items():
            require(type(item) is dict and set(item) == {'revision', 'sha256'})
            require(_revision(item['revision']) >= _revision(self._floors[ident]))
            require(type(item['sha256']) is str and re.fullmatch(r'[0-9a-f]{64}', item['sha256']) is not None)

    def __call__(self):
        inode = self._store.identity
        require(inode not in _UNCERTAIN_STORES)
        self._read_state()
        bundle = self._load()
        require(type(bundle) is dict and set(bundle) == set(self._keys))
        authenticated = {}
        # Authenticate the COMPLETE set before ANY persistence. Rollback is not
        # authentication failure: another authority's new denial must survive it.
        for ident, key in self._keys.items():
            envelope = bundle[ident]
            require(type(envelope) is dict and set(envelope) == {'payload', 'signature'})
            raw = text(envelope['payload'], MAX_RECORD).encode()
            key.verify(base64.b64decode(envelope['signature'], validate=True), raw)
            doc = decode(raw)
            require(type(doc) is dict and doc.get('audience') == self._audience)
            _revision(doc.get('revision'))
            authenticated[ident] = {'revision': doc['revision'], 'sha256': hashlib.sha256(raw).hexdigest()}
        revisions = dict(self._state['revisions'])
        conflicts, refused = [], False
        for ident, item in authenticated.items():
            number = _revision(item['revision'])
            previous = revisions[ident]
            prior = _revision(previous['revision'])
            if number < prior or number < _revision(self._floors[ident]):
                refused = True
            elif number == prior and item['sha256'] != previous['sha256']:
                conflicts.append(ident)
                refused = True
            elif number > prior:
                revisions[ident] = item
        prospective = {'identity': self._identity, 'revisions': revisions, 'conflicts': sorted(conflicts)}
        if prospective != self._state:
            try:
                self._store.write('policy_ledger', prospective)
            except BaseException:
                _UNCERTAIN_STORES.add(inode)
                raise
            self._state = prospective
        require(not refused)
        return bundle


class HostTrust:
    """Pinned root manifest with fresh signed policy reads; distinct OS identities.

    This proves the local filesystem owner, not organizational completeness.
    Root, the broker UID, interpreter/dependencies and local kernel are trusted.
    """
    def __init__(self, directory: Path):
        self._fd = _root_directory(directory)
        try:
            doc = _root_json(self._fd, 'host.json')
            self._keys, self._floors = _manifest(doc)
            self.identity = manifest_identity(doc)
            self._document = decode(compact(doc))
        except BaseException:
            self.close()
            raise BoundaryError('Host trust unavailable') from None

    def _live(self):
        require(self._fd is not None)

    def _manifest_current(self):
        self._live()
        doc = _root_json(self._fd, 'host.json')
        _manifest(doc)
        require(manifest_identity(doc) == self.identity)

    @property
    def account(self):
        self._live()
        return self._document['account']

    @property
    def primer_uid(self):
        self._live()
        return self._document['primer_uid']

    def _policy_bundle(self):
        # Changes to keys, identities or paths require a stopped, reprovisioned
        # process. Policy replacement itself is permitted and read every grant.
        self._manifest_current()
        return _root_json(self._fd, 'policy.json')

    def open_policy_store(self) -> tuple[PrivateStore, SignedPolicy]:
        """Broker-side only; all identity/path checks before credential reads."""
        self._manifest_current()
        require(os.geteuid() == self._document['broker_uid'])
        path = Path(self._document['store'])
        parent = _root_directory(path.parent)
        store = None
        try:
            # Parent descriptor prevents a second path walk through mutable
            # ancestors. PrivateStore independently checks leaf UID/mode/lock.
            store = PrivateStore(Path(f'/proc/self/fd/{parent}') / path.name)
            ledger = PolicyLedger(store, identity=self.identity, keys=self._keys,
                                  audience=self._document['audience'], floors=self._floors,
                                  load=self._policy_bundle)
            policy = SignedPolicy(self._keys, self._document['audience'], ledger)
            policy.grant(self.account)
            return store, policy
        except BaseException:
            if store is not None:
                store.close()
            raise
        finally:
            os.close(parent)

    def connect_model_socket(self) -> socket.socket:
        """Primer-side authenticated connection, BEFORE any request bytes.

        Return a nonblocking socket for async protocol composition. No generic
        endpoint, account override, management operation or credential is sent.
        """
        self._manifest_current()
        require(os.geteuid() == self.primer_uid)
        path = Path(self._document['socket'])
        parent = _root_directory(path.parent)
        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._manifest_current()
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            require(stat.S_ISSOCK(info.st_mode) and info.st_uid == self._document['broker_uid']
                    and stat.S_IMODE(info.st_mode) == 0o660)
            sock.settimeout(2)
            sock.connect(f'/proc/self/fd/{parent}/{path.name}')
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            require(uid == self._document['broker_uid'])
            sock.setblocking(False)
            return sock
        except BaseException:
            if sock is not None:
                sock.close()
            raise BoundaryError('Broker peer authentication refused') from None
        finally:
            os.close(parent)

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


@asynccontextmanager
async def host_broker(directory: Path):
    """Compose only after host provenance, UID, ledger and grant validation.

    Does not bind/listen, enroll, authenticate, register or activate a provider.
    The host remains responsible for supplying the approved listening socket.
    """
    import asyncio

    from primer.llm.codex.lifecycle import drain
    from .auth import SignIn
    from .http import FixedHTTP
    from .service import Broker, BrokerServer

    trust = HostTrust(directory)
    store = http = server = None
    try:
        store, policy = trust.open_policy_store()
        http = FixedHTTP()
        auth = SignIn(store, policy, http)
        server = BrokerServer(Broker(auth, http), peer_uid=trust.primer_uid, account=trust.account)
        yield server
    finally:
        async def cleanup():
            try:
                if server is not None:
                    await server.close()
                elif http is not None:
                    await http.close()
            finally:
                if store is not None:
                    store.close()
                trust.close()
        await drain(asyncio.create_task(cleanup()))
