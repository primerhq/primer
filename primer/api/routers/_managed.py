"""Shared CRUD guard: reject mutations on harness-managed rows."""

from __future__ import annotations

from fastapi import HTTPException, Request


# ---------------------------------------------------------------------------
# Generic factories (field-name agnostic)
# ---------------------------------------------------------------------------


def reject_if_body_sets_field(field_name: str):
    """Return a pre-create hook that rejects bodies which set *field_name*."""

    async def _hook(entity, request: Request) -> None:
        if getattr(entity, field_name, None) is not None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "managed_field_set",
                    "field": field_name,
                },
            )

    return _hook


def reject_if_managed_factory(field_name: str, *, for_action: str = "mutate"):
    """Return a pre-update / pre-delete hook that rejects managed rows.

    Parameters
    ----------
    field_name
        The model attribute that, when non-null, marks the row as managed.
    for_action
        Human-readable label used in the error message (e.g. "update" or
        "delete").
    """

    async def _hook(entity, request: Request) -> None:
        managed_value = getattr(entity, field_name, None)
        if managed_value is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "managed_entity",
                    "field": field_name,
                    "value": managed_value,
                    "message": (
                        f"this entity is managed via {field_name}={managed_value!r}; "
                        f"{for_action} via the managing system instead."
                    ),
                },
            )

    return _hook


def on_pre_update_reject_if_managed_factory(field_name: str):
    """Return a pre-update hook (entity, existing, request) that rejects managed rows."""
    _check = reject_if_managed_factory(field_name, for_action="update")

    async def _hook(entity, existing, request: Request) -> None:
        await _check(existing, request)

    return _hook


# ---------------------------------------------------------------------------
# Harness-specific thin wrappers (backward-compatible; existing callers unchanged)
# ---------------------------------------------------------------------------

_reject_managed_harness = reject_if_managed_factory("harness_id", for_action="update/delete")


async def reject_if_managed(entity, request: Request) -> None:
    """Pre-update / pre-delete hook for harness-managed entity guards."""
    harness_id = getattr(entity, "harness_id", None)
    if harness_id is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "managed_by_harness",
                "harness_id": harness_id,
                "message": (
                    f"this entity is managed by harness {harness_id!r}; "
                    "update the harness instead."
                ),
            },
        )


async def reject_if_body_sets_harness_id(entity, request: Request) -> None:
    """Pre-create hook: public callers may not set harness_id."""
    harness_id = getattr(entity, "harness_id", None)
    if harness_id is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "harness_id_forbidden",
                "message": "harness_id may only be set by the harness dispatch.",
            },
        )


async def on_pre_update_reject_if_managed(entity, existing, request: Request) -> None:
    """Adapter: pre-update hook that forwards to reject_if_managed on existing row."""
    await reject_if_managed(existing, request)


__all__ = [
    "on_pre_update_reject_if_managed",
    "on_pre_update_reject_if_managed_factory",
    "reject_if_body_sets_field",
    "reject_if_body_sets_harness_id",
    "reject_if_managed",
    "reject_if_managed_factory",
]
