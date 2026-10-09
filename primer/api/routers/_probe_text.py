"""The text a draft-probe route returns about a failure, with the credentials of the URLs in it masked (ticket 01a11cdf).

A probe's text carries what a library printed about a failed call: httpx prints the whole request URL, the ``user:password@`` of a Base URL included, and pydantic prints the
input of a field that did not validate. The text goes into a response (and, for a saved provider, onto the row), so it is cleaned where it is made. The LLM and embedding
probes (``providers.py``) and the speech and web ``_test`` routes share these.
"""

from __future__ import annotations

from pydantic import ValidationError

from primer.common.log import redact_url_secrets


def validation_detail(exc: ValidationError) -> str:
    """pydantic's own layout of a validation error WITHOUT the input.

    ``N validation error(s) for <Model>``, each field on its own line with the reason indented under it and ``[type=..., input_type=...]`` (what
    ``str(exc)`` prints with ``hide_input_in_errors``; the setup wizard parses the field and reason lines). ``str(exc)`` itself prints
    ``input_value=<the value as typed>``, which pydantic cuts to the first 25 and the last 24 characters of a long value: that removes the "@" and
    leaves a slice of a Base URL's password readable, and a raw "/", "?" or "#" in the password ends the userinfo for any URL-shaped mask. The input
    adds nothing the person needs (they typed it), so it is never printed.
    """
    errors = exc.errors(include_url=False, include_context=False)
    lines = [f"{len(errors)} validation error{'' if len(errors) == 1 else 's'} for {exc.title}"]
    for err in errors:
        lines.append(".".join(str(part) for part in err["loc"]))
        lines.append(f"  {err['msg']} [type={err['type']}, input_type={type(err.get('input')).__name__}]")
    return "\n".join(lines)


def draft_error(exc: BaseException) -> str:
    """Why a draft did not validate: the layout of a pydantic error without its input, else the exception's text, either way with URL credentials masked."""
    return redact_url_secrets(validation_detail(exc) if isinstance(exc, ValidationError) else str(exc))


def probe_error(exc: BaseException) -> str:
    """``TypeName: text`` of a probe that failed, with URL credentials masked."""
    return redact_url_secrets(f"{type(exc).__name__}: {exc}")


__all__ = ["draft_error", "probe_error", "validation_detail"]
