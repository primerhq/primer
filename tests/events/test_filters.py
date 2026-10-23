"""Filter semantics per tier: globs, excludes, field matchers, rego."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from primer.events.filters import evaluate_filter_expr, matches, validate_filter_expr
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


# G6-2: a Rego template string runs the code inside its {...} parts, so the
# string-blanking scan cannot see an opa reference there. Template strings
# are refused outright, at save time and by the evaluation backstop.
_TEMPLATE_EXPRS = {
    "double_quoted": (
        "package primer.event_filter\n"
        'match := $"{opa.runtime().env.G6_FILTER_SECRET}" == "s3cr3t"\n'
    ),
    "raw": (
        "package primer.event_filter\n"
        'match := $`{opa.runtime().env.G6_FILTER_SECRET}` == "s3cr3t"\n'
    ),
    "harmless": (
        "package primer.event_filter\n"
        'match := $"{input.event_type}" == "agent.created"\n'
    ),
}


@pytest.mark.parametrize("name", sorted(_TEMPLATE_EXPRS))
def test_save_time_check_refuses_a_template_string(name):
    with pytest.raises(ValueError, match="template strings"):
        validate_filter_expr(_TEMPLATE_EXPRS[name])


@pytest.mark.parametrize("name", sorted(_TEMPLATE_EXPRS))
def test_evaluation_backstop_refuses_a_template_string(name, monkeypatch, caplog):
    # The secret is right, so an expression that reached the interpreter
    # would answer True: the one-bit oracle on the host environment.
    monkeypatch.setenv("G6_FILTER_SECRET", "s3cr3t")
    expr = _TEMPLATE_EXPRS[name]
    with caplog.at_level(logging.WARNING, logger="primer.events.filters"):
        assert evaluate_filter_expr(expr, {"event_type": "agent.created", "payload": {}}) is False
        assert not matches(_event(), EventFilter(expr=expr))
    assert "template strings" in caplog.text


def test_a_dollar_sign_inside_a_string_or_comment_is_not_a_template():
    expr = (
        "package primer.event_filter\n"
        "# costs $5, see $`docs`\n"
        'match if input.payload.price == "$5"\n'
    )
    validate_filter_expr(expr)
    assert matches(_event(payload={"price": "$5"}), EventFilter(expr=expr))



# ---- round 2, L7: the Rego evaluator reaches no network ------------------------------------------------------------------


def test_http_send_remote_jwks_and_dns_are_unavailable_to_a_filter_expr():
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.5)
    url = f"http://127.0.0.1:{listener.getsockname()[1]}/"
    try:
        exprs = {
            "http.send": f'match := http.send({{"method": "get", "url": "{url}"}}).status_code == 200',
            "remote jwks": f'match := io.jwt.decode_verify("a.b.c", {{"jwks_url": "{url}"}})[0] == false',
            "dns": 'match := count(net.lookup_ip_addr("localhost")) > 0',
        }
        for name, body in exprs.items():
            expr = "package primer.event_filter\n" + body + "\n"
            validate_filter_expr(expr)  # data-only: nothing refuses these at save time
            assert evaluate_filter_expr(expr, {"event_type": "x", "payload": {}}) is False, name
        with pytest.raises(TimeoutError):
            listener.accept()  # nothing ever connected
    finally:
        listener.close()
