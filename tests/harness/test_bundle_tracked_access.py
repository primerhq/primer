"""The outbound bundle ships each tracked row's spec, so READ on the harness is not enough: the reader must read every tracked row (access-control P3 SEC, finding 5)."""

from __future__ import annotations

import io
import tarfile

from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.harness import Harness, HarnessDirection, TrackedEntity
from tests.harness.test_harness_access import ALICE, BOB, NOW, ROOT, _agent, _as, env  # noqa: F401


async def test_the_bundle_refuses_a_reader_who_cannot_read_a_tracked_row(env) -> None:
    sp = env.sp
    priv = _agent("ag-priv", harness_id=None, access=Access(owners=[ALICE])).model_copy(update={"description": "ALICE-SECRET-DESC"})
    await sp.get_storage(Agent).create(priv)
    await sp.get_storage(Harness).create(Harness(
        id="hn-out", slug="out", name="Out", direction=HarnessDirection.OUTBOUND, git_url="https://github.com/x/y", created_at=NOW,
        tracked_entities=[TrackedEntity(kind="agent", source_id="ag-priv", template_name="priv")],
        access=Access(owners=[ROOT], grants={BOB: "read"}),
    ))
    assert (await env.client.get("/v1/agents/ag-priv", headers=_as(BOB))).status_code == 404

    r = await env.client.get("/v1/harnesses/hn-out/bundle.tar.gz", headers=_as(BOB))
    assert r.status_code == 403, r.text
    assert "ALICE-SECRET-DESC" not in r.text

    # An admin, and a reader who also reads the tracked row, still download it.
    assert (await env.client.get("/v1/harnesses/hn-out/bundle.tar.gz", headers=_as(ROOT))).status_code == 200
    await sp.get_storage(Agent).update(priv.model_copy(update={"access": Access(owners=[ALICE], grants={BOB: "read"})}))
    ok = await env.client.get("/v1/harnesses/hn-out/bundle.tar.gz", headers=_as(BOB))
    assert ok.status_code == 200, ok.text
    with tarfile.open(fileobj=io.BytesIO(ok.content), mode="r:gz") as tf:
        assert "ALICE-SECRET-DESC" in "".join(tf.extractfile(m).read().decode() for m in tf.getmembers() if m.isfile())
