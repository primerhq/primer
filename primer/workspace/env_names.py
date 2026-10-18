"""Which environment variable names a workspace create accepts (lead ruling 17 of plan 08, from the review of #782).

A workspace gets the template's ``env`` with the per-workspace ``overrides.env`` on top. Two kinds of name used to fail late and
badly:

* **A name that is not a POSIX variable name** (``1BAD``, ``a b``, ``A=B``). Kubernetes, and ``subprocess`` on the local backend,
  refuse it, and the refusal came back as a generic 500 after the create had been rolled back. A name must match
  :data:`ENV_NAME_PATTERN`: a letter or underscore, then letters, digits and underscores.
* **A name the platform sets itself**, ``PRIMER_RUNTIME_TOKEN`` and ``RUNTIME_TOKEN`` (:data:`RUNTIME_TOKEN_ENV_NAMES`): the
  per-workspace bearer the platform dials the runtime with. The Kubernetes manifest never takes them from a template or an
  override (an explicit ``env`` entry beats the Secret the pod ``envFrom``s, so it would replace the token) and Docker overwrites
  them, so a person who set one saw nothing happen except a line in the log. Now it is refused, and the message names it.

Both are a 422 (:class:`EnvNameRefused`), raised by ``WorkspaceRegistry.materialise`` BEFORE any backend runs, so nothing is
created. A name in the request (``overrides.env``) comes with a ``loc`` (``body.overrides.env.<name>``) like any validation error;
a name stored in the template has no place in the request, so the message names the template and the variable instead. Never a
value: a value may be a secret.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from primer.model.except_ import ValidationError

if TYPE_CHECKING:
    from primer.model.workspace import WorkspaceTemplate, WorkspaceTemplateOverrides

#: A POSIX environment variable name: a letter or underscore, then letters, digits and underscores.
ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

#: The names the platform sets itself (the per-workspace Secret on Kubernetes, the container env on Docker); see
#: ``primer.workspace.k8s.backend._RUNTIME_TOKEN_ENV_NAMES``, which is this set.
RUNTIME_TOKEN_ENV_NAMES: frozenset[str] = frozenset({"PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"})

#: How many bad names one refusal lists; the rest are counted.
MAX_LISTED = 10

_INVALID = "invalid_env_name"
_RESERVED = "reserved_env_name"
_SENTENCES = {
    _INVALID: "An environment variable name may use letters, digits and underscores and must not start with a digit. Rename or remove it.",
    _RESERVED: "This name is reserved for the workspace runtime's token and cannot be set. Remove it.",
}
_SHOWN_MAX = 80


def _problem(name: str) -> str | None:
    """The error type for ``name``, or ``None`` when it is acceptable. ``fullmatch``: ``$`` alone would let a trailing newline through."""
    if name in RUNTIME_TOKEN_ENV_NAMES:
        return _RESERVED
    if re.fullmatch(ENV_NAME_PATTERN, name) is None:
        return _INVALID
    return None


def _shown(name: str) -> str:
    """The name as a message shows it: cut when it is very long (it is the caller's own input, echoed)."""
    return name if len(name) <= _SHOWN_MAX else name[: _SHOWN_MAX - 3] + "..."


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
    """Raise :class:`EnvNameRefused` naming every variable of ``overrides.env`` and of the template's ``env`` that is not a
    POSIX name or is one of the platform's own. Returns normally when every name is acceptable."""
    request_items = [(name, kind) for name in (overrides.env if overrides is not None else {}) if (kind := _problem(name))]
    template_items = [(name, kind) for name in template.env if (kind := _problem(name))]
    if request_items or template_items:
        raise EnvNameRefused(template.id, request_items, template_items)


__all__ = [
    "ENV_NAME_PATTERN",
    "MAX_LISTED",
    "RUNTIME_TOKEN_ENV_NAMES",
    "EnvNameRefused",
    "refuse_bad_env_names",
]
