"""Bug reporter — write-only POST endpoint that writes to the configured bugs directory.

Operator views bugs via the filesystem; there is no GET surface on
purpose. Each report lands in ``<bugs_dir>/bug-<iso-ts>-<uuid8>/`` as
three files:

- ``description.md`` — plain text from the textarea
- ``screenshot.png`` — PNG decoded from base64 (optional)
- ``meta.json``     — ``{id, created_at, status, page_url, viewport, captured_at}``

The bugs directory defaults to ``<project_root>/bugs/`` (resolved by
walking up from this file to the nearest ``pyproject.toml``); operators
can override via ``config.bugs.directory`` when an explicit AppConfig
field exists.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)
bugs_router = APIRouter(prefix="/bugs", tags=["bugs"])


class ViewportBody(BaseModel):
    width: int = Field(..., ge=1, le=100000)
    height: int = Field(..., ge=1, le=100000)


class BugReportBody(BaseModel):
    description: str = Field(..., min_length=1, max_length=20000)
    # ~20MB before base64 → ~15MB image once decoded. Generous; the
    # operator console captures a single viewport, not the whole page.
    screenshot_b64: str | None = Field(default=None, max_length=20_000_000)
    page_url: str = Field(default="", max_length=2000)
    viewport: ViewportBody | None = None
    captured_at: str | None = None  # ISO8601 from the browser


@bugs_router.post("", status_code=201)
async def create_bug(request: Request, body: BugReportBody) -> dict:
    cfg = getattr(request.app.state, "config", None)
    bugs_dir = _resolve_bugs_dir(cfg)
    bugs_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H%M%SZ")
    short_id = uuid.uuid4().hex[:8]
    bug_id = f"bug-{ts}-{short_id}"
    bug_dir = bugs_dir / bug_id
    # exist_ok=False is intentional — a same-second + same-uuid8 collision
    # would mean a deeper bug in uuid4, but if it ever happens we want a
    # loud failure rather than overwriting an existing report.
    bug_dir.mkdir(parents=True, exist_ok=False)

    (bug_dir / "description.md").write_text(body.description, encoding="utf-8")

    if body.screenshot_b64:
        try:
            img_bytes = base64.b64decode(_strip_data_url(body.screenshot_b64))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                422,
                detail={"code": "screenshot_invalid", "message": str(exc)},
            )
        (bug_dir / "screenshot.png").write_bytes(img_bytes)

    meta = {
        "id": bug_id,
        "created_at": now.isoformat(),
        "status": "open",
        "page_url": body.page_url,
        "viewport": body.viewport.model_dump() if body.viewport else None,
        "captured_at": body.captured_at,
    }
    (bug_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    logger.info(
        "bug_reported",
        extra={"bug_id": bug_id, "dir": str(bug_dir)},
    )
    return {"id": bug_id, "path": str(bug_dir)}


def _strip_data_url(s: str) -> str:
    """Strip a ``data:image/png;base64,`` prefix if present."""
    m = re.match(r"^data:[^;]+;base64,(.*)$", s, re.DOTALL)
    return m.group(1) if m else s


def _resolve_bugs_dir(cfg) -> Path:
    """Default: ``<project_root>/bugs/``. Override via ``cfg.bugs.directory``."""
    bugs_setting = None
    try:
        bugs_section = getattr(cfg, "bugs", None)
        if bugs_section is not None:
            bugs_setting = getattr(bugs_section, "directory", None)
    except AttributeError:
        bugs_setting = None
    if bugs_setting:
        return Path(bugs_setting).expanduser().resolve()
    return _find_project_root() / "bugs"


def _find_project_root() -> Path:
    """Walk up from this file until ``pyproject.toml`` is found."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


__all__ = ["bugs_router"]
