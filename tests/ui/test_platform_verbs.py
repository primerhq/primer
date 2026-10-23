"""The Platform sidebar's palette verbs (Platform revamp Plan 01, Task 9; the command-palette track of the Definition of Done; seam I-2's verb rule).

Every Platform page and every provider type is one palette verb on the ``platform-nav`` surface ("Open Providers", "Open Providers: TTS", "Open LLM Profiles" ...), and the sidebar
row that opens the same view carries the verb's id as its ``data-verb``, so the dual-render rule (a verb with no pointer affordance is palette-only) holds for all of them.

The real ``shell-verbs.js`` (the registry and its label lint), ``shell-url.js`` (the grammar the verbs open), the platform kit and ``nv-platform.jsx`` run here in one mini React
context (the page harness); the shell's own registration loop is cut out of ``nv-shell.jsx`` and run against the real registry, so a label the registry refuses, a surface it does
not know or a ``goView`` call with the wrong arguments fails here and not as an exception inside the shell's effect (which would take the console down).

Custom pages (``web``, ``templates``) have a row and a ``data-verb`` but NO verb in ``NV_platVerbSpecs``: a custom page registers its own, right after the loop, in the plan that ships
the page (Plans 04 and 08). ``tests/ui/test_shell_dual_render_guard.py`` holds the other half: the nav-row verbs count as rendered there.

The palette ranks a verb's whole label first (lead ruling): the shell's own core verb effect runs against the real registry and the real ``SH_rankVerbs``, so the 12 core verbs and the 22
Platform verbs are the population, and every label (exact and in lower case), the four that used to lose to an earlier verb, a prefix and the empty query are pinned.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._platform_page_harness import platform_page_context

ROOT = Path(__file__).resolve().parents[2]
SHELL_URL = (ROOT / "ui" / "foundation" / "shell-url.js").read_text(encoding="utf-8")
SHELL_VERBS = (ROOT / "ui" / "foundation" / "shell-verbs.js").read_text(encoding="utf-8")
SHELL = (ROOT / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")
PLATFORM = (ROOT / "ui" / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")

TYPES = ["llm", "embedding", "ssp", "cross_encoder", "workspace", "web_search", "channel", "artifact_storage", "tts", "stt"]
NON_CUSTOM = ("profiles", "agents", "graphs", "toolsets", "tools", "collections", "triggers", "channels", "harnesses", "services", "approvals")
IDS = ["platform.open.providers"] + ["platform.open.providers." + t for t in TYPES] + ["platform.open." + nav for nav in NON_CUSTOM]
LABELS = [
    "Open Providers",
    "Open Providers: LLM", "Open Providers: Embedding", "Open Providers: Semantic Search Storage", "Open Providers: Cross Encoding",
    "Open Providers: Workspace", "Open Providers: Web Search", "Open Providers: Channels", "Open Providers: Artifact Store",
    "Open Providers: TTS", "Open Providers: ASR",
    "Open LLM Profiles", "Open Agents", "Open Graphs", "Open Toolsets", "Open Tools", "Open Collections", "Open Triggers", "Open Channels",
    "Open Harnesses", "Open Services", "Open Approval Policies",
]
CORE = (
    "view.studio", "view.platform", "view.system", "view.profile", "theme.light", "theme.dark", "theme.system", "workspace.switch", "session.create", "workspace.create",
    "palette.open", "terminal.toggle",
)
STAND_INS = "window.WEB_SettingsPage = function () { return null; }; window.WT_TemplatesPage = function () { return null; };"


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def ctx():
    c = platform_page_context(prelude_extra=SHELL_URL + SHELL_VERBS)
    try:
        yield c
    finally:
        c.close()


def _call_source(src: str, opener: str) -> str:
    """The statement that starts with ``opener`` (``...forEach(``), through its closing parenthesis and semicolon, parentheses balanced."""
    start = src.index(opener)
    depth = 0
    for i in range(start + len(opener) - 1, len(src)):
        depth += src[i] == "("
        depth -= src[i] == ")"
        if depth == 0:
            return src[start:i + 2]   # through the ";"
    raise AssertionError("unbalanced call after " + opener)


# ---- the specs ------------------------------------------------------------------------------------------------------------------------------


def test_every_non_custom_nav_and_every_provider_type_has_one_verb_in_sidebar_order(ctx) -> None:
    assert [spec["id"] for spec in js(ctx, "NV_platVerbSpecs()")] == IDS


def test_each_spec_names_the_view_it_opens(ctx) -> None:
    for spec in js(ctx, "NV_platVerbSpecs()"):
        if spec["id"].startswith("platform.open.providers."):
            assert (spec["nav"], spec["sub"]) == ("providers", spec["id"].rsplit(".", 1)[1]), spec
        else:
            assert (spec["nav"], spec["sub"]) == (spec["id"].rsplit(".", 1)[1], None), spec


def test_the_labels(ctx) -> None:
    assert [spec["label"] for spec in js(ctx, "NV_platVerbSpecs()")] == LABELS
    labels = {spec["id"]: spec["label"] for spec in js(ctx, "NV_platVerbSpecs()")}
    assert labels["platform.open.providers"] == "Open Providers"
    assert labels["platform.open.profiles"] == "Open LLM Profiles"
    assert labels["platform.open.channels"] == "Open Channels"
    assert labels["platform.open.approvals"] == "Open Approval Policies", "the page's title is 'Approval policies'; a verb label is Title Case"
    assert labels["platform.open.providers.ssp"] == "Open Providers: Semantic Search Storage"


def test_a_custom_page_has_no_spec_whether_or_not_its_component_is_loaded(ctx) -> None:
    """The custom-page plans register their own verb (Plan 04: Open Web; Plan 08: Open Workspace Templates), right after the shell's loop."""
    before = js(ctx, "NV_platVerbSpecs()")
    ctx.eval(STAND_INS)
    assert js(ctx, "NV_platVerbSpecs()") == before
    assert not {"platform.open.web", "platform.open.templates"} & {spec["id"] for spec in before}


def test_a_label_is_title_case_whatever_the_titles_and_type_labels_say(ctx) -> None:
    """A verb label the registry refuses throws inside the shell's effect, so a title with a lower-case word, or a type with no label, must still register."""
    ctx.eval(
        'NV_PLAT_PAGES.agents.title = "my agents"; delete NV_PROVIDER_TYPE_LABELS.tts;'
        'window.SH_VIEW_SUBS["platform:providers"].push("web_fetch");'
    )
    labels = {spec["id"]: spec["label"] for spec in js(ctx, "NV_platVerbSpecs()")}
    assert labels["platform.open.agents"] == "Open My Agents"
    assert labels["platform.open.providers.tts"] == "Open Providers: Tts", "a type with no label is named by its key"
    assert labels["platform.open.providers.web_fetch"] == "Open Providers: Web_fetch"
    assert js(ctx, "NV_platVerbSpecs().every(function (s) { return SH_lintVerbLabel(s.label) === null; })") is True


def test_the_specs_are_on_window(ctx) -> None:
    assert ctx.eval("typeof window.NV_platVerbSpecs === 'function'")


# ---- the real registry ----------------------------------------------------------------------------------------------------------------------


def test_every_spec_registers_in_the_real_registry(ctx) -> None:
    """The registry lints the label (verb-noun, Title Case, a known opening verb), insists on a known surface with a pointer, and refuses a duplicate id."""
    out = js(
        ctx,
        """(function () {
          var registry = SH_createVerbRegistry();
          var errors = [];
          NV_platVerbSpecs().forEach(function (s) {
            try {
              registry.register({ id: s.id, label: s.label, surfaces: ["palette", "platform-nav"], run: function () {} });
            } catch (e) { errors.push(s.id + ": " + String(e.message || e)); }
          });
          return { errors: errors, all: registry.ids(), nav: registry.forSurface("platform-nav").length };
        })()""",
    )
    assert out["errors"] == []
    assert out["all"] == IDS and out["nav"] == len(IDS)


def test_the_ids_are_unique_and_clear_of_the_core_verbs(ctx) -> None:
    ids = [spec["id"] for spec in js(ctx, "NV_platVerbSpecs()")]
    assert len(ids) == len(set(ids))
    core = set(re.findall(r'id:\s*"([\w.]+)"', SHELL))
    assert not core & set(ids), "the shell's literal verbs and the loop's must not share an id (the second would be silently skipped by reg())"


def test_every_verb_opens_an_address_the_grammar_keeps(ctx) -> None:
    """A nav or type the grammar does not list is dropped when the address is written, so the verb would land somewhere else than its label says."""
    for spec in js(ctx, "NV_platVerbSpecs()"):
        view = {"name": "platform", "nav": spec["nav"], "sub": spec["sub"]}
        url = ctx.eval("SH_buildUrl({ wid: null, view: " + json.dumps(view) + " })")
        assert url == "#/?view=platform:" + spec["nav"] + (":" + spec["sub"] if spec["sub"] else ""), spec
        assert js(ctx, "SH_parseUrl(" + json.dumps(url) + ").view") == view, spec


# ---- the shell's loop -----------------------------------------------------------------------------------------------------------------------


def test_the_shell_loop_runs_go_view_with_nav_and_type(ctx) -> None:
    """The loop, exactly as ``nv-shell.jsx`` writes it, against the shell's own ``reg`` and the real registry, with ``goView`` recorded."""
    loop = _call_source(SHELL, "window.NV_platVerbSpecs().forEach(")
    ctx.eval(
        "var registry = SH_createVerbRegistry(); var VIEWS = [];"
        "function reg(v) { if (!registry.get(v.id)) registry.register(v); }"
        "function goView(name, nav, sub) { VIEWS.push([name, nav, sub]); }" + loop
    )
    assert js(ctx, "registry.ids()") == IDS
    ctx.eval('registry.get("platform.open.providers.tts").run()')
    assert js(ctx, "VIEWS") == [["platform", "providers", "tts"]]
    ctx.eval('registry.get("platform.open.providers").run(); registry.get("platform.open.profiles").run(); registry.get("platform.open.approvals").run();')
    assert js(ctx, "VIEWS")[1:] == [["platform", "providers", None], ["platform", "profiles", None], ["platform", "approvals", None]], (
        "a nav opens with no type, and the Providers verb is All"
    )


def test_the_loop_registers_on_the_palette_and_the_platform_nav_surface(ctx) -> None:
    loop = _call_source(SHELL, "window.NV_platVerbSpecs().forEach(")
    ctx.eval("var registry = SH_createVerbRegistry(); function reg(v) { registry.register(v); } function goView() {}" + loop)
    assert js(ctx, "registry.all().every(function (v) { return v.surfaces.join() === 'palette,platform-nav'; })") is True
    assert js(ctx, 'registry.forSurface("palette").length') == len(IDS)


def test_the_loop_is_inside_the_core_verb_effect_and_after_the_core_verbs() -> None:
    """The palette lists the first eight verbs of an empty query in registration order, so the 22 Platform verbs follow the core ones (Studio, Platform,
    System, Profile, the themes, Switch Workspace ...) and never push them off the list."""
    effect = SHELL.index("// --- Core verbs (chrome-level; view bodies add their own)")
    loop = SHELL.index("window.NV_platVerbSpecs().forEach(")
    assert effect < loop
    for verb in ("view.studio", "view.platform", "view.system", "view.profile", "theme.light", "workspace.switch", "session.create", "palette.open", "terminal.toggle"):
        assert SHELL.index(f'id: "{verb}"') < loop, verb
    end = SHELL.index("}, [registry]);", loop)
    assert "}, [registry]);" not in SHELL[effect:loop] and "React.useEffect(" not in SHELL[loop:end], (
        "the loop is in the effect that registers once, the one whose dependency list is [registry]"
    )


def _run_shell_effect(ctx) -> None:
    """The shell's core verb effect, as ``nv-shell.jsx`` writes it (``reg``, every core ``reg({...})`` call, then the Platform loop), run against the real registry.
    Only ``goView`` is a stand-in, and no ranking calls it; ``console.warn`` is recorded into ``WARNS``."""
    start = SHELL.index("// --- Core verbs (chrome-level; view bodies add their own)")
    body = SHELL.index("React.useEffect(function () {", start) + len("React.useEffect(function () {")
    effect = SHELL[body:SHELL.index("}, [registry]);", body)]
    ctx.eval(
        "var registry = SH_createVerbRegistry(); var WARNS = []; console.warn = function (m) { WARNS.push(String(m)); };"
        "(function (registry, goView) {" + effect + "\n})(registry, function () {});"
    )


@pytest.fixture
def palette(ctx):
    """The palette's verbs as the shell registers them: the core verbs first, then the Platform verbs of the loop, in the real registry; ``SH_rankVerbs`` is the real ranker."""
    _run_shell_effect(ctx)
    return ctx


def test_a_title_the_registry_refuses_costs_its_own_verb_and_nothing_else(ctx) -> None:
    """``NV_platTitleCase`` fixes a lower-case word, but a title word that starts with a digit or a symbol is still refused by the label lint, and the registry throws.
    Inside the shell's effect that throw takes the console down, so the loop skips that one verb and says so on the console: the page keeps its row, every other verb registers."""
    ctx.eval('NV_PLAT_PAGES.agents.title = "2FA agents";')
    assert js(ctx, 'SH_lintVerbLabel("Open 2FA Agents")') is not None, "the precondition: this label is one the registry refuses"
    _run_shell_effect(ctx)
    ids = js(ctx, "registry.ids()")
    assert [i for i in ids if i.startswith("platform.open.")] == [i for i in IDS if i != "platform.open.agents"]
    assert set(CORE) <= set(ids), "the core verbs registered before the loop are untouched"
    warns = js(ctx, "WARNS")
    assert len(warns) == 1 and "platform.open.agents" in warns[0] and "Open 2FA Agents" in warns[0], warns


def test_a_title_the_registry_accepts_registers_without_a_warning(palette) -> None:
    assert js(palette, "WARNS") == []


# ---- the palette ranks a verb's full label first ---------------------------------------------------------------------------------------------


def _ranked(ctx, query: str) -> list[str]:
    return js(ctx, "SH_rankVerbs(registry, " + json.dumps(query) + ", {}).map(function (v) { return v.id; })")


def test_the_palette_is_the_core_verbs_then_the_platform_verbs(palette) -> None:
    ids = js(palette, "registry.ids()")
    assert ids[len(ids) - len(IDS):] == IDS
    assert set(CORE) <= set(ids[:len(ids) - len(IDS)])


def test_a_verbs_full_label_ranks_that_verb_first_exact_and_in_lower_case(palette) -> None:
    """Without the match tier, 8 of the 68 queries (each of the 34 labels, exact and lower-case) put another verb first: a label that is the whole query ties on the
    fuzzy score with a longer or earlier one that matches it with the same longest contiguous run, and registration order broke the tie."""
    verbs = js(palette, "registry.all().map(function (v) { return [v.id, v.canonical]; })")
    assert len(verbs) >= len(CORE) + len(IDS)
    wrong = []
    for verb_id, label in verbs:
        for query in (label, label.lower()):
            first = _ranked(palette, query)[0]
            if first != verb_id:
                wrong.append(f"{query!r} puts {first} first, not {verb_id}")
    assert wrong == []


@pytest.mark.parametrize(
    ("label", "first", "used_to_win"),
    [
        ("Open Providers: TTS", "platform.open.providers.tts", "platform.open.providers.artifact_storage"),
        ("Open Providers: ASR", "platform.open.providers.stt", "platform.open.providers.ssp"),
        ("Open Tools", "platform.open.tools", "platform.open.toolsets"),
        ("Open Channels", "platform.open.channels", "platform.open.providers.channel"),
    ],
)
def test_the_labels_that_lost_to_an_earlier_verb_rank_first_and_the_old_winner_is_still_offered(palette, label, first, used_to_win) -> None:
    for query in (label, label.lower()):
        ranked = _ranked(palette, query)
        assert ranked[0] == first, query
        assert used_to_win in ranked[1:], f"{query!r}: the tier orders the matches, it does not filter them"


def test_a_prefix_of_a_label_ranks_ahead_of_a_verb_that_only_matches_fuzzily(palette) -> None:
    """Cross Encoding and Channels start with the query. Semantic Search Storage, Web Search, Workspace and Artifact Store only have a c after "Open Providers: "
    (a fuzzy match, and Semantic Search Storage is registered before Cross Encoding), so they follow."""
    query = "Open Providers: C"
    ranked = _ranked(palette, query)
    labels = dict(js(palette, "registry.all().map(function (v) { return [v.id, v.canonical.toLowerCase()]; })"))
    prefix = [i for i in ranked if labels[i].startswith(query.lower())]
    assert prefix == ["platform.open.providers.cross_encoder", "platform.open.providers.channel"], "the verbs whose label starts with the query"
    assert ranked[:len(prefix)] == prefix, ranked
    fuzzy_only = {"platform.open.providers.ssp", "platform.open.providers.workspace", "platform.open.providers.web_search", "platform.open.providers.artifact_storage"}
    assert fuzzy_only <= set(ranked[len(prefix):]), "the others still match, after them"


def test_the_whole_label_ranks_ahead_of_a_prefix_of_it(palette) -> None:
    ranked = _ranked(palette, "Open Tools")
    assert ranked[:2] == ["platform.open.tools", "platform.open.toolsets"], "Open Toolsets starts with the query too, and comes second"


def test_an_empty_query_has_no_tier_and_keeps_registration_order(palette) -> None:
    """The palette lists the first eight verbs of an empty query, and the loop-placement comment in ``nv-shell.jsx`` relies on those being the core verbs in the order they register."""
    ids = js(palette, "registry.ids()")
    ranked = _ranked(palette, "")
    assert ranked == ids
    assert not set(IDS) & set(ranked[:8]), "the Platform verbs are registered after the core verbs, so they do not push them off the list"


# ---- the pointer affordance: the sidebar rows ----------------------------------------------------------------------------------------------


def test_every_nav_row_and_sub_entry_carries_the_data_verb_of_its_view(ctx) -> None:
    """A row carries the verb of the view it opens, custom pages included (their verb is registered by their own plan, but the row is the affordance)."""
    ctx.eval(STAND_INS + " MR.mount(NV_PlatNav, {});")
    navs = js(ctx, "SH_VIEWS.platform")
    rows = js(ctx, 'MR.findAll("nv-plat-row:").map(function (e) { return [e.props["data-testid"].slice("nv-plat-row:".length), e.props["data-verb"]]; })')
    assert rows == [[nav, "platform.open." + nav] for nav in navs], "one row per nav of the grammar, each with its verb, in order"
    subs = js(ctx, 'MR.findAll("nv-plat-sub:").map(function (e) { return [e.props["data-testid"].slice("nv-plat-sub:".length), e.props["data-verb"]]; })')
    assert subs == [[t, "platform.open.providers." + t] for t in TYPES] + [["register", "provider.register"]]


def test_the_verbs_of_the_specs_are_all_rendered_by_a_row(ctx) -> None:
    ctx.eval("MR.mount(NV_PlatNav, {});")
    drawn = set(js(ctx, 'MR.findAll("nv-plat-").map(function (e) { return e.props["data-verb"]; }).filter(Boolean)'))
    assert {spec["id"] for spec in js(ctx, "NV_platVerbSpecs()")} <= drawn


def test_the_row_attributes_are_the_literals_the_dual_render_guard_looks_for() -> None:
    assert 'data-verb={"platform.open." + id}' in PLATFORM and 'data-verb={"platform.open.providers." + t}' in PLATFORM
