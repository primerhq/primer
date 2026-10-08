"""A list of strings is a command line, and the Inbox preview scrubs it as one (ticket 01a11cd3-55b6).

The scrubber's flag rules look at TEXT: ``mysql ... -phunter2``, ``curl -u admin:hunter2``, ``deploy --pass hunter2``. A tool whose command is an argv
array (``["mysql", "-u", "root", "-phunter2"]``) was walked member by member, so each rule saw one word and none matched: the password sat in the
preview in the clear. The same list in a JSON string, in a list of commands, or as the whole ``arguments`` leaked the same way.

A list whose members are strings (numbers are allowed among them: ``["curl", "--max-time", 10, ...]``) is joined into the command line it is,
each member quoted the way a shell would, and scrubbed once as text. When nothing was found the list is shown as it was; when something was, the
list comes back with the scrubbed words (``["mysql", "-u", "root", "-p<redacted>", "db"]``) and the preview is marked as truncated, so the card
offers "show all". When a rule swallows a whole word (``Authorization: Bearer x`` becomes ``Authorization: <redacted>``) the words no longer line
up with the members, and the scrubbed command line comes back as a string instead. The scrubber is handed at most ``_REDACT_MAX_TEXT``
characters of one command line, as it is for any single string; words past that are not looked at, so they are not shown (``<N more>``).

Kept limits, on purpose: a list that holds a nested object or list is walked member by member (those members are not words of one command), and
so is a list with fewer than two members (there is no program word for a flag rule to hang on). The accepted false positives of the text rules
(``find /var/lib/mysql -print``) apply to a list in the same way.
"""

from __future__ import annotations

import json

import pytest

from primer.api.routers.workspaces import _approval_preview, _redact
from tests.api.test_inbox_preview_is_bounded import _HOSTILE, _LINEAR_BACKSTOP_S, _recording_scrubber, _timed_in_a_child


def _preview(arguments) -> dict:
    return _approval_preview({"name": "run", "arguments": arguments})


def _assert_hidden(got: str, secret: str) -> None:
    """Every whitespace-separated PIECE of the secret is absent, not only the whole string."""
    leaked = [piece for piece in secret.split() if piece in got]
    assert not leaked, f"{leaked} of {secret!r} still show in {got!r}"


# --- the argv arrays that leaked --------------------------------------------------------------------------------------------------

_LEAKING = [
    pytest.param(["mysql", "-u", "root", "-phunter2", "db"], "hunter2", id="mysql attached -p"),
    pytest.param(["mysqldump", "--single-transaction", "-phunter2", "db"], "hunter2", id="mysqldump attached -p"),
    pytest.param(["curl", "-u", "admin:hunter2", "https://x/y"], "hunter2", id="curl -u user:password"),
    pytest.param(["curl", "-U", "admin:hunter2", "https://x/y"], "hunter2", id="curl -U user:password"),
    pytest.param(["curl", "--proxy-user", "admin:hunter2", "https://x/y"], "hunter2", id="curl --proxy-user"),
    pytest.param(["curl", "-u", "deploy:correct horse battery", "https://x/y"], "correct horse battery", id="a password with spaces"),
    pytest.param(["deploy", "--pass", "hunter2"], "hunter2", id="--pass"),
    pytest.param(["deploy", "--password", "hunter2", "--env", "prod"], "hunter2", id="--password among other flags"),
    pytest.param(["deploy", "--api-key", "abc123def456"], "abc123def456", id="--api-key"),
    pytest.param(["fetch", "Bearer", "abc123def456"], "abc123def456", id="a bearer split from its word"),
    pytest.param(["Authorization:", "Bearer", "abc123def456"], "abc123def456", id="a header split into words"),
    pytest.param(["curl", "--max-time", 10, "-u", "admin:hunter2", "https://x/y"], "hunter2", id="a number among the words"),
]

# Where an argv array can sit in a call.
_PLACEMENTS = {
    "under command": lambda argv: {"command": argv},
    "under args beside other arguments": lambda argv: {"args": argv, "cwd": "/srv/app"},
    "as the whole arguments": lambda argv: argv,
    "in a JSON string": lambda argv: {"command": json.dumps(argv)},
    "in a list of commands": lambda argv: {"commands": [argv, ["ls", "-l"]]},
}


@pytest.mark.parametrize("placement", sorted(_PLACEMENTS))
@pytest.mark.parametrize(("argv", "secret"), _LEAKING)
def test_an_argv_array_is_scrubbed_as_the_command_line_it_is(argv, secret, placement) -> None:
    got = _preview(_PLACEMENTS[placement](argv))

    _assert_hidden(got["arguments"], secret)
    assert got["truncated"] is True, "something was hidden, so the card must offer show all"
    assert "<redacted>" in got["arguments"]


@pytest.mark.parametrize(
    ("argv", "line"),
    [
        (["mysql", "-u", "root", "-phunter2", "db"], 'command=["mysql", "-u", "root", "-p<redacted>", "db"]'),
        (["deploy", "--pass", "hunter2"], 'command=["deploy", "--pass", "<redacted>"]'),
        (["curl", "-u", "admin:hunter2", "https://x/y"], 'command=["curl", "-u", "<redacted>", "https://x/y"]'),
        (["curl", "-u", "deploy:correct horse battery", "https://x/y"], 'command=["curl", "-u", "<redacted>", "https://x/y"]'),
        (["curl", "--max-time", 10, "-u", "admin:hunter2", "https://x/y"], 'command=["curl", "--max-time", 10, "-u", "<redacted>", "https://x/y"]'),
        (["Authorization:", "Bearer", "abc123def456"], "command=Authorization: <redacted>"),
    ],
)
def test_a_scrubbed_argv_array_keeps_its_words_and_only_the_secret_changes(argv, line) -> None:
    assert _preview({"command": argv})["arguments"] == line


def test_the_scrub_result_for_an_argv_array_is_the_list_with_the_secret_replaced_and_a_changed_flag() -> None:
    got, changed = _redact(["mysql", "-u", "root", "-phunter2", "db"])

    assert (got, changed) == (["mysql", "-u", "root", "-p<redacted>", "db"], True)


def test_a_list_given_as_json_text_keeps_the_shape_the_mapping_walk_always_gave_it() -> None:
    """The pre-existing contract (tests/api/test_workspace_yields_pending.py): a JSON list of arguments is shown as a list with the secret replaced."""
    got = _preview('["Bearer abcdefgh", "ok"]')

    assert got["arguments"] == '["Bearer <redacted>", "ok"]' and got["truncated"] is True


def test_a_rule_that_swallows_a_whole_word_gives_the_scrubbed_command_line_as_a_string() -> None:
    """``Authorization: Bearer x`` is one match: the word ``Bearer`` goes with it, so the words no longer line up with the members."""
    got, changed = _redact(["Authorization:", "Bearer", "abc123def456"])

    assert (got, changed) == ("Authorization: <redacted>", True)


# --- command lines that hold no secret are left as they were -----------------------------------------------------------------------

_CLEAN = [
    pytest.param(["git", "push", "-u", "origin", "main"], id="git push -u"),
    pytest.param(["docker", "run", "-u", "1000:1000", "alpine"], id="docker run -u uid:gid"),
    pytest.param(["ssh", "-p", "22", "host"], id="ssh -p port"),
    pytest.param(["ssh", "-p22", "host"], id="ssh -p22"),
    pytest.param(["find", ".", "-print"], id="find -print"),
    pytest.param(["mysql", "-u", "root", "db"], id="mysql without a password"),
    pytest.param(["mysql", "-p", "db"], id="mysql -p then the database"),
    pytest.param(["mysql", "-P", "3306", "-h", "db"], id="mysql -P is the port"),
    pytest.param(["curl", "-u", "admin", "https://x/y"], id="curl -u without a colon prompts"),
    pytest.param(["alpha", "beta", "gamma"], id="plain words"),
    pytest.param(["sleep", 5], id="a number"),
    pytest.param(["bypass", "compass", "passenger"], id="words that contain pass"),
]


@pytest.mark.parametrize("argv", _CLEAN)
def test_a_command_line_without_a_secret_is_left_as_the_list_it_was(argv) -> None:
    got, changed = _redact(argv)

    assert (got, changed) == (argv, False)
    assert _preview({"command": argv})["truncated"] is False


def test_a_list_with_a_nested_member_is_still_walked_member_by_member() -> None:
    """A nested object is not a word of one command: its own rules (secret-looking names) apply to it, and the strings around it keep theirs."""
    got, changed = _redact(["curl", {"password": "hunter2"}, "https://x/y"])

    assert (got, changed) == (["curl", {"password": "<redacted>"}, "https://x/y"], True)


def test_a_scrub_that_leaves_unbalanced_quotes_still_answers_with_the_scrubbed_line() -> None:
    """``password='`` quotes to ``'password='"'"''``; the assignment rule replaces the ``"'"`` piece, and the line that is left cannot be split back
    into words (an open quote). That must not reach the route as an exception: the scrubbed line comes back as a string."""
    got, changed = _redact(["curl", "password='"])

    assert changed is True
    assert isinstance(got, str) and got.startswith("curl ") and "<redacted>" in got, got
    assert _preview({"command": ["curl", "password='"]})["truncated"] is True


def test_a_json_document_among_the_words_is_still_walked_as_a_document() -> None:
    """``key`` is a secret NAME for the document walk but not a word the text rules know, so only the document step hides this value (the value has
    no shape of its own)."""
    argv = ["curl", "-d", '{"key": "zzz-not-a-shape"}', "https://x/y"]

    got = _preview({"command": argv})

    assert "zzz-not-a-shape" not in got["arguments"], got
    assert got["truncated"] is True


def test_a_list_of_one_member_is_not_a_command_line() -> None:
    assert _redact(["-phunter2"]) == (["-phunter2"], False)


# --- bounded work ------------------------------------------------------------------------------------------------------------------


def test_an_argv_array_hands_the_scrubber_a_bounded_total(monkeypatch) -> None:
    """200 members of 1 500 characters: the walk's budget decides how much is scrubbed, as it does for a mapping, and one command line is never
    handed over past the ceiling a single string has."""
    from primer.api.routers import workspaces as w

    seen = _recording_scrubber(monkeypatch)
    w._approval_preview({"name": "t", "arguments": {"command": ["word " * 300] * 200}})

    assert sum(seen) <= w._REDACT_BUDGET + 4000, f"{sum(seen)} characters were scrubbed for one preview"
    assert max(seen) <= w._REDACT_MAX_TEXT, f"the scrubber was handed {max(seen)} characters at once"


def test_many_command_lines_share_the_one_walk_budget(monkeypatch) -> None:
    """50 command lines of two 1 500 character words each: every line is under the ceiling, so only the shared budget keeps the walk from
    scrubbing 50 times the ceiling. What the walk does not look at is hidden and counted."""
    from primer.api.routers import workspaces as w

    seen = _recording_scrubber(monkeypatch)
    got, changed = w._redact([["word " * 300, "more " * 300] for _ in range(50)])

    assert sum(seen) <= w._REDACT_BUDGET + 4000, f"{sum(seen)} characters were scrubbed for one walk"
    assert changed is True and "more>" in str(got)


def test_an_argv_array_cut_at_the_member_cap_says_how_many_members_it_left_out() -> None:
    """The secret sits past the cap: it is never looked at, so it is not shown, and the cut says so."""
    got, changed = _redact(["mysql", "-u", "root"] + ["x"] * 60 + ["-phunter2"])

    assert changed is True
    assert "hunter2" not in str(got)
    assert "more>" in str(got)


def test_a_word_past_the_line_ceiling_is_not_looked_at_and_so_is_not_shown() -> None:
    got, changed = _redact(["mysql", "y" * 1990, "-phunter2"])

    assert changed is True
    assert "hunter2" not in str(got)
    assert got[-1] == "<1 more>"


def test_a_word_that_alone_is_past_the_ceiling_keeps_its_head_and_is_marked() -> None:
    got, changed = _redact(["mysql", "q" * 30_000])

    assert changed is True
    assert len(str(got)) < 2_500, len(str(got))


def test_an_argv_array_at_the_member_cap_exactly_is_not_marked() -> None:
    argv = ["echo"] + ["x"] * 49

    assert _redact(argv) == (argv, False)


@pytest.mark.parametrize(
    "shape",
    [
        "user flags with equals",
        "attached user flags",
        "mysql with attached -p",
        "mysql with a bare -p",
        "secret flags",
        "pass flags",
        "bearers",
        "quote openers",
        "a quote opener per user flag",
        "key flags",
    ],
)
def test_an_argv_array_of_hostile_members_is_linear(shape: str) -> None:
    text = _HOSTILE[shape]
    members = [text[i * 1500 : (i + 1) * 1500] for i in range(50)]

    elapsed = _timed_in_a_child("_redact", members)

    assert elapsed is not None, f"_redact was still running after 6 s on an argv array of {shape!r}"
    assert elapsed < _LINEAR_BACKSTOP_S, f"_redact took {elapsed:.2f} s on an argv array of {shape!r}"
