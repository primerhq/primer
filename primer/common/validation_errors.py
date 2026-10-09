"""A pydantic validation error as it may be shown: everything but the INPUT (review of #645).

pydantic puts the offending input in every error it lists. For the arguments of an API call or a tool call that input is what the caller typed: a Base URL with its password,
a whole config dict with the key beside the missing token. It adds nothing a person needs (the ``loc`` says which field, the ``msg`` says why), and the text goes into responses,
transcripts and logs. The REST 422 handler and the tools' "argument validation failed" text both render errors through :func:`without_input`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def without_input(errors: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``errors`` (``ValidationError.errors()``, ``RequestValidationError.errors()``) with the ``input`` of each dropped; ``type``, ``loc``, ``msg``, ``ctx`` and ``url`` stay.
    The argument is not modified."""
    return [{key: value for key, value in error.items() if key != "input"} for error in errors]


__all__ = ["without_input"]
