"""Reading and writing the canvas settings row (pivot P5, Task 5).

One row, id ``CANVAS_SETTINGS_ID``. A canvas that has no operator row uses the defaults, and reading never stores them: the row
exists only once an operator has saved a setting. A stored row that no longer decodes raises (``RowDecodeError``) instead of
quietly falling back to the defaults, so a drifted row is seen and not silently ignored.
"""

from __future__ import annotations

from typing import Any

from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings
from primer.model.except_ import ConflictError, NotFoundError


async def read_canvas_settings(storage_provider: Any) -> CanvasSettings:
    """The stored settings, else the defaults."""
    row = await storage_provider.get_storage(CanvasSettings).get(CANVAS_SETTINGS_ID)
    return row if row is not None else CanvasSettings(id=CANVAS_SETTINGS_ID)


async def write_canvas_settings(storage_provider: Any, settings: CanvasSettings) -> CanvasSettings:
    """Store ``settings`` as THE settings row (whatever id it carries): create it, or replace the row that is there.

    A replace is the whole row, not a merge: the settings are one form an operator saves. Two first saves that race both end in a
    stored row (the loser's create finds the winner's and replaces it).
    """
    store = storage_provider.get_storage(CanvasSettings)
    row = settings.model_copy(update={"id": CANVAS_SETTINGS_ID})
    for _ in range(3):
        try:
            return await store.create(row)
        except ConflictError:
            pass
        try:
            return await store.update(row)
        except NotFoundError:
            continue                    # deleted between the two calls: create it again
    raise ConflictError("the canvas settings changed while they were being saved; try again")


__all__ = ["read_canvas_settings", "write_canvas_settings"]
