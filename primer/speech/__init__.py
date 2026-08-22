"""OpenAI-audio-shaped speech adapters (ASR + TTS).

Concrete implementations of :class:`primer.int.asr.ASR` and
:class:`primer.int.tts.TTS`. Speech is plain HTTP through the ``openai``
SDK that the LLM adapters already depend on, so this package needs no
optional extra: a core install gets speech the moment a provider row is
registered.
"""
