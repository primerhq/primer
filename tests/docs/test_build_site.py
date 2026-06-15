from pathlib import Path

from scripts.docs.build_site import build_site


def test_builds_a_page_per_doc(tmp_path):
    out = tmp_path / "dist"
    build_site(Path("primer/user_docs"), out)
    assert (out / "getting-started" / "introduction" / "index.html").exists()
    home = (out / "getting-started" / "introduction" / "index.html").read_text()
    assert "Features" in home and "LLM Providers" in home
