"""The memory entry store (agent memory Task 3): playbook entries as documents of the agent's memory collection.

An entry is the document ``<folder>/<slug>`` in ``mem-<agent id>``, ``folder`` one of ``active``, ``candidates`` and ``archived``; its title is its name,
its body the rendered :class:`~primer.memory.entry.EntryBody`, and everything else (description, counts, provenance, revisions) is document ``meta``.
Promotion and archiving are a document move. Every meta change is a field-scoped ``patch_if`` of the leaves it owns (a counter is compare-and-set), so a
curator run and a reflection cannot overwrite each other. Each write emits its ``memory.*`` event with ``entity_kind="agent"``.

This is the only writer of the collection besides the curator and the purge; the generic doors refuse it (Task 2).

Crash-replay guard (EXEC2-03): a reflect run that crashes between a delta application and its mark is re-claimed and re-applied, and the credit path is
not idempotent (a replay double-credits by one). The applies of a run are therefore recorded per entry in ``meta['applied_runs']`` (a map of the run's
key to the delta keys it applied to that entry), written in the SAME statement as the apply itself, and an already-recorded delta is skipped: a replay
of a crashed run is a pure no-op at the storage level.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from primer.memory.collection import memory_collection_id
from primer.memory.entry import (
    MEMORY_FOLDERS,
    EntryBody,
    apply_edits,
    body_from_add,
    parse_body,
    render_body,
    slugify_name,
)
from primer.model.collection import Document
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.memory import MemoryAdd, MemoryPatch

logger = logging.getLogger(__name__)

Status = Literal["active", "candidates", "archived"]
#: Revisions kept per entry (spec 4 and 6.6).
MAX_REVISIONS = 10
_CAS_ATTEMPTS = 8


@dataclass
class MemoryEntry:
    name: str
    status: Status
    description: str
    body: EntryBody
    meta: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None   # the document's last write (meta-only writes do not move it)

    def as_dict(self) -> dict[str, Any]:
        """The entry as the routes and tools show it: the body's items as ``{id, text}``."""
        items = lambda pairs: [{"id": i, "text": t} for i, t in pairs]  # noqa: E731
        return {
            "name": self.name, "status": self.status, "description": self.description, "meta": self.meta,
            "body": {"steps": items(self.body.steps), "pitfalls": items(self.body.pitfalls), "example": items(self.body.example)},
        }


def _now() -> datetime:
    return datetime.now(UTC)


def _worker(agent_id: str) -> str:
    return f"memory:{agent_id}"


def _applied_key(meta: dict[str, Any], run_key: str | None, delta_key: str) -> bool:
    """Whether the reflect run ``run_key`` already applied this delta to an entry with this ``meta`` (the crash-replay guard)."""
    if run_key is None:
        return False
    return delta_key in ((meta.get("applied_runs") or {}).get(run_key) or [])


def _record_applied(meta: dict[str, Any], run_key: str, delta_key: str) -> dict[str, list[str]]:
    """The ``applied_runs`` map of the entry's CURRENT ``meta`` with this delta recorded under this run (written in the same statement as the
    apply; merged against the latest row on every CAS attempt)."""
    applied = dict(meta.get("applied_runs") or {})
    applied[run_key] = [*(applied.get(run_key) or []), delta_key]
    return applied


class MemoryStore:
    def __init__(self, sp: Any, documents: Any, recorder: Any) -> None:
        self._sp = sp
        self._documents = documents
        self._recorder = recorder
        self._docs = sp.get_storage(Document)

    # ---- reads ---------------------------------------------------------------------------------------------------------------------------

    async def _read(
        self, agent_id: str, status: Status, slug: str
    ) -> MemoryEntry | None:
        try:
            got = await self._documents.read(
                collection_id=memory_collection_id(agent_id), path=f"{status}/{slug}"
            )
        except NotFoundError:
            return None
        meta = dict(got.document.meta or {})
        return MemoryEntry(
            name=got.document.title or slug,
            status=status,
            description=meta.get("description", ""),
            body=parse_body(got.content),
            meta=meta,
            updated_at=got.document.updated_at,
        )

    async def _find(self, agent_id: str, name: str) -> MemoryEntry | None:
        """The entry called ``name`` in whichever folder holds it, else None."""
        slug = slugify_name(name)
        if slug is None:
            return None
        for status in MEMORY_FOLDERS:
            entry = await self._read(agent_id, status, slug)
            if entry is not None:
                return entry
        return None

    async def get(self, agent_id: str, name: str) -> MemoryEntry | None:
        return await self._find(agent_id, name)

    async def list_entries(
        self, agent_id: str, status: Status | None = None
    ) -> list[MemoryEntry]:
        out: list[MemoryEntry] = []
        for folder in (status,) if status else MEMORY_FOLDERS:
            for item in await self._documents.list(
                collection_id=memory_collection_id(agent_id), prefix=f"{folder}/"
            ):
                entry = await self._read(
                    agent_id, folder, item.path.removeprefix(f"{folder}/")
                )
                if entry is not None:
                    out.append(entry)
        return out

    # ---- field-scoped meta writes --------------------------------------------------------------------------------------------------------

    async def _doc_id(self, agent_id: str, status: Status, slug: str) -> str | None:
        return await self._sp.get_content_store().resolve_id(
            memory_collection_id(agent_id), f"{status}/{slug}"
        )

    async def _set_meta(self, doc_id: str, leaves: dict[str, Any]) -> None:
        """Set the named ``meta`` leaves and nothing else of the row."""
        await self._docs.patch_if(
            doc_id,
            set_paths={("meta", key): value for key, value in leaves.items()},
            where=await self._anchor(doc_id),
        )

    async def _anchor(self, doc_id: str) -> dict:
        """An always-true guard on the row's collection (``patch_if`` needs a non-empty ``where``)."""
        row = await self._docs.get(doc_id)
        if row is None:
            raise NotFoundError(f"document {doc_id!r} does not exist")
        return {"collection_id": [row.collection_id]}

    async def _bump(
        self, doc_id: str, key: str, extra: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    ) -> None:
        """Increment the counter ``meta[key]`` by compare-and-set, so concurrent credits are not lost. ``extra`` maps the row's CURRENT ``meta``
        (fresh on every attempt) to the leaves set in the same statement, so the crash-replay's applied-delta record is merged against the latest
        row and lands exactly when the counter it guards does."""
        for _ in range(_CAS_ATTEMPTS):
            row = await self._docs.get(doc_id)
            if row is None:
                raise NotFoundError(f"document {doc_id!r} does not exist")
            current = int((row.meta or {}).get(key, 0) or 0)
            extra_leaves = extra(row.meta or {}) if extra is not None else {}
            done = await self._docs.patch_if(
                doc_id,
                set_paths={
                    ("meta", key): current + 1,
                    **{("meta", k): v for k, v in extra_leaves.items()},
                },
                where={("meta", key): [current]},
            )
            if done is not None:
                return
        raise ConflictError(
            f"could not update the {key!r} count of {doc_id!r}: it keeps changing"
        )

    # ---- events --------------------------------------------------------------------------------------------------------------------------

    async def _emit_event(
        self, event_type: str, agent_id: str, actor: str, payload: dict[str, Any]
    ) -> None:
        await self._recorder.emit(
            event_type,
            payload=payload,
            actor=actor,
            entity_kind="agent",
            entity_id=agent_id,
        )

    # ---- moves ---------------------------------------------------------------------------------------------------------------------------

    async def _move(self, agent_id: str, entry: MemoryEntry, to: Status) -> None:
        slug = slugify_name(entry.name) or entry.name
        await self._documents.move(
            collection_id=memory_collection_id(agent_id),
            src=f"{entry.status}/{slug}",
            dst=f"{to}/{slug}",
        )

    # ---- writes by the worker ------------------------------------------------------------------------------------------------------------

    async def add_candidate(
        self,
        agent_id: str,
        add: MemoryAdd,
        *,
        source: dict,
        taught_by: str,
        needs_approval: bool,
    ) -> MemoryEntry | None:
        """A new candidate, or None when the name does not slugify, already names an entry in any folder, or the body is over the cap."""
        slug = slugify_name(add.name)
        if slug is None or await self._find(agent_id, slug) is not None:
            return None
        try:
            content = render_body(body_from_add(add))
        except ValueError:
            logger.info(
                "memory %s: a new entry %r was dropped: its body is over the cap",
                agent_id,
                slug,
            )
            return None
        meta = {
            "description": add.description,
            "topics": list(add.topics),
            "helpful": 0,
            "harmful": 0,
            "sources": [source],
            "revisions": [],
            "last_validated_at": None,
            "pinned": False,
            "needs_approval": needs_approval,
            "taught_by": taught_by,
            "created_at": _now().isoformat(),
        }
        await self._documents.upsert(
            collection_id=memory_collection_id(agent_id),
            path=f"candidates/{slug}",
            content=content,
            title=slug,
            meta=meta,
        )
        await self._emit_event(
            "memory.entry.added",
            agent_id,
            _worker(agent_id),
            {"name": slug, "status": "candidates", "taught_by": taught_by},
        )
        return await self._read(agent_id, "candidates", slug)

    async def patch(
        self, agent_id: str, patch: MemoryPatch, *, source: dict, run_key: str | None = None
    ) -> MemoryEntry | None:
        """Apply the item edits as a new revision; None when the entry is missing or archived (it is never created or revived by a patch).

        ``run_key`` is the crash-replay guard (see :meth:`credit`): a patch this run already applied to the entry is skipped rather than re-applied as a
        redundant revision, and the record is written with the body it guards.
        """
        entry = await self._find(agent_id, patch.name)
        if entry is None or entry.status == "archived":
            return None
        slug = slugify_name(patch.name) or patch.name
        if _applied_key(entry.meta or {}, run_key, f"patch:{slug}"):
            return entry
        try:
            content = render_body(apply_edits(entry.body, patch.edits))
        except ValueError:
            logger.info(
                "memory %s: a patch of %r was dropped: the body would be over the cap",
                agent_id,
                entry.name,
            )
            return None
        extra = {"sources": [*entry.meta.get("sources", []), source]}
        if run_key is not None:
            extra["applied_runs"] = _record_applied(entry.meta or {}, run_key, f"patch:{slug}")
        if not await self._write_body(
            agent_id,
            entry,
            content,
            extra=extra,
        ):
            return None
        await self._emit_event(
            "memory.entry.updated",
            agent_id,
            _worker(agent_id),
            {"name": entry.name, "status": entry.status, "change": "patch"},
        )
        return await self._read(agent_id, entry.status, entry.name)

    async def _write_body(
        self,
        agent_id: str,
        entry: MemoryEntry,
        content: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Replace the body and push the old one onto ``revisions`` (the last ``MAX_REVISIONS``), by document id and only while the entry is still in
        the folder it was read from; False when it moved or went since, so a concurrent curator's move is never undone or the entry re-created."""
        slug = slugify_name(entry.name) or entry.name
        doc_id = await self._doc_id(agent_id, entry.status, slug)
        if doc_id is None:
            return False
        revisions = [
            *entry.meta.get("revisions", []),
            {"body": render_body(entry.body), "at": _now().isoformat()},
        ][-MAX_REVISIONS:]
        written = await self._documents.write_body_at(
            document_id=doc_id,
            path=f"{entry.status}/{slug}",
            content=content,
            meta_set={"revisions": revisions, **(extra or {})},
        )
        return written is not None

    async def credit(
        self,
        agent_id: str,
        name: str,
        verdict: Literal["helpful", "harmful"],
        *,
        run_succeeded: bool,
        trial: bool,
        at: datetime,
        trigger_run: bool = False,
        run_key: str | None = None,
    ) -> MemoryEntry | None:
        """Count the verdict (``trigger_run``: the session was a trigger's, which keeps the entry from going stale; see the curator). A candidate that was helpful in a successful trial run (and needs no approval) is promoted; a harmful candidate is archived.
        None when the entry is missing or archived.

        ``run_key`` (the reflect run's ``<job id>:<watermark>``) is the crash-replay guard: a delta this run already applied to the entry is a no-op, so a
        re-claim after a crash between the apply and the mark cannot double-credit; the record is written in the same statement as the counter, so the
        guard is durable exactly when the credit is.
        """
        entry = await self._find(agent_id, name)
        if entry is None or entry.status == "archived":
            return None
        slug = slugify_name(entry.name) or entry.name
        delta_key = f"credit:{slug}:{verdict}"
        if _applied_key(entry.meta or {}, run_key, delta_key):
            return entry
        doc_id = await self._doc_id(agent_id, entry.status, slug)
        if doc_id is None:
            return None
        if run_key is None:
            await self._bump(doc_id, verdict)
        else:
            await self._bump(doc_id, verdict, extra=lambda meta: {"applied_runs": _record_applied(meta, run_key, delta_key)})
        actor = _worker(agent_id)
        if verdict == "helpful" and entry.status == "active":
            await self._set_meta(doc_id, {"last_validated_at": at.isoformat()})
        if trigger_run:
            await self._set_meta(doc_id, {"last_trigger_use_at": at.isoformat()})
        await self._emit_event(
            "memory.entry.updated",
            agent_id,
            actor,
            {"name": entry.name, "status": entry.status, "change": f"credit:{verdict}"},
        )
        if entry.status == "candidates":
            if (
                verdict == "helpful"
                and trial
                and run_succeeded
                and not entry.meta.get("needs_approval")
            ):
                await self._move(agent_id, entry, "active")
                await self._emit_event(
                    "memory.entry.promoted", agent_id, actor, {"name": entry.name}
                )
            elif verdict == "harmful":
                await self._move(agent_id, entry, "archived")
                await self._emit_event(
                    "memory.entry.archived",
                    agent_id,
                    actor,
                    {"name": entry.name, "reason": "harmful in a trial run"},
                )
        return await self._find(agent_id, entry.name)

    async def write_snapshot(self, agent_id: str, path: str, text: str) -> None:
        """A document outside the three folders (the curator's snapshots). Not indexed: the agent's searches never reach it."""
        from primer.knowledge.document_service import DocumentService

        await DocumentService(self._sp).upsert(collection_id=memory_collection_id(agent_id), path=path, content=text, title=path)

    # ---- writes by an owner --------------------------------------------------------------------------------------------------------------

    async def _require(self, agent_id: str, name: str) -> MemoryEntry:
        entry = await self._find(agent_id, name)
        if entry is None:
            raise NotFoundError(f"memory entry {name!r} does not exist")
        return entry

    async def approve(self, agent_id: str, name: str, *, actor: str) -> MemoryEntry:
        """Move a candidate to active and clear its ``needs_approval``. An active entry is returned as it is; an archived one cannot be approved."""
        entry = await self._require(agent_id, name)
        if entry.status == "active":
            return entry
        if entry.status == "archived":
            raise ConflictError(
                f"memory entry {entry.name!r} is archived; it cannot be approved"
            )
        await self._move(agent_id, entry, "active")
        doc_id = await self._doc_id(
            agent_id, "active", slugify_name(entry.name) or entry.name
        )
        if doc_id is not None:
            await self._set_meta(doc_id, {"needs_approval": False})
        await self._emit_event(
            "memory.entry.approved", agent_id, actor, {"name": entry.name}
        )
        return await self._require(agent_id, entry.name)

    async def archive(
        self, agent_id: str, name: str, *, actor: str, reason: str, merged_into: str | None = None
    ) -> MemoryEntry:
        """Move the entry to archived (``merged_into``: the entry that took its place, kept in meta as a pointer)."""
        entry = await self._require(agent_id, name)
        if entry.status != "archived":
            await self._move(agent_id, entry, "archived")
            if merged_into is not None:
                doc_id = await self._doc_id(agent_id, "archived", slugify_name(entry.name) or entry.name)
                if doc_id is not None:
                    await self._set_meta(doc_id, {"merged_into": merged_into})
            await self._emit_event(
                "memory.entry.archived",
                agent_id,
                actor,
                {"name": entry.name, "reason": reason},
            )
        return await self._require(agent_id, entry.name)

    async def set_pinned(
        self, agent_id: str, name: str, pinned: bool, *, actor: str
    ) -> MemoryEntry:
        entry = await self._require(agent_id, name)
        if entry.status != "active":
            raise ConflictError(
                f"memory entry {entry.name!r} is {entry.status}; only an active entry can be pinned"
            )
        doc_id = await self._doc_id(
            agent_id, "active", slugify_name(entry.name) or entry.name
        )
        if doc_id is not None:
            await self._set_meta(doc_id, {"pinned": pinned})
        await self._emit_event(
            "memory.entry.updated",
            agent_id,
            actor,
            {
                "name": entry.name,
                "status": "active",
                "change": "pinned" if pinned else "unpinned",
            },
        )
        return await self._require(agent_id, entry.name)

    async def edit(
        self,
        agent_id: str,
        name: str,
        *,
        description: str | None = None,
        body: EntryBody | None = None,
        actor: str,
    ) -> MemoryEntry:
        entry = await self._require(agent_id, name)
        if body is not None:
            # a ValueError over the cap reaches the caller
            if not await self._write_body(agent_id, entry, render_body(body)):
                raise NotFoundError(
                    f"memory entry {entry.name!r} moved while it was being edited"
                )
        if description is not None:
            doc_id = await self._doc_id(
                agent_id, entry.status, slugify_name(entry.name) or entry.name
            )
            if doc_id is not None:
                await self._set_meta(doc_id, {"description": description})
        await self._emit_event(
            "memory.entry.updated",
            agent_id,
            actor,
            {"name": entry.name, "status": entry.status, "change": "edit"},
        )
        return await self._require(agent_id, entry.name)


__all__ = ["MAX_REVISIONS", "MemoryEntry", "MemoryStore", "Status"]
