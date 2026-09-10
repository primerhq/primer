"""Static JSX checks — 01a085a1: harness delete/fetch/sync/build/push
mutations must explain a failure, not silently swallow it.

Root cause: useMutation's onError callback opts OUT of the hook's own
default error-toast fallback (use-mutation.js only pushes a toast when
NO onError is supplied), and every one of these five mutations supplies
an onError that just calls detail.refetch() — which succeeds and shows
nothing, since the harness row is unchanged. The mutation's own .error
state was captured but never read anywhere in harnesses.jsx.

These are string-presence checks on the source, matching the existing
convention for this component (see test_harnesses_outbound_detail.py) —
no MiniRacer-based rendering test exists for harnesses.jsx to extend.
"""

from __future__ import annotations

from pathlib import Path


HARNESSES = Path(__file__).resolve().parents[2] / "ui" / "components" / "harnesses.jsx"


def _src() -> str:
    return HARNESSES.read_text(encoding="utf-8")


def test_uninstall_error_is_rendered_in_the_confirm_modal():
    src = _src()
    # The mutation's own error state must actually be read somewhere -
    # before this fix `uninstallMut.error` was captured by the hook but
    # never referenced in the component at all.
    assert "uninstallMut.error" in src
    # Rendered via the file's own established error-banner convention
    # (Banner kind="error", matching list.error / detail.error above it).
    assert 'kind="error"' in src


def test_confirm_modal_does_not_close_before_the_mutation_settles():
    src = _src()
    # The old bug: setConfirmUninstall(false) ran immediately before the
    # await, unmounting the Modal before the request could even resolve
    # - so a failure had nowhere left to render, and the loading label
    # could never be seen either. The fix keeps the modal mounted across
    # the await; only Cancel and the onClose guard close it early.
    delete_click = src.split("kind=\"danger\"\n                icon=\"trash\"")[1]
    delete_click = delete_click.split("</Btn>")[0]
    assert "await uninstallMut.mutate()" in delete_click
    assert "setConfirmUninstall(false)" not in delete_click.split(
        "await uninstallMut.mutate()"
    )[0]


def test_confirm_modal_ignores_close_while_the_delete_is_in_flight():
    src = _src()
    # onClose (Escape / backdrop click) must not dismiss the modal while
    # uninstallMut.loading is true - otherwise the operator can dismiss
    # the very error this fix exists to show.
    on_close = src.split("onClose={() => {")[1].split("}}\n          footer")[0]
    assert "uninstallMut.loading" in on_close


def test_deleting_label_can_actually_render():
    src = _src()
    assert '"Deleting…"' in src
    # The confirm button itself must be disabled while loading, which
    # only matters now that the modal can still be showing it.
    assert "disabled={uninstallMut.loading}" in src


def test_fetch_sync_build_push_errors_are_surfaced_too():
    """01a085a1 requirement 3: buildMut/pushMut (and, traced the same
    way, fetchMut/syncMut) share the identical onError-suppresses-the-
    default-toast gap uninstallMut had - all five construct useMutation
    with an onError that only refetches. One shared banner covers
    whichever of the four non-destructive mutations most recently
    failed."""
    src = _src()
    assert "actionError" in src
    assert "fetchMut.error" in src
    assert "syncMut.error" in src
    assert "buildMut.error" in src
    assert "pushMut.error" in src


def test_action_error_banner_uses_the_established_banner_convention():
    src = _src()
    banner_block = src.split("{actionError && (")[1].split(")}")[0]
    assert "<Banner" in banner_block
    assert 'kind="error"' in banner_block
    assert "actionError.title" in banner_block
    assert "actionError.detail" in banner_block
