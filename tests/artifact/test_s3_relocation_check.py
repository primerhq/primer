"""GX-14 (option C): an S3 endpoint or region change on a store that holds artifacts is allowed only if the NEW settings can read one of them.

Bucket and prefix still say where the objects are and stay refused while artifacts are held. The endpoint and region may be a renamed host or a
corrected region (the same objects) or a different, empty server (every recorded artifact then reads as missing): the check builds a store from the new
row and looks for one held artifact with a metadata read (no download), within the probe bound. Moto's threaded servers stand in for S3.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

import primer.artifact.checks as checks
from primer.artifact.checks import check_artifact_provider_update
from primer.artifact.s3 import S3ArtifactStorage
from primer.common.entity_checks import EntityCheckError
from primer.model.artifact import ArtifactLocation
from primer.model.provider import ArtifactStorageProvider, SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from tests.artifact.conftest import S3_BUCKET, S3_TEST_KEY, S3_TEST_SECRET

pytestmark = pytest.mark.asyncio


def _row(endpoint: str, prefix: str, **config) -> ArtifactStorageProvider:
    return ArtifactStorageProvider.model_validate({
        "id": "s3-1", "provider": "s3",
        "config": {"bucket": S3_BUCKET, "prefix": prefix, "endpoint_url": endpoint, "access_key": S3_TEST_KEY, "secret_key": S3_TEST_SECRET, **config},
    })


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "gx14.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


@pytest.fixture
async def holding(sp, s3_endpoint, s3_prefix, isolated_aws_env):
    """A store with one real object in the session's moto server and one location record naming it."""
    row = _row(s3_endpoint, s3_prefix)
    store = S3ArtifactStorage(row.config)
    await store.initialize()
    artifact_id = await store.put(data=b"x" * 2048, mime_type="text/plain", filename="a.txt")
    await store.aclose()
    await sp.get_storage(ArtifactLocation).create(ArtifactLocation(id=artifact_id, provider_id="s3-1"))
    return row


@pytest.fixture
def other_server(isolated_aws_env):
    """A different S3 server that holds nothing: it answers every request with 404 (moto's servers in one process share one backend, so a second
    moto server would see the same objects and could not stand for a new, empty host)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Empty(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            body = b"<?xml version=\"1.0\"?><Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message></Error>"
            self.send_response(404)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", "0" if self.command == "HEAD" else str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = do_PUT = do_DELETE = _reply

        def log_message(self, *args) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Empty)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _moved(row: ArtifactStorageProvider, **config) -> ArtifactStorageProvider:
    return row.model_copy(update={"config": row.config.model_copy(update=config)})


# ---- characterization: what is refused and allowed today stays so -------------------------------------------------------------------------------


@pytest.mark.parametrize(("field", "change"), [("config.bucket", {"bucket": "elsewhere"}), ("config.prefix", {"prefix": "other/"})])
async def test_a_bucket_or_prefix_change_with_artifacts_is_still_refused(sp, holding, field, change) -> None:
    with pytest.raises(EntityCheckError) as info:
        await check_artifact_provider_update(sp, _moved(holding, **change), holding)
    assert info.value.code == "artifact_provider_holds_artifacts" and info.value.field == field and info.value.extra == {"count": 1}


async def test_any_location_change_on_a_store_holding_nothing_is_allowed(sp, s3_endpoint, s3_prefix, other_server) -> None:
    row = _row(s3_endpoint, s3_prefix)
    await check_artifact_provider_update(sp, _moved(row, endpoint_url=other_server, region="eu-west-1"), row)
    await check_artifact_provider_update(sp, _moved(row, bucket="b2", prefix="p/"), row)


async def test_new_keys_or_addressing_with_artifacts_need_no_probe(sp, holding, monkeypatch) -> None:
    def no_build(*a, **k):
        raise AssertionError("a keys-only edit must not build a store")

    monkeypatch.setattr("primer.artifact.factory.build_artifact_storage", no_build)
    await check_artifact_provider_update(sp, _moved(holding, access_key="other", secret_key="other", addressing_style="path"), holding)


# ---- the new rule ---------------------------------------------------------------------------------------------------------------------------------


async def test_an_endpoint_change_to_an_empty_server_is_refused_naming_the_count(sp, holding, other_server) -> None:
    with pytest.raises(EntityCheckError) as info:
        await check_artifact_provider_update(sp, _moved(holding, endpoint_url=other_server), holding)
    assert info.value.code == "artifact_provider_holds_artifacts" and info.value.field == "config.endpoint_url"
    assert info.value.extra == {"count": 1}
    assert "1 artifact" in info.value.message and "cannot see" in info.value.message


async def test_an_endpoint_change_that_still_reaches_the_same_objects_is_allowed(sp, holding, s3_endpoint) -> None:
    renamed = s3_endpoint.replace("127.0.0.1", "localhost")
    assert renamed != s3_endpoint
    await check_artifact_provider_update(sp, _moved(holding, endpoint_url=renamed), holding)


async def test_a_region_change_that_still_reaches_the_same_objects_is_allowed(sp, holding) -> None:
    await check_artifact_provider_update(sp, _moved(holding, region="us-west-2"), holding)


async def test_the_probe_reads_metadata_not_the_object(sp, holding, monkeypatch) -> None:
    async def no_get(self, artifact_id):
        raise AssertionError("the check must not download the artifact")

    monkeypatch.setattr(S3ArtifactStorage, "get", no_get)
    await check_artifact_provider_update(sp, _moved(holding, region="us-west-2"), holding)


async def test_the_temporary_store_is_closed_on_both_outcomes(sp, holding, other_server, s3_endpoint) -> None:
    from primer.artifact.factory import build_artifact_storage

    built: list[S3ArtifactStorage] = []

    def recording(row, *, storage_provider):
        store = build_artifact_storage(row, storage_provider=storage_provider)
        built.append(store)
        return store

    with pytest.raises(EntityCheckError):
        await check_artifact_provider_update(sp, _moved(holding, endpoint_url=other_server), holding, build=recording)
    await check_artifact_provider_update(sp, _moved(holding, endpoint_url=s3_endpoint.replace("127.0.0.1", "localhost")), holding, build=recording)
    assert len(built) == 2 and all(s.closed for s in built)


async def test_a_dead_endpoint_is_refused_within_the_probe_bound(sp, holding, monkeypatch) -> None:
    """A server that accepts the connection and never answers: the request is not held for botocore's retries."""
    monkeypatch.setattr(checks, "PROBE_TIMEOUT_S", 0.5)
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    try:
        endpoint = f"http://127.0.0.1:{silent.getsockname()[1]}"
        started = asyncio.get_running_loop().time()
        with pytest.raises(EntityCheckError) as info:
            await check_artifact_provider_update(sp, _moved(holding, endpoint_url=endpoint), holding)
        assert asyncio.get_running_loop().time() - started < 5
        assert info.value.code == "artifact_provider_holds_artifacts"
    finally:
        silent.close()


async def test_serves_and_the_update_check_share_one_definition_of_location() -> None:
    from primer.model.providers.artifact import S3ArtifactConfig

    assert S3ArtifactConfig.LOCATION_FIELDS == ("bucket", "prefix", "endpoint_url", "region")
    assert set(S3ArtifactConfig.VERIFIABLE_FIELDS) <= set(S3ArtifactConfig.LOCATION_FIELDS)
