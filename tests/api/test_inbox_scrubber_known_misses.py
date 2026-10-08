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
which are what the approver needs to read). The structural answer is the per-tool allowlist (``tests/api/test_inbox_preview_allowlist.py``): a name the tool or
the policy did not allow is never drawn, whatever its value looks like; the misses above stay open for a tool that declares nothing and for a free-text
argument that is allowed on purpose.
"""

from __future__ import annotations

import pytest

from primer.api.routers.workspaces import _approval_preview, _scrub_text


def _preview(arguments) -> dict:
    return _approval_preview({"name": "t", "arguments": arguments})


def _assert_hidden(got: str, secret: str) -> None:
    """Every whitespace-separated PIECE of the secret is absent, not just the whole string: a partial leak ("<redacted> horse battery staple") passes a
    whole-string check."""
    leaked = [piece for piece in secret.split() if piece in got]
    assert not leaked, f"{leaked} of {secret!r} still show in {got!r}"


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
    _assert_hidden(got, secret)
    assert "<redacted>" in got, got


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
        ("mysqldump -h db -u root -p'correct horse' appdb > out.sql", "correct horse"),
        ('mariadb -u root -p"hunter two" appdb', "hunter two"),
        ("mysql -phunter2", "hunter2"),
        ("MYSQL -u root -phunter2 x", "hunter2"),
        ("mysql -p22 appdb", "22"),
    ],
)
def test_an_attached_p_after_a_mysql_command_is_its_password(command: str, secret: str) -> None:
    got = _scrub_text(command)
    _assert_hidden(got, secret)
    assert "-p<redacted>" in got, got


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
    _assert_hidden(got, secret)
    assert "<redacted>" in got, got
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


# --- review of the first version (PR 577): the rules leaked through shapes they claimed to cover -------------------------------------------------


def test_a_capital_p_port_flag_does_not_eat_the_anchor_of_the_password_that_follows() -> None:
    """``-P3306`` is mysql's PORT. The case-insensitive rule matched it as ``-p`` and hid the port, then the real ``-pS3cr3t`` after it was shown."""
    got = _scrub_text("mysql -h db -P3306 -u root -pS3cr3t app")
    _assert_hidden(got, "S3cr3t")
    assert "-P3306" in got, got


def test_the_command_name_is_case_insensitive_but_the_flag_is_not() -> None:
    _assert_hidden(_scrub_text("MySQL -u root -phunter2"), "hunter2")
    assert _scrub_text("mysql -P3306 -h db") == "mysql -P3306 -h db"


def test_a_quoted_user_password_with_spaces_is_hidden_to_its_last_word() -> None:
    got = _scrub_text("curl -u 'deploy:correct horse battery staple' https://x.test")
    _assert_hidden(got, "correct horse battery staple")
    assert "https://x.test" in got
    dq = _scrub_text('curl --user "deploy:correct horse battery staple" https://x.test')
    _assert_hidden(dq, "correct horse battery staple")


@pytest.mark.parametrize(
    ("command", "secret"),
    [
        ("curl -uadmin:hunter2 -T f https://x", "hunter2"),
        ("curl -u:hunter2 https://x", "hunter2"),
        ("curl -u :hunter2 https://x", "hunter2"),
        ("curl -U admin:hunter2 https://x", "hunter2"),
        ("curl -Uadmin:hunter2 https://x", "hunter2"),
        ("curl -u 4242:hunter2 https://x", "hunter2"),
        ("curl --user=deploy:hunter2 https://x", "hunter2"),
        ("curl -s -u admin:hunter2 https://x", "hunter2"),
    ],
)
def test_an_attached_or_empty_user_or_a_numeric_user_is_still_a_credential(command: str, secret: str) -> None:
    _assert_hidden(_scrub_text(command), secret)


@pytest.mark.parametrize("command", ["docker run -u 1000:1000 img", "docker run -u=1000:1000 img", "docker run --user 1000:1000 img"])
def test_only_a_digits_colon_digits_value_is_a_uid_and_gid(command: str) -> None:
    assert _scrub_text(command) == command


@pytest.mark.parametrize("command", ["mariadb-dump -u root -pS3cr3t app", "mariadb-admin -pS3cr3t status", "mysqldump -pS3cr3t app", "mysql_upgrade -pS3cr3t"])
def test_every_mysql_family_command_name_is_a_command(command: str) -> None:
    _assert_hidden(_scrub_text(command), "S3cr3t")


def test_a_long_mysqldump_line_is_covered_up_to_a_generous_window() -> None:
    options = " ".join(f"--opt{i}" for i in range(20))
    got = _scrub_text(f"mysqldump {options} -u root -pS3cr3t appdb")
    _assert_hidden(got, "S3cr3t")


@pytest.mark.parametrize("key", ["dbPass", "adminPw", "rootPwd", "sshPass", "userPswd"])
def test_a_camel_case_name_with_a_password_component_is_never_shown(key: str) -> None:
    got = _preview({key: "hunter2", "path": "/tmp/x"})
    assert "hunter2" not in got["arguments"], got
    assert "path=/tmp/x" in got["arguments"]
    _assert_hidden(_scrub_text(f"{key}=hunter2 run"), "hunter2")


@pytest.mark.parametrize("key", ["bypass", "Bypass", "compass", "passenger", "Passenger", "trespass", "keyword"])
def test_a_longer_word_with_pass_inside_is_not_a_camel_case_password(key: str) -> None:
    assert f"{key}=ok" in _preview({key: "ok"})["arguments"]
    assert _scrub_text(f"{key}=ok") == f"{key}=ok"
