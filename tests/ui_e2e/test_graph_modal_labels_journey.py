"""Journey: the graph modals' rows and the builder's schema JSON view name their controls (console review C-003, the graphs surface).

In the real console the New graph modal's ID, Description and Seed agent are found by their label (a click on the label focuses its control), the Import spec modal's textarea is found by
"Graph spec JSON", and the schema view the builder falls back to for a schema it cannot draw as rows (a ``oneOf``/``anyOf``) has a named textarea. That last one had no name at all: the
builder called ``GR_JsonField`` with an empty label, and the name the earlier builder PR put on a fallback textarea never rendered.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e import _graph_builder_helpers as gb
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-06", status="partial")

_UNNAMED = """els => els.filter(e => e.type !== 'hidden' && e.offsetParent !== null
    && e.labels.length === 0 && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby'))
    .map(e => e.outerHTML.slice(0, 100))"""


def _unnamed(root) -> list[str]:
    return root.locator("input, select, textarea").evaluate_all(_UNNAMED)


_DESCRIPTIONS = """el => (el.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean)
    .map(id => { const n = document.getElementById(id); return n ? n.textContent.trim() : null })"""


def _described_by(locator) -> list[str | None]:
    """The text of every element the control's ``aria-describedby`` points at (None for an id that resolves to nothing)."""
    return locator.evaluate(_DESCRIPTIONS)


def _seed_graph(base_url: str, graph_id: str, begin_schema: dict | None = None) -> None:
    begin: dict = {"kind": "begin", "id": "begin"}
    if begin_schema is not None:
        begin["input_schema"] = begin_schema
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={
            "id": graph_id, "description": "modal labels probe",
            "nodes": [begin, {"kind": "end", "id": "end", "output_template": ""}],
            "edges": [{"kind": "static", "from_node": "begin", "to_node": "end"}],
        })
        assert r.status_code == 201, r.text


def _delete_graph(base_url: str, graph_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        c.delete(f"/v1/graphs/{graph_id}")


@pytest.mark.ui_e2e
def test_the_new_graph_modal_rows_are_found_by_their_label(page: Page, console_url: str) -> None:
    open_legacy_route(page, console_url, "graphs")
    page.get_by_role("button", name="New graph").first.click()
    modal = page.locator(".modal").first
    expect(modal).to_be_visible(timeout=10_000)

    assert _unnamed(modal) == []
    expect(modal.get_by_label("Description", exact=True)).to_have_count(1)
    modal.locator("label.field-label", has_text="Description").click()
    expect(modal.get_by_label("Description", exact=True)).to_be_focused()
    # the hint is a word of its own in the ID row's name
    expect(modal.get_by_label(re.compile(r"^ID optional"))).to_have_count(1)
    # the seed agent: the select keeps its own name, and the row around it (the select sits beside the New button) is a group named by its label
    expect(modal.get_by_role("combobox", name="Seed agent", exact=True)).to_have_count(1)
    group = modal.get_by_role("group", name=re.compile(r"^Seed agent"))
    expect(group).to_have_count(1)
    # the explanation under the row is the group's description, not a loose line
    described = _described_by(group)
    assert len(described) == 1 and described[0] and described[0].startswith("Once created, you can bind sessions to this graph"), described


@pytest.mark.ui_e2e
def test_the_import_spec_modal_textarea_is_found_by_its_label(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    graph_id = f"gml-import-{unique_suffix}"
    _seed_graph(base_url, graph_id)
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        page.locator('[data-testid="gb-json-tab"]').click()
        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=10_000)
        assert _unnamed(modal) == []
        expect(modal.get_by_label(re.compile(r"^Graph spec JSON"))).to_have_count(1)
        modal.locator("label.field-label", has_text="Graph spec JSON").click()
        spec = modal.get_by_test_id("graph-import-spec")
        expect(spec).to_be_focused()
        described = _described_by(spec)
        assert len(described) == 1 and described[0] and described[0].startswith("Loads the pasted spec into the visual editor"), described
    finally:
        _delete_graph(base_url, graph_id)


@pytest.mark.ui_e2e
def test_the_builders_schema_json_view_has_a_named_textarea(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    """A schema with an ``anyOf`` is too complex for the field rows, so the builder shows its JSON: the textarea of ``GR_JsonField``."""
    graph_id = f"gml-json-{unique_suffix}"
    _seed_graph(base_url, graph_id, {"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "number"}]}}})
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        rows = page.locator('[data-testid="gb-outline-row"]')
        expect(rows).to_have_count(2, timeout=15_000)
        rows.first.click()
        inspector = page.locator(gb.INSPECTOR)
        expect(inspector).to_be_visible(timeout=5_000)
        expect(inspector.get_by_text("This shape uses advanced JSON Schema")).to_be_visible(timeout=5_000)
        field = inspector.get_by_role("textbox", name="Schema as JSON")
        expect(field).to_have_count(1)
        # a parse error is an alert that the (now invalid) textarea is described by
        field.fill("{")
        field.blur()
        alert = inspector.get_by_role("alert").filter(has_text="JSON parse:")
        expect(alert).to_have_count(1, timeout=5_000)
        expect(field).to_have_attribute("aria-invalid", "true")
        assert any(d and d.startswith("JSON parse:") for d in _described_by(field)), _described_by(field)
        # a valid document clears it
        field.fill('{"type": "object", "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "number"}]}}}')
        field.blur()
        expect(alert).to_have_count(0, timeout=5_000)
        expect(field).not_to_have_attribute("aria-invalid", "true")
        # the JSON textarea itself is named (the rest of the inspector's controls are the builder PR's)
        assert _unnamed(inspector.locator("textarea").first.locator("xpath=..")) == []
    finally:
        _delete_graph(base_url, graph_id)
