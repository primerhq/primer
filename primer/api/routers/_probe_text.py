"""The text a draft-probe route returns about a failure, with the credentials of the URLs in it masked (ticket 01a11cdf).

A probe's text carries what a library printed about a failed call: httpx prints the whole request URL, the ``user:password@`` of a Base URL included, and pydantic prints the
input of a field that did not validate. The text goes into a response (and, for a saved provider, onto the row), so it is cleaned where it is made. The LLM and embedding
probes (``providers.py``) and the speech and web ``_test`` routes share these.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError

from primer.common.log import redact_url_secrets
from primer.llm._failure import scrub


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


#: The fields of a provider config that hold a secret; ``probe_error`` masks the value of each one the probe's config has, wherever the text prints it.
_SECRET_FIELDS = ("api_key", "password")


def probe_error(exc: BaseException, config: Any = None) -> str:
    """``TypeName: text`` of a probe that failed, with URL credentials masked.

    With the probe's ``config``, the secrets it holds (``api_key``, ``password``) are masked too, wherever the text prints them and in the forms the LLM
    adapters' scrub knows (itself, escaped, whitespace-normalised): a library's text for a failed call may print a secret that is not in URL form (a driver's detail
    line, a quoted bare value). Defence in depth beside the URL mask, for a probe that is handed the stored row. A secret of fewer than 4 characters, or a keyless
    placeholder such as ``none``, is not masked (``primer.llm._failure.scrub``'s rule, so ordinary words are not blanked).
    """
    text = f"{type(exc).__name__}: {exc}"
    for name in _SECRET_FIELDS:
        secret = getattr(config, name, None)
        if secret is not None:
            text = scrub(text, SimpleNamespace(config=SimpleNamespace(api_key=secret)))
    return redact_url_secrets(text)


__all__ = ["draft_error", "probe_error", "validation_detail"]
