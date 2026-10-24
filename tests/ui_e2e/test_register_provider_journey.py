"""Journeys for the Register provider overlay on the Providers page (plan 02 Task 25; audit G2-26), each round-tripping through the real server.

The console is driven the way a person drives it (Platform > Providers at a type, "+ Register", a card's Open, the card's menu), and the outcome is read back
over REST: what the server stored, not only what the page drew.

* ``test_llm_register_and_import``: Review Focus 3 and 5. The listed models carry no context window, so Import stays off until every ticked row has one; a model
  name with "/" gets a sanitised profile id, and that id answers GET 200.
* ``test_import_with_one_existing_and_one_failing_profile``: Review Focus 1 (master Review Focus 4). A real 409 (the profile exists when the import reaches it)
  reads "already imported"; one profile POST answers 500 (the one routed answer) and reads "failed"; Retry failed posts only that row; one provider POST.
* ``test_llm_edit_round_trip``: Review Focus 2. A row with every field set, one limit changed in the overlay, and every other field (the API key included) as it was.
* ``test_embedding_register_with_discovered_models``, ``test_asr_register_picks_a_model``, ``test_a_web_search_provider_registers``: one registration per page set.
* ``test_built_ins_offer_no_delete_and_delete_is_in_the_card_menu``, ``test_speech_default_selectors``,
  ``test_a_semantic_search_card_opens_its_detail_page_and_edit_opens_the_overlay`` and ``test_the_register_overlay_works_on_a_phone``.

What the LLM, Embedding and ASR pages list comes from the session's mock OpenAI server (``mock_llm_lan``): its ``/v1/models`` lists the scenario ids registered
on it, so each journey registers its own, suffixed, and ticks only those (the listing also holds every other journey's scenarios). A card is always found
through the page's filter: a busy install has more than one page of twelve cards. The edits of the Semantic Search Storage, Workspace and Channel providers live
in ``test_provider_edit_keeps_every_field_journey.py``.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._support.mock_llm import Rule
from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import boot_mobile_shell, default_wid, wait_for_overlay_url
from tests.ui_e2e._studio_helpers import open_provider_catalog

pytestmark = [smk("SMK-UI-06", status="partial"), pytest.mark.ui_e2e]

PHONE = {"width": 375, "height": 812}

PROFILE_500 = {
    "type": "/errors/internal",
    "title": "Internal Server Error",
    "status": 500,
    "detail": "The profile store is unavailable",
    "instance": "/v1/model_profiles",
}


# ---- helpers ------------------------------------------------------------------------------------------------------------------------------------------------

def _id_part(text: str) -> str:
    """``PC_idPart`` (ui/components/providers/register-common.jsx): every character outside [A-Za-z0-9._-] becomes "-", runs of "-" become one, and leading
    and trailing "-", "." and "_" are trimmed."""
    s = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9._-]", "-", text))
    return re.sub(r"[-._]+$", "", re.sub(r"^[-._]+", "", s))


def _profile_id(provider_id: str, model_name: str) -> str:
    """``PC_profileIdFor``: the two parts joined with "--"."""
    return f"{_id_part(provider_id)}--{_id_part(model_name)}"


def _delete_all(base_url: str, paths: list[str]) -> None:
    """Best effort, in the order given (profiles before their provider): a journey that failed half-way leaves nothing behind it could name."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for path in paths:
            try:
                c.delete(path)
            except Exception:  # noqa: BLE001 - cleanup must not hide the failure being reported
                pass


def _open_type(page: Page, console_url: str, cls: str) -> None:
    open_provider_catalog(page, console_url, cls=cls)
    expect(page.get_by_test_id(f"provider-body-{cls}")).to_be_visible(timeout=20_000)


def _card(page: Page, cls: str, row_id: str) -> Locator:
    """The card of one row, found the way a person finds it: its id typed into the page's filter."""
    page.get_by_test_id("pc-filter").fill(row_id)
    card = page.get_by_test_id(f"pk-card:{cls}:{row_id}")
    expect(card).to_be_visible(timeout=20_000)
    return card


def _open_register(page: Page) -> Locator:
    page.get_by_test_id("pc-register-open").click()
    overlay = page.get_by_test_id("pc-register")
    expect(overlay).to_be_visible(timeout=15_000)
    return overlay


def _toast(page: Page, text: str) -> Locator:
    return page.locator(".toast", has_text=text)


def _llm_connection(overlay: Locator, provider_id: str, url: str) -> None:
    """Page 1 of a new LLM provider: an OpenAI-compatible Chat Completions server (the mock), then Next."""
    expect(overlay.get_by_test_id("pc-field:type")).to_be_enabled(timeout=15_000)
    overlay.get_by_test_id("pc-field:type").select_option("openchat")
    overlay.get_by_test_id("pc-field:id").fill(provider_id)
    overlay.get_by_test_id("pc-field:url").fill(url)
    next_button = overlay.get_by_test_id("pc-register-next")
    expect(next_button).to_be_enabled(timeout=10_000)
    next_button.click()
    expect(overlay.get_by_test_id("pc-llm-models")).to_be_visible(timeout=20_000)


# ---- LLM ------------------------------------------------------------------------------------------------------------------------------------------------------

def test_llm_register_and_import(page: Page, base_url: str, console_url: str, mock_llm_lan, unique_suffix: str) -> None:
    registry, mock_url = mock_llm_lan
    pid = f"reg-llm-{unique_suffix}"
    alpha, beta = f"vendor/alpha-{unique_suffix}", f"beta-{unique_suffix}"
    for name in (alpha, beta):
        registry.register(name, [Rule(emit_text="ok")])
    made = [f"/v1/model_profiles/{_profile_id(pid, name)}" for name in (alpha, beta)] + [f"/v1/llm_providers/{pid}"]
    try:
        _open_type(page, console_url, "llm")
        overlay = _open_register(page)
        _llm_connection(overlay, pid, mock_url)

        submit = overlay.get_by_test_id("pc-register-submit")
        for name in (alpha, beta):
            box = overlay.get_by_test_id(f"pc-import:{name}")
            expect(box).not_to_be_checked()   # nothing is ticked for the operator (design 6.4)
            box.check()
            # the mock reports no context window (nor do Anthropic and real OpenAI): a ticked row asks for one
            expect(overlay.get_by_test_id(f"pc-import-status:{name}")).to_have_text("Required")
        expect(submit).to_have_text("Import 2 models")
        expect(submit).to_be_disabled()
        overlay.get_by_test_id(f"pc-import-context:{alpha}").fill("8192")
        expect(submit).to_be_disabled()   # one ticked row still has none
        overlay.get_by_test_id(f"pc-import-context:{beta}").fill("4096")
        expect(submit).to_be_enabled()
        submit.click()

        expect(overlay).not_to_be_visible(timeout=20_000)
        expect(_toast(page, f"{pid}: 2 profiles imported")).to_be_visible(timeout=10_000)
        _card(page, "llm", pid)

        assert _profile_id(pid, alpha) == f"{pid}--vendor-alpha-{unique_suffix}"
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(f"/v1/llm_providers/{pid}").status_code == 200
            for name, context in ((alpha, 8192), (beta, 4096)):
                r = c.get(f"/v1/model_profiles/{_profile_id(pid, name)}")
                assert r.status_code == 200, f"the imported profile of {name!r} is not addressable by its id: {r.status_code} {r.text}"
                row = r.json()
                assert (row["provider_id"], row["model_name"], row["context_length"]) == (pid, name, context), row
    finally:
        _delete_all(base_url, made)


def test_import_with_one_existing_and_one_failing_profile(page: Page, base_url: str, console_url: str, mock_llm_lan, unique_suffix: str) -> None:
    registry, mock_url = mock_llm_lan
    pid = f"reg-part-{unique_suffix}"
    alpha, beta, gamma = (f"{word}-{unique_suffix}" for word in ("alpha", "beta", "gamma"))
    for name in (alpha, beta, gamma):
        registry.register(name, [Rule(emit_text="ok")])
    ids = {name: _profile_id(pid, name) for name in (alpha, beta, gamma)}
    made = [f"/v1/model_profiles/{ids[name]}" for name in (alpha, beta, gamma)] + [f"/v1/llm_providers/{pid}"]
    provider_posts: list[str] = []
    profile_posts: list[str] = []
    state: dict[str, object] = {"seeded": None, "failed": False}

    def count_provider_posts(request) -> None:
        if request.method == "POST" and request.url.split("?")[0].endswith("/v1/llm_providers"):
            provider_posts.append(request.url)

    def profiles(route) -> None:
        request = route.request
        if request.method != "POST":
            route.continue_()
            return
        name = (request.post_data_json or {}).get("model_name")
        profile_posts.append(name)
        if name == beta and state["seeded"] is None:
            # The profile comes into being after Next and before the console's POST of it reaches the server (another tab, another admin), so the 409 the
            # page reads is the server's own. Its description says who made it: the import must leave it as it is.
            with httpx.Client(base_url=base_url, timeout=30.0) as c:
                r = c.post("/v1/model_profiles", json={
                    "id": ids[beta], "provider_id": pid, "model_name": beta, "context_length": 2048, "description": "made before the import reached it",
                })
            state["seeded"] = r.status_code
        if name == gamma and not state["failed"]:
            state["failed"] = True
            route.fulfill(status=500, content_type="application/problem+json", body=json.dumps(PROFILE_500))
            return
        route.continue_()

    page.on("request", count_provider_posts)
    page.route("**/v1/model_profiles", profiles)
    try:
        _open_type(page, console_url, "llm")
        overlay = _open_register(page)
        _llm_connection(overlay, pid, mock_url)
        for name in (alpha, beta, gamma):
            overlay.get_by_test_id(f"pc-import:{name}").check()
            overlay.get_by_test_id(f"pc-import-context:{name}").fill("4096")
        submit = overlay.get_by_test_id("pc-register-submit")
        expect(submit).to_have_text("Import 3 models")
        submit.click()

        retry = overlay.get_by_test_id("pc-register-retry-failed")
        expect(retry).to_be_visible(timeout=20_000)
        assert state["seeded"] == 201, f"the profile that makes the 409 could not be made: {state['seeded']}"
        expect(overlay.get_by_test_id(f"pc-import-status:{alpha}")).to_have_text("imported")
        expect(overlay.get_by_test_id(f"pc-import-status:{beta}")).to_have_text("already imported")
        gamma_status = overlay.get_by_test_id(f"pc-import-status:{gamma}")
        expect(gamma_status).to_have_text(re.compile(r"^failed: "))
        expect(gamma_status).to_contain_text("The profile store is unavailable")
        expect(overlay.get_by_test_id("pc-import-summary")).to_contain_text("The provider is registered, but 1 profile could not be created.")
        expect(overlay.get_by_test_id("pc-register-close")).to_be_visible()   # no Back: the provider exists
        expect(overlay.get_by_test_id("pc-register-back")).to_have_count(0)
        assert profile_posts == [alpha, beta, gamma], profile_posts

        retry.click()
        expect(overlay).not_to_be_visible(timeout=20_000)
        expect(_toast(page, f"{pid}: 3 profiles imported")).to_be_visible(timeout=10_000)
        assert profile_posts == [alpha, beta, gamma, gamma], f"Retry failed must post only the failed row: {profile_posts}"
        assert len(provider_posts) == 1, f"the provider was posted {len(provider_posts)} times"

        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(f"/v1/llm_providers/{pid}").status_code == 200
            rows = {}
            for name in (alpha, beta, gamma):
                r = c.get(f"/v1/model_profiles/{ids[name]}")
                assert r.status_code == 200, f"{name}: {r.status_code} {r.text}"
                rows[name] = r.json()
                assert (rows[name]["provider_id"], rows[name]["model_name"]) == (pid, name), rows[name]
            assert rows[beta]["description"] == "made before the import reached it", "the import wrote over the profile that already existed"
            assert rows[beta]["context_length"] == 2048
    finally:
        page.unroute_all(behavior="ignoreErrors")
        _delete_all(base_url, made)


def test_llm_edit_round_trip(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    pid = f"reg-edit-{unique_suffix}"
    key = f"sk-journey-{unique_suffix}"
    seed = {
        "id": pid,
        "provider": "openchat",
        "config": {"url": "http://models.invalid/v1", "api_key": key, "flavor": "vllm"},
        "limits": {
            "max_concurrency": 3,
            "request_timeout_seconds": 120.0,
            "connect_timeout_seconds": 7.0,
            "total_timeout_seconds": 900.0,
            "max_retries": 4,
            "retry_backoff_seconds": 1.5,
            "retry_backoff_max_seconds": 12.0,
        },
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json=seed)
        assert r.status_code == 201, r.text
        before = c.get(f"/v1/llm_providers/{pid}").json()
    try:
        assert before["config"]["api_key"] == "**********" + key[-4:], before["config"]
        _open_type(page, console_url, "llm")
        _card(page, "llm", pid)
        page.get_by_test_id(f"pk-card-open:llm:{pid}").click()
        overlay = page.get_by_test_id("pc-register")
        expect(overlay).to_be_visible(timeout=15_000)
        expect(overlay).to_contain_text(f"Edit {pid}")
        expect(overlay.get_by_test_id("pc-field:id")).to_have_value(pid)
        expect(overlay.get_by_test_id("pc-register-next")).to_have_count(0)   # an edit has no Next: its draft holds the key's mask
        concurrency = overlay.get_by_test_id("pc-limit:max_concurrency")
        expect(concurrency).to_have_value("3")
        concurrency.fill("5")
        with page.expect_response(lambda resp: resp.request.method == "PUT" and resp.url.split("?")[0].endswith(f"/v1/llm_providers/{pid}")) as answered:
            overlay.get_by_test_id("pc-register-submit").click()
        assert answered.value.status == 200, answered.value.text()
        expect(overlay).not_to_be_visible(timeout=15_000)

        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            after = c.get(f"/v1/llm_providers/{pid}").json()
        expected = {**before, "limits": {**before["limits"], "max_concurrency": 5}}
        assert after == expected, f"the edit changed a field it did not show:\nbefore {before}\nafter  {after}"
        # the tail the server serves is the stored key's own: the mask went back and the key was kept
        assert after["config"]["api_key"] == "**********" + key[-4:]
    finally:
        _delete_all(base_url, [f"/v1/llm_providers/{pid}"])


# ---- the other page sets ----------------------------------------------------------------------------------------------------------------------------------------

def test_embedding_register_with_discovered_models(page: Page, base_url: str, console_url: str, mock_llm_lan, unique_suffix: str) -> None:
    registry, mock_url = mock_llm_lan
    pid = f"reg-emb-{unique_suffix}"
    embed, chat = f"text-embed-{unique_suffix}", f"chat-{unique_suffix}"
    registry.register(chat, [Rule(emit_text="ok")])     # registered first, so the server lists it first
    registry.register(embed, [Rule(emit_text="ok")])
    try:
        _open_type(page, console_url, "embedding")
        overlay = _open_register(page)
        expect(overlay.get_by_test_id("pc-field:type")).to_be_enabled(timeout=15_000)
        overlay.get_by_test_id("pc-field:type").select_option("openai")
        overlay.get_by_test_id("pc-field:id").fill(pid)
        overlay.get_by_test_id("pc-field:url").fill(mock_url)
        overlay.get_by_test_id("pc-register-next").click()
        expect(overlay.get_by_test_id("pc-models-pick")).to_be_visible(timeout=20_000)

        expect(overlay.get_by_test_id(f"pc-import:{embed}")).to_be_checked()
        expect(overlay.get_by_test_id(f"pc-import:{chat}")).not_to_be_checked()
        order = overlay.locator('[data-testid^="pc-import:"]:not([data-testid="pc-import:all"])').evaluate_all(
            "els => els.map((e) => e.getAttribute('data-testid'))")
        assert order.index(f"pc-import:{embed}") < order.index(f"pc-import:{chat}"), f"the embed-named model is not listed first: {order}"
        # the listing holds every journey's scenarios: register exactly this one
        for testid in order:
            box = overlay.get_by_test_id(testid)
            if testid != f"pc-import:{embed}" and box.is_checked():
                box.uncheck()
        submit = overlay.get_by_test_id("pc-register-submit")
        expect(submit).to_have_text("Register with 1 model")
        submit.click()

        expect(overlay).not_to_be_visible(timeout=20_000)
        expect(_card(page, "embedding", pid)).to_contain_text(embed)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/embedding_providers/{pid}")
        assert r.status_code == 200, r.text
        assert [m["name"] for m in r.json()["models"]] == [embed]
    finally:
        _delete_all(base_url, [f"/v1/embedding_providers/{pid}"])


def test_asr_register_picks_a_model(page: Page, base_url: str, console_url: str, mock_llm_lan, unique_suffix: str) -> None:
    registry, mock_url = mock_llm_lan
    pid = f"reg-asr-{unique_suffix}"
    model = f"whisper-{unique_suffix}"
    registry.register(model, [Rule(emit_text="ok")])
    try:
        _open_type(page, console_url, "stt")
        overlay = _open_register(page)
        overlay.get_by_test_id("pc-field:id").fill(pid)
        overlay.get_by_test_id("pc-field:url").fill(mock_url)
        overlay.get_by_test_id("pc-register-next").click()
        expect(overlay.get_by_test_id("pc-speech-pick")).to_be_visible(timeout=20_000)
        submit = overlay.get_by_test_id("pc-register-submit")
        expect(submit).to_be_disabled()   # nothing is picked yet
        overlay.get_by_test_id(f"pc-pick:{model}").check()
        expect(submit).to_be_enabled()
        submit.click()

        expect(overlay).not_to_be_visible(timeout=20_000)
        expect(_card(page, "stt", pid)).to_contain_text(f"model {model}")
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/stt_providers/{pid}")
        assert r.status_code == 200, r.text
        assert r.json()["default_model"] == model
    finally:
        _delete_all(base_url, [f"/v1/stt_providers/{pid}"])


def test_a_web_search_provider_registers(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    pid = f"reg-tavily-{unique_suffix}"
    try:
        _open_type(page, console_url, "web_search")
        overlay = _open_register(page)
        kind = overlay.get_by_test_id("pc-field:type")
        expect(kind).to_be_enabled(timeout=15_000)
        offered = kind.locator("option").evaluate_all("els => els.map((e) => e.value)")
        assert "tavily" in offered and "duckduckgo" not in offered, f"a built-in kind is offered, or Tavily is not: {offered}"
        kind.select_option("tavily")
        overlay.get_by_test_id("pc-field:id").fill(pid)
        overlay.get_by_test_id("pc-field:api_key").fill(f"tvly-journey-{unique_suffix}")
        with page.expect_response(lambda resp: resp.request.method == "POST" and resp.url.split("?")[0].endswith("/v1/web_search_providers")) as answered:
            overlay.get_by_test_id("provider-form-save").click()
        assert answered.value.status == 201, answered.value.text()
        expect(overlay).not_to_be_visible(timeout=15_000)
        _card(page, "web_search", pid)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/web_search_providers/{pid}")
        assert r.status_code == 200, r.text
        assert r.json()["provider_type"] == "tavily"
    finally:
        _delete_all(base_url, [f"/v1/web_search_providers/{pid}"])


# ---- the page around the overlay ---------------------------------------------------------------------------------------------------------------------------------

def test_built_ins_offer_no_delete_and_delete_is_in_the_card_menu(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    pid = f"reg-del-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/web_search_providers", json={"id": pid, "provider_type": "tavily", "config": {"type": "tavily", "api_key": "tvly-journey"}})
        assert r.status_code == 201, r.text
    try:
        _open_type(page, console_url, "web_search")

        # the built-in row: a badge, no menu, and its overlay offers no Delete (the server answers 403 to one)
        _card(page, "web_search", "DuckDuckGo")
        expect(page.get_by_test_id("pk-card-badge:web_search:DuckDuckGo")).to_have_text("built in")
        expect(page.get_by_test_id("pk-card-menu:web_search:DuckDuckGo")).to_have_count(0)
        page.get_by_test_id("pk-card-open:web_search:DuckDuckGo").click()
        overlay = page.get_by_test_id("pc-register")
        expect(overlay).to_be_visible(timeout=15_000)
        expect(overlay).to_contain_text("Edit DuckDuckGo")
        expect(overlay.get_by_test_id("pc-register-delete")).to_have_count(0)
        page.keyboard.press("Escape")
        expect(overlay).not_to_be_visible(timeout=10_000)

        # a row of its own: Delete is in the card's menu, behind a confirm that names it
        _card(page, "web_search", pid)
        page.get_by_test_id(f"pk-card-menu:web_search:{pid}").click()
        page.get_by_test_id(f"pc-card-delete:web_search:{pid}").click()
        confirm = page.get_by_test_id("dialog-confirm")
        expect(confirm).to_be_visible(timeout=10_000)
        expect(page.get_by_text(f"Delete {pid}?")).to_be_visible()
        confirm.click()
        expect(page.get_by_test_id(f"pk-card:web_search:{pid}")).to_have_count(0, timeout=15_000)
        expect(_toast(page, "Provider deleted")).to_be_visible(timeout=10_000)

        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(f"/v1/web_search_providers/{pid}").status_code == 404
            assert c.get("/v1/web_search_providers/DuckDuckGo").status_code == 200
    finally:
        _delete_all(base_url, [f"/v1/web_search_providers/{pid}"])


def test_speech_default_selectors(page: Page, base_url: str, console_url: str, mock_llm_lan, unique_suffix: str) -> None:
    """Each selector replaces the one ActiveSpeechConfig row as a whole: changing the ASR default keeps the TTS ones, and the TTS provider change keeps the ASR
    one and clears the voice (a voice name belongs to its provider). The install's own defaults are put back at the end."""
    _, mock_url = mock_llm_lan
    stt_id, tts_id = f"reg-stt-{unique_suffix}", f"reg-tts-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        original = c.get("/v1/speech_active_config").json()
        r = c.post("/v1/stt_providers", json={"id": stt_id, "provider": "openai", "default_model": "whisper-1", "config": {"url": mock_url}, "limits": {"max_concurrency": 1}})
        assert r.status_code == 201, r.text
        r = c.post("/v1/tts_providers", json={"id": tts_id, "provider": "openai", "default_model": "kokoro", "default_voice": "af_heart", "config": {"url": mock_url},
                                              "limits": {"max_concurrency": 1}})
        assert r.status_code == 201, r.text
    keep = {k: original.get(k) for k in ("stt_provider_id", "tts_provider_id", "tts_voice")}

    def put_of_the_config(resp) -> bool:
        return resp.request.method == "PUT" and resp.url.split("?")[0].endswith("/v1/speech_active_config")

    try:
        _open_type(page, console_url, "stt")
        stt_select = page.get_by_test_id("pc-active-stt-provider")
        expect(stt_select).to_be_enabled(timeout=20_000)
        expect(stt_select.locator(f'option[value="{stt_id}"]')).to_have_count(1)
        with page.expect_response(put_of_the_config) as answered:
            stt_select.select_option(stt_id)
        assert answered.value.status == 200, answered.value.text()
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            stored = c.get("/v1/speech_active_config").json()
        assert stored["stt_provider_id"] == stt_id
        assert (stored["tts_provider_id"], stored["tts_voice"]) == (keep["tts_provider_id"], keep["tts_voice"]), "the ASR choice dropped the TTS defaults"

        _open_type(page, console_url, "tts")
        tts_select = page.get_by_test_id("pc-active-tts-provider")
        expect(tts_select).to_be_enabled(timeout=20_000)
        with page.expect_response(put_of_the_config) as answered:
            tts_select.select_option(tts_id)
        assert answered.value.status == 200, answered.value.text()
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            stored = c.get("/v1/speech_active_config").json()
        assert (stored["stt_provider_id"], stored["tts_provider_id"], stored["tts_voice"]) == (stt_id, tts_id, None), stored
        expect(page.get_by_test_id("pc-active-tts-voice")).to_have_value("")
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.put("/v1/speech_active_config", json=keep)
        _delete_all(base_url, [f"/v1/stt_providers/{stt_id}", f"/v1/tts_providers/{tts_id}"])


def test_a_semantic_search_card_opens_its_detail_page_and_edit_opens_the_overlay(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    """Section 20 Q5: Open on a panel class shows its detail page in place (not the overlay); the detail page's Edit opens the overlay on that row."""
    sid = f"reg-ssp-{unique_suffix}"
    path = f"/tmp/reg-ssp-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/ssp", json={"id": sid, "provider": "lance", "config": {"path": path}})
        assert r.status_code == 201, r.text
    try:
        _open_type(page, console_url, "ssp")
        _card(page, "ssp", sid)
        page.get_by_test_id(f"pk-card-open:ssp:{sid}").click()
        expect(page.get_by_test_id("ssp-section-overview")).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("pc-register")).to_have_count(0)
        wait_for_overlay_url(page, f"ssp/{sid}")

        page.get_by_test_id("ssp-detail-edit").click()
        overlay = page.get_by_test_id("pc-register")
        expect(overlay).to_be_visible(timeout=15_000)
        expect(overlay).to_contain_text(f"Edit {sid}")
        expect(overlay.get_by_test_id("ssp-lance-path")).to_have_value(path)
        expect(overlay.get_by_test_id("ssp-test-connect-draft")).to_have_count(0)   # an edit never tests its draft (audit G2-07)
        overlay.get_by_role("button", name="Cancel", exact=True).click()
        expect(overlay).not_to_be_visible(timeout=10_000)
        expect(page.get_by_test_id("ssp-section-overview")).to_be_visible()

        page.get_by_test_id("ssp-detail-back").click()
        expect(page.get_by_test_id("provider-body-ssp")).to_be_visible(timeout=15_000)
        _card(page, "ssp", sid)
    finally:
        _delete_all(base_url, [f"/v1/ssp/{sid}"])


def test_the_register_overlay_works_on_a_phone(page: Page, console_url: str) -> None:
    """375px: the LLM type page, reached by its link on the phone's More tab; "+ Register" opens the overlay as a bottom sheet that fits the screen, with the type
    selector shown and Next reachable."""
    page.set_viewport_size(PHONE)
    boot_mobile_shell(page, f"{console_url}#/w/{default_wid(console_url)}?view=platform:providers:llm")
    expect(page.get_by_test_id("provider-body-llm")).to_be_visible(timeout=20_000)
    overlay = _open_register(page)
    expect(overlay).to_have_class(re.compile(r"\bsheet\b"))
    expect(overlay.get_by_test_id("pc-register-type")).to_be_visible()
    overlay.get_by_test_id("pc-field:id").fill("reg-phone-journey")
    overlay.get_by_test_id("pc-field:url").fill("http://models.invalid/v1")
    next_button = overlay.get_by_test_id("pc-register-next")
    next_button.scroll_into_view_if_needed()
    expect(next_button).to_be_in_viewport()
    expect(next_button).to_be_enabled()
    box = overlay.bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"] + 1, f"the sheet is wider than the phone: {box}"
    page.keyboard.press("Escape")
    expect(overlay).not_to_be_visible(timeout=10_000)
