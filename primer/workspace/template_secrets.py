"""What a full-replace PUT of a workspace template's served body gets back of the template's secrets (ticket 01a11d32).

A template serves two kinds of secret masked: the password of a ``kind=url`` file source (``https://user:**********@host/seed.txt``, :class:`~primer.model.masked_url.MaskedUserinfoUrl`) and the
values of ``env``. PUT is a full replace, so a client that reads a template and writes it back sends the masks, and the stored secrets must survive that. :func:`restore_template_secrets`
puts them back, for REST and for the ``update_workspace_template`` tool alike, by caller:

* The url file sources are matched by their PATH, not by position: a reorder keeps every password, and a mask under a path the stored template did not hold (or held as another kind of source)
  is refused.
* A NON-ADMIN caller gets a password back only when the WHOLE URL is unchanged. Any other change that still carries the mask (another path, another query) is refused: an editor of a template
  that holds no admin-only setting could otherwise re-aim the admin's credential at any path on the same origin and read the answer through a workspace.
* An ADMIN keeps the origin rule of every other served mask (:func:`primer.common.url_userinfo.restore_userinfo`): the password comes back for the same scheme, host, port and user, whatever the
  path is.
* A refusal is a :class:`~primer.model.except_.ValidationError` (a 422 on REST, ``validation-error`` from the tool) that names no secret: the literal mask must not be stored as the password, and
  the stored credential must not go to a host the caller names.
* ``env`` is restored by key, as :func:`~primer.model.common.preserve_masked_secrets` does for the rest of the rows.

The caller runs this AFTER the privilege checks (the 403 for a template that holds an admin-only setting): a caller who may not write the template never learns what a mask could be restored to.
"""

from __future__ import annotations

from pydantic import ValidationError as PydanticValidationError

from primer.common.url_userinfo import MaskCannotBeRestored, carries_mask, mask_userinfo, restore_userinfo
from primer.model.common import preserve_masked_secrets
from primer.model.except_ import ValidationError
from primer.model.workspace import FileMount, WorkspaceTemplate, _UrlSource

_WHOLE_URL = "re-enter the password: the stored one is kept for a non-admin only when the whole URL is unchanged"
_SAME_ORIGIN = "re-enter the password: the stored one is kept only for the same origin (scheme, host and port) and user"


def _stored_url_sources(stored: WorkspaceTemplate | None) -> dict[str, _UrlSource]:
    """The stored template's url file sources by path (the first one when a path repeats)."""
    found: dict[str, _UrlSource] = {}
    for mount in stored.files if stored is not None else []:
        if isinstance(mount.source, _UrlSource):
            found.setdefault(mount.path, mount.source)
    return found


def _restore_file(mount: FileMount, kept: dict[str, _UrlSource], admin: bool) -> None:
    source = mount.source
    if not isinstance(source, _UrlSource) or not carries_mask(str(source.url)):
        return
    label = f"files[{mount.path!r}].source.url"
    stored = kept.get(mount.path)
    if stored is None:
        raise ValidationError(f"{label}: {_SAME_ORIGIN} (the stored template holds no url source at this path)")
    incoming_url, stored_url = str(source.url), str(stored.url)
    if admin:
        try:
            restored = restore_userinfo(incoming_url, stored_url)
        except MaskCannotBeRestored as exc:
            raise ValidationError(f"{label}: {_SAME_ORIGIN} ({exc})") from None
    else:
        if incoming_url != mask_userinfo(stored_url):
            raise ValidationError(f"{label}: {_WHOLE_URL}")
        restored = stored_url
    if restored is not None:
        try:
            source.url = type(source.url)(restored)
        except PydanticValidationError:
            raise ValidationError(f"{label}: the URL with the stored password put back is too long; re-enter the password") from None


def restore_template_secrets(incoming: WorkspaceTemplate, stored: WorkspaceTemplate | None, *, admin: bool) -> None:
    """Put the stored secrets back into ``incoming`` (mutated in place) wherever it carries the mask a GET served; raise ``ValidationError`` for a mask that cannot be restored.

    ``admin`` is the caller's role: it picks the rule for a url source (see the module text). A password the person typed is theirs and a removed file is removed.
    """
    kept = _stored_url_sources(stored)
    for mount in incoming.files:
        _restore_file(mount, kept, admin)
    # Every url mask is restored or refused by now; what is left for the generic helper is ``env`` (by key). It also pairs ``files`` by position, harmlessly: none of them carries a mask any more.
    preserve_masked_secrets(incoming, stored)


__all__ = ["restore_template_secrets"]
