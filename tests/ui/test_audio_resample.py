"""48k stereo in, 16k mono PCM16 WAV out. Downmix BEFORE resample.

Every assertion runs the real JavaScript in MiniRacer, the same technique
as tests/ui/test_studio_diff.py. The rule this pins comes from
asr-tts.md: services may hard-reject non-16k audio, browsers capture
44.1/48k, and downmixing after resampling costs twice the work for the
same result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "ui"
AUDIO = UI / "foundation" / "audio.js"


def _ctx():
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    ctx.eval(AUDIO.read_text(encoding="utf-8"))
    return ctx


def _arr(ctx, expression: str):
    return json.loads(ctx.eval(f"JSON.stringify(Array.from({expression}))"))


def test_the_module_is_registered_in_index_html() -> None:
    assert 'src="foundation/audio.js"' in (UI / "index.html").read_text(encoding="utf-8")


def test_downmix_averages_the_channels() -> None:
    ctx = _ctx()
    ctx.eval(
        "var out = PRIMER_downmixToMono("
        "[Float32Array.from([1, 0, -1]), Float32Array.from([0, 1, 1])]);"
    )
    assert _arr(ctx, "out") == pytest.approx([0.5, 0.5, 0.0])


def test_downmix_of_a_single_channel_is_the_identity() -> None:
    ctx = _ctx()
    ctx.eval("var out = PRIMER_downmixToMono([Float32Array.from([0.25, -0.5])]);")
    assert _arr(ctx, "out") == pytest.approx([0.25, -0.5])


def test_resampling_48k_to_16k_keeps_one_sample_in_three() -> None:
    ctx = _ctx()
    ctx.eval(
        "var src = new Float32Array(48000);"
        "for (var i = 0; i < src.length; i++) src[i] = i / 48000;"
        "var out = PRIMER_resampleLinear(src, 48000, 16000);"
    )
    assert ctx.eval("out.length") == 16000


def test_resampling_is_a_no_op_at_the_target_rate() -> None:
    ctx = _ctx()
    ctx.eval(
        "var src = Float32Array.from([0.1, 0.2, 0.3]);"
        "var out = PRIMER_resampleLinear(src, 16000, 16000);"
    )
    assert _arr(ctx, "out") == pytest.approx([0.1, 0.2, 0.3])


def test_the_wav_header_declares_16k_mono_pcm16() -> None:
    ctx = _ctx()
    ctx.eval(
        "var wav = PRIMER_encodeWavPcm16(Float32Array.from([0, 0.5, -0.5]), 16000);"
        "var view = new DataView(wav.buffer);"
    )
    assert _arr(ctx, "wav.slice(0, 4)") == [82, 73, 70, 70]  # RIFF
    assert _arr(ctx, "wav.slice(8, 12)") == [87, 65, 86, 69]  # WAVE
    assert ctx.eval("view.getUint16(22, true)") == 1  # channels
    assert ctx.eval("view.getUint32(24, true)") == 16000  # sample rate
    assert ctx.eval("view.getUint16(34, true)") == 16  # bits per sample


def test_full_pipeline_48k_stereo_to_16k_mono_wav() -> None:
    ctx = _ctx()
    ctx.eval(
        "var left = new Float32Array(48000);"
        "var right = new Float32Array(48000);"
        "for (var i = 0; i < 48000; i++) { left[i] = 0.5; right[i] = -0.5; }"
        "var wav = PRIMER_toMono16kWav([left, right], 48000);"
        "var view = new DataView(wav.buffer);"
    )
    assert ctx.eval("view.getUint32(24, true)") == 16000
    assert ctx.eval("view.getUint16(22, true)") == 1
    # One second of 16k mono PCM16 is 32000 data bytes plus a 44-byte header.
    assert ctx.eval("wav.length") == 44 + 32000
    # Averaging +0.5 and -0.5 gives silence, which proves the downmix ran.
    assert ctx.eval("view.getInt16(44, true)") == 0


def test_downmix_runs_before_resample() -> None:
    """A pipeline that resampled first would resample twice; this pins
    the single-resample cost by asserting the helper is called on the
    already-mono buffer."""
    src = AUDIO.read_text(encoding="utf-8")
    downmix_at = src.index("PRIMER_downmixToMono(channels")
    resample_at = src.index("PRIMER_resampleLinear(mono")
    assert downmix_at < resample_at


def test_pcm16_clamps_rather_than_wrapping() -> None:
    ctx = _ctx()
    ctx.eval(
        "var wav = PRIMER_encodeWavPcm16(Float32Array.from([2.0, -2.0]), 16000);"
        "var view = new DataView(wav.buffer);"
    )
    assert ctx.eval("view.getInt16(44, true)") == 32767
    assert ctx.eval("view.getInt16(46, true)") == -32768


def test_a_silence_split_point_is_found_past_the_minimum_length() -> None:
    ctx = _ctx()
    ctx.eval(
        "var samples = new Float32Array(16000 * 90);"
        "for (var i = 0; i < samples.length; i++) samples[i] = 0.4;"
        "for (var j = 16000 * 61; j < 16000 * 62; j++) samples[j] = 0.0;"
        "var at = PRIMER_findSilenceSplit(samples, 16000, 60);"
    )
    split = ctx.eval("at")
    assert 16000 * 61 <= split <= 16000 * 62


def test_no_split_is_reported_below_the_minimum_length() -> None:
    ctx = _ctx()
    ctx.eval(
        "var samples = new Float32Array(16000 * 10);"
        "var at = PRIMER_findSilenceSplit(samples, 16000, 60);"
    )
    assert ctx.eval("at") == -1
