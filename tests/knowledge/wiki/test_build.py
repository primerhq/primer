"""The wiki build: table of contents, section listings, links, the hash.

Pivot P1 task 9. Each test builds its own source tree under ``tmp_path``; the sweep at
the end builds over every tool of the built-in toolsets.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest

from primer.knowledge.wiki.build import (
    LIVE_LINK_ROOTS,
    WikiBuild,
    build_wiki,
    hash_pages,
    unresolved_links,
    wiki_links,
)
from primer.knowledge.wiki.pages import WikiPage, WikiSourceError
from primer.model.chat import ToolExample
from tests.knowledge.wiki._entries import make_entry, real_entries

DATETIME = make_entry("misc", "get_datetime", purpose="Return the time.")


def _page(title: str, summary: str, body: str = "", **front: object) -> str:
    keys = "".join(f"{key}: {value}\n" for key, value in front.items())
    return f"---\ntitle: {title}\nsummary: {summary}\n{keys}---\n{body}"


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _wiki(root: Path) -> Path:
    return _tree(root, {
        "index.md": _page("Platform wiki", "Every feature an agent can use.", "Start with start-here.\n"),
        "start-here/index.md": _page("Start here", "Read these first.", order=1),
        "start-here/calling-tools.md": _page(
            "Calling tools", "How call_tool works.",
            "See [get_datetime](reference/tools/misc/get_datetime).\n", order=2,
        ),
        "start-here/reading-this-wiki.md": _page("Reading this wiki", "How the wiki is laid out.", order=1),
        "agents/index.md": _page("Agents", "What an agent is.", "Agents run sessions.\n", order=2),
        "agents/create-and-edit.md": _page(
            "Create and edit", "Make an agent.", "Next: [calling tools](start-here/calling-tools).\n",
        ),
    })


def _wp(path: str, body: str = "", kind: str = "guide", **meta: object) -> WikiPage:
    return WikiPage(
        path=path, title=path, summary="s", body=body, kind=kind,  # type: ignore[arg-type]
        meta={"kind": kind, "summary": "s", **meta},
    )


# ---- wiki_links ------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, links",
    [
        ("[a](agents/x) [b](https://e.com) [c](#top)", ["agents/x"]),
        ("[a](agents/x#tool-calls)", ["agents/x"]),
        ("```\n[a](not/a/link)\n```", []),
        # beyond the plan's three
        ("~~~\n[a](not/a/link)\n~~~\n[b](agents/x)", ["agents/x"]),
        ("````\n```\n[a](inner/fence)\n```\n[b](still/hidden)\n````\n[c](agents/x)", ["agents/x"]),
        ("```json\n[a](hidden)\n```\n\nafter [b](agents/x)", ["agents/x"]),
        ("1. step\n\n   ```json\n   [a](hidden)\n   ```\n\n   [b](agents/x)", ["agents/x"]),
        ("```\n[a](an/unclosed/fence)\n[b](also/hidden)", []),
        ("Write `[label](agents/x)` like this, then [real](agents/y).", ["agents/y"]),
        ("Write ``[label](agents/x)`` like this, then [real](agents/y).", ["agents/y"]),
        ("[a](agents/x) and again [b](agents/x)", ["agents/x", "agents/x"]),
        ('[a](agents/x "A title")', ["agents/x"]),
        ("![diagram](agents/diagram)", ["agents/diagram"]),
        ("[mail](mailto:a@b.c) [site](HTTP://E.COM/x#y) [safe](http://e.com)", []),
        ("[a \\[b\\]](agents/x)", ["agents/x"]),
        ("[a]\n(not/a/link)", []),
        ("[a] (not/a/link) and [b]()", []),
        # a label may hold balanced brackets (one level), as CommonMark allows
        ("[see [x] here](agents/x)", ["agents/x"]),
        ("[Create an agent [admin]](agents/create-and-edit#tool-calls)", ["agents/create-and-edit"]),
        ("[a [b](agents/x)", ["agents/x"]),
        ("[a](/agents/x) [b](agents/x.md)", ["/agents/x", "agents/x.md"]),
    ],
)
def test_wiki_links(body, links):
    assert wiki_links(body) == links


# ---- the table of contents ---------------------------------------------------------


def test_the_table_of_contents_lists_sections_and_guides_with_summaries(tmp_path):
    build = build_wiki(_wiki(tmp_path), [])
    toc = build.pages["index"].body
    assert "- [Start here](start-here) - " in toc and "- [Calling tools](start-here/calling-tools) - " in toc
    assert "- [Reference](reference) - " in toc and "- [This install](this-install) - " in toc


TOC = """\
# Platform wiki

Start with start-here.

## Contents

- [Start here](start-here) - Read these first.
  - [Reading this wiki](start-here/reading-this-wiki) - How the wiki is laid out.
  - [Calling tools](start-here/calling-tools) - How call_tool works.
- [Agents](agents) - What an agent is.
  - [Create and edit](agents/create-and-edit) - Make an agent.
- [Reference](reference) - every agent-callable tool, generated from the live registry
- [This install](this-install) - live list of agents, graphs, collections, workspaces and providers
"""


def test_the_table_of_contents_has_exactly_this_shape(tmp_path):
    page = build_wiki(_wiki(tmp_path), [DATETIME]).pages["index"]
    assert page.body == TOC
    assert (page.kind, page.title, page.summary, page.path) == (
        "toc", "Platform wiki", "Every feature an agent can use.", "index",
    )
    assert page.meta == {"kind": "toc", "summary": "Every feature an agent can use."}


def test_the_table_of_contents_nests_deeper_sections(tmp_path):
    _wiki(tmp_path)
    _tree(tmp_path, {
        "agents/prompts/index.md": _page("Prompts", "Writing prompts.", order=1),
        "agents/prompts/parts.md": _page("Parts", "Prompt parts."),
        "glossary.md": _page("Glossary", "Terms.", order=3),
    })
    body = build_wiki(tmp_path, []).pages["index"].body
    lines = body.split("## Contents\n\n", 1)[1].splitlines()
    assert lines[:8] == [
        "- [Start here](start-here) - Read these first.",
        "  - [Reading this wiki](start-here/reading-this-wiki) - How the wiki is laid out.",
        "  - [Calling tools](start-here/calling-tools) - How call_tool works.",
        "- [Agents](agents) - What an agent is.",
        "  - [Prompts](agents/prompts) - Writing prompts.",
        "    - [Parts](agents/prompts/parts) - Prompt parts.",
        "  - [Create and edit](agents/create-and-edit) - Make an agent.",
        "- [Glossary](glossary) - Terms.",
    ]


def test_without_an_index_page_the_table_of_contents_has_the_default_title(tmp_path):
    build = build_wiki(tmp_path / "absent", [DATETIME])
    toc = build.pages["index"]
    assert (toc.title, toc.kind) == ("Platform wiki", "toc")
    assert toc.summary == "Every platform feature an agent can use, and the exact tool calls for it."
    assert toc.body == (
        "# Platform wiki\n\n## Contents\n\n"
        "- [Reference](reference) - every agent-callable tool, generated from the live registry\n"
        "- [This install](this-install) - live list of agents, graphs, collections, workspaces and providers\n"
    )
    assert unresolved_links(build.pages) == []


def test_the_index_pages_own_keys_survive_into_the_table_of_contents(tmp_path):
    _tree(tmp_path, {"index.md": _page("Docs home", "The front door.", "Hello.\n", audience="admin")})
    page = build_wiki(tmp_path, []).pages["index"]
    assert (page.title, page.summary) == ("Docs home", "The front door.")
    assert page.meta == {"kind": "toc", "summary": "The front door.", "audience": "admin"}
    assert page.body.startswith("# Docs home\n\nHello.\n\n## Contents\n")


def _legacy(path: str, title: str, summary: str) -> WikiPage:
    return WikiPage(path, title, summary, f"# {title}\n", "legacy", {"kind": "legacy", "summary": summary})


def test_legacy_roots_are_listed_under_legacy(tmp_path):
    legacy = {
        "docs": _legacy("docs", "Docs", "The shipped docs."),
        "docs/guide": _legacy("docs/guide", "Guide", "A shipped guide."),
        "how-to": _legacy("how-to", "How to", "Task-shaped guides."),
        "how-to/create-an-agent": _legacy("how-to/create-an-agent", "Create an agent", "Steps."),
    }
    build = build_wiki(_wiki(tmp_path), [], legacy_pages=legacy)
    toc = build.pages["index"].body
    assert toc.endswith(
        "\n## Legacy (being replaced)\n\n"
        "- [Docs](docs) - The shipped docs.\n"
        "- [How to](how-to) - Task-shaped guides.\n"
    )
    assert "(docs/guide)" not in toc and "(how-to/create-an-agent)" not in toc
    assert all(build.pages[path] is page for path, page in legacy.items())


def test_a_legacy_root_without_a_summary_is_listed_without_a_dash(tmp_path):
    build = build_wiki(_wiki(tmp_path), [], legacy_pages={"docs": _legacy("docs", "Docs", "")})
    assert build.pages["index"].body.endswith("## Legacy (being replaced)\n\n- [Docs](docs)\n")


def test_no_legacy_pages_means_no_legacy_section(tmp_path):
    for legacy in (None, {}):
        toc = build_wiki(_wiki(tmp_path), [], legacy_pages=legacy).pages["index"].body
        assert "Legacy" not in toc and "## Contents" in toc


# ---- section pages -------------------------------------------------------------------


def test_a_section_page_lists_its_children(tmp_path):
    build = build_wiki(_wiki(tmp_path), [DATETIME])
    assert build.pages["start-here"].body == (
        "# Start here\n\n## Pages\n\n"
        "- [Reading this wiki](start-here/reading-this-wiki) - How the wiki is laid out.\n"
        "- [Calling tools](start-here/calling-tools) - How call_tool works.\n"
    )
    assert build.pages["agents"].body == (
        "# Agents\n\nAgents run sessions.\n\n## Pages\n\n"
        "- [Create and edit](agents/create-and-edit) - Make an agent.\n"
    )
    assert build.pages["start-here"].kind == "section"
    assert build.pages["start-here"].meta == {"kind": "section", "summary": "Read these first.", "order": 1}


def test_a_section_with_no_children_says_none(tmp_path):
    _tree(tmp_path, {"index.md": _page("Wiki", "S."), "empty/index.md": _page("Empty", "Nothing here.", "Intro.\n")})
    assert build_wiki(tmp_path, []).pages["empty"].body == "# Empty\n\nIntro.\n\n## Pages\n\n_(none)_\n"


def test_siblings_sort_by_order_then_title(tmp_path):
    _tree(tmp_path, {
        "index.md": _page("Wiki", "S."),
        "s/index.md": _page("S", "Section."),
        "s/zz-first.md": _page("Zebra", "z", order=1),
        "s/b.md": _page("beta", "b"),
        "s/a.md": _page("Alpha", "a"),
        "s/c.md": _page("Alpha", "same title as a, later path"),
        "s/neg.md": _page("Negative", "n", order=-5),
        "s/tie.md": _page("Tie", "t", order=1),
    })
    lines = build_wiki(tmp_path, []).pages["s"].body.split("## Pages\n\n", 1)[1].splitlines()
    assert [ln.split("](")[1].split(")")[0] for ln in lines] == [
        "s/neg",         # order -5
        "s/tie",         # order 1, title Tie sorts before Zebra
        "s/zz-first",    # order 1
        "s/a",           # unordered after ordered; Alpha (title, case-insensitive) then path
        "s/c",
        "s/b",           # beta
    ]


def test_a_title_with_brackets_keeps_its_link_intact(tmp_path):
    _tree(tmp_path, {"index.md": _page("Wiki", "S."), "g.md": _page("'Tools [admin]'", "S.")})
    body = build_wiki(tmp_path, []).pages["index"].body
    assert "- [Tools \\[admin\\]](g) - S." in body
    assert "g" in wiki_links(body)


# ---- reference pages and used_in -------------------------------------------------------


def test_the_build_includes_the_reference_pages(tmp_path):
    build = build_wiki(_wiki(tmp_path), [DATETIME])
    assert {"reference", "reference/tools", "reference/tools/misc", "reference/tools/misc/get_datetime"} <= set(build.pages)
    assert build.pages["reference/tools/misc/get_datetime"].kind == "reference"


def test_used_in_links_back_to_the_feature_page(tmp_path):
    build = build_wiki(_wiki(tmp_path), [DATETIME])
    body = build.pages["reference/tools/misc/get_datetime"].body
    assert body.endswith("## Used in\n\n- [start-here/calling-tools](start-here/calling-tools)\n")


def test_a_tool_no_feature_page_links_is_not_yet_documented(tmp_path):
    other = make_entry("misc", "echo")
    build = build_wiki(_wiki(tmp_path), [DATETIME, other])
    assert build.pages["reference/tools/misc/echo"].body.endswith(
        "## Used in\n\n_(not yet documented in a feature page)_\n"
    )


def test_only_guide_and_section_links_count_as_used_in(tmp_path):
    _tree(tmp_path, {
        "index.md": _page("Wiki", "S.", "The intro links [echo](reference/tools/misc/echo).\n"),
        "g.md": _page("G", "S.", "Docs [echo](reference/tools/misc/echo#top) and `[x](reference/tools/misc/echo)`.\n"),
    })
    build = build_wiki(tmp_path, [make_entry("misc", "echo")])
    assert build.pages["reference/tools/misc/echo"].body.endswith("## Used in\n\n- [g](g)\n")


def test_a_section_pages_own_links_count_as_used_in(tmp_path):
    _tree(tmp_path, {
        "index.md": _page("Wiki", "S."),
        "s/index.md": _page("S", "S.", "See [echo](reference/tools/misc/echo).\n"),
    })
    build = build_wiki(tmp_path, [make_entry("misc", "echo")])
    assert build.pages["reference/tools/misc/echo"].body.endswith("## Used in\n\n- [s](s)\n")


# ---- unresolved links ---------------------------------------------------------------------


def test_unresolved_links_are_found_and_live_roots_allowed(tmp_path):
    _tree(tmp_path, {
        "index.md": _page("Wiki", "S."),
        "g.md": _page(
            "G", "S.",
            "[ok](reference) [ok2](this-install) [ok3](this-install/agents) [ok4](this-install/providers) "
            "[ok5](g#anchor) [ok6](https://example.com/x) [ok7](mailto:a@b.c)\n"
            "[nope](agents/missing) [nope2](this-install/agents/agent-a) [nope3](/g) [nope4](g.md)\n"
            "`[in-code](not/checked)`\n\n```\n[in-fence](not/checked/either)\n```\n"
            "[nope](agents/missing) again\n",
        ),
    })
    build = build_wiki(tmp_path, [])
    assert unresolved_links(build.pages) == [
        ("g", "agents/missing"), ("g", "this-install/agents/agent-a"), ("g", "/g"), ("g", "g.md"),
    ]
    assert LIVE_LINK_ROOTS == {
        "this-install", "this-install/agents", "this-install/graphs", "this-install/collections",
        "this-install/workspaces", "this-install/providers",
    }


def test_a_link_to_a_reference_page_resolves_only_when_the_tool_exists(tmp_path):
    _wiki(tmp_path)
    assert unresolved_links(build_wiki(tmp_path, []).pages) == [
        ("start-here/calling-tools", "reference/tools/misc/get_datetime"),
    ]
    assert unresolved_links(build_wiki(tmp_path, [DATETIME]).pages) == []


def test_related_entries_must_resolve_too(tmp_path):
    _tree(tmp_path, {
        "index.md": _page("Wiki", "S."),
        "a.md": _page("A", "S.", related="[b, missing, reference/tools/misc/echo]"),
        "b.md": _page("B", "S."),
    })
    build = build_wiki(tmp_path, [make_entry("misc", "echo")])
    assert unresolved_links(build.pages) == [("a", "missing")]


def test_a_related_entry_may_carry_an_anchor_like_a_body_link(tmp_path):
    _tree(tmp_path, {
        "index.md": _page("Wiki", "S."),
        "a.md": _page("A", "S.", related="[b#roles, missing#roles]"),
        "b.md": _page("B", "S."),
    })
    assert unresolved_links(build_wiki(tmp_path, []).pages) == [("a", "missing")]


def test_a_legacy_pages_links_are_not_checked():
    pages = {
        "legacy": _wp("legacy", "[old](/tools) [older](no/such)", kind="legacy"),
        "guide": _wp("guide", "[ok](legacy) [bad](no/such)"),
    }
    assert unresolved_links(pages) == [("guide", "no/such")]


def test_unresolved_links_come_back_in_page_order_then_reading_order():
    pages = {
        "b": _wp("b", "[x](zz) [y](aa)"),
        "a": _wp("a", "[x](qq)"),
    }
    assert unresolved_links(pages) == [("a", "qq"), ("b", "zz"), ("b", "aa")]


def test_the_build_logs_each_unresolved_link_and_does_not_raise(tmp_path, caplog):
    _wiki(tmp_path)
    with caplog.at_level(logging.WARNING, logger="primer.knowledge.wiki.build"):
        build = build_wiki(tmp_path, [])
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "primer.knowledge.wiki.build"]
    assert len(warnings) == 1
    assert "start-here/calling-tools" in warnings[0].getMessage()
    assert "reference/tools/misc/get_datetime" in warnings[0].getMessage()
    assert isinstance(build, WikiBuild)


# ---- a build without hand-written pages -------------------------------------------------------------


def test_a_missing_source_folder_is_logged_and_the_build_holds_only_what_is_generated(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="primer.knowledge.wiki.build"):
        build = build_wiki(tmp_path / "absent", [DATETIME], legacy_pages={"docs": _legacy("docs", "Docs", "Old.")})
    assert sorted(build.pages) == [
        "docs", "index", "reference", "reference/tools", "reference/tools/misc", "reference/tools/misc/get_datetime",
    ]
    assert not [p for p in build.pages.values() if p.kind in ("guide", "section")]
    warnings = [r for r in caplog.records if r.name == "primer.knowledge.wiki.build" and r.levelno == logging.WARNING]
    assert len(warnings) == 1 and str(tmp_path / "absent") in warnings[0].getMessage()


def test_an_empty_source_folder_is_logged_too(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="primer.knowledge.wiki.build"):
        build_wiki(tmp_path, [])
    assert [r.name for r in caplog.records if r.levelno == logging.WARNING] == ["primer.knowledge.wiki.build"]


def test_a_source_folder_with_pages_is_not_logged(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="primer.knowledge.wiki.build"):
        build_wiki(_wiki(tmp_path), [DATETIME])
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


# ---- the hash ---------------------------------------------------------------------------------


def _digest(pages: dict[str, WikiPage]) -> str:
    parts: list[str] = []
    for path in sorted(pages):
        page = pages[path]
        parts += [path, page.title, json.dumps(page.meta, sort_keys=True), page.body]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def test_the_hash_is_the_documented_digest(tmp_path):
    build = build_wiki(_wiki(tmp_path), [DATETIME])
    assert build.content_hash == _digest(build.pages) == hash_pages(build.pages)
    assert len(build.content_hash) == 64


def test_the_digest_of_two_known_pages_is_pinned():
    pages = {
        "b": WikiPage("b", "B", "s", "# B\n", "guide", {"kind": "guide", "summary": "s"}),
        "a": WikiPage("a", "A", "s", "# A\n", "guide", {"summary": "s", "kind": "guide", "order": 1}),
    }
    expected = hashlib.sha256(
        b'a\x00A\x00{"kind": "guide", "order": 1, "summary": "s"}\x00# A\n\x00b\x00B\x00{"kind": "guide", "summary": "s"}\x00# B\n'
    ).hexdigest()
    assert hash_pages(pages) == expected
    assert hash_pages(dict(reversed(pages.items()))) == expected


def test_the_hash_is_stable(tmp_path):
    first = build_wiki(_wiki(tmp_path), [DATETIME])
    second = build_wiki(_wiki(tmp_path), [DATETIME])
    assert first.content_hash == second.content_hash and first.pages == second.pages
    assert list(first.pages) == sorted(first.pages)


def test_the_hash_changes_with_one_body(tmp_path):
    base = build_wiki(_wiki(tmp_path), [DATETIME]).content_hash
    (tmp_path / "agents" / "create-and-edit.md").write_text(
        _page("Create and edit", "Make an agent.", "Next: [calling tools](start-here/calling-tools). Changed.\n"),
        encoding="utf-8",
    )
    assert build_wiki(tmp_path, [DATETIME]).content_hash != base


@pytest.mark.parametrize(
    "path, text",
    [
        ("agents/create-and-edit.md", _page("Create and edit", "A different summary.", "Next: [calling tools](start-here/calling-tools).\n")),
        ("agents/create-and-edit.md", _page("Create and edit", "Make an agent.", "Next: [calling tools](start-here/calling-tools).\n", order=9)),
        ("agents/create-and-edit.md", _page("Create or edit", "Make an agent.", "Next: [calling tools](start-here/calling-tools).\n")),
    ],
)
def test_the_hash_changes_with_a_summary_an_order_or_a_title(tmp_path, path, text):
    base = build_wiki(_wiki(tmp_path), [DATETIME]).content_hash
    (tmp_path / path).write_text(text, encoding="utf-8")
    assert build_wiki(tmp_path, [DATETIME]).content_hash != base


def test_the_hash_changes_with_a_tool_the_wiki_documents(tmp_path):
    _wiki(tmp_path)
    base = build_wiki(tmp_path, [DATETIME]).content_hash
    assert build_wiki(tmp_path, [make_entry("misc", "get_datetime", purpose="Return the clock.")]).content_hash != base
    assert build_wiki(tmp_path, [make_entry("misc", "get_datetime", purpose="Return the time.", role="admin")]).content_hash != base
    changed_example = make_entry("misc", "get_datetime", purpose="Return the time.", examples=[ToolExample(args={"tz": "UTC"})])
    assert build_wiki(tmp_path, [changed_example]).content_hash != base
    assert build_wiki(tmp_path, [DATETIME, make_entry("misc", "echo")]).content_hash != base


def test_the_hash_does_not_depend_on_the_order_of_the_entries(tmp_path):
    _wiki(tmp_path)
    echo = make_entry("misc", "echo")
    assert build_wiki(tmp_path, [DATETIME, echo]).content_hash == build_wiki(tmp_path, [echo, DATETIME]).content_hash


def test_the_hash_covers_the_legacy_pages(tmp_path):
    _wiki(tmp_path)
    base = build_wiki(tmp_path, [DATETIME]).content_hash
    one = {"docs": _wp("docs", "# Docs\n", kind="legacy")}
    two = {"docs": _wp("docs", "# Docs\n\nchanged\n", kind="legacy")}
    hashes = {build_wiki(tmp_path, [DATETIME], legacy_pages=pages).content_hash for pages in (one, two)}
    assert len(hashes) == 2 and base not in hashes


def test_the_hash_is_identical_across_processes_and_hash_seeds(tmp_path):
    """The stamp compares build hashes across processes, and pods of a
    rolling deploy run with different ``PYTHONHASHSEED``; a seed-dependent
    digest would let an identical fleet of builds fight the stamp forever,
    so two fresh interpreters must agree on the production-shape digest."""
    import os
    import subprocess
    import sys

    src = tmp_path / "src"
    _wiki(src)
    snippet = (
        "import pathlib, sys\n"
        "from primer.knowledge.wiki.build import build_wiki\n"
        "from tests.knowledge.wiki._entries import real_entries\n"
        "print(build_wiki(pathlib.Path(sys.argv[1]), real_entries()).content_hash)\n"
    )
    hashes = {
        subprocess.run(
            [sys.executable, "-c", snippet, str(src)],
            capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout.strip()
        for seed in ("0", "42")
    }
    assert len(hashes) == 1
    assert len(next(iter(hashes))) == 64


# ---- source mistakes ------------------------------------------------------------------------------


def test_a_malformed_page_raises_a_source_error_naming_the_file(tmp_path):
    _wiki(tmp_path)
    (tmp_path / "start-here" / "broken.md").write_text("no frontmatter\n", encoding="utf-8")
    with pytest.raises(WikiSourceError, match=r"start-here/broken\.md"):
        build_wiki(tmp_path, [DATETIME])


@pytest.mark.parametrize(
    "files",
    [
        {"reference/index.md": _page("X", "S.")},
        {"reference.md": _page("X", "S.")},
        {"reference/index.md": _page("X", "S."), "reference/tools.md": _page("Y", "S.")},
        {"this-install/index.md": _page("X", "S.")},
        {"this-install.md": _page("X", "S.")},
        {"this-install/index.md": _page("X", "S."), "this-install/agents.md": _page("Y", "S.")},
    ],
)
def test_a_source_page_cannot_sit_on_a_generated_or_live_path(tmp_path, files):
    _tree(tmp_path, {"index.md": _page("Wiki", "S."), **files})
    with pytest.raises(WikiSourceError, match="reserved"):
        build_wiki(tmp_path, [DATETIME])


def test_a_legacy_page_cannot_shadow_another_page(tmp_path):
    _wiki(tmp_path)
    for clash in ("agents", "reference", "index", "reference/tools/misc/get_datetime"):
        with pytest.raises(WikiSourceError, match=f"{clash}: two pages"):
            build_wiki(tmp_path, [DATETIME], legacy_pages={clash: _wp(clash, "x", kind="legacy")})


# ---- the production shape -----------------------------------------------------------------------------


def test_a_wiki_over_every_built_in_tool_has_no_unresolved_link_and_a_stable_hash(tmp_path):
    entries = real_entries()
    root = _wiki(tmp_path)
    linked = entries[:3]
    _tree(root, {"agents/tools.md": _page(
        "Tools", "All tools.",
        "".join(f"[{e.scoped_id}](reference/tools/{e.toolset_id}/{e.tool.id})\n" for e in linked),
    )})
    build = build_wiki(root, entries[3:] + linked)
    assert unresolved_links(build.pages) == []
    toolsets = {e.toolset_id for e in entries}
    tool_pages = [p for p in build.pages if p.startswith("reference/tools/") and p.count("/") == 3]
    assert len(tool_pages) == len(entries)
    assert {p for p in build.pages if p.startswith("reference/tools/") and p.count("/") == 2} == {
        f"reference/tools/{t}" for t in toolsets
    }
    # no generated page grows a markdown link out of a tool's own text (its Used in section does link)
    assert [p for p in tool_pages if wiki_links(build.pages[p].body.split("## Used in")[0])] == []
    assert build.content_hash == build_wiki(root, entries).content_hash == _digest(build.pages)
    for entry in linked:
        body = build.pages[f"reference/tools/{entry.toolset_id}/{entry.tool.id}"].body
        assert body.endswith("## Used in\n\n- [agents/tools](agents/tools)\n")
