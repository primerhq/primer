"""The memory entry body format (agent memory Task 1): markdown sections of `- [id] text` bullets."""

import pytest

from primer.memory.entry import BODY_MAX_CHARS, MEMORY_FOLDERS, EntryBody, apply_edits, body_from_add, parse_body, render_body, slugify_name
from primer.model.memory import ItemEdit, MemoryAdd


def _body() -> EntryBody:
    return EntryBody(steps=[("s1", "do a"), ("s2", "do b")], pitfalls=[("p1", "not c")], example=[("e1", "like this")])


def test_render_parse_round_trip_keeps_item_ids() -> None:
    text = render_body(_body())
    assert "## Steps" in text and "- [s1] do a" in text and "- [p1] not c" in text and "- [e1] like this" in text
    assert parse_body(text) == _body()


def test_empty_sections_round_trip() -> None:
    body = EntryBody(steps=[("s1", "only")], pitfalls=[], example=[])
    assert parse_body(render_body(body)) == body


def test_body_from_add_numbers_each_section() -> None:
    add = MemoryAdd(op="add", name="n", description="d", steps=["a", "b"], pitfalls=["x"], example="ex", topics=[])
    assert body_from_add(add) == EntryBody(steps=[("s1", "a"), ("s2", "b")], pitfalls=[("p1", "x")], example=[("e1", "ex")])
    assert body_from_add(add.model_copy(update={"example": None})).example == []


def test_apply_edits_add_change_remove_by_item_id() -> None:
    edits = [
        ItemEdit(action="add", section="steps", item_id=None, text="new"),
        ItemEdit(action="change", section="steps", item_id="s1", text="do a better"),
        ItemEdit(action="remove", section="pitfalls", item_id="p1", text=None),
    ]
    out = apply_edits(_body(), edits)
    assert out.steps == [("s1", "do a better"), ("s2", "do b"), ("s3", "new")]
    assert out.pitfalls == [] and out.example == [("e1", "like this")]
    assert _body().steps[0] == ("s1", "do a"), "the input is not changed"


def test_a_new_id_is_max_plus_one_not_count_plus_one() -> None:
    body = EntryBody(steps=[("s1", "a"), ("s5", "b")], pitfalls=[], example=[])
    assert apply_edits(body, [ItemEdit(action="add", section="steps", item_id=None, text="c")]).steps[-1] == ("s6", "c")


def test_apply_edits_unknown_item_id_is_ignored() -> None:
    edits = [ItemEdit(action="change", section="steps", item_id="s9", text="x"), ItemEdit(action="remove", section="steps", item_id="s9", text=None)]
    assert apply_edits(_body(), edits) == _body()


def test_apply_edits_without_text_adds_and_changes_nothing() -> None:
    edits = [ItemEdit(action="add", section="steps", item_id=None, text=None), ItemEdit(action="change", section="steps", item_id="s1", text=None)]
    assert apply_edits(_body(), edits) == _body()


def test_slugify_name() -> None:
    assert slugify_name("Retry GitHub 429!") == "retry-github-429"
    assert slugify_name("  a__b  ") == "a-b"
    assert slugify_name("!!!") is None and slugify_name("") is None


def test_render_body_over_cap_is_refused() -> None:
    with pytest.raises(ValueError):
        render_body(EntryBody(steps=[("s1", "x" * (BODY_MAX_CHARS + 1))], pitfalls=[], example=[]))


def test_constants() -> None:
    assert BODY_MAX_CHARS == 3200 and MEMORY_FOLDERS == ("active", "candidates", "archived")
