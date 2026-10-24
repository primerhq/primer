"""The S3 artifact store (plan 09, Task 9; design section 15.2).

Each artifact is one object at ``<prefix><artifact id>``: the bytes as the body, the MIME type as the ``Content-Type`` and the filename, percent-encoded
because S3 user metadata is ASCII only, as the ``filename`` metadata. The store talks to S3 through aiobotocore (the ``s3`` extra), imported only when a
store is initialized.

The tests that talk to S3 use moto's threaded server on 127.0.0.1 (``tests/artifact/conftest.py``), never a real AWS endpoint, and every one of them
runs in an environment with no AWS credentials, profile or region of the machine's own (``isolated_aws_env``).

The lead rulings of slice D pinned here: the serves rule compares bucket, prefix, endpoint and region (ruling 4); a half pair of keys is refused,
never completed from the ambient credentials (ruling 5); the secret is never logged (ruling 7); aiobotocore is imported by nothing but ``initialize``
(ruling 3).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import pytest

import primer
from primer.artifact.db import DbArtifactStorage
from primer.artifact.factory import build_artifact_storage
from primer.artifact.probe import probe_artifact_provider
from primer.artifact.routing import RoutingArtifactStorage
from primer.artifact.s3 import S3ArtifactStorage, s3_addressing_style, s3_client_kwargs
from primer.common.entity_checks import EntityCheckError
from primer.int.artifact_storage import ArtifactBlob
from primer.model.artifact import ArtifactLocation
from primer.model.except_ import ConfigError, ConflictError
from primer.model.provider import ArtifactStorageProvider, S3ArtifactConfig, SqliteConfig
from primer.model.providers.artifact import ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, ActiveArtifactStorageConfig
from primer.model.storage import OffsetPage
from primer.storage.sqlite import SqliteStorageProvider
from tests.artifact.conftest import S3_BUCKET, S3_TEST_KEY, S3_TEST_SECRET, s3_keys_under

#: A secret no other text in a test run holds, so finding it in a log or an answer can only mean it leaked.
SECRET = "s3cr3t-never-shown-0123456789"
ACCESS_KEY = "AKIANEVERSHOWN0123"


def _config(endpoint: str | None = None, prefix: str = "", **over) -> S3ArtifactConfig:
    values = {"bucket": S3_BUCKET, "prefix": prefix, "endpoint_url": endpoint, "access_key": S3_TEST_KEY, "secret_key": S3_TEST_SECRET}
    values.update(over)
    return S3ArtifactConfig(**values)


def _s3_row(provider_id: str = "s3-1", **config) -> ArtifactStorageProvider:
    return ArtifactStorageProvider.model_validate({"id": provider_id, "provider": "s3", "config": {"bucket": S3_BUCKET, **config}})


@pytest.fixture
async def store(s3_endpoint: str, s3_prefix: str, isolated_aws_env: None):
    s3 = S3ArtifactStorage(_config(s3_endpoint, s3_prefix))
    await s3.initialize()
    yield s3
    await s3.aclose()


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "s3.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


@pytest.fixture
def extra_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``s3`` extra reads as installed, so a build gets past ``require_extra`` whatever this environment holds. Building imports no aiobotocore."""
    monkeypatch.setattr("primer.common.optional._find_spec", lambda name: object())


# ---- the client settings ------------------------------------------------------------------------------------------------------------------------


def test_auto_addressing_is_path_style_with_an_endpoint_and_botocore_default_on_aws() -> None:
    assert s3_addressing_style(S3ArtifactConfig(bucket="b", endpoint_url="http://minio:9000")) == "path"
    assert s3_addressing_style(S3ArtifactConfig(bucket="b")) == "auto"
    assert s3_addressing_style(S3ArtifactConfig(bucket="b", endpoint_url="http://x:9000", addressing_style="virtual")) == "virtual"
    assert s3_addressing_style(S3ArtifactConfig(bucket="b", addressing_style="path")) == "path"


def test_client_kwargs_carry_the_region_the_endpoint_only_when_set_and_both_keys() -> None:
    assert s3_client_kwargs(S3ArtifactConfig(bucket="b")) == {"region_name": None}
    assert s3_client_kwargs(S3ArtifactConfig(bucket="b", region="eu-west-1", endpoint_url="")) == {"region_name": "eu-west-1"}, (
        "a blank endpoint (what the console sends for an empty box) never reaches botocore"
    )
    both = s3_client_kwargs(S3ArtifactConfig(bucket="b", endpoint_url="http://minio:9000", access_key="k", secret_key="s"))
    assert both == {"region_name": None, "endpoint_url": "http://minio:9000", "aws_access_key_id": "k", "aws_secret_access_key": "s"}


@pytest.mark.parametrize(("keys", "missing"), [({"access_key": ACCESS_KEY}, "secret_key"), ({"secret_key": SECRET}, "access_key")])
def test_a_half_pair_of_keys_never_reaches_botocore(keys: dict, missing: str) -> None:
    """Ruling 5: with one key dropped, botocore would sign with whatever ambient credentials the server has (a pod role, an env pair) and write
    the operator's files with an identity the operator did not name. A half pair is refused instead."""
    config = S3ArtifactConfig(bucket="b", **keys)

    with pytest.raises(ConfigError, match=f"set both access_key and secret_key, or neither.*{missing} is blank") as info:
        s3_client_kwargs(config)
    with pytest.raises(ConfigError):
        S3ArtifactStorage(config)

    assert ACCESS_KEY not in str(info.value) and SECRET not in str(info.value)


def test_a_half_pair_row_is_not_built(extra_installed: None) -> None:
    """A row stored before the write check refused half pairs (an ``s3`` row could be saved before this backend shipped) is refused at build."""
    with pytest.raises(ConfigError, match="access_key and secret_key"):
        build_artifact_storage(_s3_row(access_key=ACCESS_KEY), storage_provider=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(("keys", "field"), [({"access_key": ACCESS_KEY}, "config.secret_key"), ({"secret_key": SECRET}, "config.access_key")])
def test_one_key_without_the_other_is_refused(keys: dict, field: str) -> None:
    from primer.artifact.checks import check_artifact_provider_write

    with pytest.raises(EntityCheckError) as info:
        check_artifact_provider_write(_s3_row("s3-half", **keys))

    assert (info.value.kind, info.value.code, info.value.field) == ("validation", "artifact_s3_keys_incomplete", field)
    assert info.value.message == "set both access_key and secret_key, or neither to use the server's own credentials"


def test_both_keys_or_neither_pass_the_check() -> None:
    from primer.artifact.checks import check_artifact_provider_write

    check_artifact_provider_write(_s3_row("s3-both", access_key=ACCESS_KEY, secret_key=SECRET))
    check_artifact_provider_write(_s3_row("s3-none"))
    check_artifact_provider_write(_s3_row("s3-blank", access_key="", secret_key="  "))


def test_building_s3_without_the_extra_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("primer.common.optional._find_spec", lambda name: None)

    with pytest.raises(ConfigError, match="'s3' extra"):
        build_artifact_storage(_s3_row(), storage_provider=None)  # type: ignore[arg-type]


def test_a_built_store_uses_the_rows_config(extra_installed: None) -> None:
    store = build_artifact_storage(_s3_row(prefix="media/", region="eu-west-1"), storage_provider=None)  # type: ignore[arg-type]

    assert type(store) is S3ArtifactStorage
    assert (store.bucket, store.prefix, store.key_for("artifact-" + "a" * 32)) == (S3_BUCKET, "media/", "media/artifact-" + "a" * 32)


def test_the_s3_module_and_a_build_import_no_aiobotocore() -> None:
    """Ruling 3: a core install (no ``s3`` extra) imports the app, builds an S3 store object and never needs aiobotocore or botocore; only
    ``initialize`` imports them."""
    code = (
        "import sys\n"
        "import primer.api.app\n"
        "import primer.common.optional as optional\n"
        "optional._find_spec = lambda name: object()\n"
        "from primer.artifact.factory import build_artifact_storage\n"
        "from primer.model.provider import ArtifactStorageProvider\n"
        "row = ArtifactStorageProvider.model_validate({'id': 's3-1', 'provider': 's3', 'config': {'bucket': 'b'}})\n"
        "build_artifact_storage(row, storage_provider=None)\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in ('aiobotocore', 'botocore', 'boto3'))\n"
        "assert not loaded, loaded\n"
    )
    root = Path(primer.__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr[-2000:]


# ---- serves: the location a store was built for (ruling 4) --------------------------------------------------------------------------------------


_BUILT_FOR = {"prefix": "media/", "endpoint_url": "http://minio:9000", "region": "eu-west-1", "access_key": ACCESS_KEY, "secret_key": SECRET}


def test_a_store_serves_the_row_it_was_built_from() -> None:
    store = S3ArtifactStorage(_s3_row(**_BUILT_FOR).config)

    assert store.serves(_s3_row(**_BUILT_FOR))


@pytest.mark.parametrize(
    "moved",
    [{"bucket": "other-bucket"}, {"prefix": "other/"}, {"prefix": ""}, {"endpoint_url": "http://other:9000"}, {"endpoint_url": None},
     {"region": "us-east-1"}, {"region": None}],
    ids=["bucket", "prefix", "no prefix", "endpoint", "no endpoint", "region", "no region"],
)
def test_a_store_does_not_serve_a_row_whose_location_moved(moved: dict) -> None:
    """A put the routing store finds no longer served (the row was moved while the put was in flight) removes its object and record and fails
    with a 409 (``primer/artifact/routing.py``)."""
    store = S3ArtifactStorage(_s3_row(**_BUILT_FOR).config)

    assert not store.serves(_s3_row(**{**_BUILT_FOR, **moved}))


def test_new_keys_or_another_addressing_style_do_not_move_the_location() -> None:
    store = S3ArtifactStorage(_s3_row(**_BUILT_FOR).config)

    assert store.serves(_s3_row(**{**_BUILT_FOR, "access_key": "AKIAOTHER", "secret_key": "other-secret", "addressing_style": "virtual"}))


def test_a_store_does_not_serve_a_row_of_another_kind(tmp_path: Path) -> None:
    store = S3ArtifactStorage(_s3_row().config)

    assert not store.serves(ArtifactStorageProvider(id="s3-1", provider="db"))
    assert not store.serves(ArtifactStorageProvider(id="s3-1", provider="filesystem", config={"root": str(tmp_path)}))


def test_key_for_is_the_prefix_and_the_id_and_refuses_anything_else() -> None:
    store = S3ArtifactStorage(_s3_row(prefix="media/").config)
    aid = "artifact-" + "0123456789abcdef" * 2

    assert store.key_for(aid) == "media/" + aid
    for bad in ("artifact-abc", "../" + aid, aid + "\n", "artifact-" + "A" * 32, ""):
        with pytest.raises(ValueError):
            store.key_for(bad)


# ---- against the S3 server ----------------------------------------------------------------------------------------------------------------------


async def test_objects_are_stored_under_prefix_plus_id(store: S3ArtifactStorage, s3_admin, s3_prefix: str) -> None:
    aid = await store.put(data=b"\x89PNG", mime_type="image/png", filename="résumé.png")

    head = s3_admin.head_object(Bucket=S3_BUCKET, Key=s3_prefix + aid)

    assert aid.startswith("artifact-") and len(aid) == len("artifact-") + 32
    assert head["ContentType"] == "image/png"
    assert head["Metadata"] == {"filename": "r%C3%A9sum%C3%A9.png"}
    assert s3_keys_under(s3_admin, s3_prefix) == [s3_prefix + aid]
    assert await store.get(aid) == ArtifactBlob(data=b"\x89PNG", mime_type="image/png", filename="résumé.png")


async def test_a_missing_filename_writes_no_metadata(store: S3ArtifactStorage, s3_admin, s3_prefix: str) -> None:
    aid = await store.put(data=b"x", mime_type="text/plain")

    assert s3_admin.head_object(Bucket=S3_BUCKET, Key=s3_prefix + aid)["Metadata"] == {}
    assert (await store.get(aid)).filename is None


async def test_the_client_uses_the_addressing_style_timeouts_and_retries(store: S3ArtifactStorage) -> None:
    config = store._client.meta.config

    assert config.s3 == {"addressing_style": "path"}, "an endpoint URL makes auto path-style"
    assert (config.connect_timeout, config.read_timeout) == (10, 60)
    # ``max_attempts: 3`` counts retries; botocore keeps it as the total, the first attempt and three retries.
    assert config.retries == {"mode": "standard", "total_max_attempts": 4}


async def test_ambient_credentials_are_used_when_the_row_has_no_keys(
    s3_endpoint: str, s3_prefix: str, isolated_aws_env: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    s3 = S3ArtifactStorage(_config(s3_endpoint, s3_prefix, access_key=None, secret_key=None))
    await s3.initialize()
    try:
        aid = await s3.put(data=b"ambient", mime_type="text/plain", filename="a.txt")
        assert await s3.get(aid) == ArtifactBlob(data=b"ambient", mime_type="text/plain", filename="a.txt")
    finally:
        await s3.aclose()


async def test_without_any_credentials_the_probe_names_put(s3_endpoint: str, s3_prefix: str, isolated_aws_env: None, sp) -> None:
    row = _s3_row("s3-nocreds", prefix=s3_prefix, endpoint_url=s3_endpoint)

    result = await probe_artifact_provider(row, storage_provider=sp)

    assert (result.ok, result.step) == (False, "put")
    assert "credentials" in result.error.lower()


async def test_a_half_pair_is_refused_even_when_ambient_credentials_would_work(
    s3_endpoint: str, s3_prefix: str, isolated_aws_env: None, monkeypatch: pytest.MonkeyPatch, sp, s3_admin,
) -> None:
    """Ruling 5: the server HAS working ambient credentials here, and a row with one key must still not use them: the probe stops at build,
    and nothing reaches the bucket."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    row = _s3_row("s3-half", prefix=s3_prefix, endpoint_url=s3_endpoint, access_key=ACCESS_KEY)

    result = await probe_artifact_provider(row, storage_provider=sp)

    assert (result.ok, result.step) == (False, "build")
    assert "set both access_key and secret_key, or neither" in result.error and ACCESS_KEY not in result.error
    assert s3_keys_under(s3_admin, s3_prefix) == []


async def test_a_probe_of_a_working_store_passes_and_leaves_no_object(s3_endpoint: str, s3_prefix: str, isolated_aws_env: None, sp, s3_admin) -> None:
    row = _s3_row("s3-ok", prefix=s3_prefix, endpoint_url=s3_endpoint, access_key=S3_TEST_KEY, secret_key=S3_TEST_SECRET)

    assert (await probe_artifact_provider(row, storage_provider=sp)).as_dict() == {"ok": True}
    assert s3_keys_under(s3_admin, s3_prefix) == []


async def test_a_malformed_id_reads_none_and_deletes_nothing(store: S3ArtifactStorage, monkeypatch: pytest.MonkeyPatch) -> None:
    """As for the filesystem store: an id that is not a backend id is never turned into a key, so no request is made for it."""
    async def no_request(**kwargs):
        raise AssertionError(f"a request was made for {kwargs.get('Key')!r}")

    monkeypatch.setattr(store._client, "get_object", no_request)
    monkeypatch.setattr(store._client, "delete_object", no_request)

    for bad in ("artifact-nope", "../artifact-" + "0" * 32, "artifact-" + "0" * 32 + "\n", ""):
        assert await store.get(bad) is None
        await store.delete(bad)


async def test_a_read_error_that_is_not_a_missing_key_is_raised(s3_endpoint: str, s3_prefix: str, isolated_aws_env: None) -> None:
    """Only "no such key" reads as a missing artifact. A missing bucket (or a refused read) is an outage, not an absent file: it is raised."""
    from botocore.exceptions import ClientError

    s3 = S3ArtifactStorage(_config(s3_endpoint, s3_prefix, bucket="no-such-bucket"))
    await s3.initialize()
    try:
        with pytest.raises(ClientError, match="NoSuchBucket"):
            await s3.get("artifact-" + "0" * 32)
    finally:
        await s3.aclose()


async def test_a_cancelled_put_leaves_no_object(store: S3ArtifactStorage, s3_admin, s3_prefix: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The #890 round-2 rule (item 2) for S3: a put cancelled while its request is in flight (a client that went away) never hands its id to
    anyone, and the request may still complete on the server: once it does, the object is removed."""
    entered, release = asyncio.Event(), asyncio.Event()
    real = store._client.put_object

    async def held(**kwargs):
        entered.set()
        await release.wait()
        return await real(**kwargs)

    monkeypatch.setattr(store._client, "put_object", held)
    put = asyncio.create_task(store.put(data=b"cancelled", mime_type="text/plain", filename="c.txt"))
    async with asyncio.timeout(10):
        await entered.wait()
    put.cancel()
    with pytest.raises(asyncio.CancelledError):
        await put
    release.set()
    async with asyncio.timeout(10):
        await store.drain()

    assert s3_keys_under(s3_admin, s3_prefix) == []
    monkeypatch.setattr(store._client, "put_object", real)
    aid = await store.put(data=b"after", mime_type="text/plain")
    assert (await store.get(aid)).data == b"after", "the store still works"


async def test_a_put_that_times_out_after_the_server_stored_it_leaves_no_object(
    store: S3ArtifactStorage, s3_admin, s3_prefix: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure that may come after the server stored the object (a read timeout, a dropped connection) never hands out the id either: the
    object is removed in the background, and the caller sees the failure at once."""
    from botocore.exceptions import ReadTimeoutError

    real = store._client.put_object

    async def stored_then_timed_out(**kwargs):
        await real(**kwargs)
        raise ReadTimeoutError(endpoint_url="http://s3.example")

    monkeypatch.setattr(store._client, "put_object", stored_then_timed_out)
    with pytest.raises(ReadTimeoutError):
        await store.put(data=b"maybe", mime_type="text/plain")
    async with asyncio.timeout(10):
        await store.drain()

    assert s3_keys_under(s3_admin, s3_prefix) == []


async def test_a_put_whose_retry_ends_in_a_client_error_after_the_first_attempt_stored_leaves_no_object(
    store: S3ArtifactStorage, s3_admin, s3_prefix: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #899, item 3: botocore retries a timed-out attempt, and the retry can end in a ``ClientError`` even though the first attempt
    stored the object. A ``ClientError`` is therefore no proof that nothing was stored: the object is removed in the background."""
    from botocore.exceptions import ClientError

    real = store._client.put_object

    async def stored_then_refused_on_retry(**kwargs):
        await real(**kwargs)                         # the first attempt reached the server and stored the object, then timed out
        raise ClientError({"Error": {"Code": "SlowDown", "Message": "Please reduce your request rate."}}, "PutObject")

    monkeypatch.setattr(store._client, "put_object", stored_then_refused_on_retry)
    with pytest.raises(ClientError, match="SlowDown"):
        await store.put(data=b"maybe", mime_type="text/plain", filename="m.txt")
    async with asyncio.timeout(10):
        await store.drain()

    assert s3_keys_under(s3_admin, s3_prefix) == []


async def test_a_put_that_was_never_sent_schedules_no_removal(s3_endpoint: str, s3_prefix: str, isolated_aws_env: None) -> None:
    """Only a failure raised before any request was sent (no credentials, refused parameters) proves nothing was stored."""
    from botocore.exceptions import NoCredentialsError

    s3 = S3ArtifactStorage(_config(s3_endpoint, s3_prefix, access_key=None, secret_key=None))
    try:
        with pytest.raises(NoCredentialsError):
            await s3.put(data=b"x", mime_type="text/plain")
        assert not s3._pending
    finally:
        await s3.aclose()


async def test_closing_a_store_waits_for_the_cleanup_of_a_cancelled_put(
    store: S3ArtifactStorage, s3_admin, s3_prefix: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store closed right after a cancelled put (an eviction, a race-built store released) would otherwise close its client under the
    cleanup and leave the object behind."""
    entered, release = asyncio.Event(), asyncio.Event()
    real = store._client.put_object

    async def held(**kwargs):
        entered.set()
        await release.wait()
        return await real(**kwargs)

    monkeypatch.setattr(store._client, "put_object", held)
    put = asyncio.create_task(store.put(data=b"cancelled", mime_type="text/plain"))
    async with asyncio.timeout(10):
        await entered.wait()
    put.cancel()
    with pytest.raises(asyncio.CancelledError):
        await put
    asyncio.get_running_loop().call_later(0.05, release.set)

    async with asyncio.timeout(10):
        await store.aclose()

    assert s3_keys_under(s3_admin, s3_prefix) == []


async def test_initialize_opens_one_client_however_often_it_is_called(
    s3_endpoint: str, s3_prefix: str, isolated_aws_env: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiobotocore.session import AioSession

    created = []
    real = AioSession.create_client

    def counting(self, *args, **kwargs):
        created.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(AioSession, "create_client", counting)
    s3 = S3ArtifactStorage(_config(s3_endpoint, s3_prefix))
    try:
        await asyncio.gather(s3.initialize(), s3.initialize(), s3.initialize())
        await s3.initialize()
        assert len(created) == 1
    finally:
        await s3.aclose()


async def test_a_closed_store_refuses_to_be_used(s3_endpoint: str, s3_prefix: str, isolated_aws_env: None) -> None:
    """A store the registry closed (an eviction) must not open a new client that nothing would ever close."""
    s3 = S3ArtifactStorage(_config(s3_endpoint, s3_prefix))
    await s3.initialize()
    await s3.aclose()
    await s3.aclose()

    assert s3.closed
    with pytest.raises(RuntimeError, match="closed"):
        await s3.put(data=b"x", mime_type="text/plain")
    with pytest.raises(RuntimeError, match="closed"):
        await s3.initialize()


async def test_a_store_that_was_never_initialized_initializes_on_first_use(s3_endpoint: str, s3_prefix: str, isolated_aws_env: None) -> None:
    s3 = S3ArtifactStorage(_config(s3_endpoint, s3_prefix))
    try:
        aid = await s3.put(data=b"lazy", mime_type="text/plain")
        assert (await s3.get(aid)).data == b"lazy"
    finally:
        await s3.aclose()


async def test_the_secret_is_never_logged(
    s3_endpoint: str, s3_prefix: str, isolated_aws_env: None, caplog: pytest.LogCaptureFixture, sp,
) -> None:
    """Ruling 7: every logger, at DEBUG, through a working round trip, a failed put and a failed probe. The secret appears in no record.

    primer keeps botocore and aiobotocore at INFO (``configure_logging``, the next test), so this test turns their DEBUG on itself: even then,
    with about a hundred records a round trip captured, the secret key is in none (SigV4 sends only a signature derived from it)."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="botocore")
    caplog.set_level(logging.DEBUG, logger="aiobotocore")
    s3 = S3ArtifactStorage(_config(s3_endpoint, s3_prefix, access_key=ACCESS_KEY, secret_key=SECRET))
    try:
        aid = await s3.put(data=b"x", mime_type="text/plain", filename="x.txt")
        await s3.get(aid)
        await s3.delete(aid)
    finally:
        await s3.aclose()
    broken = S3ArtifactStorage(_config(s3_endpoint, s3_prefix, bucket="no-such-bucket", access_key=ACCESS_KEY, secret_key=SECRET))
    try:
        with pytest.raises(Exception):
            await broken.put(data=b"x", mime_type="text/plain")
    finally:
        await broken.aclose()
    result = await probe_artifact_provider(
        _s3_row("s3-log", bucket="no-such-bucket", endpoint_url=s3_endpoint, access_key=ACCESS_KEY, secret_key=SECRET), storage_provider=sp,
    )

    assert not result.ok
    botocore_debug = [r for r in caplog.records if r.name.split(".")[0] in ("botocore", "aiobotocore") and r.levelno == logging.DEBUG]
    assert len(botocore_debug) > 20, "botocore's DEBUG records were captured, so the check below reads them"
    assert any("Sending http request" in r.getMessage() for r in botocore_debug), "the signed requests themselves were logged"
    assert SECRET not in caplog.text
    assert not [r for r in caplog.records if SECRET in r.getMessage() or SECRET in str(r.args)]
    assert SECRET not in result.error and ACCESS_KEY not in result.error
    assert SECRET not in repr(s3) and SECRET not in repr(s3.config)


@pytest.fixture
def primer_logging_to(tmp_path: Path):
    """primer's own logging setup (``configure_logging``) at DEBUG, written to a file; the root and library logger state is put back after."""
    from primer.common.log import configure_logging

    root = logging.getLogger()
    names = ("botocore", "aiobotocore", "aiosqlite", "asyncio", "httpcore", "httpx")
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_levels = [logging.getLogger(n).level for n in names]
    path = tmp_path / "primer.log"
    configure_logging(level=logging.DEBUG, json_format=False, file_path=path)
    try:
        yield path
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        for name, level in zip(names, saved_levels, strict=True):
            logging.getLogger(name).setLevel(level)


async def test_primer_at_debug_writes_no_signature_key_id_or_filename_header(
    primer_logging_to: Path, s3_endpoint: str, s3_prefix: str, isolated_aws_env: None,
) -> None:
    """Review of #899, item 4: with the application logging at DEBUG, a put and a get write no SigV4 signature, no access key id and no
    ``x-amz-meta-filename`` header (botocore's per-request DEBUG lines), and no secret."""
    s3 = S3ArtifactStorage(_config(s3_endpoint, s3_prefix, access_key=ACCESS_KEY, secret_key=SECRET))
    try:
        aid = await s3.put(data=b"x", mime_type="text/plain", filename="secret-report.pdf")
        await s3.get(aid)
        logging.getLogger("primer.test").debug("primer DEBUG lines are written")
    finally:
        await s3.aclose()
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = primer_logging_to.read_text(encoding="utf-8")

    assert "primer DEBUG lines are written" in text, "the file is the DEBUG output, so the checks below read it"
    for leaked in ("Signature=", ACCESS_KEY, "x-amz-meta-filename", "secret-report.pdf", SECRET):
        assert leaked not in text, leaked


# ---- through the routing store (the #869 rules hold for S3) ------------------------------------------------------------------------------------


async def _routing_on(sp, store: S3ArtifactStorage, row: ArtifactStorageProvider) -> RoutingArtifactStorage:
    await sp.get_storage(ArtifactStorageProvider).create(row)
    await sp.get_storage(ActiveArtifactStorageConfig).create(
        ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id=row.id),
    )

    async def resolve(provider_id: str) -> S3ArtifactStorage:
        assert provider_id == row.id
        return store

    return RoutingArtifactStorage(
        resolve=resolve,
        fallback=DbArtifactStorage(sp),
        locations=sp.get_storage(ArtifactLocation),
        active_config=sp.get_storage(ActiveArtifactStorageConfig),
        providers=sp.get_storage(ArtifactStorageProvider),
    )


async def test_a_put_through_the_routing_store_records_the_provider_and_reads_back(
    store: S3ArtifactStorage, s3_endpoint: str, s3_prefix: str, sp,
) -> None:
    row = _s3_row("s3-routed", prefix=s3_prefix, endpoint_url=s3_endpoint, access_key=S3_TEST_KEY, secret_key=S3_TEST_SECRET)
    routing = await _routing_on(sp, store, row)

    aid = await routing.put(data=b"routed", mime_type="text/plain", filename="r.txt")

    assert (await sp.get_storage(ArtifactLocation).get(aid)).provider_id == "s3-routed"
    assert await routing.get(aid) == ArtifactBlob(data=b"routed", mime_type="text/plain", filename="r.txt")
    await routing.delete(aid)
    assert await routing.get(aid) is None


@pytest.mark.parametrize("moved", [{"bucket": "elsewhere"}, {"prefix": "elsewhere/"}, {"endpoint_url": "http://elsewhere:9000"}, {"region": "eu-west-1"}])
async def test_a_put_whose_row_moved_meanwhile_leaves_no_object_and_no_record(
    store: S3ArtifactStorage, s3_endpoint: str, s3_prefix: str, sp, s3_admin, moved: dict,
) -> None:
    """The store was built from the row before an edit moved it, and the edit landed while this put was in flight (no record yet, so the move
    check let it through): the put removes its object and fails with a 409, instead of handing out an id the moved row cannot read."""
    row = _s3_row(
        "s3-moved", **{"prefix": s3_prefix, "endpoint_url": s3_endpoint, "access_key": S3_TEST_KEY, "secret_key": S3_TEST_SECRET, **moved},
    )
    routing = await _routing_on(sp, store, row)

    with pytest.raises(ConflictError, match="was moved to another location while this upload was being written"):
        await routing.put(data=b"moved", mime_type="text/plain")

    assert s3_keys_under(s3_admin, s3_prefix) == []
    assert (await sp.get_storage(ArtifactLocation).list(OffsetPage(length=10))).total == 0
