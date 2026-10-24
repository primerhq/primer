"""The filesystem artifact store (plan 09, Task 8; design section 15.2).

A root folder holds a ``.primer-artifact-root`` marker, written when the provider is prepared (created, edited, or tested as a draft). Every process
requires the marker before it uses the store, which is how a pod that lacks the shared volume is caught: its root is missing, or an empty folder on
the container's own disk, and it must refuse rather than write files the API pod cannot read (Review Focus 1).

Each artifact is two files under ``root/<hex[0:2]>/<hex[2:4]>/``: the bytes at ``<id>`` and a JSON sidecar at ``<id>.json``. An id is checked against
``BACKEND_ARTIFACT_ID_RE`` before it touches a path, every folder below the root must be a real folder (a symlink is refused) and the two files are
opened without following a symlink, so no id and no planted link reads, writes or deletes anything outside the root. All file IO runs in a thread.

Every test works inside its own ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from pathlib import Path

import pytest

from primer.artifact import filesystem
from primer.artifact.filesystem import ROOT_MARKER, FilesystemArtifactStorage, check_root, prepare_root
from primer.artifact.ids import BACKEND_ARTIFACT_ID_RE, is_backend_artifact_id, new_backend_artifact_id
from primer.artifact.routing import RoutingArtifactStorage
from primer.model.artifact import ArtifactLocation
from primer.model.except_ import ConfigError, ConflictError
from primer.model.provider import ArtifactStorageProvider, SqliteConfig
from primer.model.providers.artifact import ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, ActiveArtifactStorageConfig
from primer.model.storage import OffsetPage
from primer.storage.sqlite import SqliteStorageProvider


async def _ready(tmp_path: Path, name: str = "root") -> FilesystemArtifactStorage:
    root = tmp_path / name
    prepare_root(str(root))
    store = FilesystemArtifactStorage(str(root))
    await store.initialize()
    return store


def _blob_path(root: Path, artifact_id: str) -> Path:
    hexpart = artifact_id.removeprefix("artifact-")
    return root / hexpart[:2] / hexpart[2:4] / artifact_id


def _files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.name != ROOT_MARKER)


# ---- ids ---------------------------------------------------------------------------------------------------------------------------------------


def test_a_backend_id_is_artifact_and_32_lowercase_hex() -> None:
    first, second = new_backend_artifact_id(), new_backend_artifact_id()

    assert BACKEND_ARTIFACT_ID_RE.match(first) and is_backend_artifact_id(first)
    assert first != second


@pytest.mark.parametrize(
    "value",
    [
        "artifact-" + "a" * 32 + "\n",          # ``$`` would match before a trailing newline; the pattern must not
        "artifact-" + "A" * 32,
        "artifact-" + "a" * 31,
        "artifact-" + "a" * 33,
        "artifact-0123456789ab",                # a database id (12 hex) is not a backend id
        "artifact-" + "g" * 32,
        " artifact-" + "a" * 32,
        None,
        42,
    ],
)
def test_anything_else_is_not_a_backend_id(value: object) -> None:
    assert not is_backend_artifact_id(value)
    if isinstance(value, str):
        assert BACKEND_ARTIFACT_ID_RE.match(value) is None


# ---- prepare and check -------------------------------------------------------------------------------------------------------------------------


def test_prepare_creates_the_root_and_its_marker(tmp_path: Path) -> None:
    root = tmp_path / "a" / "b"

    prepare_root(str(root))

    assert (root / ROOT_MARKER).is_file()
    assert [p.name for p in root.iterdir()] == [ROOT_MARKER], "the write probe leaves nothing behind"


def test_prepare_is_idempotent_and_keeps_the_marker(tmp_path: Path) -> None:
    root = tmp_path / "root"
    prepare_root(str(root))
    marker = (root / ROOT_MARKER).read_bytes()

    prepare_root(str(root))

    assert (root / ROOT_MARKER).read_bytes() == marker
    check_root(str(root))


def test_a_root_with_a_nul_character_is_refused_by_name(tmp_path: Path) -> None:
    """``os.mkdir`` raises a bare ``ValueError`` for a NUL in a path, which reached the REST route as a 500 (review of #890, item 1)."""
    bad = str(tmp_path / "x\x00y")

    for check in (prepare_root, check_root):
        with pytest.raises(ConfigError, match="must not contain a NUL character") as info:
            check(bad)
        assert repr(bad) in str(info.value)
    assert list(tmp_path.iterdir()) == []


def test_prepare_refuses_a_folder_that_already_holds_files_and_no_marker(tmp_path: Path) -> None:
    """Review of #890, item 5: a typo guard. A root naming ``/``, ``/etc`` or an application's data folder must not be adopted as an artifact
    store: the folder holds files and was never prepared."""
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "unrelated.txt").write_text("keep me")

    with pytest.raises(ConfigError, match="already holds files") as info:
        prepare_root(str(taken))

    assert repr(str(taken)) in str(info.value)
    assert sorted(p.name for p in taken.iterdir()) == ["unrelated.txt"], "no marker, no probe file"


def test_prepare_takes_an_empty_folder_and_a_fresh_volume(tmp_path: Path) -> None:
    """An empty folder is a new store; so is a freshly formatted volume, whose only entry is ``lost+found``."""
    empty, fresh = tmp_path / "empty", tmp_path / "fresh"
    empty.mkdir()
    (fresh / "lost+found").mkdir(parents=True)

    prepare_root(str(empty))
    prepare_root(str(fresh))

    assert (empty / ROOT_MARKER).is_file() and (fresh / ROOT_MARKER).is_file()


def test_the_missing_marker_message_says_the_volume_is_not_mounted(tmp_path: Path) -> None:
    """Review of #890, item 5: it told the operator to "save the provider again", which re-created the marker on a pod WITHOUT the volume."""
    (tmp_path / "bare").mkdir()

    with pytest.raises(ConfigError) as info:
        check_root(str(tmp_path / "bare"))

    message = str(info.value)
    assert "save the provider again" not in message
    assert "the shared artifact volume is not mounted here" in message and "new or empty folder" in message


@pytest.mark.parametrize("bad", ["relative/dir", "", "./artifacts"])
def test_prepare_refuses_a_root_that_is_not_absolute(bad: str) -> None:
    with pytest.raises(ConfigError, match="must be an absolute path"):
        prepare_root(bad)
    with pytest.raises(ConfigError, match="must be an absolute path"):
        check_root(bad)


def test_prepare_refuses_a_root_that_is_a_file(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")

    with pytest.raises(ConfigError, match="is not a directory"):
        prepare_root(str(tmp_path / "f"))
    assert (tmp_path / "f").read_text() == "x"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes through any mode bits")
def test_prepare_refuses_an_unwritable_root(tmp_path: Path) -> None:
    root = tmp_path / "ro"
    root.mkdir()
    root.chmod(0o500)
    try:
        with pytest.raises(ConfigError, match="is not writable"):
            prepare_root(str(root))
        assert not (root / ROOT_MARKER).exists()
    finally:
        root.chmod(0o700)


async def test_a_pod_without_the_volume_cannot_use_the_store(tmp_path: Path) -> None:
    """Review Focus 1: a missing root, and an existing folder that was never prepared (a pod's own disk where the shared volume should be)."""
    missing = tmp_path / "missing"
    with pytest.raises(ConfigError, match="does not exist in this process") as info:
        await FilesystemArtifactStorage(str(missing)).initialize()
    assert repr(str(missing)) in str(info.value) and "mount the shared artifact volume" in str(info.value)
    assert not missing.exists(), "initialize never creates the root"

    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(ConfigError, match=re.escape(ROOT_MARKER)) as info:
        await FilesystemArtifactStorage(str(bare)).initialize()
    assert repr(str(bare)) in str(info.value) and "mount the shared artifact volume" in str(info.value)
    assert list(bare.iterdir()) == [], "initialize never writes the marker"


@pytest.mark.parametrize("swap", ["empty mount point", "no folder"])
async def test_a_volume_lost_after_initialize_refuses_the_put_and_writes_nothing(tmp_path: Path, swap: str) -> None:
    """Review of #890, item 4: ``initialize`` runs once per process and the store is cached. When the shared volume goes away afterwards, the
    root is an empty folder on the pod's own disk (or no folder at all), and a put must refuse rather than write files no other pod can read."""
    store = await _ready(tmp_path)
    root = tmp_path / "root"
    os.rename(root, tmp_path / "volume-elsewhere")
    if swap == "empty mount point":
        root.mkdir()

    with pytest.raises(ConfigError, match=re.escape(repr(str(root)))) as info:
        await store.put(data=b"local only", mime_type="text/plain")

    assert "mount the shared artifact volume" in str(info.value)
    assert (list(root.iterdir()) == []) if swap == "empty mount point" else not root.exists(), "nothing is written"


def test_building_the_store_touches_nothing(tmp_path: Path) -> None:
    FilesystemArtifactStorage(str(tmp_path / "never"))

    assert not (tmp_path / "never").exists()


# ---- put, get, delete --------------------------------------------------------------------------------------------------------------------------


async def test_a_put_writes_a_sharded_blob_and_its_sidecar(tmp_path: Path) -> None:
    store = await _ready(tmp_path)

    aid = await store.put(data=b"\x00\xffpng", mime_type="image/png", filename="x.png")

    blob = _blob_path(tmp_path / "root", aid)
    assert BACKEND_ARTIFACT_ID_RE.match(aid) and blob.read_bytes() == b"\x00\xffpng"
    assert json.loads(blob.with_name(aid + ".json").read_text(encoding="utf-8")) == {"mime_type": "image/png", "filename": "x.png", "size": 5}
    assert store.path_for(aid) == blob
    assert not list((tmp_path / "root").rglob("*.tmp"))


async def test_the_sidecar_keeps_a_filename_that_is_not_ascii_as_utf8(tmp_path: Path) -> None:
    store = await _ready(tmp_path)

    aid = await store.put(data=b"x", mime_type="text/plain", filename="résumé 日本.txt")

    raw = _blob_path(tmp_path / "root", aid).with_name(aid + ".json").read_bytes()
    assert "résumé 日本.txt".encode() in raw


async def test_a_put_whose_sidecar_fails_leaves_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = await _ready(tmp_path)
    real = filesystem._write_atomic
    calls: list[Path] = []

    def failing_second(path: Path, payload: bytes) -> None:
        calls.append(path)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        real(path, payload)

    monkeypatch.setattr(filesystem, "_write_atomic", failing_second)

    with pytest.raises(OSError, match="No space left"):
        await store.put(data=b"bytes", mime_type="text/plain")

    assert len(calls) == 2 and calls[1].name.endswith(".json")
    assert _files_under(tmp_path / "root") == [], "the blob written before the sidecar is removed again"


async def test_get_and_delete_of_an_id_with_no_files(tmp_path: Path) -> None:
    store = await _ready(tmp_path)
    unknown = "artifact-" + "0" * 32

    assert await store.get(unknown) is None
    await store.delete(unknown)
    assert _files_under(tmp_path / "root") == []


async def test_a_blob_without_its_sidecar_reads_as_missing(tmp_path: Path) -> None:
    store = await _ready(tmp_path)
    aid = await store.put(data=b"x", mime_type="text/plain")
    _blob_path(tmp_path / "root", aid).with_name(aid + ".json").unlink()

    assert await store.get(aid) is None
    await store.delete(aid)
    assert _files_under(tmp_path / "root") == []


async def test_delete_removes_both_files(tmp_path: Path) -> None:
    store = await _ready(tmp_path)
    aid = await store.put(data=b"x", mime_type="text/plain", filename="a.txt")

    await store.delete(aid)

    assert await store.get(aid) is None
    assert _files_under(tmp_path / "root") == []


# ---- ruling 1: nothing reads, writes or deletes outside the root -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../outside.bin",
        "artifact-../../outside.bin",
        "/etc/passwd",
        str(Path("/") / "outside.bin"),
        "artifact-" + "A" * 32,
        "artifact-" + "a" * 31,
        "artifact-" + "a" * 32 + "/../../outside.bin",
        "artifact-" + "a" * 32 + "\x00",
        "artifact-" + "a" * 32 + "\n",
    ],
)
async def test_an_id_that_could_escape_the_root_reads_and_deletes_nothing(tmp_path: Path, bad_id: str) -> None:
    (tmp_path / "outside.bin").write_bytes(b"secret")
    store = await _ready(tmp_path)

    assert await store.get(bad_id) is None
    await store.delete(bad_id)

    assert (tmp_path / "outside.bin").read_bytes() == b"secret"
    with pytest.raises(ValueError):
        store.path_for(bad_id)


async def test_an_absolute_id_never_reaches_a_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absolute id joined onto the root would REPLACE it (``Path("/r") / "/etc/passwd"`` is ``/etc/passwd``): the id check comes first, so the
    file helpers are never called with one."""
    store = await _ready(tmp_path)
    touched: list[str] = []
    for name in ("_read_files", "_remove_files"):
        monkeypatch.setattr(filesystem, name, lambda *a, _n=name: touched.append(_n))

    assert await store.get(str(tmp_path / "outside.bin")) is None
    await store.delete(str(tmp_path / "outside.bin"))

    assert touched == []


def _plant_outside(tmp_path: Path, artifact_id: str) -> Path:
    """A folder outside the root that holds a sidecar and a blob for ``artifact_id`` at the shard path a symlink would lead to."""
    hexpart = artifact_id.removeprefix("artifact-")
    outside = tmp_path / "outside"
    target = outside / hexpart[2:4]
    target.mkdir(parents=True)
    (target / artifact_id).write_bytes(b"secret")
    (target / (artifact_id + ".json")).write_text(json.dumps({"mime_type": "text/plain", "filename": None, "size": 6}))
    return outside


async def test_a_shard_folder_that_links_out_of_the_root_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = await _ready(tmp_path)
    aid = "artifact-ab" + "c" * 30
    outside = _plant_outside(tmp_path, aid)
    (tmp_path / "root" / "ab").symlink_to(outside, target_is_directory=True)

    assert await store.get(aid) is None, "nothing is read through the link"
    await store.delete(aid)
    assert (outside / "cc" / aid).read_bytes() == b"secret", "nothing is deleted through the link"
    assert (outside / "cc" / (aid + ".json")).exists()

    monkeypatch.setattr(filesystem, "new_backend_artifact_id", lambda: "artifact-ab" + "d" * 30)
    with pytest.raises(ValueError, match="outside the artifact root"):
        await store.put(data=b"new", mime_type="text/plain")
    assert not (outside / "dd").exists(), "nothing is written through the link"


@pytest.mark.parametrize("linked", ["blob", "sidecar"])
async def test_a_file_that_links_out_of_the_root_is_not_followed(tmp_path: Path, linked: str) -> None:
    store = await _ready(tmp_path)
    aid = await store.put(data=b"mine", mime_type="text/plain")
    blob = _blob_path(tmp_path / "root", aid)
    target = blob if linked == "blob" else blob.with_name(aid + ".json")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret" if linked == "blob" else json.dumps({"mime_type": "text/x-secret", "filename": "s", "size": 6}).encode())
    target.unlink()
    target.symlink_to(outside)

    assert await store.get(aid) is None

    await store.delete(aid)
    assert outside.exists(), "the delete removes the link, never the file it points at"
    assert not target.is_symlink() and not target.exists()


async def test_a_root_that_is_itself_a_link_is_allowed(tmp_path: Path) -> None:
    """A mount reached through a symlinked path is a normal deployment; only the folders and files BELOW the root must not be links."""
    real = tmp_path / "volume"
    prepare_root(str(real))
    (tmp_path / "mnt").symlink_to(real, target_is_directory=True)
    store = FilesystemArtifactStorage(str(tmp_path / "mnt"))
    await store.initialize()

    aid = await store.put(data=b"via link", mime_type="text/plain")

    assert (await store.get(aid)).data == b"via link"
    assert _blob_path(real, aid).read_bytes() == b"via link"


# ---- ruling 2: every file operation runs off the event loop ------------------------------------------------------------------------------------


async def test_file_io_runs_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def spy(name: str):
        real = getattr(filesystem, name)

        def wrapper(*args, **kwargs):
            seen[name] = threading.get_ident()
            return real(*args, **kwargs)

        return wrapper

    for name in ("_write_files", "_read_files", "_remove_files", "check_root"):
        monkeypatch.setattr(filesystem, name, spy(name))
    root = tmp_path / "root"
    prepare_root(str(root))
    store = FilesystemArtifactStorage(str(root))

    await store.initialize()
    aid = await store.put(data=b"x", mime_type="text/plain")
    await store.get(aid)
    await store.delete(aid)

    assert set(seen) == {"_write_files", "_read_files", "_remove_files", "check_root"}
    assert all(ident != loop_thread for ident in seen.values()), seen


async def test_a_slow_disk_does_not_block_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = await _ready(tmp_path)
    release = threading.Event()
    real = filesystem._write_files

    def slow(*args, **kwargs):
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(filesystem, "_write_files", slow)
    put = asyncio.create_task(store.put(data=b"x", mime_type="text/plain"))
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0.01)
        ticks += 1
    assert not put.done() and ticks == 5, "the loop kept running while the write was held"
    release.set()
    assert BACKEND_ARTIFACT_ID_RE.match(await put)


# ---- the routing rules hold for this store -----------------------------------------------------------------------------------------------------


async def test_a_put_whose_provider_was_deleted_meanwhile_leaves_no_file(tmp_path: Path) -> None:
    """The routing store reads the provider row again after it writes the record (the G5 audit, G5-03) and, when the row is gone, removes the record
    and the bytes. With this store the bytes are two files: neither may stay behind."""
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "a.sqlite"))
    await sp.initialize()
    try:
        store = await _ready(tmp_path)
        await sp.get_storage(ActiveArtifactStorageConfig).create(
            ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id="fs-gone"),
        )

        async def resolve(provider_id: str):
            return store

        routing = RoutingArtifactStorage(
            resolve=resolve,
            fallback=None,  # type: ignore[arg-type]  # never reached: the active store is resolved
            locations=sp.get_storage(ArtifactLocation),
            active_config=sp.get_storage(ActiveArtifactStorageConfig),
            providers=sp.get_storage(ArtifactStorageProvider),   # holds no "fs-gone" row: the store was deleted meanwhile
        )

        with pytest.raises(ConflictError, match="was deleted while this upload was being written"):
            await routing.put(data=b"orphan?", mime_type="text/plain")

        assert _files_under(tmp_path / "root") == []
        assert (await sp.get_storage(ArtifactLocation).list(OffsetPage(length=10))).total == 0
    finally:
        await sp.aclose()


async def test_a_cancelled_put_leaves_no_file_and_no_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of #890, item 2: a put cancelled while its thread is writing (a client that went away) never hands its id to anyone, and the thread
    cannot be stopped, so the files it goes on to write must be removed once it finishes; the routing store writes no record."""
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "a.sqlite"))
    await sp.initialize()
    try:
        store = await _ready(tmp_path)
        await sp.get_storage(ArtifactStorageProvider).create(
            ArtifactStorageProvider(id="fs-1", provider="filesystem", config={"root": str(tmp_path / "root")}),
        )
        await sp.get_storage(ActiveArtifactStorageConfig).create(
            ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id="fs-1"),
        )

        async def resolve(provider_id: str):
            return store

        routing = RoutingArtifactStorage(
            resolve=resolve,
            fallback=None,  # type: ignore[arg-type]  # never reached: the active store is resolved
            locations=sp.get_storage(ArtifactLocation),
            active_config=sp.get_storage(ActiveArtifactStorageConfig),
            providers=sp.get_storage(ArtifactStorageProvider),
        )
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        real = filesystem._write_files

        def held(*args, **kwargs):
            entered.set()
            release.wait(10)
            try:
                return real(*args, **kwargs)
            finally:
                done.set()

        monkeypatch.setattr(filesystem, "_write_files", held)
        put = asyncio.create_task(routing.put(data=b"orphan?", mime_type="text/plain"))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        put.cancel()
        with pytest.raises(asyncio.CancelledError):
            await put
        release.set()
        await asyncio.to_thread(done.wait, 10)
        for _ in range(100):
            if not _files_under(tmp_path / "root"):
                break
            await asyncio.sleep(0.02)

        assert _files_under(tmp_path / "root") == [], "the files the thread wrote after the cancel are removed"
        assert (await sp.get_storage(ArtifactLocation).list(OffsetPage(length=10))).total == 0
        assert not filesystem._PENDING_CLEANUPS, "the cleanup finished and let go of its task"

        monkeypatch.setattr(filesystem, "_write_files", real)
        aid = await routing.put(data=b"kept", mime_type="text/plain")
        assert (await routing.get(aid)).data == b"kept", "a put that is not cancelled still round-trips"
    finally:
        await sp.aclose()


def test_a_filesystem_store_serves_only_a_filesystem_row_with_its_root(tmp_path: Path) -> None:
    store = FilesystemArtifactStorage(str(tmp_path / "a"))

    def row(provider: str, root: str | None = None) -> ArtifactStorageProvider:
        config = {"root": root} if root is not None else ({"bucket": "b"} if provider == "s3" else {})
        return ArtifactStorageProvider(id="fs-1", provider=provider, config=config)

    assert store.serves(row("filesystem", str(tmp_path / "a")))
    assert store.serves(row("filesystem", str(tmp_path / "a") + "/")), "the same folder, written with a trailing slash"
    assert not store.serves(row("filesystem", str(tmp_path / "b")))
    assert not store.serves(row("db"))
    assert not store.serves(row("s3"))


async def test_a_root_moved_while_a_put_is_in_flight_loses_nothing_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of #890, item 3: an admin moves the store's root while an upload to it is in flight. The move check passes (no record yet), the
    cached store goes on writing under the OLD root, and the record would point at a provider whose root is now elsewhere: an id that reads as
    missing. The put sees at its re-read that its store no longer serves the row, removes its files and record, and fails with a 409."""
    from primer.api.registries.artifact_storage_registry import ArtifactStorageRegistry
    from primer.artifact.checks import check_artifact_provider_update

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "a.sqlite"))
    await sp.initialize()
    try:
        prepare_root(str(tmp_path / "A"))
        prepare_root(str(tmp_path / "B"))
        providers = sp.get_storage(ArtifactStorageProvider)
        row = await providers.create(ArtifactStorageProvider(id="fs-1", provider="filesystem", config={"root": str(tmp_path / "A")}))
        await sp.get_storage(ActiveArtifactStorageConfig).create(
            ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id="fs-1"),
        )
        registry = ArtifactStorageRegistry(storage=providers, storage_provider=sp)
        routing = await registry.get_default()
        entered, release = threading.Event(), threading.Event()
        real = filesystem._write_files

        def held(*args, **kwargs):
            entered.set()
            release.wait(10)
            return real(*args, **kwargs)

        monkeypatch.setattr(filesystem, "_write_files", held)
        put = asyncio.create_task(routing.put(data=b"in flight", mime_type="text/plain"))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        moved = row.model_copy(update={"config": type(row.config)(root=str(tmp_path / "B"))})
        await check_artifact_provider_update(sp, moved, row)                    # passes: the store holds no record yet
        await providers.update(moved)
        await registry.invalidate("fs-1")
        release.set()

        with pytest.raises(ConflictError, match="'fs-1' was moved to another location while this upload was being written"):
            await asyncio.wait_for(put, 10)

        assert _files_under(tmp_path / "A") == [] and _files_under(tmp_path / "B") == []
        assert (await sp.get_storage(ArtifactLocation).list(OffsetPage(length=10))).total == 0
        monkeypatch.setattr(filesystem, "_write_files", real)
        again = await routing.put(data=b"retried", mime_type="text/plain")
        assert (await routing.get(again)).data == b"retried" and _files_under(tmp_path / "B"), "the retry goes to the new root"
        await registry.aclose()
    finally:
        await sp.aclose()
