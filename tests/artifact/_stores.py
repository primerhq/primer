"""Test doubles for the artifact-storage tests. The module name starts with an underscore, so pytest does not collect it."""

from __future__ import annotations

from uuid import uuid4

from primer.int.artifact_storage import ArtifactBlob, ArtifactStorage


class MemStore(ArtifactStorage):
    """A dict-backed backend that is NOT the database, so, like the filesystem and S3 stores, it needs a location record.

    ``blobs`` holds what was written, ``closed`` says whether ``aclose`` ran, and ``corrupt_reads`` makes ``get`` hand back bytes that differ from
    the stored ones (a backend that reads back the wrong object).
    """

    needs_location_record = True

    def __init__(self, *, corrupt_reads: bool = False) -> None:
        self.blobs: dict[str, ArtifactBlob] = {}
        self.closed = False
        self.corrupt_reads = corrupt_reads

    async def initialize(self) -> None:
        return None

    async def put(self, *, data: bytes, mime_type: str, filename: str | None = None) -> str:
        artifact_id = "artifact-" + uuid4().hex
        self.blobs[artifact_id] = ArtifactBlob(data=data, mime_type=mime_type, filename=filename)
        return artifact_id

    async def get(self, artifact_id: str) -> ArtifactBlob | None:
        blob = self.blobs.get(artifact_id)
        if blob is None:
            return None
        if self.corrupt_reads:
            return ArtifactBlob(data=blob.data + b"\x00corrupt", mime_type=blob.mime_type, filename=blob.filename)
        return blob

    async def delete(self, artifact_id: str) -> None:
        self.blobs.pop(artifact_id, None)

    async def aclose(self) -> None:
        self.closed = True
