"""CV-8: Limits.request_timeout_seconds is documented 'set to None to disable'; the speech adapters honour it.

The field's own default is 300.0, so a row that never mentions it still reads 300 s. A null means no read bound; 0 (no real bound either) keeps the
300 s fallback it always had.
"""
import pytest

from primer.model.provider import SpeechToTextProvider, TextToSpeechProvider
from primer.speech.openai_asr import OpenAIASR
from primer.speech.openai_tts import OpenAITTS


def _stt(**limits):
    return OpenAIASR(SpeechToTextProvider(id="s", provider="openai", default_model="m", config={"url": "http://x/v1"},
                                          limits={"max_concurrency": 1, **limits}))


def _tts(**limits):
    return OpenAITTS(TextToSpeechProvider(id="t", provider="openai", default_model="m", default_voice="v", config={"url": "http://x/v1"},
                                          limits={"max_concurrency": 1, **limits}))


@pytest.mark.parametrize("make", [_stt, _tts], ids=["asr", "tts"])
def test_null_means_no_read_bound(make) -> None:
    t = make(request_timeout_seconds=None)._timeout()
    assert t.read is None and t.write is None and t.connect == 5.0


@pytest.mark.parametrize("make", [_stt, _tts], ids=["asr", "tts"])
@pytest.mark.parametrize(("value", "read"), [(45.0, 45.0), (0.0, 300.0)])
def test_a_number_is_the_bound_and_zero_keeps_the_fallback(make, value, read) -> None:
    assert make(request_timeout_seconds=value)._timeout().read == read


@pytest.mark.parametrize("make", [_stt, _tts], ids=["asr", "tts"])
def test_an_unmentioned_field_reads_the_models_default_of_300(make) -> None:
    assert make()._timeout().read == 300.0
