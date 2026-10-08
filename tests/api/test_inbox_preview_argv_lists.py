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

Review of #618 (security): the first version joined the words with ``shlex.quote``, which rewrites a quote inside a word (``'`` becomes ``'"'"'``) and wraps any word
with a character outside ``[\\w@%+=:,./-]`` (``-pS3cr3t!``, ``-phunter$2``, ``Bearer tok~abc``). Every quoted-value alternative of the rules then failed, and a secret that
main's per-member scrub hid was SHOWN (``["sh", "-c", "curl -u 'admin:hunter2' https://x"]``). Now:

* each string word is scrubbed ALONE first, exactly as before the first fix (a floor: the result is never worse than main's), and the scrubbed words are what joins;
* the words are joined with the unit separator ``\\x1f``, which every rule sees as whitespace (it is ``str.isspace()``) and no word holds, so the line splits back into
  the SAME words; a word is never rewritten. A word with spaces and no quote is wrapped in single quotes for the line only (so ``-u 'deploy:correct horse'`` is one
  value) and unwrapped again; the quote characters of a word with no space are swapped for private-use stand-ins for the scan, so a ``'`` inside a secret does not end it;
* numbers, ``None``, booleans, objects and lists are words too (a ``None`` or an object between a flag and its value no longer breaks the command line).

Kept limits, on purpose: a list of fewer than two members, or with no string in it, is walked member by member (there is no program word for a flag rule to hang on).
The accepted false positives of the text rules (``find /var/lib/mysql -print``, ``--no-password db``, ``--password-stdin registry``) apply to a list in the same way.
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


def test_a_word_with_an_open_quote_is_not_an_error_and_not_rewritten() -> None:
    """The words are never re-split by a shell parser, so an unbalanced quote cannot raise (the first version split with ``shlex`` and had to fall back). A quote after
    ``password=`` is part of the value for the scan (a ``'`` inside a secret must not end it), so it is hidden: the safe direction."""
    assert _redact(["curl", "password='"]) == (["curl", "password=<redacted>"], True)
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
    from primer.api.routers.workspaces import _scrub_text

    assert not [p for p in secret.split() if p in _scrub_text(text)], "the corpus entry is hidden by the text rules today"
    for argv in (["sh", "-c", text], ["x", text], [text, "y"], ["bash", "-lc", text, "--"], ["a", "b", text, "c", "d"]):
        shown = json.dumps(_redact(argv)[0], ensure_ascii=False) + " || " + _preview({"command": argv})["arguments"]
        leaked = [p for p in secret.split() if p in shown]
        assert not leaked, f"{leaked} show for {argv!r}: {shown}"


def test_the_separator_is_whitespace_to_every_rule() -> None:
    """The words join with U+001F: if a rule stopped treating it as a gap, a flag and its value would stop being neighbours."""
    from primer.api.routers.workspaces import _scrub_text

    assert "\x1f".isspace()
    assert _scrub_text("mysql\x1f-u\x1froot\x1f-phunter2\x1fdb") == "mysql\x1f-u\x1froot\x1f-p<redacted>\x1fdb"
    assert _scrub_text("deploy\x1f--pass\x1fhunter2") == "deploy\x1f--pass\x1f<redacted>"
    # _BEARER and _BASIC write a plain space between the word and the placeholder, so those two words come back as ONE (the line is then returned as a string).
    assert _scrub_text("fetch\x1fBearer\x1ftok~abc123") == "fetch\x1fBearer <redacted>"


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
    assert _redact(["mysql", "-phunter2", {"a": 1}]) == (["mysql", "-p<redacted>", {"a": 1}], True)
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
    from primer.api.routers.workspaces import _scrub_text

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
