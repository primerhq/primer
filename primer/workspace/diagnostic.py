"""What a workspace diagnostic command may be (architecture review A-06).

``POST /v1/workspaces/{id}/diagnostic`` confirms a workspace is reachable end to end by running one short read-only command.
It used to check only the FIRST whitespace token against a small whitelist while both backends then ran the WHOLE string
through a shell (``create_subprocess_shell`` locally, ``/bin/sh -c`` in the sandbox runtime client), so ``ls ; echo
INJECTED $(id -u)`` ran arbitrary commands for any non-admin operator.

The rule now lives here, once, and is applied twice:

* the route calls :func:`parse_diagnostic_command` with the allowlist (policy: which program may run);
* every backend calls it again with no allowlist (mechanism: the string is split with ``shlex`` into an argv LIST that is
  executed WITHOUT a shell, and anything that would mean something to a shell is refused rather than passed on).

Refusing the metacharacters even though exec makes them inert is deliberate: a diagnostic that quietly runs
``echo $(id)`` as the literal text ``$(id)`` hides the mistake of whoever wrote it, and a future backend that does use a shell
stays safe.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence

# Programs the diagnostic route allows (the route owns this policy; backends do not enforce it). ``printenv`` is allowed
# for exactly ONE named variable (see ``_ENV_VARIABLE``), never bare, so it cannot dump the environment.
DIAGNOSTIC_COMMANDS: frozenset[str] = frozenset({"echo", "pwd", "whoami", "uname", "ls", "printenv"})
_ENV_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Characters a shell would act on, plus the escape character. Any argv element containing one is refused.
_SHELL_METACHARACTERS = frozenset(";&|<>`$()\\")
# A line break or NUL inside the raw command is refused before it is split (``shlex`` would treat a newline as a space).
_CONTROL_CHARACTERS = frozenset("\n\r\x00")


class DiagnosticCommandError(ValueError):
    """The command is not an acceptable diagnostic.

    ``code`` is ``command_not_whitelisted`` (the program is not on the allowlist; ``head`` carries it) or ``command_rejected``
    (empty, unparseable, or it contains shell syntax or control characters).
    """

    def __init__(self, message: str, *, code: str = "command_rejected", head: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.head = head


def parse_diagnostic_command(
    command: str | Sequence[str], *, allowed: frozenset[str] | None = None,
) -> list[str]:
    """Return the argv for ``command`` or raise :class:`DiagnosticCommandError`.

    A string is split with ``shlex`` (POSIX rules); a sequence is taken as the argv. No element may contain a shell
    metacharacter or a control character, and the raw string may not contain a line break. When ``allowed`` is given,
    ``argv[0]`` must be in it.
    """
    if isinstance(command, str):
        if any(c in command for c in _CONTROL_CHARACTERS):
            raise DiagnosticCommandError("the command contains a line break or control character")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise DiagnosticCommandError(f"the command cannot be parsed: {exc}") from exc
    else:
        argv = list(command)
        if not all(isinstance(a, str) for a in argv):
            raise DiagnosticCommandError("every argument must be a string")
    if not argv:
        raise DiagnosticCommandError("the command is empty")
    for arg in argv:
        if any(c in _CONTROL_CHARACTERS for c in arg):
            raise DiagnosticCommandError("an argument contains a line break or control character")
        bad = sorted(c for c in arg if c in _SHELL_METACHARACTERS)
        if bad:
            raise DiagnosticCommandError(
                f"the command contains shell syntax ({' '.join(repr(c) for c in bad)}); "
                "it is run as a plain program with arguments, never through a shell"
            )
    if allowed is not None and argv[0] not in allowed:
        raise DiagnosticCommandError(
            f"diagnostic command {argv[0]!r} is not on the allowlist; allowed commands are: {sorted(allowed)}",
            code="command_not_whitelisted", head=argv[0],
        )
    if allowed is not None and argv[0] == "printenv" and (len(argv) != 2 or not _ENV_VARIABLE.fullmatch(argv[1])):
        raise DiagnosticCommandError("printenv takes exactly one variable name (it never dumps the whole environment)")
    return argv


__all__ = ["DIAGNOSTIC_COMMANDS", "DiagnosticCommandError", "parse_diagnostic_command"]
