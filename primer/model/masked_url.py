"""A URL field that is served without the password of its userinfo (tickets 01a11cdf part 3 and 01a11d32, and 01a1294c for the ``str`` flavour).

A URL may carry ``user:password@`` (a reverse proxy in front of a server, a private file host): httpx and aiohttp send it as Basic auth, and every API response, CRUD event and tool result used to
serve it in clear (a provider row's CRUD events too). :data:`MaskedUserinfoUrl` is an :class:`~pydantic.HttpUrl` whose JSON-mode dump masks it (``http://svc:**********@host/v1``; a lone ``https://TOKEN@host`` whole). It masks in a
JSON-mode dump only (the serializer itself returns the value unchanged for a python-mode dump, which is why it is registered ``when_used="always"`` without a return type): a python-mode dump and the
object itself keep the real URL, which is what the adapters, the probes and the fetch read; ``dump_for_storage`` keeps it for the stored row; ``preserve_masked_secrets`` puts the stored credential
back when a full-replace PUT sends the served mask back, for the same scheme, host, port and user only. Anything that FINGERPRINTS or COMPARES a row must use the storage form (the served form is the
same for two URLs that differ only by password).

:data:`MaskedUserinfoStr` applies the SAME serializer to a plain ``str`` field. Some URL fields are stored as a plain ``str`` (a URL is compared and keyed as typed, and a git URL may be a
``file://`` under the operator opt-in), so they are not an :class:`~pydantic.HttpUrl`; the :data:`MaskedGitUrl` (the harness ``git_url`` and its resolved dependencies) is this ``str`` flavour, not
a copy in :mod:`primer.model.harness` (the review of #722, nit N6).

It lives here, not in ``primer.model.providers._shared`` (which re-exports it for the provider models), because the workspace models use it too and importing ``primer.model.providers`` from
``primer.model.workspace`` would pull every provider module in.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import HttpUrl, PlainSerializer, SerializationInfo

from primer.common.url_userinfo import mask_userinfo, strip_userinfo
from primer.model.common import SERVING_NON_ADMIN, STORAGE_DUMP_CONTEXT


def _for_storage(info: SerializationInfo) -> bool:
    context = info.context
    return isinstance(context, dict) and all(context.get(key) == flag for key, flag in STORAGE_DUMP_CONTEXT.items())


def _serialize_url(value, info: SerializationInfo):
    """The dump of such a URL: unchanged in a python-mode dump (the adapters and the probes read the real URL), the real URL under the storage context (``dump_for_storage`` passes
    :data:`~primer.model.common.STORAGE_DUMP_CONTEXT`), and otherwise, in a JSON-mode dump, the URL with the password of its userinfo masked (``http://svc:**********@host/v1``; a
    lone ``https://TOKEN@host`` whole).

    No return annotation, and the serializer is registered without ``return_type``: a ``str`` return type makes pydantic 2.13 warn (PydanticSerializationUnexpectedValue, with the
    URL in the text) on every PYTHON-mode dump, and the warning goes to stderr, not through the log filter.
    """
    if not info.mode_is_json():
        return value
    if _for_storage(info):
        return str(value)
    if SERVING_NON_ADMIN.get():
        return strip_userinfo(str(value))  # a non-admin's read of a global setting: no userinfo at all
    return mask_userinfo(str(value))


def _serialize_url_stripped_for_non_admin(value, info: SerializationInfo):
    """A URL served as typed (to an admin, and stored), except that a non-admin's read of a global setting gets it with no userinfo."""
    if info.mode_is_json() and not _for_storage(info) and SERVING_NON_ADMIN.get():
        return strip_userinfo(value)
    return value


# NEVER dump a model that holds one with ``serialize_as_any=True`` or a ``SerializeAsAny`` annotation: in pydantic 2.13 that skips a field's own serializer, so the URL password would be served in
# clear (and a provider's ``api_key`` unmasked). Nothing under ``primer/`` does it; ``tests/model/test_provider_url_userinfo_is_masked.py`` fails the day something does.
MaskedUserinfoUrl = Annotated[
    HttpUrl,
    PlainSerializer(_serialize_url, when_used="always"),
]

# The same rule for a field stored as a plain ``str`` (a URL is compared and keyed as typed, and a git URL may be a ``file://`` under the operator opt-in), so it is not an :class:`~pydantic.HttpUrl`.
MaskedUserinfoStr = Annotated[
    str,
    PlainSerializer(_serialize_url, when_used="always"),
]

#: The harness ``git_url`` and its resolved dependencies' ``git_url`` are the shared ``str`` flavour (one rule, one serializer; the review of #722, nit N6).
MaskedGitUrl = MaskedUserinfoStr

#: A plain ``str`` URL an admin is served as typed (the S3 ``endpoint_url``); only a non-admin's read strips its userinfo.
NonAdminStrippedUrlStr = Annotated[
    str,
    PlainSerializer(_serialize_url_stripped_for_non_admin, when_used="always"),
]

__all__ = ["MaskedGitUrl", "MaskedUserinfoStr", "MaskedUserinfoUrl", "NonAdminStrippedUrlStr"]
