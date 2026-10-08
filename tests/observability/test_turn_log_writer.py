"""Tests for the three TurnLogWriter implementations + to_problem_details."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

from primer.api.errors import ProblemDetails
from primer.model.turn_log import (
    TurnLogKind,
    TurnLogRecord,
    TurnLogStarted,
)
from primer.observability.turn_log_writer import (
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
    async def test_bootstrap_seeds_seq_from_existing_file(self):
        """A worker restart mid-session should resume with seq=max+1,
        not seq=1, so the JSONL stream stays monotonic."""
        captured: list[bytes] = []

        async def fake_append(line: bytes) -> None:
            captured.append(line)

        async def fake_read() -> bytes:
            return (
                b'{"seq":1,"kind":"started","ts":"2026-06-05T10:00:00Z"}\n'
                b'{"seq":2,"kind":"completed","ts":"2026-06-05T10:00:05Z","duration_ms":0}\n'
            )

        w = WorkspaceTurnLogWriter(
            append_line=fake_append, read_existing=fake_read,
        )
        seq = await w.append(TurnLogStarted(
            seq=0, ts=_now(), model="m", input_message_count=1,
        ))
        assert seq == 3
        obj = json.loads(captured[0].decode())
        assert obj["seq"] == 3

    @pytest.mark.asyncio
    async def test_bootstrap_handles_missing_file_silently(self):
        captured: list[bytes] = []

        async def fake_append(line: bytes) -> None:
            captured.append(line)

        async def fake_read() -> bytes:
            from primer.model.except_ import NotFoundError
            raise NotFoundError("file gone")

        w = WorkspaceTurnLogWriter(
            append_line=fake_append, read_existing=fake_read,
        )
        seq = await w.append(TurnLogStarted(
            seq=0, ts=_now(), model="m", input_message_count=1,
        ))
        assert seq == 1

    @pytest.mark.asyncio
    async def test_failed_read_aborts_the_append_and_is_retried(self):
        """01a08bfb item 4: a failed read is NOT "brand-new log".

        The old bootstrap swallowed any read error, seeded seq at 0 and
        wrote seq=1 after lines already holding higher seqs (breaking
        since_seq pagination). A real read failure must propagate without
        writing anything and without marking the bootstrap done, so the
        next append retries the read and seeds from the real file.
        """
        captured: list[bytes] = []
        reads = 0

        async def fake_append(line: bytes) -> None:
            captured.append(line)

        async def flaky_read() -> bytes:
            nonlocal reads
            reads += 1
            if reads == 1:
                raise ConnectionError("runtime websocket dropped")
            return b'{"seq":1,"kind":"started"}\n{"seq":2,"kind":"completed"}\n'

        w = WorkspaceTurnLogWriter(
            append_line=fake_append, read_existing=flaky_read,
        )
        with pytest.raises(ConnectionError):
            await w.append(TurnLogStarted(
                seq=0, ts=_now(), model="m", input_message_count=1,
            ))
        assert captured == []  # nothing written over the unread log

        seq = await w.append(TurnLogStarted(
            seq=0, ts=_now(), model="m", input_message_count=1,
        ))
        assert seq == 3  # seeded from the real file, not restarted at 1
        assert json.loads(captured[0].decode())["seq"] == 3
        assert reads == 2

    @pytest.mark.asyncio
    async def test_concurrent_first_appends_share_one_bootstrap(self):
        """Two appends racing the first bootstrap must not both skip it
        (the second used to see the flag already set and use seq 0)."""
        import asyncio

        captured: list[bytes] = []
        reads = 0

        async def fake_append(line: bytes) -> None:
            captured.append(line)

        async def slow_read() -> bytes:
            nonlocal reads
            reads += 1
            await asyncio.sleep(0.01)
            return b'{"seq":1,"kind":"started"}\n{"seq":2,"kind":"completed"}\n'

        w = WorkspaceTurnLogWriter(
            append_line=fake_append, read_existing=slow_read,
        )
        seqs = await asyncio.gather(*(
            w.append(TurnLogStarted(
                seq=0, ts=_now(), model="m", input_message_count=1,
            ))
            for _ in range(2)
        ))
        assert sorted(seqs) == [3, 4]
        assert reads == 1

    @pytest.mark.asyncio
    async def test_bootstrap_skips_bogus_lines(self):
        captured: list[bytes] = []

        async def fake_append(line: bytes) -> None:
            captured.append(line)

        async def fake_read() -> bytes:
            return (
                b'{"seq":1,"kind":"started"}\n'
                b'not even json\n'
                b'{"seq":5,"kind":"completed"}\n'
            )

        w = WorkspaceTurnLogWriter(
            append_line=fake_append, read_existing=fake_read,
        )
        seq = await w.append(TurnLogStarted(
            seq=0, ts=_now(), model="m", input_message_count=1,
        ))
        assert seq == 6  # max(1, 5) + 1

    @pytest.mark.asyncio
    async def test_bootstrap_runs_only_once(self):
        """Subsequent appends MUST NOT re-read the file (perf + races)."""
        read_count = 0

        async def fake_append(line: bytes) -> None:
            pass

        async def fake_read() -> bytes:
            nonlocal read_count
            read_count += 1
            return b""

        w = WorkspaceTurnLogWriter(
            append_line=fake_append, read_existing=fake_read,
        )
        for _ in range(3):
            await w.append(TurnLogStarted(
                seq=0, ts=_now(), model="m", input_message_count=1,
            ))
        assert read_count == 1

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

    def test_traceback_stays_in_the_server_log_keyed_by_error_id(self, caplog):
        """The envelope is served to session readers (messages, turn log,
        tap): it carries the class, the message and an error_id, never a
        traceback. The traceback goes to the server log under that id."""
        try:
            raise ValueError("traceback test")
        except ValueError as caught:
            exc = caught
            with caplog.at_level(
                logging.ERROR, logger="primer.observability.turn_log_writer",
            ):
                pd = to_problem_details(exc)
        assert pd.extensions is not None
        assert "traceback" not in pd.extensions
        assert __file__ not in pd.model_dump_json()
        assert pd.extensions["exception_class"] == "ValueError"
        assert "traceback test" in pd.detail
        error_id = pd.extensions["error_id"]
        assert isinstance(error_id, str) and len(error_id) >= 16
        [rec] = [r for r in caplog.records if error_id in r.getMessage()]
        assert rec.levelno == logging.ERROR
        assert rec.exc_info is not None and rec.exc_info[1] is exc

    def test_a_mapped_primer_error_logs_a_warning_without_traceback(self, caplog):
        """A NetworkError / ProviderError is an expected failure class: one
        WARNING under the error_id, no traceback. Unexpected exceptions
        keep ERROR with the traceback (the test above)."""
        from primer.model.except_ import NetworkError

        try:
            raise NetworkError("Connection reset by peer")
        except NetworkError as caught:
            exc = caught
            with caplog.at_level(
                logging.DEBUG, logger="primer.observability.turn_log_writer",
            ):
                pd = to_problem_details(exc)
        error_id = pd.extensions["error_id"]
        [rec] = [r for r in caplog.records if error_id in r.getMessage()]
        assert rec.levelno == logging.WARNING
        assert rec.exc_info is None

    def test_a_credential_url_in_the_detail_is_redacted(self):
        """The detail is served to session readers; an upstream error
        message can embed the request URL with its ?key=."""
        from primer.model.except_ import ProviderError

        secret = "AIzaSyD-DETAIL-SECRET"
        for exc in (
            RuntimeError(f"401 for url 'https://g.example/v1/models?key={secret}'"),
            ProviderError(f"401 for url 'https://g.example/v1/models?key={secret}'"),
        ):
            pd = to_problem_details(exc)
            assert secret not in pd.detail
            assert "key=[REDACTED]" in pd.detail

    def test_a_bare_primer_error_is_unexpected_and_logs_error_with_traceback(self, caplog):
        """The PrimerError catch-all row (the generic 500) is not a mapped
        failure class: ERROR with exc_info, not the WARNING split."""
        from primer.model.except_ import PrimerError

        try:
            raise PrimerError("x")
        except PrimerError as caught:
            exc = caught
            with caplog.at_level(
                logging.DEBUG, logger="primer.observability.turn_log_writer",
            ):
                pd = to_problem_details(exc)
        assert pd.status == 500
        error_id = pd.extensions["error_id"]
        [rec] = [r for r in caplog.records if error_id in r.getMessage()]
        assert rec.levelno == logging.ERROR
        assert rec.exc_info is not None and rec.exc_info[1] is exc

    def test_string_problem_extensions_are_redacted(self):
        """problem_extensions are merged into the served envelope too."""
        from primer.model.except_ import ProviderError

        secret = "AIzaSyD-EXT-SECRET"
        exc = ProviderError("upstream failed")
        exc.problem_extensions = {
            "upstream_url": f"https://g.example/v1/models?key={secret}",
            "attempts": 3,
        }
        pd = to_problem_details(exc)
        assert secret not in pd.model_dump_json()
        assert pd.extensions["upstream_url"].endswith("key=[REDACTED]")
        assert pd.extensions["attempts"] == 3

    def test_each_envelope_gets_its_own_error_id(self):
        a = to_problem_details(RuntimeError("a")).extensions["error_id"]
        b = to_problem_details(RuntimeError("a")).extensions["error_id"]
        assert a != b

    def test_authentication_error_maps_to_401(self):
        from primer.model.except_ import AuthenticationError

        exc = AuthenticationError("bad key")
        pd = to_problem_details(exc)
        assert pd.status == 401
        assert pd.title == "Authentication Failed"

    def test_a_context_overflow_that_compaction_cannot_fix_has_its_own_type(self):
        from primer.model.except_ import ContextOverflowUnrecoverable

        pd = to_problem_details(ContextOverflowUnrecoverable("the prompt is too large (fixed_over_budget)"))
        assert (pd.status, pd.type, pd.title) == (413, "/errors/context-overflow-unrecoverable", "Context Overflow Unrecoverable")
        assert pd.extensions["exception_class"] == "ContextOverflowUnrecoverable"
        assert "fixed_over_budget" in pd.detail

    def test_an_exceptions_problem_extensions_are_merged_beside_the_class_and_traceback(self):
        from primer.session.compaction import NothingToCompact

        pd = to_problem_details(NothingToCompact("empty_head"))
        assert pd.status == 422
        assert pd.extensions["reason"] == "empty_head"
        assert pd.extensions["exception_class"] == "NothingToCompact" and "error_id" in pd.extensions
        assert "traceback" not in pd.extensions

    def test_an_exception_without_problem_extensions_adds_no_keys(self):
        from primer.model.except_ import ConflictError

        assert set(to_problem_details(ConflictError("taken")).extensions) == {"exception_class", "error_id"}

    def test_every_row_mirrors_the_api_map(self):
        """The two tables are kept in step by hand: a row added to one and not the other would map the same
        exception to two different problem types depending on which surface reported it."""
        from primer.api.errors import _PRIMER_ERROR_MAP as api_map
        from primer.observability.turn_log_writer import _PRIMER_ERROR_MAP as log_map

        assert set(log_map) <= set(api_map), sorted(r[0].__name__ for r in set(log_map) - set(api_map))

    def test_specific_subclass_preferred_over_base(self):
        from primer.model.except_ import RateLimitError

        # RateLimitError inherits from ProviderError; the map should match
        # RateLimitError (429) not ProviderError (502).
        exc = RateLimitError("slow down")
        pd = to_problem_details(exc)
        assert pd.status == 429
        assert pd.title == "Rate Limited"


def test_a_problem_extensions_attribute_on_the_exception_lands_in_the_problem_details():
    """An exception that says more about how a turn failed (the executor's ContextOverflowUnrecoverable) carries it
    through ``problem_extensions``; the ERROR record and the turn log read the same envelope."""
    from primer.model.except_ import BadRequestError, ContextOverflowUnrecoverable
    from primer.observability.turn_log_writer import to_problem_details

    exc = ContextOverflowUnrecoverable(
        "the replay was rejected too", cause=BadRequestError("maximum context length"),
        forced_compaction=True, replay_attempted=True, persisted_rounds=3,
    )
    problem = to_problem_details(exc)
    assert problem.extensions["forced_compaction"] is True and problem.extensions["persisted_rounds"] == 3
    assert problem.extensions["exception_class"] == "ContextOverflowUnrecoverable"
    assert problem.detail.endswith("the replay was rejected too") or "the replay was rejected too" in problem.detail
