"""Outbound bundles must survive literal Jinja delimiters (KN-17).

The inbound side renders the whole template YAML with Jinja, so a tracked
document whose body (or spec string) contains ``{{ }}``, ``{% %}`` or
``{# #}`` -- common in docs about templating -- made every install of what
we built fail with template_render_failed, and ``{# ... #}`` (pandoc header
attributes) was silently deleted from the body. build_outbound now escapes
the delimiters in every payload string and in the document body; the
``{{ overrides.* }}`` tokens inserted afterwards stay live.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from primer.harness.outbound import build_outbound
from primer.harness.template import render_bundle
from primer.knowledge.tree import DocumentTreeService
from primer.model.collection import Collection
from primer.model.harness import (
    Harness,
    HarnessDirection,
    OverrideMapping,
    TrackedEntity,
)
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory

_ALL_THREE = (
    "Write {{ name }} in a prompt.\n"
    "{% if debug %}a block{% endif %}\n"
    "{# a pandoc-style comment #} kept"
)


@pytest_asyncio.fixture
async def sp(tmp_path):
    p = StorageProviderFactory.create(StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    ))
    await p.initialize()
    await p.get_content_store().ensure_schema()
    yield p
    await p.aclose()


async def _build_and_render(
    sp, tmp_path, *, body: str, overrides: dict | None = None,
    entity_overrides: list | None = None,
):
    await sp.get_storage(Collection).create(Collection(id="kb", description="kb"))
    d = await DocumentTreeService(sp).create(
        collection_id="kb", parent="", slug="howto", body=body,
    )
    tracked = TrackedEntity(
        kind="document", source_id=d.id, template_name="howto",
        overrides=entity_overrides or [],
    )
    h = Harness(
        id="out-id", slug="out", name="Out",
        direction=HarnessDirection.OUTBOUND,
        git_url="https://github.com/x/y",
        created_at=datetime.now(timezone.utc),
        tracked_entities=[tracked],
    )
    result = await build_outbound(h, storage_provider=sp)
    checkout = tmp_path / "checkout"
    for f in result.files:
        p = checkout / f.template_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f.rendered_text, encoding="utf-8")
    rendered = await render_bundle(
        checkout_dir=str(checkout), subpath=None,
        overrides=overrides or {}, harness_ctx={},
    )
    return next(f for f in rendered if f.template_path == "howto.yaml")


async def test_outbound_document_with_jinja_text_round_trips(sp, tmp_path):
    """Pre-fix red: the built template shipped the raw {{ }} / {% %} /
    {# #} delimiters, so rendering it (what every install does) raised
    template_render_failed, or the {# ... #} half was silently deleted."""
    out = await _build_and_render(sp, tmp_path, body=_ALL_THREE)
    assert out.rendered["content_inline"] == _ALL_THREE


async def test_outbound_document_without_delimiters_round_trips_unchanged(sp, tmp_path):
    """Characterization: a body with no delimiters is byte-identical
    through build + render, before and after the escape."""
    body = "A plain document about templating.\nIt ships the way it was written."
    out = await _build_and_render(sp, tmp_path, body=body)
    assert out.rendered["content_inline"] == body


async def test_the_escape_keeps_the_override_tokens_live(sp, tmp_path):
    """Characterization: a tracked field mapped to an override must still
    render its token -- the escape runs before apply_override_mappings
    inserts the tokens, never over them."""
    out = await _build_and_render(
        sp, tmp_path, body="plain body",
        entity_overrides=[OverrideMapping(field_path="/slug", override_path="slug")],
        overrides={"slug": "renamed"},
    )
    assert out.rendered["spec"]["slug"] == "renamed"
    assert out.rendered["content_inline"] == "plain body"


# Each body is chosen to push the YAML dumper onto a different scalar
# style for the escaped value (block literal, the double-quoted fallback,
# or a style where the escape's own single quote would be doubled if the
# escape-aware dumper were not used). All of them must round-trip exactly.
_NASTY_BODIES = [
    "line one # hash-hash\nline two {{ x }}",  # ' #' + multi-line
    "it's a 'quoted' {{ x }}",                  # single quotes in the body
    'double " quoted and {{ x }}',              # double quotes in the body
    "tab\there and {{ x }}",                    # tab forces double-quoted
    "unicode \u00e9 and {{ x }}",               # non-ascii forces double-quoted
    "ends with a break {{ x }}\n",              # trailing newline
    "  leading space {{ x }}",                  # leading space
    "{{ x }}",                                  # leading { indicator
]


@pytest.mark.parametrize("body", _NASTY_BODIES)
async def test_escaped_bodies_round_trip_through_the_yaml_style_choice(
    sp, tmp_path, body,
):
    out = await _build_and_render(sp, tmp_path, body=body)
    assert out.rendered["content_inline"] == body
