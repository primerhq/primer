"""Tests for matrix.workspace.cache."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import timedelta
from pathlib import Path

import pytest

from matrix.workspace.cache import (
    TruncatedOutput,
    TruncationStore,
)


# ===========================================================================
# Construction
# ===========================================================================


class TestConstruction:
    def test_creates_root_directory_when_missing(self, tmp_path: Path) -> None:
        root = tmp_path / "tmp"
        assert not root.exists()
        TruncationStore(root)
        assert root.is_dir()

    def test_accepts_existing_root_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "tmp"
        root.mkdir()
        # Adding a sentinel file before construction; should still be there after.
        (root / "sentinel").write_text("hi")
        TruncationStore(root)
        assert (root / "sentinel").read_text() == "hi"

    def test_rejects_zero_max_lines(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_lines"):
            TruncationStore(tmp_path / "tmp", max_lines=0)

    def test_rejects_zero_max_bytes(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            TruncationStore(tmp_path / "tmp", max_bytes=0)

    def test_rejects_zero_retention(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="retention"):
            TruncationStore(tmp_path / "tmp", retention=timedelta(seconds=0))

    def test_root_property_returns_resolved_path(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        assert store.root == tmp_path / "tmp"


# ===========================================================================
# write()
# ===========================================================================


class TestWrite:
    async def test_creates_per_session_subdir(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        path = await store.write("hello", session_id="sess-1")
        assert path.parent == tmp_path / "tmp" / "sess-1"
        assert path.parent.is_dir()

    async def test_writes_text_content(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        path = await store.write("hello world", session_id="sess-1")
        assert path.read_text(encoding="utf-8") == "hello world"

    async def test_filename_matches_pattern(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        path = await store.write("x", session_id="sess-1")
        assert re.match(r"^tool_\d+_\d{8}\.txt$", path.name) is not None

    async def test_two_writes_get_distinct_filenames(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        p1 = await store.write("a", session_id="sess-1")
        p2 = await store.write("b", session_id="sess-1")
        assert p1 != p2

    async def test_two_sessions_are_isolated(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        p_a = await store.write("a", session_id="sess-a")
        p_b = await store.write("b", session_id="sess-b")
        assert p_a.parent != p_b.parent
        assert (tmp_path / "tmp" / "sess-a").is_dir()
        assert (tmp_path / "tmp" / "sess-b").is_dir()

    async def test_writes_unicode(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        path = await store.write("日本語\n🎉", session_id="sess-1")
        assert path.read_text(encoding="utf-8") == "日本語\n🎉"

    async def test_rejects_empty_session_id(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        with pytest.raises(ValueError, match="session_id"):
            await store.write("x", session_id="")

    @pytest.mark.parametrize("bad_id", ["..", ".", "a/b", "a\\b", "x\x00y"])
    async def test_rejects_session_ids_that_could_escape(
        self, tmp_path: Path, bad_id: str
    ) -> None:
        store = TruncationStore(tmp_path / "tmp")
        with pytest.raises(ValueError, match="session_id"):
            await store.write("x", session_id=bad_id)


# ===========================================================================
# output() — under-limit fast path
# ===========================================================================


class TestOutputUnderLimit:
    async def test_returns_text_unchanged_when_short(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp", max_lines=10, max_bytes=1024)
        result = await store.output("hello", session_id="sess-1")
        assert result == TruncatedOutput(content="hello", truncated=False, output_path=None)

    async def test_does_not_create_session_dir_when_short(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp", max_lines=10, max_bytes=1024)
        await store.output("hello", session_id="sess-1")
        assert not (tmp_path / "tmp" / "sess-1").exists()

    async def test_at_limit_lines_is_not_truncated(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp", max_lines=3, max_bytes=10_000)
        text = "a\nb\nc\n"  # 3 newlines, no trailing partial line — fits
        result = await store.output(text, session_id="sess-1")
        assert result.truncated is False

    async def test_at_limit_bytes_is_not_truncated(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp", max_lines=10_000, max_bytes=5)
        result = await store.output("hello", session_id="sess-1")  # exactly 5 bytes
        assert result.truncated is False


# ===========================================================================
# output() — over-limit truncation path
# ===========================================================================


class TestOutputOverLimit:
    async def test_over_lines_writes_cache_file(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp", max_lines=2, max_bytes=10_000)
        text = "line0\nline1\nline2\nline3\n"
        result = await store.output(text, session_id="sess-1")
        assert result.truncated is True
        assert result.output_path is not None
        assert Path(result.output_path).read_text(encoding="utf-8") == text

    async def test_over_bytes_writes_cache_file(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp", max_lines=10_000, max_bytes=4)
        text = "hello world"
        result = await store.output(text, session_id="sess-1")
        assert result.truncated is True
        assert Path(result.output_path).read_text(encoding="utf-8") == text

    async def test_over_lines_preview_is_head_anchored_by_default(
        self, tmp_path: Path
    ) -> None:
        store = TruncationStore(tmp_path / "tmp", max_lines=2, max_bytes=10_000)
        text = "first\nsecond\nthird\nfourth\n"
        result = await store.output(text, session_id="sess-1")
        body, _, _ = result.content.partition("\n\nThe tool call succeeded")
        assert body == "first\nsecond\n"

    async def test_over_lines_preview_can_be_tail_anchored(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp", max_lines=2, max_bytes=10_000)
        text = "first\nsecond\nthird\nfourth\n"
        result = await store.output(text, session_id="sess-1", direction="tail")
        body, _, _ = result.content.partition("\n\nThe tool call succeeded")
        assert "third\nfourth\n" in body
        assert "first" not in body
        assert "second" not in body

    async def test_preview_includes_hint_with_path(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp", max_lines=1, max_bytes=10_000)
        text = "a\nb\nc\n"
        result = await store.output(text, session_id="sess-1")
        assert "Full output saved to:" in result.content
        assert result.output_path in result.content

    async def test_per_call_max_lines_overrides_default(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp", max_lines=10_000, max_bytes=10_000)
        # Default would NOT truncate; per-call cap of 1 line forces truncation.
        result = await store.output("a\nb\n", session_id="sess-1", max_lines=1)
        assert result.truncated is True

    async def test_per_call_max_bytes_overrides_default(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp", max_lines=10_000, max_bytes=10_000)
        result = await store.output("hello", session_id="sess-1", max_bytes=1)
        assert result.truncated is True

    async def test_rejects_zero_max_lines_per_call(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        with pytest.raises(ValueError, match="max_lines"):
            await store.output("x", session_id="sess-1", max_lines=0)

    async def test_rejects_zero_max_bytes_per_call(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        with pytest.raises(ValueError, match="max_bytes"):
            await store.output("x", session_id="sess-1", max_bytes=0)


# ===========================================================================
# cleanup() — retention sweep
# ===========================================================================


class TestCleanup:
    async def test_removes_files_older_than_retention(self, tmp_path: Path) -> None:
        store = TruncationStore(
            tmp_path / "tmp",
            retention=timedelta(seconds=1),
        )
        # Synthesise an old file by hand so we don't have to sleep.
        sess_dir = tmp_path / "tmp" / "sess-1"
        sess_dir.mkdir(parents=True)
        old_nanos = time.time_ns() - int(2 * 1_000_000_000)  # 2s ago
        old_file = sess_dir / f"tool_{old_nanos}_00000001.txt"
        old_file.write_text("old")
        new_nanos = time.time_ns()
        new_file = sess_dir / f"tool_{new_nanos}_00000002.txt"
        new_file.write_text("new")

        removed = await store.cleanup()
        assert removed == 1
        assert not old_file.exists()
        assert new_file.exists()

    async def test_walks_every_session_subdirectory(self, tmp_path: Path) -> None:
        store = TruncationStore(
            tmp_path / "tmp",
            retention=timedelta(seconds=1),
        )
        old_nanos = time.time_ns() - int(2 * 1_000_000_000)
        for sess in ("sess-a", "sess-b", "sess-c"):
            sess_dir = tmp_path / "tmp" / sess
            sess_dir.mkdir(parents=True)
            (sess_dir / f"tool_{old_nanos}_00000001.txt").write_text("old")
        removed = await store.cleanup()
        assert removed == 3

    async def test_ignores_files_that_dont_match_pattern(self, tmp_path: Path) -> None:
        store = TruncationStore(
            tmp_path / "tmp",
            retention=timedelta(seconds=1),
        )
        sess_dir = tmp_path / "tmp" / "sess-1"
        sess_dir.mkdir(parents=True)
        # Foreign files survive — only tool_<nanos>_<n>.txt are managed.
        foreign = sess_dir / "README.md"
        foreign.write_text("hello")
        await store.cleanup()
        assert foreign.exists()

    async def test_returns_zero_when_root_missing(self, tmp_path: Path) -> None:
        # Construct, then remove root before sweep — must not raise.
        import shutil as _shutil

        store = TruncationStore(tmp_path / "tmp")
        _shutil.rmtree(tmp_path / "tmp")
        assert await store.cleanup() == 0

    async def test_does_not_remove_empty_session_subdir(self, tmp_path: Path) -> None:
        store = TruncationStore(
            tmp_path / "tmp",
            retention=timedelta(seconds=1),
        )
        sess_dir = tmp_path / "tmp" / "sess-1"
        sess_dir.mkdir(parents=True)
        old_nanos = time.time_ns() - int(2 * 1_000_000_000)
        old_file = sess_dir / f"tool_{old_nanos}_00000001.txt"
        old_file.write_text("x")
        await store.cleanup()
        assert sess_dir.exists()  # subdir survives even when emptied


# ===========================================================================
# cleanup_session()
# ===========================================================================


class TestCleanupSession:
    async def test_removes_session_subdirectory(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        await store.write("a", session_id="sess-1")
        await store.write("b", session_id="sess-1")
        removed = await store.cleanup_session("sess-1")
        assert removed == 2
        assert not (tmp_path / "tmp" / "sess-1").exists()

    async def test_does_not_touch_other_sessions(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        await store.write("a", session_id="sess-a")
        path_b = await store.write("b", session_id="sess-b")
        await store.cleanup_session("sess-a")
        assert path_b.exists()
        assert (tmp_path / "tmp" / "sess-b").is_dir()

    async def test_returns_zero_for_missing_session(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        assert await store.cleanup_session("nope") == 0

    async def test_counts_only_managed_files(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        await store.write("a", session_id="sess-1")
        # Foreign file in the session subdir doesn't bump the count.
        (tmp_path / "tmp" / "sess-1" / "stray.log").write_text("noise")
        removed = await store.cleanup_session("sess-1")
        assert removed == 1

    async def test_rejects_empty_session_id(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        with pytest.raises(ValueError, match="session_id"):
            await store.cleanup_session("")


# ===========================================================================
# start_background_cleanup()
# ===========================================================================


class TestBackgroundCleanup:
    async def test_runs_cleanup_periodically(self, tmp_path: Path) -> None:
        store = TruncationStore(
            tmp_path / "tmp",
            retention=timedelta(seconds=1),
        )
        sess_dir = tmp_path / "tmp" / "sess-1"
        sess_dir.mkdir(parents=True)
        old_nanos = time.time_ns() - int(2 * 1_000_000_000)
        target = sess_dir / f"tool_{old_nanos}_00000001.txt"
        target.write_text("old")

        # Use a tiny interval so the test is fast.
        task = store.start_background_cleanup(interval_seconds=0.05)
        try:
            # Poll for removal rather than fixed sleep.
            for _ in range(100):
                if not target.exists():
                    break
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not target.exists()

    def test_rejects_non_positive_interval(self, tmp_path: Path) -> None:
        store = TruncationStore(tmp_path / "tmp")
        with pytest.raises(ValueError, match="interval_seconds"):
            store.start_background_cleanup(interval_seconds=0)

    async def test_swallows_cleanup_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Replace cleanup() with a function that raises once then succeeds;
        # background loop must keep running.
        store = TruncationStore(tmp_path / "tmp")
        calls = {"count": 0}

        async def flaky_cleanup() -> int:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("disk on fire")
            return 0

        monkeypatch.setattr(store, "cleanup", flaky_cleanup)
        task = store.start_background_cleanup(interval_seconds=0.02)
        try:
            for _ in range(100):
                if calls["count"] >= 2:
                    break
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert calls["count"] >= 2
