"""Wiki source pages: the frontmatter rules, the file-to-path mapping, the kinds.

Pivot P1 task 8. These are the rules the hand-written ``docs/wiki/`` pages are
held to ("Page format" in the plan); every test builds its own tree under
``tmp_path`` and none reads the repository's wiki.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import primer
from primer.knowledge.wiki.pages import (
    SUMMARY_MAX,
    WIKI_DIR_ENV,
    WikiPage,
    WikiSourceError,
    load_source_pages,
    parse_page_source,
    resolve_wiki_dir,
)

PAGE = "---\ntitle: A page\nsummary: One line.\n---\nBody.\n"


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


# ---- parse_page_source -------------------------------------------------------


def test_frontmatter_becomes_title_summary_and_meta():
    page = parse_page_source(
        "---\ntitle: Calling tools\nsummary: How call_tool works.\n"
        "tools: [system__call_tool]\norder: 2\n---\n## What it is\nx\n",
        path="start-here/calling-tools",
    )
    assert (page.title, page.summary, page.kind) == (
        "Calling tools", "How call_tool works.", "guide",
    )
    assert page.meta == {
        "kind": "guide",
        "summary": "How call_tool works.",
        "tools": ["system__call_tool"],
        "order": 2,
    }
    assert page.body == "# Calling tools\n\n## What it is\nx\n"
    assert page.path == "start-here/calling-tools"


@pytest.mark.parametrize(
    "text, rule",
    [
        ("no frontmatter\n", "frontmatter"),
        ("---\ntitle: T\n---\nx\n", "summary"),
        ("---\ntitle: T\nsummary: S\nauthor: me\n---\nx\n", "unknown key 'author'"),
        ("---\ntitle: T\nsummary: |\n  two\n  lines\n---\nx\n", "one line"),
        ("---\ntitle: T\nsummary: S\n---\n# T\nx\n", "heading"),
        ("---\ntitle: T\nsummary: S\naudience: root\n---\nx\n", "audience"),
        # beyond the plan's six: every other rule of the Page format
        ("---\ntitle: T\nsummary: S\n", "not closed"),
        ("---\ntitle: [unclosed\nsummary: S\n---\nx\n", "not valid YAML"),
        ("---\n- a\n- b\n---\nx\n", "mapping"),
        ("---\n---\nx\n", "title is required"),
        ("---\nsummary: S\n---\nx\n", "title is required"),
        ("---\ntitle: 2026\nsummary: S\n---\nx\n", "title must be a string"),
        ("---\ntitle: [a]\nsummary: S\n---\nx\n", "title must be a string"),
        ("---\ntitle: \"\"\nsummary: S\n---\nx\n", "title is required"),
        ("---\ntitle: |\n  two\n  lines\nsummary: S\n---\nx\n", "title must be one line"),
        ("---\ntitle: T\nsummary: 7\n---\nx\n", "summary must be a string"),
        ("---\ntitle: T\nsummary: \"\"\n---\nx\n", "summary is required"),
        ("---\ntitle: T\nsummary: S\ntools: crud__create_agent\n---\nx\n", "tools must be a list"),
        ("---\ntitle: T\nsummary: S\ntools: [1]\n---\nx\n", "tools must be a list"),
        ("---\ntitle: T\nsummary: S\nrelated: [a, \"\"]\n---\nx\n", "related must be a list"),
        ("---\ntitle: T\nsummary: S\norder: \"2\"\n---\nx\n", "order must be an integer"),
        ("---\ntitle: T\nsummary: S\norder: true\n---\nx\n", "order must be an integer"),
        ("---\ntitle: T\nsummary: S\norder: 1.5\n---\nx\n", "order must be an integer"),
    ],
)
def test_a_bad_page_is_refused_with_the_rule_it_broke(text, rule):
    with pytest.raises(WikiSourceError, match=re.escape(rule)):
        parse_page_source(text, path="a/b")


def test_a_summary_over_the_limit_is_refused_and_the_limit_itself_passes():
    ok = "x" * SUMMARY_MAX
    page = parse_page_source(f"---\ntitle: T\nsummary: {ok}\n---\nx\n", path="a")
    assert page.summary == ok and SUMMARY_MAX == 160
    with pytest.raises(WikiSourceError, match="at most 160 characters"):
        parse_page_source(f"---\ntitle: T\nsummary: {ok}y\n---\nx\n", path="a")


def test_the_message_names_the_file_when_the_caller_knows_it():
    with pytest.raises(WikiSourceError) as named:
        parse_page_source("no frontmatter\n", path="a/b", source="a/b.md")
    assert str(named.value).startswith("a/b.md: ")
    with pytest.raises(WikiSourceError) as unnamed:
        parse_page_source("no frontmatter\n", path="a/b")
    assert str(unnamed.value).startswith("a/b: ")


def test_audience_is_in_the_meta_only_when_the_page_sets_it():
    plain = parse_page_source(PAGE, path="a")
    assert "audience" not in plain.meta
    for audience in ("user", "admin"):
        page = parse_page_source(
            f"---\ntitle: T\nsummary: S\naudience: {audience}\n---\nx\n", path="a",
        )
        assert page.meta["audience"] == audience


def test_every_optional_key_lands_in_the_meta():
    page = parse_page_source(
        "---\n"
        "title: T\n"
        "summary: S\n"
        "# a YAML comment is fine\n"
        "audience: admin\n"
        "tools: [crud__create_agent, misc__get_datetime]\n"
        "related: [agents/create-and-edit, reference/tools/misc/get_datetime]\n"
        "order: -3\n"
        "---\n"
        "x\n",
        path="a",
    )
    assert page.meta == {
        "kind": "guide",
        "summary": "S",
        "audience": "admin",
        "tools": ["crud__create_agent", "misc__get_datetime"],
        "related": ["agents/create-and-edit", "reference/tools/misc/get_datetime"],
        "order": -3,
    }


def test_title_and_summary_are_stripped():
    page = parse_page_source(
        "---\ntitle: '  Padded  '\nsummary: '  Also padded.  '\n---\nx\n", path="a",
    )
    assert (page.title, page.summary) == ("Padded", "Also padded.")
    assert page.body.startswith("# Padded\n")


def test_the_kind_argument_is_the_pages_kind_and_goes_into_the_meta():
    page = parse_page_source(PAGE, path="a", kind="section")
    assert page.kind == "section" and page.meta["kind"] == "section"


def test_only_a_heading_at_the_top_of_the_body_is_refused():
    # a '# ' line deeper in the body (a shell comment in a code block) is not a heading
    # the page opens with, a '##' section is the intended start, and '#tag' is no heading
    body = "## What it is\n\n```sh\n# a comment\n```\n\n#tag\n"
    page = parse_page_source(f"---\ntitle: T\nsummary: S\n---\n{body}", path="a")
    assert page.body == f"# T\n\n{body}"
    # blank lines before the heading do not hide it
    with pytest.raises(WikiSourceError, match="heading"):
        parse_page_source("---\ntitle: T\nsummary: S\n---\n\n\n# T\nx\n", path="a")


def test_a_frontmatter_only_page_has_just_its_heading():
    page = parse_page_source("---\ntitle: Start here\nsummary: S\n---\n", path="start-here")
    assert page.body.startswith("# Start here\n") and page.body.strip() == "# Start here"


def test_crlf_and_a_byte_order_mark_are_tolerated():
    text = chr(0xFEFF) + "---\r\ntitle: T\r\nsummary: S\r\n---\r\nline one\r\nline two\r\n"
    page = parse_page_source(text, path="a")
    assert page.body == "# T\n\nline one\nline two\n"


def test_a_horizontal_rule_in_the_body_is_not_a_second_frontmatter():
    page = parse_page_source(
        "---\ntitle: T\nsummary: S\n---\nabove\n\n---\n\nbelow\n", path="a",
    )
    assert page.body == "# T\n\nabove\n\n---\n\nbelow\n"


def test_a_page_is_a_frozen_value():
    page = parse_page_source(PAGE, path="a")
    assert isinstance(page, WikiPage)
    with pytest.raises(AttributeError):
        page.title = "other"  # type: ignore[misc]


# ---- load_source_pages ---------------------------------------------------------


def test_load_maps_files_to_paths_and_kinds(tmp_path):
    _tree(tmp_path, {
        "index.md": PAGE,
        "start-here/index.md": PAGE,
        "start-here/calling-tools.md": PAGE,
        "_template.md": "junk",
    })
    pages = load_source_pages(tmp_path)
    assert {p: v.kind for p, v in pages.items()} == {
        "index": "toc",
        "start-here": "section",
        "start-here/calling-tools": "guide",
    }
    assert all(v.meta["kind"] == v.kind for v in pages.values())
    assert list(pages) == sorted(pages)


def test_a_nested_section_and_a_guide_next_to_the_root_index(tmp_path):
    _tree(tmp_path, {
        "index.md": PAGE,
        "glossary.md": PAGE,
        "agents/index.md": PAGE,
        "agents/tools/index.md": PAGE,
        "agents/tools/grants.md": PAGE,
    })
    assert {p: v.kind for p, v in load_source_pages(tmp_path).items()} == {
        "index": "toc",
        "glossary": "guide",
        "agents": "section",
        "agents/tools": "section",
        "agents/tools/grants": "guide",
    }


def test_files_that_are_not_pages_are_ignored(tmp_path):
    _tree(tmp_path, {
        "index.md": PAGE,
        "_template.md": "no frontmatter at all",
        "agents/index.md": PAGE,
        "agents/_draft.md": "no frontmatter at all",
        "agents/diagram.png": "not text",
        "agents/notes.txt": "not markdown",
        ".obsidian/workspace.md": "tooling",
        ".hidden.md": "tooling",
    })
    assert sorted(load_source_pages(tmp_path)) == ["agents", "index"]


def test_a_folder_without_an_index_is_refused(tmp_path):
    _tree(tmp_path, {"index.md": PAGE, "agents/create-and-edit.md": PAGE})
    with pytest.raises(WikiSourceError) as err:
        load_source_pages(tmp_path)
    assert str(err.value) == "agents: a folder needs an index.md"


def test_an_ancestor_folder_without_an_index_is_refused(tmp_path):
    _tree(tmp_path, {"index.md": PAGE, "agents/index.md": PAGE, "agents/deep/er/page.md": PAGE})
    with pytest.raises(WikiSourceError, match=r"agents/deep: a folder needs an index\.md"):
        load_source_pages(tmp_path)


def test_the_root_needs_an_index_too(tmp_path):
    _tree(tmp_path, {"agents/index.md": PAGE})
    with pytest.raises(WikiSourceError, match=re.escape(f"{tmp_path}: a folder needs an index.md")):
        load_source_pages(tmp_path)


def test_a_folder_holding_only_ignored_files_needs_no_index(tmp_path):
    _tree(tmp_path, {"index.md": PAGE, "scratch/_notes.md": "x", "scratch/pic.png": "x"})
    assert sorted(load_source_pages(tmp_path)) == ["index"]


@pytest.mark.parametrize(
    "bad", ["Bad_Name.md", "UPPER.md", "with space.md", "dotted.name.md", "under_score/index.md"],
)
def test_a_path_segment_outside_the_slug_charset_is_refused(tmp_path, bad):
    _tree(tmp_path, {"index.md": PAGE, bad: PAGE})
    with pytest.raises(WikiSourceError) as err:
        load_source_pages(tmp_path)
    assert bad in str(err.value) and "[a-z0-9-]+" in str(err.value)


def test_two_files_cannot_define_one_page(tmp_path):
    _tree(tmp_path, {"index.md": PAGE, "agents.md": PAGE, "agents/index.md": PAGE})
    with pytest.raises(WikiSourceError, match=r"agents: two files define this page"):
        load_source_pages(tmp_path)


def test_an_error_names_the_file_with_its_extension(tmp_path):
    _tree(tmp_path, {
        "index.md": PAGE,
        "start-here/index.md": PAGE,
        "start-here/broken.md": "no frontmatter\n",
    })
    with pytest.raises(WikiSourceError) as err:
        load_source_pages(tmp_path)
    assert str(err.value).startswith("start-here/broken.md: ")


def test_a_file_that_is_not_utf8_is_a_source_error(tmp_path):
    _tree(tmp_path, {"index.md": PAGE})
    (tmp_path / "bad.md").write_bytes(b"---\ntitle: T\nsummary: S\n---\n\xff\xfe\n")
    with pytest.raises(WikiSourceError, match=r"bad\.md: .*UTF-8"):
        load_source_pages(tmp_path)


def test_a_missing_root_is_an_empty_wiki(tmp_path):
    assert load_source_pages(tmp_path / "absent") == {}


def test_an_empty_root_is_an_empty_wiki(tmp_path):
    assert load_source_pages(tmp_path) == {}


def test_a_root_that_is_a_file_is_refused(tmp_path):
    target = tmp_path / "wiki.md"
    target.write_text(PAGE, encoding="utf-8")
    with pytest.raises(WikiSourceError, match="not a directory"):
        load_source_pages(target)


def test_loading_twice_gives_equal_pages(tmp_path):
    _tree(tmp_path, {"index.md": PAGE, "a/index.md": PAGE, "a/b.md": PAGE})
    assert load_source_pages(tmp_path) == load_source_pages(tmp_path)


# ---- resolve_wiki_dir ----------------------------------------------------------


def test_the_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(WIKI_DIR_ENV, str(tmp_path))
    assert resolve_wiki_dir() == tmp_path
    assert WIKI_DIR_ENV == "PRIMER_WIKI_DIR"


def test_without_the_override_it_is_docs_wiki_beside_the_package(monkeypatch):
    monkeypatch.delenv(WIKI_DIR_ENV, raising=False)
    expected = Path(primer.__file__).resolve().parent.parent / "docs" / "wiki"
    assert resolve_wiki_dir() == expected


def test_an_empty_override_counts_as_unset(monkeypatch):
    monkeypatch.setenv(WIKI_DIR_ENV, "")
    assert resolve_wiki_dir().parts[-2:] == ("docs", "wiki")
