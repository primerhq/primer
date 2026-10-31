"""A ``document`` file source is read as the person who creates the workspace or saves the template (access-control P3 SEC, finding 4).

Ports of the reviewer's probes: a workspace create (template mount or override) used to read ANY document, whatever the creator could read, and the
resolver's error named the collection the document really lives in.
"""

from __future__ import annotations

import pytest

from primer.access.person import Person
from primer.model.except_ import NotFoundError
from primer.model.storage import OffsetPage
from primer.model.collection import Document
from primer.model.workspace import FileMount
from primer.workspace.resolvers import make_document_resolver, refuse_unreadable_document_mounts
from tests.api.test_collection_access import CID, env  # noqa: F401

ALICE_P = Person("alice", "user")
DAVE_P = Person("dave", "user")
ROOT_P = Person("root", "admin")


async def _mount(env, collection_id: str = CID) -> FileMount:
    rows = (await env.sp.get_storage(Document).list(OffsetPage(offset=0, length=50))).items
    doc = next(d for d in rows if d.path == "a")
    return FileMount(path="leak.md", source={"kind": "document", "collection_id": collection_id, "document_id": doc.id})


async def test_a_creator_who_cannot_read_the_collection_cannot_mount_its_document(env) -> None:
    fm = await _mount(env)

    with pytest.raises(RuntimeError) as exc:
        await make_document_resolver(env.sp, DAVE_P)(fm)

    assert "not found" in str(exc.value) and CID not in str(exc.value)


async def test_the_owner_and_an_admin_still_mount_it_and_so_does_a_caller_with_no_person(env) -> None:
    fm = await _mount(env)
    for person in (ALICE_P, ROOT_P, None):
        assert await make_document_resolver(env.sp, person)(fm) == b"body a"


async def test_the_resolver_error_does_not_name_the_real_collection(env) -> None:
    fm = await _mount(env, collection_id="nope")

    with pytest.raises(RuntimeError) as exc:
        await make_document_resolver(env.sp, ALICE_P)(fm)

    assert "nope" in str(exc.value) and CID not in str(exc.value)


async def test_a_template_saver_who_cannot_read_the_document_is_refused(env) -> None:
    fm = await _mount(env)

    with pytest.raises(NotFoundError):
        await refuse_unreadable_document_mounts(env.sp, DAVE_P, [fm])
    await refuse_unreadable_document_mounts(env.sp, ALICE_P, [fm])
    await refuse_unreadable_document_mounts(env.sp, ROOT_P, [fm])
