"""Admin GET/PUT for the SSO JIT-provisioning settings (Task 9, Layer 2).

Mounted at ``/v1/admin/sso-settings`` and gated by
:func:`primer.api.deps.require_admin` at include-router time (see
:func:`primer.api._app_routes._mount_routers`), mirroring how
``oidc_providers_router`` (Task 3) is mounted. Reads/writes the two
``system_state`` fields added in Task 2 --
:attr:`~primer.model.system_state.SystemState.sso_jit_enabled` and
:attr:`~primer.model.system_state.SystemState.sso_default_access` --
via the storage provider's ``get_system_state`` /
``set_sso_jit_enabled`` / ``set_sso_default_access``.

Security note: ``sso_default_access`` becomes the role granted to a
brand-new local account the FIRST time a not-yet-linked SSO identity
logs in (see the JIT-provisioning path in
:mod:`primer.api.routers.sso`). Only ``"restricted"``, ``"user"``, or
``null`` are accepted on PUT -- ``"admin"`` (and any other string) is
rejected with 422 by the ``Literal`` type on :class:`SsoSettingsPutBody`.
This is the input-boundary half of a defense-in-depth pair: the JIT
path in ``sso.py`` ALSO clamps defensively (never trusting
``system_state`` blindly), but that clamp must never be the only
thing standing between a misconfigured value and an auto-provisioned
admin account.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from primer.api.deps import get_storage_provider


sso_settings_router = APIRouter(prefix="/admin/sso-settings", tags=["sso-settings"])


class SsoSettingsOut(BaseModel):
    """Response shape for both GET and PUT."""

    sso_jit_enabled: bool
    sso_default_access: str | None = None


class SsoSettingsPutBody(BaseModel):
    """Wire body for PUT.

    ``sso_default_access`` is deliberately typed as a closed
    ``Literal`` (never a bare ``str | None``) so pydantic itself
    rejects ``"admin"`` -- or any value other than the two JIT-eligible
    roles -- with a 422 before the handler runs.
    """

    sso_jit_enabled: bool
    sso_default_access: Literal["restricted", "user"] | None = None


@sso_settings_router.get(
    "",
    response_model=SsoSettingsOut,
    summary="Read the SSO JIT-provisioning settings",
)
async def get_sso_settings(
    storage_provider=Depends(get_storage_provider),
) -> SsoSettingsOut:
    state = await storage_provider.get_system_state()
    return SsoSettingsOut(
        sso_jit_enabled=state.sso_jit_enabled,
        sso_default_access=state.sso_default_access,
    )


@sso_settings_router.put(
    "",
    response_model=SsoSettingsOut,
    summary="Update the SSO JIT-provisioning settings",
)
async def put_sso_settings(
    body: SsoSettingsPutBody,
    storage_provider=Depends(get_storage_provider),
) -> SsoSettingsOut:
    await storage_provider.set_sso_jit_enabled(body.sso_jit_enabled)
    await storage_provider.set_sso_default_access(body.sso_default_access)
    state = await storage_provider.get_system_state()
    return SsoSettingsOut(
        sso_jit_enabled=state.sso_jit_enabled,
        sso_default_access=state.sso_default_access,
    )


__all__ = ["sso_settings_router", "SsoSettingsOut", "SsoSettingsPutBody"]
