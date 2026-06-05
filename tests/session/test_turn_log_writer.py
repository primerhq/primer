"""Tests for the three TurnLogWriter implementations + to_problem_details."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.api.errors import ProblemDetails
from primer.model.turn_log import (
    TurnLogKind,
    TurnLogRecord,
    TurnLogStarted,
)
from primer.session.turn_log_writer import (
    NoopTurnLogWriter,
    StorageTurnLogWriter,
    WorkspaceTurnLogWriter,
    to_problem_details,
)


def _now() -> datetime:
    return datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)


class TestNoop:
    @pytest.mark.asyncio
    async def test_noop_append_returns_monotonic(self):
        w = NoopTurnLogWriter()
        s1 = await w.append(TurnLogStarted(
            seq=0, ts=_now(), model="x", input_message_count=1,
        ))
        s2 = await w.append(TurnLogStarted(
            seq=0, ts=_now(), model="x", input_message_count=1,
        ))
        assert s2 == s1 + 1
        await w.aclose()


class TestWorkspaceTurnLogWriter:
    @pytest.mark.asyncio
    async def test_write_one_event_appends_jsonl_line(self):
        captured: list[bytes] = []

        async def fake_append(line: bytes) -> None:
            captured.append(line)

        w = WorkspaceTurnLogWriter(append_line=fake_append)
        seq = await w.append(TurnLogStarted(
            seq=0, ts=_now(), model="m", input_message_count=2,
        ))
        await w.aclose()
        assert seq == 1
        assert len(captured) == 1
        assert captured[0].endswith(b"\n")
        obj = json.loads(captured[0].decode())
        assert obj["kind"] == "started"
        assert obj["seq"] == 1
        assert obj["model"] == "m"

    @pytest.mark.asyncio
    async def test_seq_monotonic_across_appends(self):
        captured: list[bytes] = []

        async def fake_append(line: bytes) -> None:
            captured.append(line)

        w = WorkspaceTurnLogWriter(append_line=fake_append)
        for _ in range(3):
            await w.append(TurnLogStarted(
                seq=0, ts=_now(), model="x", input_message_count=1,
            ))
        await w.aclose()
        seqs = [json.loads(line.decode())["seq"] for line in captured]
        assert seqs == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self):
        async def fake_append(line: bytes) -> None:
            pass

        w = WorkspaceTurnLogWriter(append_line=fake_append)
        await w.aclose()
        await w.aclose()

    @pytest.mark.asyncio
    async def test_append_after_close_raises(self):
        async def fake_append(line: bytes) -> None:
            pass

        w = WorkspaceTurnLogWriter(append_line=fake_append)
        await w.aclose()
        with pytest.raises(RuntimeError):
            await w.append(TurnLogStarted(
                seq=0, ts=_now(), model="x", input_message_count=1,
            ))

    @pytest.mark.asyncio
    async def test_io_failure_does_not_corrupt_seq(self):
        """If the backing append raises, the writer's seq still advances.

        Turn-log writes are best-effort; the dispatcher catches and logs
        writer failures. The seq counter still advances so subsequent
        successful writes are monotonic relative to each other (not to
        the failed attempts).
        """
        async def failing_append(line: bytes) -> None:
            raise RuntimeError("disk full")

        w = WorkspaceTurnLogWriter(append_line=failing_append)
        with pytest.raises(RuntimeError):
            await w.append(TurnLogStarted(
                seq=0, ts=_now(), model="x", input_message_count=1,
            ))
        # seq advanced before the IO; subsequent successful write is seq=2.
        captured: list[bytes] = []

        async def good_append(line: bytes) -> None:
            captured.append(line)

        # Replace the underlying append for the rest of the test.
        w._append = good_append  # noqa: SLF001
        seq = await w.append(TurnLogStarted(
            seq=0, ts=_now(), model="x", input_message_count=1,
        ))
        assert seq == 2


class _FakeStorage:
    """In-memory Storage[TurnLogRecord] for the storage writer tests."""

    def __init__(self) -> None:
        self.rows: list[TurnLogRecord] = []

    async def create(self, row: TurnLogRecord) -> TurnLogRecord:
        self.rows.append(row)
        return row


class TestStorageTurnLogWriter:
    @pytest.mark.asyncio
    async def test_create_row_per_event(self):
        storage = _FakeStorage()
        w = StorageTurnLogWriter(
            storage=storage, run_id="run-x", node_id="node-a",
        )
        seq = await w.append(TurnLogStarted(
            seq=0, ts=_now(), model="m", input_message_count=2,
            node_id="node-a",
        ))
        await w.aclose()
        assert seq == 1
        assert len(storage.rows) == 1
        row = storage.rows[0]
        assert row.run_id == "run-x"
        assert row.node_id == "node-a"
        assert row.seq == 1
        assert row.kind == TurnLogKind.STARTED
        assert row.payload["model"] == "m"
        # The base fields should NOT appear in payload.
        for excluded in ("seq", "kind", "ts", "node_id", "iteration", "superstep_id", "turn_no"):
            assert excluded not in row.payload

    @pytest.mark.asyncio
    async def test_graph_level_writer_has_null_node_id(self):
        storage = _FakeStorage()
        w = StorageTurnLogWriter(
            storage=storage, run_id="run-x", node_id=None,
        )
        await w.append(TurnLogStarted(
            seq=0, ts=_now(), model=None, input_message_count=0,
        ))
        assert storage.rows[0].node_id is None

    @pytest.mark.asyncio
    async def test_append_after_close_raises(self):
        w = StorageTurnLogWriter(
            storage=_FakeStorage(), run_id="run-x",
        )
        await w.aclose()
        with pytest.raises(RuntimeError):
            await w.append(TurnLogStarted(
                seq=0, ts=_now(), model=None, input_message_count=0,
            ))


class TestToProblemDetails:
    def test_known_exception_uses_map(self):
        from primer.model.except_ import NetworkError

        exc = NetworkError("Connection reset")
        pd = to_problem_details(exc)
        assert isinstance(pd, ProblemDetails)
        assert pd.status == 504
        assert pd.type == "/errors/network-error"
        assert pd.title == "Network Error"
        assert "Connection reset" in pd.detail
        assert pd.extensions is not None
        assert pd.extensions["exception_class"] == "NetworkError"

    def test_unknown_exception_falls_back_to_500(self):
        exc = RuntimeError("boom")
        pd = to_problem_details(exc)
        assert pd.status == 500
        assert pd.title == "RuntimeError"
        assert "boom" in pd.detail

    def test_traceback_included_in_extensions(self):
        try:
            raise ValueError("traceback test")
        except ValueError as exc:
            pd = to_problem_details(exc)
        assert pd.extensions is not None
        assert "traceback" in pd.extensions
        assert "ValueError" in pd.extensions["traceback"]

    def test_authentication_error_maps_to_401(self):
        from primer.model.except_ import AuthenticationError

        exc = AuthenticationError("bad key")
        pd = to_problem_details(exc)
        assert pd.status == 401
        assert pd.title == "Authentication Failed"

    def test_specific_subclass_preferred_over_base(self):
        from primer.model.except_ import RateLimitError

        # RateLimitError inherits from ProviderError; the map should match
        # RateLimitError (429) not ProviderError (502).
        exc = RateLimitError("slow down")
        pd = to_problem_details(exc)
        assert pd.status == 429
        assert pd.title == "Rate Limited"
