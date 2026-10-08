"""Platform "New channel" hosts the channel dialog on the page, and with no channel provider it offers to add one (ADM-06 and ADM-07 of the 2026-10-08 admin review).

Two findings meet on this button.

ADM-06: "New channel" on Platform > Channels opened the LEGACY Channels list overlay (a table with its own filter and "New channel") and only a second press
opened the dialog. The page now hosts ``NewChannelModal`` (the dialog the legacy list used, unchanged) the way the other entities' dialogs are hosted
(``tests/ui/test_platform_create_opens_the_form.py`` tabulates what every page's create does).

ADM-07: with no channel provider the legacy list disabled its own "New channel" and said "Create a channel provider first." as prose, with no way to follow it.
A host that opened the dialog anyway would offer a form with an empty provider select, so the host fetches the providers itself and, when there are none, shows
an explanation with an action that goes to Platform > Providers (the catalogue, on every family) instead of a dead end.

Two differences from the other hosts. The channel dialog calls ``onCreated()`` with NO row (the legacy list shows its own "Channel created" toast), so the host
toasts itself and has no row to hand to ``NV_createdRow``; and the channels overlay draws the list whatever id it is given (there is no per-channel detail), so
opening a detail after a create would show the list again: the host refreshes the cards and says so.

The decisions are pure functions that run here in MiniRacer; the host and mount are JSX, so they are source checks (this checkout has no render harness for
them), and the real journeys are ``tests/ui_e2e/test_platform_channel_create_journey.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
PLAT = (UI / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")
CHANNELS = (UI / "components" / "channels.jsx").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval("var window = globalThis;")
    ctx.eval(PLAT[PLAT.index("function NV_fact("):PLAT.index("var NV_PLAT_PAGE_SIZE")])
    ctx.eval(
        "var modals = [], overlays = [], views = [];"
        "var con = {"
        " openOverlay: function (a, b, c) { overlays.push([a, b === undefined ? null : b, c === undefined ? null : c]); },"
        " goView: function (a, b) { views.push([a, b === undefined ? null : b]); } };"
        "function setModal(m) { modals.push(m); }"
    )
    return ctx


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def test_the_channels_create_sets_a_modal_and_opens_no_overlay() -> None:
    ctx = _ctx()

    ctx.eval("NV_PLAT_PAGES.channels.create(con, setModal);")

    assert _js(ctx, "modals") == [{"kind": "channel"}]
    assert _js(ctx, "overlays") == [], "a hosted dialog must not also open the legacy list"


def test_the_channels_rules_button_still_opens_its_overlay() -> None:
    """The page's secondary button is not touched by this change."""
    ctx = _ctx()

    ctx.eval("NV_PLAT_PAGES.channels.extraNav.run(con, setModal);")

    assert _js(ctx, "overlays") == [["channels", "rules", None]]
    assert _js(ctx, "modals") == []


@pytest.mark.parametrize(
    "resource,expected",
    [
        ("{ loading: true, data: undefined }", "loading"),
        ("{ loading: true, data: null }", "loading"),
        ("{ loading: false, data: { items: [] } }", "none"),
        ("{ loading: false, data: {} }", "none"),
        ("{ loading: false, data: { items: [{ id: 'p' }] } }", "ready"),
        ("{ loading: true, data: { items: [{ id: 'p' }] } }", "ready"),  # a background refetch keeps the dialog it already has
        ("{ loading: false, error: { detail: 'boom' }, data: undefined }", "error"),
        ("{ loading: false, error: { detail: 'boom' }, data: { items: [{ id: 'p' }] } }", "ready"),  # stale data beats a failed refetch
    ],
)
def test_the_host_knows_what_to_show_from_the_provider_fetch(resource: str, expected: str) -> None:
    ctx = _ctx()

    assert ctx.eval(f"NV_channelCreateState({resource})") == expected


def test_with_no_channel_provider_the_way_out_is_the_platform_providers_page() -> None:
    """The Providers catalogue is mounted inline on the Platform page (test_console_platform.py pins that the page addresses no providers overlay), so the
    way out is a view change. It opens on every family: the address has no slot for a family on an inline page."""
    ctx = _ctx()

    ctx.eval("NV_addChannelProvider(con);")

    assert _js(ctx, "views") == [["platform", "providers"]]
    assert _js(ctx, "overlays") == [], "the page must not address a providers overlay"
    assert _js(ctx, "modals") == []


def test_the_dialog_is_reachable_from_the_platform_page() -> None:
    assert "window.NewChannelModal = NewChannelModal;" in CHANNELS


def test_the_host_fetches_the_providers_and_hosts_the_dialog_or_the_way_out() -> None:
    host = re.search(r"function NV_ChannelCreateHost[\s\S]{0,2200}", PLAT)
    assert host, "the page needs a host for the channel dialog"
    text = host.group(0)
    assert "/channel_providers?limit=200" in text, "the dialog needs the provider list, which the host has to fetch"
    assert "NV_channelCreateState(" in text
    assert "window.NewChannelModal" in text
    assert re.search(r"pushToast=\{window\.primerApi\.toastPush\}", text)
    # The dialog calls onCreated() with no row, so the host says the channel was created itself.
    assert re.search(r"Channel created", text)
    # The dead end: an action, not prose.
    assert "NV_addChannelProvider(" in text and "Add a channel provider" in text


def test_the_page_mounts_the_host_and_refreshes_the_cards_when_a_channel_is_created() -> None:
    shown = re.search(r"modal\.kind === \"channel\"[\s\S]{0,700}", PLAT)
    assert shown and "<NV_ChannelCreateHost" in shown.group(0)
    block = shown.group(0)
    assert block.count("setModal(null)") >= 2, "the dialog must close on Cancel and on a created channel"
    assert "res.refetch()" in block, "a created channel must appear in the grid behind"
    # No per-channel detail exists (the overlay draws the list for any id), so the created channel does not open an overlay.
    assert "NV_createdRow(" not in block and "openOverlay(" not in block


def test_the_add_provider_action_closes_the_dialog_before_it_navigates() -> None:
    """Otherwise the Providers overlay would open behind a dialog that still says there is no provider."""
    host = re.search(r"function NV_ChannelCreateHost[\s\S]{0,2200}", PLAT).group(0)
    action = re.search(r"onClick=\{function \(\) \{[^}]*NV_addChannelProvider\([^}]*\}\}", host)
    assert action, "the action must call NV_addChannelProvider from its click handler"
    assert action.group(0).index("props.onClose()") < action.group(0).index("NV_addChannelProvider("), action.group(0)
