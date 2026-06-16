import re

from scripts.docs.build_site import render_markdown


SLUG_URL_MAP = {
    "getting-started/quickstart": "/getting-started/quickstart/",
}

INLINE_MD = """\
## A Heading

```python
print("hi")
```

See [q](ref:getting-started/quickstart) for more.
"""

BLOCK_MD = """\
## Another Heading

```ref:getting-started/quickstart
Start here.
```
"""


def test_renders_heading_code_and_inline_ref():
    out = render_markdown(INLINE_MD, SLUG_URL_MAP)
    assert re.search(r'<h2 id="[^"]+"', out)
    assert "<pre><code" in out
    assert 'href="/getting-started/quickstart/"' in out


def test_resolves_ref_block_form():
    out = render_markdown(BLOCK_MD, SLUG_URL_MAP)
    assert 'href="/getting-started/quickstart/"' in out
    assert "Start here." in out
