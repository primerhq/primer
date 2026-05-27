"""Shared CRUD guard: reject mutations on harness-managed rows."""

from __future__ import annotations

from fastapi import HTTPException, Request


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
    "reject_if_body_sets_harness_id",
    "reject_if_managed",
]
