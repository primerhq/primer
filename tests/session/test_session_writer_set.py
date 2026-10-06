"""The whole-document writers of ``Storage[WorkspaceSession]`` are pinned.

A whole-document ``update`` of a session row can erase a field a concurrent writer committed between
its ``get`` and its ``update`` (a park, a flag). So the set of such writers is a pinned list: a NEW one
fails CI until it is classified in ``session_writers.txt``, and a converted or removed one fails until its
line is deleted. Every conversion to a field-scoped ``patch_if`` therefore edits the allowlist, which is
the point.

``tests/_support/session_writer_scan.py`` finds the writers by following the HANDLE TYPE (not variable
names), over ``primer/`` as plain ``ast``. This file also tests that scanner on synthetic sources, which
is what proves a renamed variable cannot hide a writer.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import re
import textwrap

import pytest

from tests._support import session_writer_scan as scanner
from tests._support.session_writer_scan import ScanResult, default_root, scan

ALLOWLIST = pathlib.Path(__file__).with_name("session_writers.txt")
ALLOWLIST_REL = "tests/session/session_writers.txt"
SECTIONS = ("writers", "deletes", "unresolved")

Key = tuple[str, str, str]

# ---------------------------------------------------------------------------------------------
# the allowlist file: one `file | function | method | sites | disposition` line per key


@dataclasses.dataclass(frozen=True)
class Entry:
    sites: int
    disposition: str
    line_no: int


def parse_allowlist(text: str) -> tuple[dict[str, dict[Key, Entry]], list[str]]:
    """The entries per section, and one message per malformed line, duplicate or empty disposition."""
    entries: dict[str, dict[Key, Entry]] = {s: {} for s in SECTIONS}
    problems: list[str] = []
    section: str | None = None
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            name = line.strip("[]")
            if line != f"[{name}]" or name not in SECTIONS:
                problems.append(f"line {n}: unknown section {line!r}, expected one of {SECTIONS}")
                section = None
            else:
                section = name
            continue
        if section is None:
            problems.append(f"line {n}: an entry outside a known section: {line!r}")
            continue
        parts = re.split(r"\s*\|\s*", line, maxsplit=4)
        if len(parts) != 5:
            problems.append(
                f"line {n}: malformed, want 'file | function | method | sites | disposition': {line!r}"
            )
            continue
        file, function, method, sites_text, disposition = parts
        if not (file and function and method):
            problems.append(f"line {n}: an empty file, function or method: {line!r}")
            continue
        if not sites_text.isdigit() or int(sites_text) < 1:
            problems.append(f"line {n}: sites must be a positive integer, got {sites_text!r}")
            continue
        if not disposition:
            problems.append(f"line {n}: {file}: {function}.{method} has an empty disposition")
            continue
        key = (file, function, method)
        if key in entries[section]:
            problems.append(
                f"line {n}: duplicate entry for {file}: {function}.{method} "
                f"(first at line {entries[section][key].line_no})"
            )
            continue
        entries[section][key] = Entry(int(sites_text), disposition, n)
    return entries, problems


_NEW = {
    "writers": (
        "new whole-document session writer {file}: {function}.{method}; add it to "
        f"{ALLOWLIST_REL} with a disposition, and read plan section 3.4(e) first"
    ),
    "deletes": (
        "new session delete site {file}: {function}.{method}; add it to "
        f"{ALLOWLIST_REL} with a disposition, and read plan section 3.4(e) first"
    ),
    "unresolved": (
        "new update on a receiver the scan cannot type {file}: {function}.{method}; if it can write "
        "a WorkspaceSession it is a whole-document writer (make the handle visible to the scan, "
        f"then list it under [writers]); otherwise add it to {ALLOWLIST_REL} under [unresolved] "
        "with the reason it is not a session"
    ),
}


def diff_against_allowlist(
    section: str, found: dict[Key, int], listed: dict[Key, Entry]
) -> list[str]:
    out: list[str] = []
    for key in sorted(found.keys() - listed.keys()):
        file, function, method = key
        out.append(_NEW[section].format(file=file, function=function, method=method))
    for key in sorted(listed.keys() - found.keys()):
        file, function, method = key
        out.append(
            f"stale entry: {file}: {function}.{method} ({ALLOWLIST_REL} line {listed[key].line_no}): "
            "the writer was converted or removed, delete the line (good news)"
        )
    for key in sorted(found.keys() & listed.keys()):
        file, function, method = key
        have, want = found[key], listed[key].sites
        if have > want:
            out.append(
                f"new whole-document site in an allowlisted function {file}: {function}.{method}: "
                f"the code has {have}, {ALLOWLIST_REL} line {listed[key].line_no} says {want}; classify "
                "the new site and raise the count, and read plan section 3.4(e) first"
            )
        elif have < want:
            out.append(
                f"stale entry: {file}: {function}.{method} ({ALLOWLIST_REL} line "
                f"{listed[key].line_no}): the code has {have} site(s), the line says {want}; a site "
                "was converted or removed, lower the count (good news)"
            )
    return out


# ---------------------------------------------------------------------------------------------
# the pin over the real tree


@pytest.fixture(scope="module")
def repo_scan() -> ScanResult:
    return scan(default_root())


@pytest.fixture(scope="module")
def allowlist() -> dict[str, dict[Key, Entry]]:
    return parse_allowlist(ALLOWLIST.read_text(encoding="utf-8"))[0]


def test_the_scan_sees_the_repo(repo_scan: ScanResult) -> None:
    """An empty scan would make every pin below pass vacuously."""
    assert default_root().name == "primer" and (default_root() / "session").is_dir()
    assert repo_scan.writers or repo_scan.patches or repo_scan.deletes, (
        "the scan found no WorkspaceSession handle in primer/; the scanner is broken"
    )


def test_allowlist_file_is_well_formed() -> None:
    _, problems = parse_allowlist(ALLOWLIST.read_text(encoding="utf-8"))
    assert not problems, f"{ALLOWLIST_REL} is malformed:\n" + "\n".join(problems)


@pytest.mark.parametrize("section", SECTIONS)
def test_pinned_set_matches_the_allowlist(
    section: str, repo_scan: ScanResult, allowlist: dict[str, dict[Key, Entry]]
) -> None:
    problems = diff_against_allowlist(section, repo_scan.counts(section), allowlist[section])
    assert not problems, f"[{section}] differs from the code:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------------------------
# the allowlist parser and the comparison


GOOD = """
# a comment

[writers]
primer/a.py | f | update | 2 | S4 patch_if | with a bar in the disposition
[deletes]
primer/a.py | g | delete | 1 | regression pin
[unresolved]
primer/b.py | h | rows.update | 1 | a dict
"""


def test_parser_reads_a_well_formed_file() -> None:
    entries, problems = parse_allowlist(GOOD)
    assert problems == []
    assert entries["writers"] == {
        ("primer/a.py", "f", "update"): Entry(2, "S4 patch_if | with a bar in the disposition", 5)
    }
    assert list(entries["deletes"]) == [("primer/a.py", "g", "delete")]
    assert list(entries["unresolved"]) == [("primer/b.py", "h", "rows.update")]


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("[writers]\nprimer/a.py | f | update | 1\n", "malformed"),
        ("[writers]\nprimer/a.py | f | update\n", "malformed"),
        ("[writers]\nprimer/a.py | f | update | many | x\n", "sites must be a positive integer"),
        ("[writers]\nprimer/a.py | f | update | 0 | x\n", "sites must be a positive integer"),
        ("[writers]\nprimer/a.py | f | update | 1 | \n", "empty disposition"),
        ("[writers]\nprimer/a.py | f | update | 1 |\n", "empty disposition"),
        ("[writers]\n | f | update | 1 | x\n", "empty file, function or method"),
        ("primer/a.py | f | update | 1 | x\n", "outside a known section"),
        ("[patches]\nprimer/a.py | f | update | 1 | x\n", "unknown section"),
    ],
)
def test_parser_rejects_a_malformed_line(text: str, fragment: str) -> None:
    entries, problems = parse_allowlist(text)
    assert problems and fragment in problems[0], problems
    assert not any(entries.values())


def test_parser_rejects_two_entries_with_the_same_key() -> None:
    text = "[writers]\nprimer/a.py | f | update | 1 | x\nprimer/a.py | f | update | 1 | y\n"
    entries, problems = parse_allowlist(text)
    assert len(problems) == 1 and "duplicate entry" in problems[0] and "first at line 2" in problems[0]
    assert entries["writers"][("primer/a.py", "f", "update")].disposition == "x"


def test_the_same_key_in_two_sections_is_not_a_duplicate() -> None:
    text = "[writers]\nprimer/a.py | f | update | 1 | x\n[unresolved]\nprimer/a.py | f | update | 1 | y\n"
    assert parse_allowlist(text)[1] == []


def test_diff_names_a_new_writer_a_stale_entry_and_a_count_change() -> None:
    key_new, key_old, key_more, key_less = (
        ("primer/n.py", "new", "update"),
        ("primer/o.py", "old", "update"),
        ("primer/m.py", "more", "update"),
        ("primer/l.py", "less", "update"),
    )
    found = {key_new: 1, key_more: 3, key_less: 1}
    listed = {key_old: Entry(1, "x", 10), key_more: Entry(2, "x", 11), key_less: Entry(2, "x", 12)}
    problems = diff_against_allowlist("writers", found, listed)
    assert len(problems) == 4
    joined = "\n".join(problems)
    assert (
        "new whole-document session writer primer/n.py: new.update; add it to "
        "tests/session/session_writers.txt with a disposition, and read plan section 3.4(e) first"
    ) in joined
    assert (
        "stale entry: primer/o.py: old.update (tests/session/session_writers.txt line 10): "
        "the writer was converted or removed, delete the line (good news)"
    ) in joined
    assert "primer/m.py: more.update: the code has 3" in joined and "says 2" in joined
    assert "primer/l.py: less.update" in joined and "lower the count" in joined


def test_diff_is_empty_when_the_code_matches() -> None:
    key = ("primer/a.py", "f", "update")
    assert diff_against_allowlist("writers", {key: 2}, {key: Entry(2, "x", 3)}) == []


# ---------------------------------------------------------------------------------------------
# the scanner, on synthetic sources: it follows the handle TYPE, not the names


# Other models of the synthetic package: a name is "another model" only when it is a real class.
OTHER_MODELS = "class Harness: ...\nclass Collection: ...\n"


def scan_sources(tmp_path: pathlib.Path, **files: str) -> ScanResult:
    """Write ``name=source`` modules under ``tmp_path/primer`` and scan them."""
    root = tmp_path / "primer"
    for name, source in {"_models": OTHER_MODELS, **files}.items():
        path = root / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return scan(root)


def writers(result: ScanResult) -> list[tuple[str, str, str]]:
    return [s.key() for s in result.writers]


def test_a_direct_get_storage_call_is_a_writer(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, s):
            await sp.get_storage(WorkspaceSession).update(s)
        """,
    )
    assert writers(result) == [("primer/a.py", "f", "update")]


@pytest.mark.parametrize("name", ["sessions", "x", "storage", "_thing", "handle_for_rows"])
def test_a_handle_under_a_renamed_variable_is_still_found(tmp_path: pathlib.Path, name: str) -> None:
    result = scan_sources(
        tmp_path,
        a=f"""
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, s):
            {name} = sp.get_storage(WorkspaceSession)
            await {name}.update(s)
        """,
    )
    assert writers(result) == [("primer/a.py", "f", "update")]


def test_an_import_alias_of_the_model_is_followed(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession as _WS

        async def f(sp, s):
            await sp.get_storage(_WS).update(s)
        """,
    )
    assert writers(result) == [("primer/a.py", "f", "update")]


def test_a_handle_aliased_through_another_local_is_found(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, s):
            first = sp.get_storage(WorkspaceSession)
            second = first
            third = second or None
            await third.update_unless(s, field="status", forbidden="ended")
        """,
    )
    assert writers(result) == [("primer/a.py", "f", "update_unless")]


def test_a_class_attribute_handle_is_found_in_another_method(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        class Adapter:
            def __init__(self, sp):
                self._rows = sp.get_storage(WorkspaceSession)

            async def on_release(self, s):
                await self._rows.update(s)
        """,
    )
    assert writers(result) == [("primer/a.py", "Adapter.on_release", "update")]


def test_a_class_attribute_assigned_from_an_annotated_parameter_is_found(
    tmp_path: pathlib.Path,
) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.int.storage import Storage
        from primer.model.workspace_session import WorkspaceSession

        class Tap:
            def __init__(self, backing: Storage[WorkspaceSession]):
                self._anything = backing

            async def go(self, s):
                await self._anything.update(s)
        """,
    )
    assert writers(result) == [("primer/a.py", "Tap.go", "update")]


def test_an_annotated_class_field_and_a_base_class_attribute_are_found(
    tmp_path: pathlib.Path,
) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        class Base:
            def __init__(self, sp):
                self._rows = sp.get_storage(WorkspaceSession)

        class Child(Base):
            async def go(self, s):
                await self._rows.update(s)

        class Deps:
            rows: "Storage[WorkspaceSession]"

            async def other(self, s):
                await self.rows.update(s)
        """,
    )
    assert writers(result) == [
        ("primer/a.py", "Child.go", "update"),
        ("primer/a.py", "Deps.other", "update"),
    ]


@pytest.mark.parametrize(
    "annotation",
    [
        "Storage[WorkspaceSession]",
        '"Storage[WorkspaceSession]"',
        "Storage[WorkspaceSession] | None",
        "Optional[Storage[WorkspaceSession]]",
        "primer.int.storage.Storage[WorkspaceSession]",
    ],
)
def test_a_parameter_annotated_storage_of_the_session_is_a_handle(
    tmp_path: pathlib.Path, annotation: str
) -> None:
    result = scan_sources(
        tmp_path,
        a=f"""
        from primer.model.workspace_session import WorkspaceSession

        async def f(rows: {annotation}, s):
            await rows.update(s)
        """,
    )
    assert writers(result) == [("primer/a.py", "f", "update")]


@pytest.mark.parametrize("name", ["session_storage", "my_session_storage", "sessions", "session_store"])
def test_an_unannotated_parameter_named_like_a_session_store_is_a_handle(
    tmp_path: pathlib.Path, name: str
) -> None:
    result = scan_sources(tmp_path, a=f"async def f({name}, s):\n    await {name}.update(s)\n")
    assert writers(result) == [("primer/a.py", "f", "update")]


def test_a_parameter_annotated_any_but_named_like_a_store_is_a_handle(tmp_path: pathlib.Path) -> None:
    result = scan_sources(tmp_path, a="async def f(sessions: Any, s):\n    await sessions.update(s)\n")
    assert writers(result) == [("primer/a.py", "f", "update")]


def test_a_parameter_annotated_as_another_models_storage_is_not_a_handle(
    tmp_path: pathlib.Path,
) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        async def f(session_storage: Storage[Harness], h):
            await session_storage.update(h)
        """,
    )
    assert result.writers == ()


def test_a_call_on_an_unrelated_storage_is_not_a_writer(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, h, s):
            harnesses = sp.get_storage(Harness)
            await harnesses.update(h)
            await harnesses.update_unless(h, field="x", forbidden=1)
            data = {}
            data.update(h)
            sessions = sp.get_storage(WorkspaceSession)
            return await sessions.get(s.id)
        """,
    )
    assert result.writers == () and result.deletes == () and result.patches == ()


def test_a_function_parameter_shadows_a_closure_handle(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        def build(sp):
            store = sp.get_storage(WorkspaceSession)

            async def other(store, h):
                await store.update(h)

            return other
        """,
    )
    assert result.writers == ()


def test_a_nested_function_is_attributed_to_itself_and_sees_its_closure(
    tmp_path: pathlib.Path,
) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        def build(sp):
            store = sp.get_storage(WorkspaceSession)

            async def handler(s):
                await store.update(s)

            return handler
        """,
    )
    assert writers(result) == [("primer/a.py", "build.handler", "update")]


def test_patch_if_is_visible_but_not_a_writer(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession
        from primer.storage.cas import patch_if_checked

        async def f(sp, sid):
            sessions = sp.get_storage(WorkspaceSession)
            await sessions.patch_if(sid, {"title": "t"}, where={})
            await patch_if_checked(sessions, sid, {"title": "t"}, where={})
        """,
    )
    assert result.writers == ()
    assert [(s.function, s.method) for s in result.patches] == [
        ("f", "patch_if"),
        ("f", "patch_if_checked"),
    ]


def test_a_delete_is_reported_apart_from_the_writers(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, sid):
            await sp.get_storage(WorkspaceSession).delete(sid)
        """,
    )
    assert result.writers == ()
    assert [s.key() for s in result.deletes] == [("primer/a.py", "f", "delete")]


def test_create_is_not_a_writer(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, s):
            await sp.get_storage(WorkspaceSession).create(s)
        """,
    )
    assert result.writers == () and result.deletes == () and result.patches == ()


def test_a_bare_reference_and_a_literal_getattr_count_like_a_call(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        import asyncio
        from primer.model.workspace_session import WorkspaceSession

        async def by_reference(sp, s):
            sessions = sp.get_storage(WorkspaceSession)
            await asyncio.to_thread(sessions.update, s)

        async def by_getattr(sp, s):
            sessions = sp.get_storage(WorkspaceSession)
            await getattr(sessions, "update")(s)
        """,
    )
    assert writers(result) == [
        ("primer/a.py", "by_getattr", "update"),
        ("primer/a.py", "by_reference", "update"),
    ]


def test_a_handle_returned_by_a_function_is_followed_across_files(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        deps="""
        from primer.model.workspace_session import WorkspaceSession

        def get_session_storage(sp) -> "Storage[WorkspaceSession]":
            return sp.get_storage(WorkspaceSession)

        def unannotated(sp):
            return sp.get_storage(WorkspaceSession)
        """,
        route="""
        from fastapi import Depends
        from primer.deps import get_session_storage, unannotated

        async def r(s, rows=Depends(get_session_storage)):
            await rows.update(s)

        async def u(sp, s):
            await unannotated(sp).update_unless(s, field="a", forbidden=1)
        """,
    )
    assert writers(result) == [
        ("primer/route.py", "r", "update"),
        ("primer/route.py", "u", "update_unless"),
    ]


def test_a_handle_passed_to_a_helper_makes_the_helper_parameter_a_handle(
    tmp_path: pathlib.Path,
) -> None:
    result = scan_sources(
        tmp_path,
        helper="""
        async def write_it(store, row):
            await store.update(row)

        class Svc:
            def __init__(self, backing):
                self._backing = backing

            async def go(self, row):
                await self._backing.update_unless(row, field="a", forbidden=1)
        """,
        caller="""
        from primer.model.workspace_session import WorkspaceSession
        from primer.helper import Svc, write_it

        async def f(sp, s):
            sessions = sp.get_storage(WorkspaceSession)
            await write_it(sessions, s)
            await Svc(backing=sessions).go(s)
        """,
    )
    assert writers(result) == [
        ("primer/helper.py", "Svc.go", "update_unless"),
        ("primer/helper.py", "write_it", "update"),
    ]


def test_a_whole_document_update_on_an_untyped_receiver_is_unresolved(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        async def copy_shape(store, row):
            await store.update(row.model_copy(update={"a": 1}))

        async def local_copy(store, row):
            new = row.model_copy(update={"a": 1})
            await store.update(new)

        async def session_named(store, session):
            await store.update_unless(session, field="a", forbidden=1)

        async def plain_dict(d, other):
            d.update(other)
            d.update({"a": 1})

        async def typed_elsewhere(sp, row):
            await sp.get_storage(Harness).update(row.model_copy(update={"a": 1}))
        """,
    )
    assert result.writers == ()
    assert [(s.function, s.method) for s in result.unresolved] == [
        ("copy_shape", "store.update"),
        ("local_copy", "store.update"),
        ("session_named", "store.update_unless"),
    ]


def test_output_is_sorted_and_deterministic(tmp_path: pathlib.Path) -> None:
    body = """
        from primer.model.workspace_session import WorkspaceSession

        async def zed(sp, s):
            await sp.get_storage(WorkspaceSession).update(s)
            await sp.get_storage(WorkspaceSession).update(s)

        async def alpha(sp, s):
            await sp.get_storage(WorkspaceSession).update_unless(s, field="a", forbidden=1)
            await sp.get_storage(WorkspaceSession).update(s)
    """
    first = scan_sources(tmp_path, z=body, b=body, a=body)
    second = scan(tmp_path / "primer")
    assert first == second
    assert json.dumps(first.as_json()) == json.dumps(second.as_json())
    assert list(first.writers) == sorted(first.writers)
    assert writers(first)[0] == ("primer/a.py", "alpha", "update")
    assert first.counts("writers") == dict(sorted(first.counts("writers").items()))
    assert first.counts("writers")[("primer/z.py", "zed", "update")] == 2


def test_the_scan_is_pure_ast_and_does_not_depend_on_the_cwd(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = ast.parse(pathlib.Path(scanner.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not [m for m in imported if m.split(".")[0] in {"primer", "tests"}], imported
    before = default_root()
    monkeypatch.chdir(tmp_path)
    assert default_root() == before == pathlib.Path(__file__).resolve().parents[2] / "primer"


# ---------------------------------------------------------------------------------------------
# a handle of an UNKNOWN model is never "another model": generic CRUD cannot hide a session writer

UNKNOWN_MODEL_SOURCES = {
    "storage_of_any": """
        async def f(rows: Storage[Any], entity):
            await rows.update(entity)
    """,
    "storage_of_a_typevar": """
        async def f(rows: "Storage[T]", entity):
            await rows.update(entity)
    """,
    "get_storage_of_a_variable": """
        async def f(sp, model_cls, entity):
            rows = sp.get_storage(model_cls)
            await rows.update(entity)
    """,
    "get_storage_of_dunder_class": """
        async def f(sp, session):
            await sp.get_storage(session.__class__).update(session)
    """,
    "get_storage_of_type_of": """
        async def f(sp, row):
            await sp.get_storage(type(row)).update(row)
    """,
    "get_storage_of_a_loop_variable": """
        async def f(sp, row):
            for model in (Harness, Collection):
                await sp.get_storage(model).update(row)
    """,
    "update_unless": """
        async def f(sp, model_cls, entity):
            await sp.get_storage(model_cls).update_unless(entity, field="a", forbidden=1)
    """,
}


@pytest.mark.parametrize("name", UNKNOWN_MODEL_SOURCES)
def test_a_write_on_a_handle_of_an_unknown_model_is_unresolved_whatever_its_argument(
    tmp_path: pathlib.Path, name: str
) -> None:
    result = scan_sources(tmp_path, a=UNKNOWN_MODEL_SOURCES[name])
    assert result.writers == ()
    assert [s.function for s in result.unresolved] == ["f"], result.unresolved
    assert result.unresolved[0].method.endswith((".update", ".update_unless"))


def test_a_write_on_a_receiver_typed_as_another_concrete_model_is_ignored(
    tmp_path: pathlib.Path,
) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer._models import Harness as _H

        async def f(sp, h, rows: Storage[Collection]):
            await sp.get_storage(Harness).update(h.model_copy(update={"a": 1}))
            await sp.get_storage(_H).update(h)
            await rows.update(h.model_copy(update={"a": 1}))
        """,
    )
    assert result.writers == () and result.unresolved == ()


def test_a_helper_over_storage_of_t_fed_a_session_handle_is_a_writer(
    tmp_path: pathlib.Path,
) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        async def save(storage: "Storage[T]", row):
            await storage.update(row)

        async def f(sp, row):
            await save(sp.get_storage(WorkspaceSession), row)
        """,
    )
    assert writers(result) == [("primer/a.py", "save", "update")]
    assert result.unresolved == ()


def test_storage_of_any_named_like_a_session_store_is_a_handle(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="async def f(sessions: Storage[Any], row):\n    await sessions.update(row)\n",
    )
    assert writers(result) == [("primer/a.py", "f", "update")]


@pytest.mark.parametrize(
    "alias_lines",
    [
        "M = WorkspaceSession\n    handle = sp.get_storage(M)",
        "M = WorkspaceSession\n    N = M\n    handle = sp.get_storage(N)",
        "M: type = WorkspaceSession\n    handle = sp.get_storage(M)",
    ],
)
def test_a_function_local_alias_of_the_model_is_followed(
    tmp_path: pathlib.Path, alias_lines: str
) -> None:
    result = scan_sources(
        tmp_path,
        a=f"""
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, row):
            {alias_lines}
            await handle.update(row)
        """,
    )
    assert writers(result) == [("primer/a.py", "f", "update")]


# ---------------------------------------------------------------------------------------------
# shapes the scan closes: assignment targets, calls, defaults, decorators, same-named defs

CLOSED_SHAPES = {
    "tuple_unpacking": ("f", """
        async def f(sp, row):
            rows, others = sp.get_storage(WorkspaceSession), sp.get_storage(Harness)
            await rows.update(row)
    """),
    "for_target_over_a_literal": ("f", """
        async def f(sp, row):
            for rows in (sp.get_storage(WorkspaceSession),):
                await rows.update(row)
    """),
    "comprehension_target": ("f", """
        async def f(sp, row):
            return [await h.update(row) for h in [sp.get_storage(WorkspaceSession)]]
    """),
    "with_as_target": ("f", """
        import contextlib

        @contextlib.asynccontextmanager
        async def opened(sp):
            yield sp.get_storage(WorkspaceSession)

        async def f(sp, row):
            async with opened(sp) as rows:
                await rows.update(row)
    """),
    "cast": ("f", """
        from typing import cast

        async def f(x, row):
            rows = cast("Storage[WorkspaceSession]", x)
            await rows.update(row)
    """),
    "default_argument": ("build", """
        def build(sp):
            h = sp.get_storage(WorkspaceSession)

            async def handler(row, _write=h.update):
                await _write(row)

            return handler
    """),
    "decorator_of_a_nested_def": ("build", """
        def build(sp, reg):
            h = sp.get_storage(WorkspaceSession)

            @reg(h.update)
            async def handler(row):
                return row

            return handler
    """),
}


@pytest.mark.parametrize("name", CLOSED_SHAPES)
def test_the_scan_follows_a_handle_through(tmp_path: pathlib.Path, name: str) -> None:
    function, source = CLOSED_SHAPES[name]
    header = "from primer.model.workspace_session import WorkspaceSession\n"
    result = scan_sources(tmp_path, a=header + textwrap.dedent(source))
    assert writers(result) == [("primer/a.py", function, "update")]


def test_a_handle_passed_positionally_to_a_staticmethod_lands_on_the_right_parameter(
    tmp_path: pathlib.Path,
) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        class Helper:
            @staticmethod
            async def write(store, row):
                await store.update(row)

            @classmethod
            async def write_cls(cls, store, row):
                await store.update_unless(row, field="a", forbidden=1)

        async def f(sp, row):
            h = sp.get_storage(WorkspaceSession)
            await Helper.write(h, row)
            await Helper.write_cls(h, row)
        """,
    )
    assert writers(result) == [
        ("primer/a.py", "Helper.write", "update"),
        ("primer/a.py", "Helper.write_cls", "update_unless"),
    ]


def test_a_handle_passed_positionally_to_a_method_or_a_constructor_is_followed(
    tmp_path: pathlib.Path,
) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        class Svc:
            def __init__(self, backing, other=None):
                self._backing = backing

            async def write(self, row, store):
                await store.update(row)

            async def via_backing(self, row):
                await self._backing.update_unless(row, field="a", forbidden=1)

        async def f(sp, svc, row):
            handle = sp.get_storage(WorkspaceSession)
            await svc.write(row, handle)
            await Svc(handle).via_backing(row)
        """,
    )
    assert writers(result) == [
        ("primer/a.py", "Svc.via_backing", "update_unless"),
        ("primer/a.py", "Svc.write", "update"),
    ]


def test_two_writes_on_one_line_are_two_sites(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        import asyncio
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, a, b):
            h = sp.get_storage(WorkspaceSession)
            await asyncio.gather(h.update(a), h.update(b))
        """,
    )
    assert result.counts("writers") == {("primer/a.py", "f", "update"): 2}


def test_same_named_defs_in_one_scope_are_separate_keys_by_source_order(
    tmp_path: pathlib.Path,
) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        from primer.model.workspace_session import WorkspaceSession

        def build(sp, flag):
            h = sp.get_storage(WorkspaceSession)
            if flag:
                async def handler(row):
                    await h.update(row)
            else:
                async def handler(row):
                    await h.update(row)
                    await h.update(row)
            return handler
        """,
    )
    assert result.counts("writers") == {
        ("primer/a.py", "build.handler", "update"): 1,
        ("primer/a.py", "build.handler#2", "update"): 2,
    }


def test_a_string_named_like_a_session_store_is_not_a_handle(tmp_path: pathlib.Path) -> None:
    result = scan_sources(
        tmp_path,
        a="""
        def f(session_store_path, session_storage_dir):
            return session_store_path.replace("a", "b"), session_storage_dir.replace("a", "b")
        """,
    )
    assert result.writers == () and result.unresolved == ()


# ---------------------------------------------------------------------------------------------
# the documented limits are real: each shape is still invisible, and the docstring says so.
# When the scan learns one of them, move it to CLOSED_SHAPES and delete it from the docstring.

KNOWN_LIMITS = {
    "generic_rest_crud_through_depends_of_a_parameter": """
        from primer.model.workspace_session import WorkspaceSession

        def make_crud_router(*, model_cls, storage_dep):
            async def _update(entity, storage=Depends(storage_dep)):
                return await storage.update(entity)

        router = make_crud_router(model_cls=WorkspaceSession, storage_dep=get_session_storage)
    """,
    "handle_factory_passed_by_reference": """
        from primer.model.workspace_session import WorkspaceSession

        def make_handler(storage_factory):
            async def _handler(entity):
                await storage_factory().update(entity)
            return _handler

        def build(sp):
            return make_handler(lambda: sp.get_storage(WorkspaceSession))
    """,
    "instance_attribute_read_from_outside_its_class": """
        from primer.model.workspace_session import WorkspaceSession

        class Adapter:
            def __init__(self, sp):
                self._rows = sp.get_storage(WorkspaceSession)

        async def f(sp, row):
            await Adapter(sp)._rows.update(row)
    """,
    "dataclass_field_read_through_a_typed_parameter": """
        from primer.model.workspace_session import WorkspaceSession

        class Deps:
            rows: "Storage[WorkspaceSession]"

        async def f(deps: Deps, row):
            await deps.rows.update(row)
    """,
    "unbound_storage_update": """
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, row):
            await Storage.update(sp.get_storage(WorkspaceSession), row)
    """,
    "keyword_splat_forwarding": """
        from primer.model.workspace_session import WorkspaceSession

        async def write(store, row):
            await store.update(row)

        async def f(sp, row):
            await write(**{"store": sp.get_storage(WorkspaceSession), "row": row})
    """,
    "non_literal_getattr": """
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, row, name):
            await getattr(sp.get_storage(WorkspaceSession), name)(row)
    """,
    "tuple_unpacked_from_a_call": """
        from primer.model.workspace_session import WorkspaceSession

        def pair(sp):
            return sp.get_storage(WorkspaceSession), 1

        async def f(sp, row):
            rows, other = pair(sp)
            await rows.update(row)
    """,
    "reassigned_to_another_model_hides_the_session_handle": """
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, row, make):
            rows = sp.get_storage(Harness)
            rows = make()
            await rows.update(row)
    """,
    "handle_in_a_container": """
        from primer.model.workspace_session import WorkspaceSession

        async def f(sp, row):
            handles = {"s": sp.get_storage(WorkspaceSession)}
            await handles["s"].update(row)
    """,
}


@pytest.mark.parametrize("name", KNOWN_LIMITS)
def test_a_documented_limit_is_still_a_limit(tmp_path: pathlib.Path, name: str) -> None:
    result = scan_sources(tmp_path, a=KNOWN_LIMITS[name])
    assert result.writers == (), (
        f"the scan now sees {name}: move it from KNOWN_LIMITS to CLOSED_SHAPES and delete it "
        "from the module docstring's limits"
    )


def test_an_attribute_chain_named_sessions_is_only_an_unresolved_candidate(
    tmp_path: pathlib.Path,
) -> None:
    result = scan_sources(
        tmp_path, a="async def f(deps, row):\n    await deps.sessions.update(row)\n"
    )
    assert result.writers == ()
    assert [(s.function, s.method) for s in result.unresolved] == [("f", "deps.sessions.update")]


def test_main_prints_the_result_as_json(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture) -> None:
    scan_sources(
        tmp_path,
        a="""
        async def f(session_storage, s):
            await session_storage.update(s)
        """,
    )
    assert scanner.main([str(tmp_path / "primer")]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["writers"] == [["primer/a.py", 3, "f", "update"]]
    assert set(printed) == {"writers", "deletes", "patches", "unresolved"}


def test_a_syntax_error_in_a_scanned_file_is_loud(tmp_path: pathlib.Path) -> None:
    with pytest.raises(SyntaxError):
        scan_sources(tmp_path, a="def f(:\n")
