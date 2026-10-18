"""Which environment variable names a workspace create accepts (lead ruling 17 of plan 08, FINAL after the review of #789).

A workspace gets the template's ``env`` with the per-workspace ``overrides.env`` on top. Two kinds of name used to fail late and
badly:

* **A name Kubernetes refuses** (``1BAD``, ``a b``, ``A=B``, an empty name, a name with a non-ASCII character, a name over
  :data:`MAX_ENV_NAME_LENGTH` characters). The refusal came back as a generic 500 after the create had been rolled back. A name
  must match :data:`ENV_NAME_PATTERN`, Kubernetes' own environment variable name pattern (ASCII letters, digits, ``_``, ``-`` and
  ``.``, not starting with a digit), and be at most :data:`MAX_ENV_NAME_LENGTH` characters. The pattern is Kubernetes' and not
  POSIX's on purpose: ``my-var`` and ``a.b`` are valid on Kubernetes, on Docker and on the local backend, and a stored template that
  has one must keep creating workspaces. The first version of this rule was POSIX-only and turned such a template into a 422 on
  every create, the boot's default workspace included.
* **A name the platform sets itself**, ``PRIMER_RUNTIME_TOKEN`` and ``RUNTIME_TOKEN`` (:data:`RUNTIME_TOKEN_ENV_NAMES`): the
  per-workspace bearer the platform dials the runtime with. The Kubernetes manifest never takes them from a template or an
  override (an explicit ``env`` entry beats the Secret the pod ``envFrom``s, so it would replace the token) and Docker overwrites
  them, so a person who set one saw nothing happen except a line in the log. Now it is refused, and the message names it.

Both are a 422 (:class:`EnvNameRefused`), raised by ``WorkspaceRegistry.materialise`` BEFORE any backend runs, so nothing is
created. A name in the request (``overrides.env``) comes with a ``loc`` (``body.overrides.env.<name>``, the name as it was sent)
like any validation error; a name stored in the template has no place in the request, so the message names the template and the
variable instead. In the message a name is shown through ``ascii()``: the message is logged and the name is the caller's own input.
Never a value: a value may be a secret.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from primer.model.except_ import ValidationError

if TYPE_CHECKING:
    from primer.model.workspace import WorkspaceTemplate, WorkspaceTemplateOverrides

#: Kubernetes' own environment variable name pattern (``envVarNameFmt`` in ``k8s.io/apimachinery/pkg/util/validation``): ASCII
#: letters, digits, ``_``, ``-`` and ``.``, and not starting with a digit.
ENV_NAME_PATTERN = r"^[-._a-zA-Z][-._a-zA-Z0-9]*$"

#: The longest name accepted, in characters.
MAX_ENV_NAME_LENGTH = 256

#: The names the platform sets itself (the per-workspace Secret on Kubernetes, the container env on Docker); see
#: ``primer.workspace.k8s.backend._RUNTIME_TOKEN_ENV_NAMES``, which is this set.
RUNTIME_TOKEN_ENV_NAMES: frozenset[str] = frozenset({"PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"})

#: How many bad names one refusal lists; the rest are counted.
MAX_LISTED = 10

_INVALID = "invalid_env_name"
_RESERVED = "reserved_env_name"
_SENTENCES = {
    _INVALID: (
        f"An environment variable name is 1 to {MAX_ENV_NAME_LENGTH} characters of ASCII letters, digits, '_', '-' and '.', "
        "and does not start with a digit. Rename or remove it."
    ),
    _RESERVED: "This name is reserved for the workspace runtime's token and cannot be set. Remove it.",
}
_SHOWN_MAX = 80


def _problem(name: str) -> str | None:
    """The error type for ``name``, or ``None`` when it is acceptable. ``fullmatch``: ``$`` alone would let a trailing newline through."""
    if name in RUNTIME_TOKEN_ENV_NAMES:
        return _RESERVED
    if len(name) > MAX_ENV_NAME_LENGTH or re.fullmatch(ENV_NAME_PATTERN, name) is None:
        return _INVALID
    return None


def _shown(name: str) -> str:
    """The name as a message shows it: through ``ascii()`` (quoted, a newline, another control character or a non-ASCII character
    escaped), so a name cannot split a log line or reach a terminal as it was sent, and cut when it is long. It is the caller's own
    input, echoed; only the head of a very long name is escaped, so the work is bounded."""
    head = ascii(name[:_SHOWN_MAX])
    return head if len(head) <= _SHOWN_MAX else head[: _SHOWN_MAX - 3] + "..."


class EnvNameRefused(ValidationError):
    """The 422 for an environment variable name a workspace create refuses.

    ``request_items`` are the names in ``overrides.env`` and ``template_items`` those in the template's ``env``, each
    ``(name, error type)``. ``message`` lists the first :data:`MAX_LISTED` (the request's first, because the person can fix those
    now) and counts the rest; ``problem_extensions`` has one ``errors`` entry per listed request name, with its ``loc``, and
    nothing for the template's.
    """

    def __init__(
        self,
        template_id: str,
        request_items: list[tuple[str, str]],
        template_items: list[tuple[str, str]],
    ) -> None:
        self.template_id = template_id
        self.request_items = list(request_items)
        self.template_items = list(template_items)
        listed_request = self.request_items[:MAX_LISTED]
        listed_template = self.template_items[: MAX_LISTED - len(listed_request)]
        self._errors = [
            {"loc": ["body", "overrides", "env", name], "msg": _SENTENCES[kind], "type": kind}
            for name, kind in listed_request
        ]
        parts = [f"overrides.env.{_shown(name)}: {_SENTENCES[kind]}" for name, kind in listed_request]
        parts += [f"template {template_id!r} sets env.{_shown(name)}: {_SENTENCES[kind]}" for name, kind in listed_template]
        hidden = len(self.request_items) + len(self.template_items) - len(listed_request) - len(listed_template)
        if hidden:
            parts.append(f"... and {hidden} more.")
        super().__init__(" ".join(parts))

    @property
    def problem_extensions(self) -> dict[str, Any]:
        return {"errors": [dict(e) for e in self._errors]} if self._errors else {}


def refuse_bad_env_names(template: "WorkspaceTemplate", overrides: "WorkspaceTemplateOverrides | None") -> None:
    """Raise :class:`EnvNameRefused` naming every variable of ``overrides.env`` and of the template's ``env`` that Kubernetes
    would refuse (:data:`ENV_NAME_PATTERN`, :data:`MAX_ENV_NAME_LENGTH`) or that is one of the platform's own. Returns normally
    when every name is acceptable."""
    request_items = [(name, kind) for name in (overrides.env if overrides is not None else {}) if (kind := _problem(name))]
    template_items = [(name, kind) for name in template.env if (kind := _problem(name))]
    if request_items or template_items:
        raise EnvNameRefused(template.id, request_items, template_items)


__all__ = [
    "ENV_NAME_PATTERN",
    "MAX_ENV_NAME_LENGTH",
    "MAX_LISTED",
    "RUNTIME_TOKEN_ENV_NAMES",
    "EnvNameRefused",
    "refuse_bad_env_names",
]
