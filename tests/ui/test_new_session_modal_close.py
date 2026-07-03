"""Regression: the new-session form must close on success and gate
double-submits.

Bug it locks in: pre-fix, the form called `create.mutate(body)` without
awaiting the promise and relied on `useMutation`'s `onSuccess` callback
to invoke `onCreate`. A rapid double-click landed two POSTs before
React re-rendered with `create.loading=true`, producing two sessions
and leaving the dialog open (the parent's `onCreate` did call
`setNewSessionOpen(false)`, but the still-pending second submit kept
the modal alive in the next render).

The submit logic was unified into ui/components/new-session-form.jsx
(FD2), so these checks now target window.SharedNewSessionForm.
"""

from __future__ import annotations

from pathlib import Path

SHARED = (
    Path(__file__).resolve().parents[2]
    / "ui"
    / "components"
    / "new-session-form.jsx"
)


def _modal_body() -> str:
    src = SHARED.read_text(encoding="utf-8")
    start = src.index("function SharedNewSessionForm")
    end = src.index("window.SharedNewSessionForm =", start)
    return src[start:end]


def test_modal_awaits_mutate_directly() -> None:
    body = _modal_body()
    assert "await create.mutate" in body, (
        "onSubmit must await the mutation so onCreate fires after the "
        "POST resolves, not via useMutation.onSuccess"
    )


def test_modal_ref_gates_submission() -> None:
    body = _modal_body()
    assert "submittingRef" in body, (
        "expected a submittingRef to gate double-clicks before React "
        "re-renders with create.loading=true"
    )


def test_modal_does_not_use_onsuccess_for_close() -> None:
    body = _modal_body()
    # Allow the option object to declare invalidates etc, but onSuccess
    # must not be where the close lives — close must be in onSubmit.
    assert "onSuccess:" not in body, (
        "drop useMutation.onSuccess for closing — call onCreate() "
        "inline after the awaited mutate() returns so close is "
        "guaranteed even if a future cache-invalidate step throws"
    )
