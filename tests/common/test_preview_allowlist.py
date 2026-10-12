"""scrub_args_for_display: the single place the console route and the channel prompt ask what may show (01a125ed-9ef6).

The allowlist language (paths, classify, closed sets) is :mod:`primer.common.preview_paths`; this module
is where the language is APPLIED: the stamped allowlist, the default rule for an unstamped park (design
ruling D5), and the bare-value withhold.
"""

from primer.common.preview_allowlist import (
    _HIDDEN,
    _preview_stamp,
    scrub_args_for_display,
)


def test_none_and_empty_arguments_pass_through() -> None:
    assert scrub_args_for_display(None, None)[0] is None
    assert scrub_args_for_display("", None) == ("", [])


def test_a_dict_without_a_stamp_takes_the_value_rule() -> None:
    shown, withheld = scrub_args_for_display({"a": "secret", "n": 3}, None)

    assert shown == {"a": _HIDDEN, "n": 3}
    assert withheld == ["a"]


def test_a_stamp_names_only_the_paths_it_allows() -> None:
    shown, withheld = scrub_args_for_display(
        {"path": "/x", "content": "big", "meta": {"k": "v"}},
        {"paths": ["path"], "source": "policy"},
    )

    assert shown["path"] == "/x"
    assert shown["content"] is _HIDDEN
    assert shown["meta"] is _HIDDEN
    assert withheld == ["content", "meta"]


def test_a_policy_stamp_does_not_mutate_the_call_arguments() -> None:
    args = {"path": "/x", "content": "big"}

    shown, _withheld = scrub_args_for_display(args, {"paths": ["path"], "source": "policy"})

    assert args["content"] == "big"
    assert shown["content"] is _HIDDEN


def test_an_unparseable_string_is_withheld_whole() -> None:
    shown, _withheld = scrub_args_for_display(
        "not json at all", {"paths": ["a"], "source": "policy"}
    )

    assert shown is _HIDDEN


def test_a_json_list_string_is_a_bare_value() -> None:
    shown, _withheld = scrub_args_for_display(
        '["a", "b"]', {"paths": ["a"], "source": "policy"}
    )

    assert shown is _HIDDEN


def test_preview_stamp_refuses_a_malformed_stamp() -> None:
    assert _preview_stamp(
        {"preview": {"paths": ["a"], "source": "policy"}}
    ) == {"paths": ["a"], "source": "policy"}
    assert _preview_stamp({"preview": {"paths": ["a"], "source": "bogus"}}) is None
    assert _preview_stamp({"preview": {"paths": "a", "source": "policy"}}) is None
    assert _preview_stamp({}) is None
