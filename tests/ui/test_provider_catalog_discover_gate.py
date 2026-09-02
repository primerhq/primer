"""CI fix: PC_InstanceCard's discovered_models fetch was firing on every
card, every page load, regardless of provider class or reachability -
`discoverable ? key : null` looks like a skip-the-fetch gate but
useResource has no null-key short-circuit (getOrCreate/runFetch in
ui/foundation/use-resource.js never check the key), so the fetcher ran
unconditionally. That 404d for embedding/cross_encoder (GET .../
discovered_models is an llm_providers-only route, providers.py:617 -
no per-id GET exists for the other classes) and 400d for unreachable
llm rows, breaking test_console_loads' zero-console-errors check.

Static-source checks only (the tests/ui suite convention).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "provider-catalog.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _card_src() -> str:
    src = _src()
    start = src.index("function PC_InstanceCard(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


def test_the_fetcher_itself_gates_the_real_network_call() -> None:
    """The bug wasn't the condition's intent, it was relying on
    useResource's key to skip a call it never skips. The fetcher
    function must branch internally instead."""
    card = _card_src()
    assert "useResource" in card
    fetcher_start = card.index("(signal) => canProbe")
    fetcher_end = card.index(",", card.index("Promise.resolve(null)"))
    fetcher_src = card[fetcher_start:fetcher_end]
    assert "canProbe" in fetcher_src
    assert "? apiFetch(" in fetcher_src
    assert "Promise.resolve(null)" in fetcher_src


def test_cache_key_no_longer_collapses_to_a_shared_null_key() -> None:
    """Every row gets its own real cache key regardless of canProbe -
    the old `discoverable ? key : null` pattern meant every
    non-discoverable row shared one literal "null" cache entry."""
    card = _card_src()
    assert "`pc:discovered:${row.id}`" in card
    assert ": null,\n" not in card.split("useResource(")[1][:120]


def test_only_llm_providers_class_can_probe() -> None:
    """GET /{plural}/{id}/discovered_models is llm_providers-only -
    embedding/cross_encoder/etc. have no such route (404)."""
    card = _card_src()
    assert 'klass.plural === "llm_providers"' in card


def test_only_a_confirmed_reachable_row_can_probe() -> None:
    """No auto-probing on page load: a virgin (never-probed) or
    unreachable row must not fire the request either - only a row with
    a prior successful probe (last_probe_ok===true) does. This is the
    same virgin-is-not-reachable rule PC_ReachabilityBadge uses."""
    card = _card_src()
    assert "row.last_probe_ok === true" in card


def test_model_count_is_also_gated_not_just_the_fetch() -> None:
    card = _card_src()
    assert "const modelCount = canProbe &&" in card


def test_discoverable_prop_is_fully_removed_not_left_dangling() -> None:
    """The prop was dead code before this fix too: ProviderCatalog never
    passed it down to PC_InstanceGrid in the first place, so it was
    always undefined at PC_InstanceCard regardless. Confirm it's gone
    everywhere rather than left as a red herring."""
    src = _src()
    # 01a063ab added profileCount/filterQuery/onCountChange for the
    # designer reconciliation (default-for-N-profiles fact, card filter) -
    # additive real props, not a reintroduction of the dead `discoverable`
    # prop this test guards against.
    assert "PC_InstanceCard({ klass, row, onOpen, onChanged, profileCount })" in src
    assert "PC_InstanceGrid({ klass, onSelect, onRegisterRefetch, filterQuery, onCountChange })" in src
    assert "discoverable={discoverable}" not in src


def test_bundle_transpiles_with_the_discover_gate_fix() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/provider-catalog.jsx === */" in text
