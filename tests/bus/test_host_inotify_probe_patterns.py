"""Review 2026-11-23 6.7: the host probe matches directories, globs and the whole workspace the way ``watch_files`` documents them."""

from __future__ import annotations

import asyncio

import pytest

from primer.bus.host_inotify_probe import HostInotifyProbe
from primer.bus.ws_watch_probe import Change


async def _first(probe: HostInotifyProbe, paths: list[str], mutate, *, file: str | None = None) -> Change:
    """The first change (the first one for ``file`` when given: a platform may report a parent directory's change before the file's)."""

    async def consume() -> Change:
        async for change in probe.watch(paths):
            if file is None or change.path.endswith(file):
                return change
        raise AssertionError("stream ended without a change")  # pragma: no cover

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)
    mutate()
    return await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.asyncio
async def test_a_watched_directory_fires_for_a_file_beneath_it(tmp_path) -> None:
    (tmp_path / "src" / "sub").mkdir(parents=True)
    target = tmp_path / "src" / "sub" / "app.py"
    change = await _first(HostInotifyProbe(root=str(tmp_path)), ["src"], lambda: target.write_text("x"), file="app.py")
    assert change.path == "src/sub/app.py"


@pytest.mark.asyncio
async def test_a_glob_fires_only_for_matching_files(tmp_path) -> None:
    (tmp_path / "src").mkdir()

    def mutate() -> None:
        (tmp_path / "src" / "notes.txt").write_text("no")
        (tmp_path / "src" / "app.py").write_text("yes")

    change = await _first(HostInotifyProbe(root=str(tmp_path)), ["src/*.py"], mutate, file="app.py")
    assert change.path == "src/app.py"


@pytest.mark.asyncio
@pytest.mark.parametrize("whole", [".", "", "./"])
async def test_dot_watches_the_whole_workspace(tmp_path, whole) -> None:
    (tmp_path / "deep" / "er").mkdir(parents=True)
    target = tmp_path / "deep" / "er" / "f.txt"
    change = await _first(HostInotifyProbe(root=str(tmp_path)), [whole], lambda: target.write_text("x"), file="f.txt")
    assert change.path == "deep/er/f.txt"


@pytest.mark.asyncio
async def test_a_directory_that_does_not_exist_yet_is_seen_once_created(tmp_path) -> None:
    def mutate() -> None:
        (tmp_path / "later").mkdir()
        (tmp_path / "later" / "f.txt").write_text("x")

    change = await _first(HostInotifyProbe(root=str(tmp_path)), ["later"], mutate)
    assert change.path.startswith("later")


def _fake_awatch(batches, seen_paths):
    """A stand-in for ``watchfiles.awatch`` that records what it was asked to watch and replays ``batches`` of (change, path)."""

    async def awatch(*paths, **_kw):
        seen_paths.extend(paths)
        for batch in batches:
            yield batch

    return awatch


@pytest.mark.asyncio
async def test_matching_dir_glob_exact_and_whole_tree(tmp_path, monkeypatch) -> None:
    """The probe's own matching, deterministically: what is yielded for which watch spec, and that the WORKSPACE ROOT is what is watched."""
    import watchfiles

    root = str(tmp_path)
    (tmp_path / "src" / "sub").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    added = watchfiles.Change.added
    batch = {(added, f"{root}/src/sub/app.py"), (added, f"{root}/src/notes.txt"), (added, f"{root}/docs/a.md"), (added, root)}

    async def got(specs):
        watched: list[str] = []
        monkeypatch.setattr(watchfiles, "awatch", _fake_awatch([batch], watched))
        out = sorted([c.path async for c in HostInotifyProbe(root=root).watch(specs)])
        assert watched == [root], "watch the workspace root (an inotify watch on a subdirectory dies with it)"
        return out

    assert await got(["src"]) == ["src/notes.txt", "src/sub/app.py"]
    assert await got(["src/*.py"]) == []  # a segment glob does not reach src/sub/
    assert await got(["src/**/*.py"]) == ["src/sub/app.py"]
    assert await got(["src/sub/app.py"]) == ["src/sub/app.py"]
    assert await got(["docs/", "./src/notes.txt"]) == ["docs/a.md", "src/notes.txt"]
    for whole in (".", "", "./"):
        assert await got([whole]) == ["docs/a.md", "src/notes.txt", "src/sub/app.py"]  # not the root itself
    assert await got([f"{root}/docs"]) == ["docs/a.md"]  # an absolute spec under the root


@pytest.mark.asyncio
async def test_a_file_in_a_directory_that_was_deleted_and_recreated_is_still_seen(tmp_path) -> None:
    """``rm -rf out && mkdir out && write out/report.md`` while watching ``out/report.md``: a watch tied to ``out``'s old inode never sees it (Linux
    inotify; macOS FSEvents hides the bug, so this passes there either way and only fails on Linux against a per-directory watch)."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "report.md").write_text("v1")

    def mutate() -> None:
        import shutil

        shutil.rmtree(tmp_path / "out")
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "report.md").write_text("v2")

    change = await _first(HostInotifyProbe(root=str(tmp_path)), ["out/report.md"], mutate, file="report.md")
    assert change.path == "out/report.md"
