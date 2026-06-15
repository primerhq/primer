import shutil
from pathlib import Path

import pytest

from scripts.docs.build_site import DocsLintError, build_site


def test_builds_a_page_per_doc(tmp_path):
    out = tmp_path / "dist"
    build_site(Path("primer/user_docs"), out)
    assert (out / "getting-started" / "introduction" / "index.html").exists()
    home = (out / "getting-started" / "introduction" / "index.html").read_text()
    assert "Features" in home and "LLM Providers" in home


def test_excludes_internal_meta_authoring_docs(tmp_path):
    out = tmp_path / "dist"
    build_site(Path("primer/user_docs"), out)
    # _meta is writer guidance, not public docs: no pages built for it.
    assert not (out / "_meta").exists()


def test_emits_404_and_sitemap(tmp_path):
    out = tmp_path / "dist"
    build_site(Path("primer/user_docs"), out)

    notfound = out / "404.html"
    sitemap = out / "sitemap.xml"
    assert notfound.exists()
    assert sitemap.exists()

    # The 404 uses the page shell (sidebar nav present) and is a real page.
    nf_html = notfound.read_text()
    assert "Page not found" in nf_html
    assert "nav-link" in nf_html

    # The sitemap lists every published page url, including a known one.
    sm_xml = sitemap.read_text()
    assert "<urlset" in sm_xml
    assert "<loc>/getting-started/introduction/</loc>" in sm_xml


def test_build_fails_on_broken_corpus(tmp_path):
    # Copy the real corpus, then inject a dangling ref: the lint gate must
    # turn that into a build failure before any page is rendered.
    src = tmp_path / "user_docs"
    shutil.copytree(Path("primer/user_docs"), src)
    intro = src / "getting-started" / "introduction.md"
    intro.write_text(
        intro.read_text()
        + "\n\n```ref:getting-started/does-not-exist\nDangling.\n```\n",
        encoding="utf-8",
    )

    with pytest.raises(DocsLintError):
        build_site(src, tmp_path / "dist")
