"""A list of strings is a command line, and the Inbox preview scrubs it as one (ticket 01a11cd3-55b6).

The scrubber's flag rules look at TEXT: ``mysql ... -phunter2``, ``curl -u admin:hunter2``, ``deploy --pass hunter2``. A tool whose command is an argv
array (``["mysql", "-u", "root", "-phunter2"]``) was walked member by member, so each rule saw one word and none matched: the password sat in the
preview in the clear. The same list in a JSON string, in a list of commands, or as the whole ``arguments`` leaked the same way.

Safe by construction (review of #618, round 3). Two earlier designs let the line pass REPLACE a word's scrub, and each leaked something main's per-member scrub hid
(shlex quoting; then a joined-line version that lost the quoted-value rules, and a string fallback built from the line alone). Now:

* every word of the list is the FLOOR: exactly what the member-by-member walk always gave it (``_redact`` of the member), and nothing else is ever shown for it;
* the words are also joined into one line (``\x1f`` between them, a word with spaces and no quote wrapped in single quotes, the quotes of a word with no space swapped
  for private-use stand-ins) and scrubbed ONCE with a length-preserving mark (``_scrub_text(line, mark)``: every replacement becomes as many marks as it replaced);
* a word whose marks in the line are not the marks its own text gets alone was found to hold a secret only because of its neighbours: it is shown as the literal
  ``<redacted>``, never as text derived from the line. So the line pass can only hide MORE than the floor, whatever it finds, and the result is always a list;
* the work is bounded by construction: only the words that fit in ``_REDACT_MAX_TEXT`` characters of line are taken (the rest are counted in ``<N more>``), and every
  character handed to the scrubber was charged to the walk's shared budget.

Kept limits, on purpose: a list of fewer than two members, or with no string in it, is walked member by member (there is no program word for a flag rule to hang on).
The accepted false positives of the text rules (``find /var/lib/mysql -print``, ``--no-password db``, ``--password-stdin registry``) apply to a list in the same way.
"""

from __future__ import annotations

import json
import random
import re

import pytest

from primer.api.routers.workspaces import (
    _REDACT_BUDGET,
    _REDACT_MAX_TEXT,
    _approval_preview,
    _bounded,
    _redact,
    _scrub_text,
)
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
    pytest.param(["curl", "-u", "ad min:hunter2", "https://x/y"], "hunter2", id="a user name with a space (only the quotes the line gives the word keep it one value)"),
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
        (["mysql", "-u", "root", "-phunter2", "db"], 'command=["mysql", "-u", "root", "<redacted>", "db"]'),
        (["deploy", "--pass", "hunter2"], 'command=["deploy", "--pass", "<redacted>"]'),
        (["curl", "-u", "admin:hunter2", "https://x/y"], 'command=["curl", "-u", "<redacted>", "https://x/y"]'),
        (["curl", "-u", "deploy:correct horse battery", "https://x/y"], 'command=["curl", "-u", "<redacted>", "https://x/y"]'),
        (["curl", "--max-time", 10, "-u", "admin:hunter2", "https://x/y"], 'command=["curl", "--max-time", 10, "-u", "<redacted>", "https://x/y"]'),
        (["Authorization:", "Bearer", "abc123def456"], 'command=["Authorization:", "<redacted>", "<redacted>"]'),
    ],
)
def test_a_scrubbed_argv_array_keeps_its_words_and_only_the_secret_changes(argv, line) -> None:
    assert _preview({"command": argv})["arguments"] == line


def test_the_scrub_result_for_an_argv_array_is_the_list_with_the_secret_replaced_and_a_changed_flag() -> None:
    got, changed = _redact(["mysql", "-u", "root", "-phunter2", "db"])

    assert (got, changed) == (["mysql", "-u", "root", "<redacted>", "db"], True)


def test_a_list_given_as_json_text_keeps_the_shape_the_mapping_walk_always_gave_it() -> None:
    """The pre-existing contract (tests/api/test_workspace_yields_pending.py): a JSON list of arguments is shown as a list with the secret replaced."""
    got = _preview('["Bearer abcdefgh", "ok"]')

    assert got["arguments"] == '["Bearer <redacted>", "ok"]' and got["truncated"] is True


def test_a_rule_that_swallows_a_whole_word_still_gives_a_list() -> None:
    """``Authorization: Bearer x`` is one match that spans three words: each word the match touches is ``<redacted>``, and the result is never a string built from the line."""
    got, changed = _redact(["Authorization:", "Bearer", "abc123def456"])

    assert (got, changed) == (["Authorization:", "<redacted>", "<redacted>"], True)


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


def test_a_word_with_an_open_quote_is_not_an_error_and_not_rewritten() -> None:
    """The words are never re-split by a shell parser, so an unbalanced quote cannot raise (the first version split with ``shlex`` and had to fall back). A quote after
    ``password=`` is part of the value for the scan (a ``'`` inside a secret must not end it), so the line pass finds a value the word alone does not have, and the
    word is hidden whole: the safe direction."""
    assert _redact(["curl", "password='"]) == (["curl", "<redacted>"], True)
    assert _redact(["curl", '"', "it's", "a b"]) == (["curl", '"', "it's", "a b"], False)
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


# ---- review of #618: the lead's probes (/var/tmp/rv618_probe/probe.py), each a secret that main's per-member scrub hid or the ticket's own shape ----------------------------

_PROBES = [
    pytest.param(["bash", "-c", "mysql -u root -p'hunter2' db"], ["hunter2"], id="A1 sh -c mysql -p'quoted'"),
    pytest.param(["sh", "-c", "curl -u 'admin:hunter2' https://x"], ["hunter2"], id="A2 sh -c curl -u 'user:pass'"),
    pytest.param(["sh", "-c", "export DB_PASSWORD='hunter2'; run"], ["hunter2"], id="A3 sh -c export DB_PASSWORD='x'"),
    pytest.param(["sh", "-c", "deploy --password 'hunter2'"], ["hunter2"], id="A4 sh -c deploy --password 'x'"),
    pytest.param(["curl", "-d", "password='hunter2'"], ["hunter2"], id="A5 curl -d password='x'"),
    pytest.param(["sh", "-c", "echo token: 'correct horse battery'"], ["correct", "horse", "battery"], id="A6 sh -c token: 'x y'"),
    pytest.param(["sh", "-c", "mysqldump -p'correct horse' db"], ["correct", "horse"], id="A7 sh -c mysqldump -p'two words'"),
    pytest.param(["bash", "-lc", "PGPASSWORD='hunter2' psql -h db"], ["hunter2"], id="A8 bash -lc PGPASSWORD='x'"),
    pytest.param(["sh", "-c", "curl -H 'X-Api-Key: hunter2' https://x"], ["hunter2"], id="A9 sh -c curl -H 'X-Api-Key: x'"),
    pytest.param(["sh", "-c", "git clone https://user:hunter2@host/repo.git"], ["hunter2"], id="A10 sh -c git clone user:pw@"),
    pytest.param(["sh", "-c", "echo 'Bearer abc123def456'"], ["abc123def456"], id="A11 sh -c echo 'Bearer x'"),
    pytest.param(["mysql", "-u", "root", "-pS3cr3t!", "db"], ["S3cr3t!"], id="B1 mysql -p with !"),
    pytest.param(["mysql", "-u", "root", "-phunter$2", "db"], ["hunter$2"], id="B2 mysql -p with $"),
    pytest.param(["mysql", "-u", "root", "-pP#ssw0rd", "db"], ["P#ssw0rd"], id="B3 mysql -p with #"),
    pytest.param(["mysql", "-u", "root", "-p\u043f\u0430\u0440\u043e\u043b\u044c", "db"], ["\u043f\u0430\u0440\u043e\u043b\u044c"], id="B4 mysql -p non-ascii"),
    pytest.param(["mysql", "-u", "root", "-phunter\x002", "db"], ["hunter"], id="B5 mysql -p with NUL"),
    pytest.param(["mysql", "-u", "root", "-p'hunter2'", "db"], ["hunter2"], id="B6 mysql -p'quoted' as a word"),
    pytest.param(["mysql", "-u", "root", '-p"hunter2"', "db"], ["hunter2"], id="B7 mysql -p\"quoted\" as a word"),
    pytest.param(["mysql", "-u", "root", "-phunter&2", "db"], ["hunter&2"], id="B8 mysql -p with &"),
    pytest.param(["deploy", "--pass", "abc'hunter2xyz"], ["hunter2xyz"], id="C1 --pass with a quote inside"),
    pytest.param(["curl", "-u", "admin:pa'ssw0rdTail", "https://x"], ["ssw0rdTail"], id="C2 -u with a quote inside"),
    pytest.param(["deploy", "--pass", "'hunter2'"], ["hunter2"], id="C3 --pass pre-quoted"),
    pytest.param(["deploy", "--pass", "S3cr3t!"], ["S3cr3t!"], id="C4 --pass with !"),
    pytest.param(["curl", "-u", "admin:S3cr3t!", "https://x"], ["S3cr3t!"], id="C5 -u with !"),
    pytest.param(["fetch", "Bearer", "tok~abc123"], ["tok~abc123"], id="C6 Bearer with ~"),
    pytest.param(["run", "password=it'shunter2"], ["shunter2"], id="C7 password= with a quote"),
    pytest.param(["mysql", None, "-phunter2"], ["hunter2"], id="E1 None among the words"),
    pytest.param(["mysql", "-phunter2", {"a": 1}], ["hunter2"], id="E2 an object among the words"),
    pytest.param(["mysql", True, "-phunter2"], ["hunter2"], id="E3 a boolean among the words"),
    pytest.param([{"cmd": ["mysql", "-phunter2"]}], ["hunter2"], id="E6 a command list inside an object inside a list"),
    pytest.param(["curl", "-uadmin:hunter2", "https://x"], ["hunter2"], id="E8 -u attached"),
    pytest.param(["curl", ["-u", "admin:hunter2"], "https://x"], ["hunter2"], id="E9 the flag and its value in a nested list"),
]


@pytest.mark.parametrize(("argv", "secrets"), _PROBES)
def test_no_probe_shows_its_secret_in_the_scrub_or_in_the_preview(argv, secrets) -> None:
    got, _ = _redact(argv)
    line = _preview({"command": argv})["arguments"]
    shown = json.dumps(got, ensure_ascii=False, default=str) + " || " + line

    leaked = [s for s in secrets if s in shown]
    assert not leaked, f"{leaked} still show: {shown}"


# Text a person types into a shell, with the secret the TEXT rules already hide. An argv holding the text as ONE word (sh -c "<text>") must hide exactly what the text form
# hides: the per-member scrub is the floor, so the list form is never worse than main was.
_TEXT_FLOOR = [
    ("mysql -u root -p'hunter2' db", "hunter2"),
    ("mysql -u root -phunter2 db", "hunter2"),
    ("curl -u 'admin:hunter2' https://x", "hunter2"),
    ("curl -u admin:hunter2 https://x", "hunter2"),
    ("curl -u 'deploy:correct horse battery' https://x", "correct horse battery"),
    ("export DB_PASSWORD='hunter2'; run", "hunter2"),
    ("export DB_PASSWORD=hunter2; run", "hunter2"),
    ("deploy --password 'hunter2'", "hunter2"),
    ("deploy --password hunter2", "hunter2"),
    ("PGPASSWORD='hunter2' psql -h db", "hunter2"),
    ("echo token: 'correct horse battery'", "correct horse battery"),
    ("curl -H 'Authorization: Bearer abc123def456' https://x", "abc123def456"),
    ("curl -H \"X-Api-Key: hunter2\" https://x", "hunter2"),
    ("password='hunter2'", "hunter2"),
    ('{"password": "hunter2"}', "hunter2"),
    ("git clone https://user:hunter2@host/repo.git", "hunter2"),
    ("echo sk-abcdefghijklmnop", "sk-abcdefghijklmnop"),
    ("echo ghp_abcdefghijklmnop", "ghp_abcdefghijklmnop"),
    ("echo " + "A1b2C3d4" * 6, "A1b2C3d4A1b2C3d4"),
]


@pytest.mark.parametrize(("text", "secret"), _TEXT_FLOOR)
def test_a_word_holding_a_command_hides_what_the_text_form_hides(text: str, secret: str) -> None:
    assert not [p for p in secret.split() if p in _scrub_text(text)], "the corpus entry is hidden by the text rules today"
    for argv in (["sh", "-c", text], ["x", text], [text, "y"], ["bash", "-lc", text, "--"], ["a", "b", text, "c", "d"]):
        shown = json.dumps(_redact(argv)[0], ensure_ascii=False) + " || " + _preview({"command": argv})["arguments"]
        leaked = [p for p in secret.split() if p in shown]
        assert not leaked, f"{leaked} show for {argv!r}: {shown}"


def test_the_separator_is_whitespace_to_every_rule() -> None:
    """The words join with U+001F: if a rule stopped treating it as a gap, a flag and its value would stop being neighbours."""
    assert "\x1f".isspace()
    assert _scrub_text("mysql\x1f-u\x1froot\x1f-phunter2\x1fdb") == "mysql\x1f-u\x1froot\x1f-p<redacted>\x1fdb"
    assert _scrub_text("deploy\x1f--pass\x1fhunter2") == "deploy\x1f--pass\x1f<redacted>"
    # _BEARER and _BASIC write a plain space between the word and the placeholder (a plain scrub; the marking scrub keeps the separator, so words keep their places).
    assert _scrub_text("fetch\x1fBearer\x1ftok~abc123") == "fetch\x1fBearer <redacted>"
    assert _scrub_text("fetch\x1fBearer\x1ftok~abc123", "\ue003") == "fetch\x1fBearer\x1f" + "\ue003" * 10
    assert _scrub_text("mysql\x1f-u\x1froot\x1f-phunter2\x1fdb", "\ue003") == "mysql\x1f-u\x1froot\x1f-p" + "\ue003" * 7 + "\x1fdb"


def test_no_shell_quoting_reaches_the_approver() -> None:
    """The first version showed a quote inside a word as ``'"'"'`` and wrapped words in quotes of its own."""
    got, changed = _redact(["deploy", "--pass", "abc'hunter2xyz", "it's here", "plain"])
    assert changed is True
    assert got == ["deploy", "--pass", "<redacted>", "it's here", "plain"], got
    assert "'\"'\"'" not in json.dumps(got)

    line = _preview({"command": ["curl", "-u", "a b:c d", "https://x/y"]})["arguments"]
    assert "'" not in line.replace("<redacted>", ""), line
    assert _redact(["echo", "it's", "a b", "x'y"]) == (["echo", "it's", "a b", "x'y"], False)


def test_a_word_with_spaces_and_no_quote_stays_one_word_in_the_result() -> None:
    got, changed = _redact(["curl", "-u", "deploy:correct horse battery", "https://x/y"])

    assert (got, changed) == (["curl", "-u", "<redacted>", "https://x/y"], True)
    unchanged, flag = _redact(["echo", "two words", "three more words"])
    assert (unchanged, flag) == (["echo", "two words", "three more words"], False)


def test_an_object_among_the_words_stays_an_object_and_the_words_around_it_are_scrubbed() -> None:
    assert _redact(["mysql", "-phunter2", {"a": 1}]) == (["mysql", "<redacted>", {"a": 1}], True)
    assert _redact(["curl", None, "-u", "admin:hunter2"]) == (["curl", None, "-u", "<redacted>"], True)
    assert _redact(["sleep", 5, True, None, "x"]) == (["sleep", 5, True, None, "x"], False)
    assert _redact(["curl", {"password": "hunter2"}, "https://x/y"]) == (["curl", {"password": "<redacted>"}, "https://x/y"], True)


def test_a_json_document_among_the_words_is_still_json_when_it_is_scrubbed() -> None:
    got, changed = _redact(["curl", "-d", '{"key": "zzz-not-a-shape", "n": 1}', "https://x/y"])

    assert changed is True
    assert json.loads(got[2]) == {"key": "<redacted>", "n": 1}, got


@pytest.mark.parametrize(
    ("argv", "shown"),
    [
        (["pg_dump", "--no-password", "db"], ["pg_dump", "--no-password", "<redacted>"]),
        (["docker", "login", "--password-stdin", "registry.example.com"], ["docker", "login", "--password-stdin", "<redacted>"]),
    ],
)
def test_the_false_positives_of_the_text_rules_apply_to_a_list_the_same_way(argv, shown) -> None:
    """Accepted (a click on show all): the flag rule reads ``--no-password`` and ``--password-stdin`` as a secret flag and hides the word after it, in text and in a list."""
    assert _scrub_text(" ".join(argv)).split(" ") == shown, "the text form does the same"
    assert _redact(argv) == (shown, True)


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
def test_an_argv_array_of_hostile_members_is_linear(shape: str) -> None:  # noqa: C901 - the next test adds the quote-heavy words
    text = _HOSTILE[shape]
    members = [text[i * 1500 : (i + 1) * 1500] for i in range(50)]

    elapsed = _timed_in_a_child("_redact", members)

    assert elapsed is not None, f"_redact was still running after 6 s on an argv array of {shape!r}"
    assert elapsed < _LINEAR_BACKSTOP_S, f"_redact took {elapsed:.2f} s on an argv array of {shape!r}"


@pytest.mark.parametrize("word", ["'" * 2000, "'\"" * 1000, "-u'" * 600, "mysql -p'" * 300, "x y '" * 400, "\\" * 2000])
def test_an_argv_array_of_quote_heavy_words_is_linear_and_bounded(word: str, monkeypatch) -> None:
    from primer.api.routers import workspaces as w

    seen = _recording_scrubber(monkeypatch)
    elapsed = _timed_in_a_child("_redact", [word] * 50)
    assert elapsed is not None and elapsed < _LINEAR_BACKSTOP_S, f"_redact took {elapsed} s"

    w._redact([word] * 50)
    assert max(seen) <= w._REDACT_MAX_TEXT, f"the scrubber was handed {max(seen)} characters at once"
    assert sum(seen) <= w._REDACT_BUDGET + 4000, f"{sum(seen)} characters were scrubbed"


# ---- review of #618, round 3 (security): the per-word scrub is a FLOOR, and the line pass may only hide MORE ---------------------------------------------------------

_MARK = ""


@pytest.mark.parametrize(
    ("text", "marked"),
    [
        ("mysql -u root -phunter2 db", "mysql -u root -p" + _MARK * 7 + " db"),
        ("deploy --pass hunter2", "deploy --pass " + _MARK * 7),
        ("Authorization: Bearer abc123", "Authorization: " + _MARK * 13),
        ("fetch Bearer tok~abc123", "fetch Bearer " + _MARK * 10),
        ("git clone https://user:hunter2@host/r.git", "git clone https://" + _MARK * 12 + "@host/r.git"),
        ("echo sk-abcdefghijkl", "echo " + _MARK * 15),
    ],
)
def test_the_marking_scrub_replaces_each_piece_with_as_many_marks_as_it_had(text: str, marked: str) -> None:
    assert _scrub_text(text, _MARK) == marked


_PIECES = [
    "mysql", "-u", "-p", "-phunter2", "--password", "--pass", "hunter2", "Bearer", "abc123def456", "Authorization:", "token:", "password=", "admin:hunter2",
    "https://user:pw@host/x", "sk-abcdefghijkl", "A1b2C3d4" * 6, "'", '"', " ", "\x1f", ";", "&", ",", "<", ">", "=", ":", "x", "y z", "", "", "",
]


def test_the_marking_scrub_keeps_the_length_and_marks_exactly_when_the_plain_scrub_changes_the_text() -> None:
    """The line pass reads WHERE the marks fall, so a replacement that did not keep the length would shift every word after it onto the wrong span."""
    rng = random.Random(618)
    marked_some = 0
    for _ in range(4000):
        text = "".join(rng.choice(_PIECES) for _ in range(rng.randint(1, 9)))
        marked = _scrub_text(text, _MARK)
        assert len(marked) == len(text), (text, marked)
        assert (_MARK in marked) == (_scrub_text(text) != text), (text, marked)
        assert all(m == o for m, o in zip(marked, text, strict=True) if m != _MARK), (text, marked)
        marked_some += _MARK in marked
    assert marked_some > 500, "the corpus has to find secrets, or the checks above say nothing"


def _floor(member):
    """What the member-by-member walk gives ``member`` on its own: the floor a word of a command line never drops below."""
    return _redact(member, 1, [_REDACT_BUDGET])[0]


_WORDS = [
    "mysql", "mysqldump", "-u", "root", "-p", "-phunter2", "-p'hunter2'", "--password", "--pass", "--api-key", "hunter2", "Bearer", "abc123def456", "Authorization:", "token:",
    "curl", "-H", "admin:hunter2", "deploy:correct horse", "PGPASSWORD='hun;ter2'", 'DB_PASS="x,ysecret"', "password='x;hunter2' z", "--password='a&bsecret'",
    "https://user:pw@host/x", "sk-abcdefghijkl", '{"k": "v"}', '{"password": "hunter2"}', "[1]", "'", '"', "a b", "it's", "x", ";", "&", "=", ":", "\x1f", "", "",
    "", "", "sh", "-c", "mysql -u root -phunter2 db", "curl -u admin:hunter2 https://x", "export DB_PASSWORD='hunter2'; run",
]
_SCALARS = [None, True, 5, 2.5]
_STRUCTURES = [{"a": 1}, {"password": "hunter2"}, ["l"], ["mysql", "-phunter2"], {"cmd": ["x", "--pass", "hunter2"]}]


def _random_argv(rng: random.Random) -> list:
    """Three to eight words: a list of exactly two whose first word names a secret is the name/value pair rule's (``["--pass", x]``), which runs before the command line does."""
    argv: list = []
    for _ in range(rng.randint(3, 8)):
        r = rng.random()
        argv.append(rng.choice(_WORDS) if r < 0.78 else rng.choice(_SCALARS) if r < 0.88 else rng.choice(_STRUCTURES))
    if not any(isinstance(m, str) for m in argv):
        argv.append("x")
    return argv


def test_every_word_of_a_list_is_its_floor_or_the_literal_redacted() -> None:
    """The differential property of the review (2 543 of 22 213 lists showed a secret main hides): whatever the line pass finds, a word is either what the
    member-by-member walk shows for it, or ``<redacted>``. Never text derived from the line, never a string for the whole list."""
    rng = random.Random(618)
    hid_more = 0
    for _ in range(4000):
        argv = _random_argv(rng)
        got, changed = _redact(argv)
        assert isinstance(got, list) and len(got) == len(argv), (argv, got)
        for member, shown in zip(argv, got, strict=True):
            floor = _floor(member)
            assert shown == floor or shown == "<redacted>", (argv, member, shown, floor)
            hid_more += shown != floor
        assert changed is True or got == argv, (argv, got)
        assert isinstance(_preview({"command": argv})["arguments"], str)
    assert hid_more > 300, "the line pass has to find things beyond the floor, or the property above says nothing"


def test_a_secret_the_word_alone_hides_is_hidden_wherever_the_word_sits() -> None:
    """The review's fuzz: a secret holding ``, ; & < >`` or a quote, in a word the per-member scrub hides, beside neighbours of every kind."""
    rng = random.Random(618)
    names = ["PGPASSWORD", "DB_PASS", "password", "api_key", "token", "--password", "--db-password", "secret"]
    seps = ["", ";", "&", ",", "<", ">", "|", " ", "'", '"', "\\", "\x1f", "", "!", "$"]
    pool = ["sh", "-c", "env", "token:", "--pass", "Authorization:", "Bearer", "x", "-H", '{"a":1}', None, 5, True, {"k": "v"}, ["l"], "a b", "it's", "--", "mysql", "-u", "root"]
    cases = 0
    for _ in range(3000):
        secret = "Zq" + "".join(rng.choice("abcdefghk0123456789") for _ in range(6))
        eq = rng.choice(["=", ": ", "="])
        name = rng.choice(names)
        if name.startswith("--") and rng.random() < 0.5:
            eq = " "
        quote = rng.choice(["'", '"', ""])
        word = f"{name}{eq}{quote}{rng.choice(['', 'ab', 'x y', 'p'])}{rng.choice(seps)}{secret}{quote}"
        if secret in _scrub_text(word):
            continue
        argv = [rng.choice(pool) for _ in range(rng.randint(0, 4))]
        argv.insert(rng.randint(0, len(argv)), word)
        if len(argv) < 2:
            argv.append("y")
        cases += 1
        shown = json.dumps(_redact(argv)[0], ensure_ascii=False, default=str) + " || " + _preview({"command": argv})["arguments"]
        assert secret not in shown, (argv, shown)
    assert cases > 1500


_FLOOR_HIDES = [
    pytest.param(["env", "PGPASSWORD='hun;ter2'", "psql"], ["ter2"], id="F1 a quoted secret holding a semicolon"),
    pytest.param(["deploy", "--password='a&bsecret'"], ["bsecret"], id="F2 an ampersand"),
    pytest.param(["run", 'DB_PASS="x,ysecret"'], ["ysecret"], id="F3 a comma"),
    pytest.param(["run", "token=x'password=';hunter2'"], ["hunter2"], id="F4 a second assignment inside the first"),
    pytest.param(["-H", "token:", "password='x;hunter2' z"], ["hunter2"], id="F5 beside a cross-word anchor"),
    pytest.param(["Authorization:", "Bearer", "x", "PGPASSWORD='hun;ter2'"], ["ter2"], id="F6 beside the words of a header"),
    pytest.param(["env", "API_KEY='abc<def>ghi'", "x"], ["ghi"], id="F7 angle brackets"),
    pytest.param(["sh", "-c", "PGPASSWORD='hun;ter2' psql"], ["ter2"], id="F8 as one spaced word"),
    pytest.param(["app", '--db-password="p&ssw0rd"'], ["ssw0rd"], id="F9 a double-quoted ampersand"),
    pytest.param(["kubectl", "create", "secret", "generic", "db", "--from-literal=password='S3cr3t&x9'"], ["x9"], id="K1 --from-literal"),
    pytest.param(["docker", "run", "-e", "POSTGRES_PASSWORD='p;w0rdTail'", "postgres"], ["w0rdTail"], id="K2 docker -e"),
    pytest.param(["env", "PGPASSWORD='&hunter2'", "psql"], ["hunter2"], id="K3 a secret that starts with an ampersand"),
    pytest.param(["x", '"password":"hunter2"'], ["hunter2"], id="K4 the floor alone (the line swaps the quotes and misses it)"),
]


@pytest.mark.parametrize(("argv", "secrets"), _FLOOR_HIDES)
def test_a_secret_main_hides_is_never_shown_because_the_line_pass_saw_it_differently(argv, secrets) -> None:
    assert not [s for s in secrets if s in json.dumps([_floor(m) for m in argv], ensure_ascii=False)], "the corpus entry is hidden by the per-member scrub today"
    shown = json.dumps(_redact(argv)[0], ensure_ascii=False, default=str) + " || " + _preview({"command": argv})["arguments"]
    leaked = [s for s in secrets if s in shown]
    assert not leaked, f"{leaked} show: {shown}"


def test_the_floor_alone_case_keeps_the_words_main_shows() -> None:
    assert _redact(["x", '"password":"hunter2"']) == (["x", '"password":<redacted>'], True)


_LINE_FINDS = [
    pytest.param(["deploy", "--pass", "hun\x1fter2"], ["hun", "ter2"], id="G2 a value holding the separator"),
    pytest.param(["deploy", "--pass", "abc'hunter2"], ["hunter2"], id="G3 a quote inside the value"),
    pytest.param(["mysql", "\x1f", "-phunter2"], ["hunter2"], id="G7a a word that is the separator"),
    pytest.param(["password", "=", "hunter2"], ["hunter2"], id="H1 name, equals sign and value as three words"),
    pytest.param(["--password", "'a b", "c d'", "e"], ["a b", "c d"], id="H4 a quote opened in one word and closed in the next"),
]


@pytest.mark.parametrize(("argv", "secrets"), _LINE_FINDS)
def test_what_only_the_neighbours_show_is_hidden_whole(argv, secrets) -> None:
    shown = json.dumps(_redact(argv)[0], ensure_ascii=False, default=str) + " || " + _preview({"command": argv})["arguments"]
    leaked = [s for s in secrets if s in shown]
    assert not leaked, f"{leaked} show: {shown}"


def test_a_word_with_a_secret_and_nothing_cross_word_keeps_what_main_shows_of_it() -> None:
    """The line pass hides a word only for what ITS NEIGHBOURS add. A script given to ``sh -c`` is scrubbed as it always was (the secret replaced, the rest readable)."""
    assert _redact(["sh", "-c", "export DB_PASSWORD='hunter2'; run"]) == (["sh", "-c", "export DB_PASSWORD=<redacted>; run"], True)
    assert _redact(["curl", "-H", "Authorization: Bearer abc123def456", "https://x/y"]) == (["curl", "-H", "Authorization: <redacted>", "https://x/y"], True)
    assert _redact(["git", "clone", "https://user:hunter2@host/r.git"]) == (["git", "clone", "https://<redacted>@host/r.git"], True)


# --- a JSON-document word that was cut is never shown whole ---------------------------------------------------------------------------


def test_a_json_document_cut_at_the_ceiling_does_not_show_what_lies_past_the_cut() -> None:
    """A bulk body of two JSON lines: the head parses as a document and is unchanged, and the old code answered with the ORIGINAL member (secret and all)."""
    body = '{"index":{}}\n{"password":"hunter2","data":"' + "A" * 2000 + '"}'
    got, changed = _redact(["curl", "-H", "Content-Type: application/x-ndjson", "-d", body, "https://es/_bulk"])

    assert changed is True
    assert "hunter2" not in json.dumps(got)
    assert all(len(w) <= _REDACT_MAX_TEXT for w in got if isinstance(w, str)), [len(w) for w in got if isinstance(w, str)]
    assert "hunter2" not in _preview({"command": ["curl", "-d", body]})["arguments"]


def test_a_cut_word_is_shown_cut_even_when_what_is_left_is_a_document() -> None:
    padded = "[1]" + " " * 1997 + " password=hunter2"
    got, _ = _redact(["curl", "-d", padded])
    assert "hunter2" not in json.dumps(got)

    got, _ = _redact(["curl", "-d", "[1] " + "x" * 50_000])
    assert len(json.dumps(got)) < 2_500, len(json.dumps(got))
    assert got[2] == _bounded("[1] " + "x" * 50_000, _REDACT_MAX_TEXT)


# --- the work is bounded by construction ------------------------------------------------------------------------------------------------


def _nested_bearer_lists(levels: int) -> list:
    inner: object = {f"k{i}": '"' * 2000 for i in range(5)}
    for _ in range(levels):
        inner = ["Authorization:", "Bearer", "x", inner]
    return inner  # type: ignore[return-value]


_BOUNDED_CASES = {
    "a 10 MB word after a header": lambda: ["Authorization:", "Bearer", "x", "[1] " + "token" * 2_000_000],
    "a 10 MB word and nothing to find": lambda: ["curl", "-d", "[1] " + "token" * 2_000_000],
    "seven nested lists around 10 000 quotes": lambda: _nested_bearer_lists(7),
    "forty-seven huge words after a header": lambda: ["Authorization:", "Bearer", "x"] + ["[1] " + "x" * 100_000] * 47,
    "fifty hostile words": lambda: ["[1] " + "token" * 400] * 50,
}


@pytest.mark.parametrize("case", sorted(_BOUNDED_CASES))
def test_a_command_line_is_scrubbed_in_bounded_work_whatever_its_words_hold(case: str, monkeypatch) -> None:
    from primer.api.routers import workspaces as w

    command = _BOUNDED_CASES[case]()
    call = {"name": "t", "arguments": {"command": command}}
    elapsed = _timed_in_a_child("_approval_preview", call)
    assert elapsed is not None and elapsed < _LINEAR_BACKSTOP_S, f"the preview took {elapsed} s"

    seen = _recording_scrubber(monkeypatch)
    w._approval_preview(call)
    assert max(seen) <= w._REDACT_MAX_TEXT, f"the scrubber was handed {max(seen)} characters at once"
    assert sum(seen) <= w._REDACT_BUDGET + 4000, f"{sum(seen)} characters were scrubbed for one preview"
    seen.clear()
    got, _ = w._redact(command)
    assert max(seen) <= w._REDACT_MAX_TEXT and sum(seen) <= w._REDACT_BUDGET + 4000, (max(seen), sum(seen))
    assert len(json.dumps(got, default=str)) < 3 * w._REDACT_BUDGET, "what comes back is bounded too"


def test_a_first_word_that_fills_the_ceiling_enters_the_line_without_its_quotes(monkeypatch) -> None:
    """``"word " * 400`` is exactly the ceiling. A word with spaces and no quote is wrapped for the line, which would make it 2002 characters; the first word has nothing to
    give way to, so it enters as it is, and the word after it is counted in ``<N more>``."""
    from primer.api.routers import workspaces as w

    seen = _recording_scrubber(monkeypatch)
    got, changed = w._redact(["word " * 400, "-phunter2"])

    assert max(seen) <= w._REDACT_MAX_TEXT, f"the scrubber was handed {max(seen)} characters at once"
    assert (got[0], got[-1], changed) == ("word " * 400, "<1 more>", True)


# ---- follow-ups of #618 (the review that approved e091e937) ------------------------------------------------------------------------------------------------------------


def test_a_token_the_budget_cuts_is_hidden_and_its_head_is_not_drawn() -> None:
    """Two lists of three 660-character words use up the walk's budget; the 64-character hex token after them was cut to its first 18 characters, which is too short
    for the blob rule, and ``command=deadbeefdeadbeefde`` was drawn."""
    args = {"path": ["q" * 660] * 3, "file_path": ["r" * 660] * 3, "command": "deadbeef" * 8}

    got = _approval_preview({"name": "run", "arguments": args})

    assert "deadbeef" not in got["arguments"], got["arguments"]
    assert got["arguments"].endswith("command=<redacted>") and got["truncated"] is True


@pytest.mark.parametrize("budget", [1, 5, 20, 32])
@pytest.mark.parametrize("token", ["deadbeef" * 8, "correcthorsebatterystaple" * 3])
def test_a_token_cut_by_what_is_left_of_the_budget_is_redacted_whole(token: str, budget: int) -> None:
    assert _redact(token, 0, [budget]) == ("<redacted>", True)


def test_a_cut_that_falls_between_tokens_keeps_the_tokens_it_kept() -> None:
    """Only a cut INSIDE a token hides it: a cut at a space shows the whole tokens before it (the rest is not looked at, and the preview says so)."""
    assert _redact("alpha beta gamma", 0, [11]) == ("alpha beta", True)
    assert _redact("deadbeef" * 2, 0, [16]) == ("deadbeef" * 2, False)           # a token that fits in what is left is not cut at all


def test_a_token_cut_by_the_ceiling_alone_keeps_its_head_as_before() -> None:
    """The ceiling (``_REDACT_MAX_TEXT``) leaves the blob rule 2000 characters to work with, so the head of a long plain word is only cut, not hidden."""
    assert _redact("q" * 3000) == ("q" * _REDACT_MAX_TEXT, True)


@pytest.mark.parametrize(
    ("argv", "secrets"),
    [
        pytest.param(["mysql", "-phun\x1fter2"], ["hun"], id="G1 -p value holding the separator"),
        pytest.param(["mysql", "-pcorrect horse", "db"], ["correct", "horse"], id="G9 mysql -p password with a space"),
        pytest.param(["mysqldump", "-u", "root", "-pcorrect horse battery", "db"], ["correct", "horse", "battery"], id="G10 mysqldump -p password with spaces"),
    ],
)
def test_a_spaced_word_that_starts_with_a_dash_is_a_flag_with_its_value_not_a_value(argv, secrets) -> None:
    """The line wraps a word with spaces in single quotes so ``-u 'deploy:correct horse'`` is one value, and the quote hid the ``-p`` of ``-pcorrect horse`` from the rule
    that needs a space before it. A word that starts with a dash is a flag, not a value: it enters the line as it is."""
    shown = json.dumps(_redact(argv)[0], ensure_ascii=False) + " || " + _preview({"command": argv})["arguments"]

    leaked = [s for s in secrets if s in shown]
    assert not leaked, f"{leaked} show: {shown}"


def test_the_line_wraps_a_spaced_value_and_leaves_a_spaced_flag_alone() -> None:
    from primer.api.routers.workspaces import _line_word

    assert _line_word("correct horse") == ("'correct horse'", True)
    assert _line_word("-pcorrect horse") == ("-pcorrect horse", False)
    assert _line_word("--message=fix the bug") == ("--message=fix the bug", False)


def test_the_kept_misses_of_the_list_form_are_the_ones_the_docs_list() -> None:
    """Kept on purpose (listed in ui-pages.md): a header split from its value across two words, the first holding a space. The line wraps that word in quotes, which hides its
    ``Bearer`` from the rule; the token in the next word is shown. The per-word scrub never hid it either. If this test starts failing the miss is fixed: say so in the docs."""
    got, _ = _redact(["curl", "-H", "Authorization: Bearer", "tok123456"])

    assert got[-1] == "tok123456"


def test_the_marking_scrub_may_mark_what_the_plain_scrub_leaves_but_never_marks_less() -> None:
    """A later rule sees a mark where the plain scrub shows ``<redacted>``, which no rule matches: after ``token:value`` is hidden, ``-u`` + ``token:`` is a user part with a
    password the marking scrub still sees. So it can mark MORE than the plain scrub hides. A line the plain scrub changes is always marked (the fuzz above)."""
    plain = _scrub_text("-utoken:mysqly")
    marked = _scrub_text("-utoken:mysqly", _MARK)

    assert plain == "-utoken:<redacted>"
    assert marked == "-u" + _MARK * len("token:mysqly")


def test_no_literal_private_use_character_is_written_in_the_scrubber_or_its_tests() -> None:
    """The marks and stand-ins are private-use characters (U+E000 to U+E003): they are written as escapes, because the literal character renders as nothing in a diff and an
    editor, and a reader cannot tell the mark from an empty string."""
    from pathlib import Path

    from primer.api.routers import workspaces as w

    for path in (Path(__file__), Path(w.__file__)):
        text = path.read_text(encoding="utf-8")
        found = [n for n, line in enumerate(text.split("\n"), 1) if re.search("[\ue000-\uf8ff]", line)]
        assert not found, f"{path.name}: a literal private-use character on line(s) {found}"
