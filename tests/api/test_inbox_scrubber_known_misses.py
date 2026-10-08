"""The Inbox preview scrubber's known heuristic misses (ticket 01a11b7d, found in the review of PR 503 round 3).

The preview is shoulder-surfing protection, not access control: "show all" and the session's own pending-yields route return the whole call.
It is a heuristic and errs towards hiding (a false positive costs a click on "show all", a false negative leaks a key). Three misses were
known and are closed here, each with the false positives it must NOT create:

* a name that is a credential without any of the secret words: ``DB_PASS``, ``PASS``, ``LOGIN_PW``, ``--pass x`` (``pass`` and ``pw`` count
  as whole name components, never inside another word: ``bypass``, ``compass``, ``passenger``);
* ``mysql -phunter2`` (the attached short flag, only after a mysql-family command: ``ssh -p22`` and ``find -print`` are not passwords);
* ``curl -u admin:hunter2`` (a ``user:password`` after ``-u`` / ``--user``: ``git push -u origin`` and ``docker run -u 1000:1000`` are not).

Still NOT caught, on purpose: a secret with no recognisable shape and no telling name (``{"note": "correct horse battery staple"}``), and a
40-character base64 secret that contains ``/`` and ``+`` and sits under a harmless name (adding ``/`` to the blob rule would redact long paths,
which are what the approver needs to read). Widening further inverts the default (a per-tool allowlist of safe argument names), a choice for
the lead, not a patch.
"""

from __future__ import annotations

import pytest

from primer.api.routers.workspaces import _approval_preview, _scrub_text


def _preview(arguments) -> dict:
    return _approval_preview({"name": "t", "arguments": arguments})


# --- names that are credentials without a secret word ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("export DB_PASS=hunter2 && run", "hunter2"),
        ("LOGIN_PW=hunter2 ./go", "hunter2"),
        ("PASS=hunter2 ./go", "hunter2"),
        ("pass: hunter2", "hunter2"),
        ("db-pass=hunter2", "hunter2"),
        ("deploy --pass hunter2 --env prod", "hunter2"),
        ("deploy --db-pw hunter2 --env prod", "hunter2"),
        ("deploy --pswd=hunter2", "hunter2"),
    ],
)
def test_a_name_with_pass_or_pw_as_a_component_hides_its_value(text: str, secret: str) -> None:
    got = _scrub_text(text)
    assert secret not in got and "<redacted>" in got, got


@pytest.mark.parametrize(
    "text",
    [
        "bypass=true",
        "compass=north",
        "passenger=3",
        "--bypass-cache yes",
        "ssh-keygen -t ed25519",
        "echo password-less",
        "pwd",
    ],
)
def test_pass_inside_another_word_is_not_a_credential(text: str) -> None:
    """``password-less`` still hits the existing ``passw`` rule only when it is a name with ``=`` or ``:`` after it; none of these is."""
    assert _scrub_text(text) == text


@pytest.mark.parametrize("key", ["DB_PASS", "pass", "login_pw", "db_pwd", "PW", "user-pswd"])
def test_an_argument_named_like_a_password_is_never_shown(key: str) -> None:
    got = _preview({key: "hunter2", "path": "/tmp/x"})
    assert "hunter2" not in got["arguments"], got
    assert "path=/tmp/x" in got["arguments"], "the other arguments are still shown"


@pytest.mark.parametrize("key", ["bypass", "compass", "passenger", "keyword", "monkey"])
def test_an_argument_with_pass_inside_a_longer_word_is_shown(key: str) -> None:
    assert f"{key}=ok" in _preview({key: "ok"})["arguments"]


# --- mysql -pSECRET ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "secret"),
    [
        ("mysql -u root -phunter2 appdb", "hunter2"),
        ("mysqldump -h db -u root -p'my secret' appdb > out.sql", "my secret"),
        ('mariadb -u root -p"hunter two" appdb', "hunter two"),
        ("mysql -phunter2", "hunter2"),
        ("MYSQL -u root -phunter2 x", "hunter2"),
        ("mysql -p22 appdb", "22"),
    ],
)
def test_an_attached_p_after_a_mysql_command_is_its_password(command: str, secret: str) -> None:
    got = _scrub_text(command)
    assert secret not in got and "-p<redacted>" in got, got


@pytest.mark.parametrize(
    "command",
    [
        "mysql -u root -p appdb",            # no attached value: mysql prompts
        "ssh -p22 host",
        "nc -p80 host",
        "find . -type f -print",
        "find . -perm -u+x -print0",
        "mysql -u root -e 'select 1' && ssh -p22 host",     # a separator ends the mysql command
        "grep -p foo file",
    ],
)
def test_other_attached_p_flags_are_left_alone(command: str) -> None:
    assert _scrub_text(command) == command


# --- curl -u user:password --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "secret"),
    [
        ("curl -u admin:hunter2 https://x.test", "hunter2"),
        ("curl --user admin:hunter2 https://x.test", "hunter2"),
        ("curl --user=admin:hunter2 https://x.test", "hunter2"),
        ("curl --proxy-user admin:hunter2 https://x.test", "hunter2"),
        ("curl -u 'admin:hunter2' https://x.test", "hunter2"),
        ('curl -u "admin:hunter2" https://x.test', "hunter2"),
    ],
)
def test_a_user_and_password_after_u_or_user_is_a_credential(command: str, secret: str) -> None:
    got = _scrub_text(command)
    assert secret not in got and "<redacted>" in got, got
    assert "https://x.test" in got, "the target is still readable"


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin main",
        "sort -u names.txt",
        "pip install -U pip",
        "docker run -u 1000:1000 img",
        "curl -u admin https://x.test",        # no colon: curl prompts for the password
        "useradd -u 1001 bob",
    ],
)
def test_other_uses_of_u_are_left_alone(command: str) -> None:
    assert _scrub_text(command) == command
