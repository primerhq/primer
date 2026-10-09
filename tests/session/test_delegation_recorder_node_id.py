"""``DelegationRecorder`` stamps the graph node that delegated to a run (ticket 01a11cca).

A delegated run's records say which CALL delegated to them by its raw provider id, and raw ids are not unique: two fan-out siblings (graph nodes ``A`` and ``B``) whose providers
synthesise ``call_0`` both delegate under ``call_0``. The parent's own call rows carry their node (``node_id``), the delegated records had none, so a reader could not tell whose run a
record was. ``delegate_node_id`` (the fan-out-instance-qualified node id, ``worker[0]`` for the first instance of ``worker``) is stamped on every record of the run when the caller gives
it, and left off when it does not (a session that is not a graph, or a record written before this).
"""

from __future__ import annotations

from primer.model.chat import Done, Error, TextDelta
from primer.session.delegation import DelegationRecorder


class _Writer:
    def __init__(self) -> None:
        self.records: list = []

    async def append(self, rec) -> int:
        self.records.append(rec)
        return len(self.records)


class _Bus:
    async def publish(self, key, payload) -> None:
        return None


def _ids(run: str, node: str | None) -> dict:
    ids = {"delegate_tool_call_id": "call_0", "delegate_run_id": run, "delegate_parent_run_id": None, "delegate_depth": 1}
    if node is not None:
        ids["delegate_node_id"] = node
    return ids


def _recorder() -> tuple[DelegationRecorder, _Writer]:
    writer = _Writer()
    return DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="s"), writer


async def test_every_record_of_a_run_carries_the_node_it_was_given() -> None:
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="answer"), **_ids("rA", "A"))
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), **_ids("rA", "A"))
    assert [r.kind.value for r in w.records] == ["assistant_token", "done"]
    assert all(r.payload["delegate_node_id"] == "A" for r in w.records)


async def test_two_runs_under_one_raw_call_id_keep_their_own_nodes() -> None:
    rec, w = _recorder()
    for run, node in (("rA", "A"), ("rB", "B")):
        await rec.on_event(TextDelta(index=0, text=f"answer of {node}"), **_ids(run, node))
        await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), **_ids(run, node))
    by_run = {(r.payload["delegate_run_id"], r.payload["delegate_node_id"]) for r in w.records}
    assert by_run == {("rA", "A"), ("rB", "B")}


async def test_a_fan_out_instance_id_is_carried_as_it_is() -> None:
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="x"), **_ids("r0", "worker[0]"))
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), **_ids("r0", "worker[0]"))
    assert {r.payload["delegate_node_id"] for r in w.records} == {"worker[0]"}


async def test_no_node_means_no_field() -> None:
    """The control: a session that is not a graph, and a record from before this, carry no ``delegate_node_id``."""
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="x"), **_ids("r0", None))
    await rec.on_event(Done(stop_reason="stop", raw_reason="stop"), **_ids("r0", None))
    assert w.records and all("delegate_node_id" not in r.payload for r in w.records)


async def test_the_text_a_failed_run_never_flushed_is_stamped_with_its_node_too() -> None:
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="partial"), **_ids("rA", "A"))
    await rec.on_event(Error(message="fell over", code="server_error", fatal=True), **_ids("rA", "A"))
    assert [r.kind.value for r in w.records] == ["assistant_token", "error"]
    assert all(r.payload["delegate_node_id"] == "A" for r in w.records)


async def test_finish_run_stamps_the_flushed_output_with_the_node() -> None:
    rec, w = _recorder()
    await rec.on_event(TextDelta(index=0, text="half an answer"), **_ids("rB", "B"))
    assert w.records == []
    await rec.finish_run(**_ids("rB", "B"))
    assert [r.payload["text"] for r in w.records] == ["half an answer"]
    assert w.records[0].payload["delegate_node_id"] == "B"
