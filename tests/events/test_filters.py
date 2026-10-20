"""Filter semantics per tier: globs, excludes, field matchers, rego."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from primer.events.filters import matches
from primer.model.event import Event, EventFilter, FieldMatcher


def _event(**kwargs) -> Event:
    base = dict(
        id=1,
        event_type="agent.created",
        occurred_at=datetime.now(timezone.utc),
        actor="system",
        payload={},
    )
    base.update(kwargs)
    return Event(**base)


def test_type_globs_include_and_exclude():
    f = EventFilter(event_types=["agent.*", "session.steered"])
    assert matches(_event(event_type="agent.created"), f)
    assert matches(_event(event_type="session.steered"), f)
    assert not matches(_event(event_type="graph.created"), f)

    f = EventFilter(event_types=["*"], exclude_types=["tool.*"])
    assert matches(_event(event_type="session.ended"), f)
    assert not matches(_event(event_type="tool.called"), f)


def test_field_matchers_all_must_hold():
    e = _event(
        entity_kind="agent", entity_id="a1",
        payload={"collection_id": "kb", "path": "guides/refunds"},
    )
    f = EventFilter(fields=[
        FieldMatcher(path="entity_kind", op="eq", value="agent"),
        FieldMatcher(path="payload.collection_id", op="eq", value="kb"),
    ])
    assert matches(e, f)
    f = EventFilter(fields=[
        FieldMatcher(path="entity_kind", op="eq", value="agent"),
        FieldMatcher(path="payload.collection_id", op="eq", value="other"),
    ])
    assert not matches(e, f)


def test_field_paths_fall_back_into_payload():
    e = _event(payload={"collection_id": "kb"})
    assert matches(
        e,
        EventFilter(fields=[
            FieldMatcher(path="collection_id", op="eq", value="kb"),
        ]),
    )


def test_prefix_and_regex_ops():
    e = _event(payload={"path": "guides/refunds"})
    assert matches(e, EventFilter(fields=[
        FieldMatcher(path="payload.path", op="prefix", value="guides/"),
    ]))
    assert matches(e, EventFilter(fields=[
        FieldMatcher(path="payload.path", op="regex", value=r"refund|billing"),
    ]))
    assert not matches(e, EventFilter(fields=[
        FieldMatcher(path="payload.path", op="regex", value=r"^billing"),
    ]))


def test_missing_path_and_bad_regex_fail_closed():
    e = _event()
    assert not matches(e, EventFilter(fields=[
        FieldMatcher(path="payload.nope", op="eq", value="x"),
    ]))
    assert not matches(e, EventFilter(fields=[
        FieldMatcher(path="event_type", op="regex", value="([unclosed"),
    ]))


def test_rego_expr_matches_and_fails_closed():
    e = _event(
        event_type="collection.document_pushed",
        payload={"collection_id": "kb"},
    )
    good = (
        "package primer.event_filter\n"
        "default match := false\n"
        'match if input.payload.collection_id == "kb"\n'
    )
    assert matches(e, EventFilter(expr=good))
    other = good.replace('"kb"', '"other"')
    assert not matches(e, EventFilter(expr=other))
    # A module that does not compile is a no-match, not an error.
    assert not matches(e, EventFilter(expr="this is not rego"))
    # A module without a boolean `match` rule is a no-match.
    no_match_rule = "package primer.event_filter\nx := 1\n"
    assert not matches(e, EventFilter(expr=no_match_rule))


def test_a_legacy_expr_that_references_opa_runtime_fails_closed(caplog):
    e = _event(payload={"collection_id": "kb"})
    expr = (
        "package primer.event_filter\n"
        "match := opa.runtime() != {}\n"
    )

    with caplog.at_level(logging.WARNING, logger="primer.events.filters"):
        assert not matches(e, EventFilter(expr=expr))

    assert "references to the opa package are not allowed" in caplog.text


def test_a_data_only_expr_may_compare_text_that_names_opa_runtime():
    e = _event(payload={"label": "opa.runtime()"})
    expr = (
        "package primer.event_filter\n"
        "# A comment that names opa.runtime() is event documentation.\n"
        'match if input.payload.label == "opa.runtime()"\n'
    )

    assert matches(e, EventFilter(expr=expr))
