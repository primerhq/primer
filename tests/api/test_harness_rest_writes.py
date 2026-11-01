"""T115 (GS-7, GS-27): the harness REST writes change only their own fields, racing enqueues cannot both win, and refusals are RFC 7807."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from primer.api.routers.harness import HarnessUpdateBody, update_harness
from primer.model.except_ import ConflictError
from primer.model.harness import Harness, HarnessDirection, HarnessOperation, HarnessStatus
from primer.model.providers.storage import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


def _harness(**kw) -> Harness:
    base = dict(id="hns_t", slug="tt", name="T", git_url="https://example.com/r.git", status=HarnessStatus.READY, created_at=datetime.now(UTC))
    base.update(kw)
    return Harness(**base)


class _AfterFirstRead:
    """Storage proxy: right after the route's first read, another writer (a worker finishing, a second request) writes its own fields."""

    def __init__(self, inner, interfere):
        self._inner, self._interfere, self._fired = inner, interfere, False

    async def get(self, entity_id, **kw):
        row = await self._inner.get(entity_id, **kw)
        if not self._fired:
            self._fired = True
            await self._interfere(self._inner, entity_id)
        return row

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _SP:
    def __init__(self, sp, storage):
        self._sp, self._storage = sp, storage

    def get_storage(self, cls):
        return self._storage if cls is Harness else self._sp.get_storage(cls)


@pytest.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "h.sqlite")))
    await provider.initialize()
    yield provider
    await provider.aclose()


async def test_a_rename_during_an_install_keeps_the_finished_operation_finished(sp):
    real = sp.get_storage(Harness)
    await real.create(_harness(pending_operation=HarnessOperation.INSTALL, status=HarnessStatus.READY))

    async def worker_finishes(inner, entity_id):
        await inner.patch_if(
            entity_id, {"pending_operation": None, "operation_inputs": None, "status": HarnessStatus.INSTALLED.value, "resolved_commit": "c0ffee"},
            where={"pending_operation": [HarnessOperation.INSTALL.value]},
        )

    await update_harness(body=HarnessUpdateBody(name="Renamed"), harness_id="hns_t", sp=_SP(sp, _AfterFirstRead(real, worker_finishes)))

    final = await real.get("hns_t")
    assert final.name == "Renamed"
    assert final.pending_operation is None and final.status is HarnessStatus.INSTALLED and final.resolved_commit == "c0ffee"


async def test_an_enqueue_that_lost_the_race_answers_409_and_writes_nothing(sp):
    from primer.api.routers.harness import fetch_harness

    real = sp.get_storage(Harness)
    await real.create(_harness(status=HarnessStatus.DRAFT))

    async def other_enqueues(inner, entity_id):
        await inner.patch_if(entity_id, {"pending_operation": HarnessOperation.UNINSTALL.value}, where={"pending_operation": [None]})

    with pytest.raises(ConflictError):
        await fetch_harness(harness_id="hns_t", sp=_SP(sp, _AfterFirstRead(real, other_enqueues)), event_bus=None, engine=None)
    assert (await real.get("hns_t")).pending_operation is HarnessOperation.UNINSTALL


async def test_two_racing_enqueues_over_http_answer_202_and_409(client):
    made = await client.post("/v1/harnesses", json={"name": "H", "slug": "racy", "git_url": "https://github.com/x/y"})
    hid = made.json()["id"]
    first, second = await asyncio.gather(client.post(f"/v1/harnesses/{hid}/fetch"), client.delete(f"/v1/harnesses/{hid}"))
    assert sorted([first.status_code, second.status_code]) == [202, 409], (first.text, second.text)


async def test_a_non_json_overrides_body_answers_422(client, fake_storage_provider):
    row = _harness(id="hns_ov", slug="ov", overrides_schema={"type": "object"})
    await fake_storage_provider.get_storage(Harness).create(row)
    for content in (b"not json at all", b"[1, 2]"):
        r = await client.put("/v1/harnesses/hns_ov/overrides", content=content, headers={"content-type": "application/json"})
        assert r.status_code == 422, (content, r.text)
        assert r.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize(("method", "path", "body", "status", "code"), [
    ("post", "/v1/harnesses", {"name": "H", "slug": "no-url"}, 422, "git_url_required_inbound"),
    ("post", "/v1/harnesses", {"name": "H", "slug": "tracked-in", "git_url": "https://github.com/x/y",
                                "tracked_entities": [{"kind": "agent", "source_id": "a", "template_name": "t"}]}, 422, "tracked_entities_on_inbound"),
])
async def test_refusals_are_problem_json_with_the_code_in_the_extensions(client, method, path, body, status, code):
    r = await getattr(client, method)(path, json=body)
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    problem = r.json()
    assert problem["status"] == status and problem["extensions"]["code"] == code and problem["extensions"]["message"]


async def test_the_action_refusals_use_the_same_envelope(client, fake_storage_provider):
    store = fake_storage_provider.get_storage(Harness)
    await store.create(_harness(id="hns_out", slug="out", direction=HarnessDirection.OUTBOUND, git_url=None))
    await store.create(_harness(id="hns_in", slug="in", status=HarnessStatus.READY))
    cases = [
        (await client.post("/v1/harnesses/hns_out/fetch"), 409, "direction_mismatch"),
        (await client.post("/v1/harnesses/hns_in/build"), 409, "direction_mismatch"),
        (await client.post("/v1/harnesses/hns_in/install"), 422, "overrides_schema_missing"),
        (await client.post("/v1/harnesses/hns_out/build"), 422, "outbound_no_entities"),
    ]
    for r, status, code in cases:
        assert r.status_code == status and r.headers["content-type"].startswith("application/problem+json"), r.text
        assert r.json()["extensions"]["code"] == code
