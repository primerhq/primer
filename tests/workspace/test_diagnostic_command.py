"""The one rule for what a workspace diagnostic command may be (architecture review A-06).

``parse_diagnostic_command`` turns a string (or an argv sequence) into an argv LIST that is executed without a shell, and
refuses anything a shell would act on. The route passes the allowlist; the backends pass none.
"""

from __future__ import annotations

import pytest

from primer.workspace.diagnostic import DIAGNOSTIC_COMMANDS, DiagnosticCommandError, parse_diagnostic_command


def test_a_plain_command_becomes_an_argv_list() -> None:
    assert parse_diagnostic_command("echo hello world") == ["echo", "hello", "world"]


def test_quotes_group_words_and_are_removed_like_a_shell_would() -> None:
    assert parse_diagnostic_command("echo 'two words' \"and more\"") == ["echo", "two words", "and more"]


def test_a_sequence_is_taken_as_the_argv() -> None:
    assert parse_diagnostic_command(["ls", "-la", "sub dir"]) == ["ls", "-la", "sub dir"]


@pytest.mark.parametrize("char", list(";&|<>`$()\\"))
def test_every_shell_metacharacter_is_refused_in_any_argument(char: str) -> None:
    with pytest.raises(DiagnosticCommandError) as refused:
        parse_diagnostic_command(["echo", f"a{char}b"])

    assert refused.value.code == "command_rejected" and isinstance(refused.value, ValueError)


@pytest.mark.parametrize("command", ["ls ; id", "ls&&id", "ls | cat", "echo $(id)", "echo `id`", "echo $HOME", "ls > out"])
def test_shell_syntax_in_a_string_is_refused(command: str) -> None:
    with pytest.raises(DiagnosticCommandError):
        parse_diagnostic_command(command)


@pytest.mark.parametrize("command", ["ls\nid", "ls\r\nid", "ls\x00id", "echo hi\n"])
def test_a_line_break_or_nul_in_the_raw_string_is_refused(command: str) -> None:
    """``shlex`` treats a newline as a space, which would let a second 'command' ride along as arguments."""
    with pytest.raises(DiagnosticCommandError):
        parse_diagnostic_command(command)


def test_an_unterminated_quote_is_refused_not_guessed() -> None:
    with pytest.raises(DiagnosticCommandError):
        parse_diagnostic_command("echo 'oops")


@pytest.mark.parametrize("command", ["", "   ", []])
def test_an_empty_command_is_refused(command) -> None:
    with pytest.raises(DiagnosticCommandError):
        parse_diagnostic_command(command)


def test_non_string_arguments_are_refused() -> None:
    with pytest.raises(DiagnosticCommandError):
        parse_diagnostic_command(["echo", 1])  # type: ignore[list-item]


def test_the_allowlist_checks_the_program_and_names_it() -> None:
    with pytest.raises(DiagnosticCommandError) as refused:
        parse_diagnostic_command("rm -rf /", allowed=DIAGNOSTIC_COMMANDS)

    assert (refused.value.code, refused.value.head) == ("command_not_whitelisted", "rm")


def test_no_allowlist_means_any_program_but_still_no_shell_syntax() -> None:
    assert parse_diagnostic_command("sleep 5") == ["sleep", "5"]
    with pytest.raises(DiagnosticCommandError):
        parse_diagnostic_command("sleep 5; id")


def test_shell_syntax_is_refused_before_the_allowlist_is_consulted() -> None:
    """``ls;`` is not the program ``ls``: the metacharacter is the more useful thing to report."""
    with pytest.raises(DiagnosticCommandError) as refused:
        parse_diagnostic_command("ls; id", allowed=DIAGNOSTIC_COMMANDS)

    assert refused.value.code == "command_rejected"


def test_the_allowlist_is_the_read_only_programs() -> None:
    assert DIAGNOSTIC_COMMANDS == frozenset({"echo", "pwd", "whoami", "uname", "ls", "printenv"})


def test_printenv_reads_one_named_variable() -> None:
    assert parse_diagnostic_command("printenv PRIMER_SMK_VAR", allowed=DIAGNOSTIC_COMMANDS) == ["printenv", "PRIMER_SMK_VAR"]


@pytest.mark.parametrize("command", ["printenv", "printenv A B", "printenv -0", "printenv 1BAD", "printenv A-B", "printenv A=b"])
def test_printenv_never_dumps_the_environment_or_takes_options(command: str) -> None:
    with pytest.raises(DiagnosticCommandError):
        parse_diagnostic_command(command, allowed=DIAGNOSTIC_COMMANDS)


def test_the_printenv_rule_is_route_policy_the_backends_do_not_apply() -> None:
    assert parse_diagnostic_command("printenv", allowed=None) == ["printenv"]
