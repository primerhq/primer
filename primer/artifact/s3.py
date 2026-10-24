"""The S3 artifact store: artifacts as objects in an S3 or S3-compatible (MinIO) bucket (plan 09, Task 9; design section 15.2).

Layout. Each artifact is one object at ``<prefix><artifact id>``: the bytes are its body, the MIME type its ``Content-Type``, and the filename its
``filename`` user metadata, percent-encoded (``quote(filename, safe="")``) because S3 user metadata is ASCII only; a put without a filename writes
no metadata. Ids are backend ids (:mod:`primer.artifact.ids`); an id in any other format is never turned into a key, so ``get`` answers ``None``
and ``delete`` does nothing for it, without a request.

The extra. aiobotocore (the ``s3`` extra) is imported by :meth:`S3ArtifactStorage.initialize` only, never at module import: a core install imports
this module (the factory builds the store object, which does no IO) and must not need the extra. The import-linter "core layers" contract keeps
the core packages from importing this module at all.

Credentials. Both keys or neither. With both, the client signs with them; with neither, botocore's ambient chain is used (environment, shared
config files, the instance or pod role). A half pair is refused when the store is built (:func:`s3_client_kwargs`), never completed from the
ambient chain, and the REST route and the system tools refuse to store one (``primer/artifact/checks.py``). The keys are ``SecretStr`` on the row
and reach botocore only through :func:`s3_client_kwargs`; this module never logs them.

The client. One per store, opened by ``initialize`` inside an ``AsyncExitStack`` and closed by ``aclose``. A closed store stays closed: an
operation on it raises, it never opens a client that nothing would close. An operation on a store that was not initialized initializes it.

A put that does not complete (cancelled while its request is in flight, or failed in any way but one raised before a request was sent) never
hands out its id, so the object it may have left is removed in the background once the request settles; ``aclose`` waits for those removals.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, ClassVar, Literal
from urllib.parse import quote, unquote

from primer.artifact.ids import is_backend_artifact_id, new_backend_artifact_id
from primer.int.artifact_storage import ArtifactBlob, ArtifactStorage
from primer.model.except_ import ConfigError
from primer.model.providers.artifact import (
    S3_KEYS_INCOMPLETE_MESSAGE,
    ArtifactStorageProvider,
    ArtifactStorageProviderType,
    S3ArtifactConfig,
)

logger = logging.getLogger(__name__)

#: The error codes S3 answers for a key that is not there (``NoSuchKey`` for a read, ``404`` / ``NotFound`` for a head).
_MISSING_CODES = frozenset({"NoSuchKey", "404", "NotFound"})
_FILENAME_META = "filename"
#: How long ``aclose`` waits for the removals of puts that did not complete before it closes the client anyway.
_DRAIN_TIMEOUT_S = 30.0
_CONNECT_TIMEOUT_S = 10
_READ_TIMEOUT_S = 60
_RETRIES = {"max_attempts": 3, "mode": "standard"}


def s3_addressing_style(config: S3ArtifactConfig) -> Literal["auto", "path", "virtual"]:
    """``path`` or ``virtual`` as set; ``auto`` is ``path`` when ``endpoint_url`` is set (MinIO and most S3-compatible servers) and botocore's own
    ``auto`` on AWS."""
    if config.addressing_style != "auto":
        return config.addressing_style
    return "path" if config.endpoint_url else "auto"


def s3_client_kwargs(config: S3ArtifactConfig) -> dict[str, Any]:
    """The arguments of ``create_client("s3", ...)``: ``region_name``, ``endpoint_url`` only when set, and the two keys only when BOTH are set.

    With neither key botocore uses its ambient chain. A half pair raises :class:`ConfigError` (the text names the blank key, never a value): with
    one key dropped, botocore would sign with whatever ambient credentials the server has.
    """
    missing = config.missing_key()
    if missing is not None:
        raise ConfigError(f"S3 artifact store on bucket {config.bucket!r}: {S3_KEYS_INCOMPLETE_MESSAGE}; {missing} is blank")
    kwargs: dict[str, Any] = {"region_name": config.region}
    if config.endpoint_url:
        kwargs["endpoint_url"] = config.endpoint_url
    if config.access_key is not None and config.secret_key is not None:
        kwargs["aws_access_key_id"] = config.access_key.get_secret_value()
        kwargs["aws_secret_access_key"] = config.secret_key.get_secret_value()
    return kwargs


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None) or {}
    return str((response.get("Error") or {}).get("Code", ""))


def _never_sent(exc: BaseException) -> bool:
    """True only for a put failure raised before any request was sent (no credentials, partial credentials, a parameter botocore refused):
    the one kind of failure that proves nothing was stored. Any other may have come after the server stored the object, a ``ClientError``
    included: botocore retries a timed-out attempt, and the retry can be refused after the first attempt stored it (the review of #899)."""
    from botocore.exceptions import NoCredentialsError, ParamValidationError, PartialCredentialsError

    return isinstance(exc, (NoCredentialsError, PartialCredentialsError, ParamValidationError))


class S3ArtifactStorage(ArtifactStorage):
    """Artifacts as objects under ``prefix`` in ``bucket``. Its ids are not database ids, so the routing store records them."""

    needs_location_record: ClassVar[bool] = True

    def __init__(self, config: S3ArtifactConfig) -> None:
        # No IO. A half pair is refused here, so a row stored before the write check refused it fails at build, not with ambient credentials.
        s3_client_kwargs(config)
        self._config = config
        self._lock = asyncio.Lock()
        self._stack: contextlib.AsyncExitStack | None = None
        self._client: Any = None
        self._closed = False
        # Removals of the objects of puts that did not complete; the event loop keeps only weak references to tasks.
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def config(self) -> S3ArtifactConfig:
        return self._config

    @property
    def bucket(self) -> str:
        return self._config.bucket

    @property
    def prefix(self) -> str:
        return self._config.prefix

    @property
    def closed(self) -> bool:
        return self._closed

    def serves(self, row: ArtifactStorageProvider) -> bool:
        """True for an S3 row with this store's bucket, prefix, endpoint and region: the four values that say where its objects are. New keys or
        another addressing style reach the same objects."""
        if row.provider is not ArtifactStorageProviderType.S3:
            return False
        mine, theirs = self._config, row.config
        return (theirs.bucket, theirs.prefix, theirs.endpoint_url, theirs.region) == (mine.bucket, mine.prefix, mine.endpoint_url, mine.region)

    def key_for(self, artifact_id: str) -> str:
        """``prefix + artifact_id``. Raises ``ValueError`` for an id that is not a backend artifact id, before any key is built."""
        if not is_backend_artifact_id(artifact_id):
            raise ValueError(f"{artifact_id!r} is not a backend artifact id")
        return self._config.prefix + artifact_id

    def _key_or_none(self, artifact_id: str, verb: str) -> str | None:
        try:
            return self.key_for(artifact_id)
        except ValueError:
            logger.warning("S3 artifact store on bucket %r: refused to %s %r, which is not a backend artifact id", self.bucket, verb, artifact_id)
            return None

    def _closed_error(self) -> RuntimeError:
        return RuntimeError(f"the S3 artifact store on bucket {self.bucket!r} is closed")

    async def initialize(self) -> None:
        """Open the client: imports aiobotocore, and is idempotent (concurrent calls open one client). Raises on a closed store."""
        async with self._lock:
            if self._closed:
                raise self._closed_error()
            if self._client is not None:
                return
            from aiobotocore.config import AioConfig
            from aiobotocore.session import get_session

            client_config = AioConfig(
                s3={"addressing_style": s3_addressing_style(self._config)},
                connect_timeout=_CONNECT_TIMEOUT_S,
                read_timeout=_READ_TIMEOUT_S,
                retries=dict(_RETRIES),
            )
            stack = contextlib.AsyncExitStack()
            try:
                self._client = await stack.enter_async_context(
                    get_session().create_client("s3", config=client_config, **s3_client_kwargs(self._config)),
                )
            except BaseException:
                await stack.aclose()
                raise
            self._stack = stack

    async def _ready(self) -> Any:
        if self._client is None:
            await self.initialize()
        client = self._client
        if client is None or self._closed:
            raise self._closed_error()
        return client

    async def put(self, *, data: bytes, mime_type: str, filename: str | None = None) -> str:
        client = await self._ready()
        artifact_id = new_backend_artifact_id()
        key = self.key_for(artifact_id)
        request: dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Body": data, "ContentType": mime_type}
        if filename is not None:
            request["Metadata"] = {_FILENAME_META: quote(filename, safe="")}
        write = asyncio.ensure_future(client.put_object(**request))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            # The caller went away (a client that disconnected mid-upload). The request may still complete on the server, and no one will
            # ever be handed this id: once it settles, remove what it may have written.
            self._remove_when_settled(client, write, key)
            raise
        except Exception as exc:
            if not _never_sent(exc):
                self._remove_when_settled(client, write, key)
            raise
        return artifact_id

    def _remove_when_settled(self, client: Any, write: asyncio.Future[Any], key: str) -> None:
        task = asyncio.ensure_future(self._remove_after(client, write, key))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _remove_after(self, client: Any, write: asyncio.Future[Any], key: str) -> None:
        await asyncio.wait([write])
        if not write.cancelled():
            write.exception()   # retrieve it: no one is waiting for this write any more
        try:
            await client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - best effort: the put already failed for its caller
            logger.warning(
                "S3 artifact store on bucket %r: removing the object %r of a put that did not complete failed: %s", self.bucket, key, exc,
            )

    async def drain(self, timeout: float = _DRAIN_TIMEOUT_S) -> None:
        """Wait (at most ``timeout`` seconds) for the removals of puts that did not complete."""
        pending = set(self._pending)
        if not pending:
            return
        _, not_done = await asyncio.wait(pending, timeout=timeout)
        if not_done:
            logger.warning(
                "S3 artifact store on bucket %r: %d removal(s) of incomplete puts did not finish within %gs", self.bucket, len(not_done), timeout,
            )

    async def get(self, artifact_id: str) -> ArtifactBlob | None:
        key = self._key_or_none(artifact_id, "read")
        if key is None:
            return None
        client = await self._ready()
        from botocore.exceptions import ClientError

        try:
            response = await client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            # Only a missing key reads as a missing artifact. A missing bucket or a refused read is an outage, not an absent file: it is raised.
            if _error_code(exc) in _MISSING_CODES:
                return None
            raise
        async with response["Body"] as stream:
            data = await stream.read()
        filename = (response.get("Metadata") or {}).get(_FILENAME_META)
        return ArtifactBlob(
            data=data,
            mime_type=response.get("ContentType") or "application/octet-stream",
            filename=unquote(filename) if filename is not None else None,
        )

    async def delete(self, artifact_id: str) -> None:
        key = self._key_or_none(artifact_id, "delete")
        if key is None:
            return
        client = await self._ready()
        await client.delete_object(Bucket=self.bucket, Key=key)

    async def aclose(self) -> None:
        """Close the client, after the removals of incomplete puts (bounded by the drain timeout). Idempotent."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
        stack, self._stack = self._stack, None
        try:
            await self.drain()
        finally:
            self._client = None
            if stack is not None:
                await stack.aclose()


__all__ = ["S3ArtifactStorage", "s3_addressing_style", "s3_client_kwargs"]
