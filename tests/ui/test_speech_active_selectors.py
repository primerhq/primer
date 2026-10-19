"""ASR and TTS active selectors for the Providers page (plan 02, Task 16).

The selectors replace the old catalog-wide speech defaults panel. They edit
the singleton as one three-field value, enumerate voices from the selected
provider, recover to the served value after a refused write, and register as
seam I-4 extras without replacing another plan's entry.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._register_harness import COMPONENTS, base_code, context, js, settle, transpiled

FILE = COMPONENTS / "providers" / "speech-active.jsx"
ACTIVE_KEY = "pc:speech-active"

STUBS = r"""
var RELOADS = 0;
function mountStt(rows) {
  MR.mount(PC_SttActiveSelector, { rows: rows, reload: function () { RELOADS += 1; } });
}
function mountTts(rows) {
  MR.mount(PC_TtsActiveSelector, { rows: rows, reload: function () { RELOADS += 1; } });
}
function changeSelect(testid, value) {
  var node = MR.find(testid);
  SETTLED = "pending";
  var out = node.props.onChange({ target: { value: value } });
  Promise.resolve(out).then(function () { SETTLED = "fulfilled"; }, function () { SETTLED = "rejected"; });
  MR.rerender();
}
function options(testid) {
  return ALL(testid).filter(function (n) { return n.type === "option"; }).map(function (n) { return n.props.value; });
}
function calls(method, path) {
  return CALLS.filter(function (c) { return c.method === method && c.path === path; });
}
"""


@pytest.fixture(scope="module")
def code() -> str:
    return base_code() + "\n" + transpiled(FILE)


@pytest.fixture
def ctx(code):
    made = context(code)
    made.eval(STUBS)
    try:
        yield made
    finally:
        made.close()


def _serve_active(ctx, **overrides) -> dict:
    row = {
        "id": "active-speech",
        "stt_provider_id": "asr-old",
        "tts_provider_id": "tts-old",
        "tts_voice": "voice-old",
        **overrides,
    }
    ctx.eval(f"serve({json.dumps(ACTIVE_KEY)}, {json.dumps(row)});")
    return row


def _put_bodies(ctx) -> list[dict]:
    return js(ctx, 'calls("PUT", "/speech_active_config").map(function (c) { return c.body; })')


def test_changing_asr_keeps_the_tts_fields(ctx) -> None:
    _serve_active(ctx)
    ctx.eval('mountStt([{ id: "asr-old" }, { id: "asr-new" }]);')
    ctx.eval('changeSelect("pc-active-stt-provider", "asr-new");')
    assert settle(ctx) == "fulfilled"
    assert _put_bodies(ctx) == [{
        "stt_provider_id": "asr-new",
        "tts_provider_id": "tts-old",
        "tts_voice": "voice-old",
    }]
    assert js(ctx, "TOASTS")[-1] == {"kind": "success", "title": "Speech default saved"}
    assert ctx.eval("RELOADS") == 1


def test_changing_the_tts_provider_resets_the_voice(ctx) -> None:
    _serve_active(ctx)
    ctx.eval('mountTts([{ id: "tts-old" }, { id: "tts-new" }]);')
    ctx.eval('changeSelect("pc-active-tts-provider", "tts-new");')
    assert settle(ctx) == "fulfilled"
    assert _put_bodies(ctx) == [{
        "stt_provider_id": "asr-old",
        "tts_provider_id": "tts-new",
        "tts_voice": None,
    }]


def test_voices_come_from_the_chosen_provider_through_the_passthrough(ctx) -> None:
    _serve_active(ctx, tts_provider_id="tts-one", tts_voice=None)
    ctx.eval('serve("pc:speech-voices:tts-one", { voices: ["voice-a", "voice-b"] });')
    ctx.eval('mountTts([{ id: "tts-one" }]);')
    assert js(ctx, 'options("pc-active-tts-voice")') == ["", "voice-a", "voice-b"]
    source = FILE.read_text(encoding="utf-8")
    assert "/audio/voices?provider_id=" in source
    for name in ("af_heart", "am_adam", "af_bella"):
        assert name not in source


def test_the_selectors_edit_all_three_settings(ctx) -> None:
    _serve_active(ctx)
    ctx.eval('serve("pc:speech-voices:tts-old", { voices: ["voice-old", "voice-new"] });')
    ctx.eval('mountTts([{ id: "tts-old" }]);')
    ctx.eval('changeSelect("pc-active-tts-voice", "voice-new");')
    assert settle(ctx) == "fulfilled"
    assert _put_bodies(ctx) == [{
        "stt_provider_id": "asr-old",
        "tts_provider_id": "tts-old",
        "tts_voice": "voice-new",
    }]


def test_a_refused_put_shows_the_served_value_again(ctx) -> None:
    _serve_active(ctx)
    ctx.eval(r'''
      HANDLER = function () {
        return refuse({
          type: "/errors/invalid-active-config", title: "Invalid speech defaults",
          status: 422, detail: "That provider cannot be selected.", extensions: {}
        });
      };
      mountStt([{ id: "asr-old" }, { id: "asr-new" }]);
      changeSelect("pc-active-stt-provider", "asr-new");
    ''')
    assert settle(ctx) == "fulfilled", "the component handles the refusal"
    assert ctx.eval('MR.find("pc-active-stt-provider").props.value') == "asr-old"
    toast = js(ctx, "TOASTS")[0]
    assert toast["kind"] == "error" and toast["detail"] == "That provider cannot be selected."
    assert ctx.eval("RELOADS") == 0


def test_loading_and_load_error_have_status_and_retry(ctx) -> None:
    ctx.eval('mountStt([{ id: "asr-one" }]);')
    assert ctx.eval('MR.find("pc-active-loading").props.role') == "status"
    ctx.eval(r'''
      fail("pc:speech-active", {
        type: "/errors/internal", title: "Unavailable", status: 503,
        detail: "Speech defaults are unavailable.", extensions: {}
      });
      MR.rerender();
    ''')
    assert ctx.eval('MR.find("pc-active-error").props.role') == "alert"
    ctx.eval('MR.click("pc-active-retry");')
    assert ctx.eval('__entry("pc:speech-active").refetched') == 1


def test_the_slot_registry_merges_instead_of_replacing() -> None:
    code = (
        base_code()
        + "\nwindow.PC_TYPE_EXTRAS = { artifact_storage: function ExistingExtra() {} };\n"
        + transpiled(FILE)
    )
    made = context(code)
    try:
        assert js(made, "Object.keys(window.PC_TYPE_EXTRAS).sort()") == [
            "artifact_storage",
            "stt",
            "tts",
        ]
        assert made.eval("window.PC_TYPE_EXTRAS.artifact_storage.name") == "ExistingExtra"
    finally:
        made.close()
