import json
from pathlib import Path

from scripts.docs.build_site import build_site


def test_writes_search_index(tmp_path):
    out = tmp_path / "dist"
    build_site(Path("primer/user_docs"), out)

    index_path = out / "search-index.json"
    assert index_path.exists()

    data = json.loads(index_path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert data, "search index should not be empty"

    # One entry per published doc (the _meta authoring section is excluded
    # from pages and must be excluded from the search index too). The root
    # index.html is a redirect to the docs home, not a published doc, so it
    # is excluded from the count.
    page_count = sum(1 for p in out.rglob("index.html") if p.parent != out)
    assert len(data) == page_count

    # No _meta urls leak in.
    assert all(not e["url"].startswith("/_meta/") for e in data)

    for entry in data:
        assert isinstance(entry["title"], str) and entry["title"]
        assert isinstance(entry["section"], str) and entry["section"]
        assert isinstance(entry["url"], str) and entry["url"].startswith("/")
        assert isinstance(entry["headings"], list)
        assert isinstance(entry["excerpt"], str)

    by_url = {e["url"]: e for e in data}
    llm = by_url.get("/features/llm-providers/")
    assert llm is not None, "LLM Providers page should be indexed"
    assert llm["title"] == "LLM Providers"
    assert llm["section"] == "features"
    assert llm["headings"], "LLM Providers should expose its headings"
    assert llm["excerpt"], "LLM Providers should expose an excerpt"
    # The excerpt is plain text, not HTML.
    assert "<" not in llm["excerpt"]
