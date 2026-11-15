---
title: "Speech and voice configuration"
summary: "Configure speech providers and defaults while keeping voice as a human-facing edge transform."
audience: admin
order: 10
related: [providers, agents/create-and-edit, not-agent-callable]
---

## What it is

Voice is an edge transform: agents stay text in, text out. Speech-to-text turns a person's recording into a draft message; text-to-speech reads a completed reply aloud. Registering providers and choosing defaults does not give an agent a microphone or create a voice conversation inside its turn.

## Concepts

- The speech-to-text and text-to-speech provider rows hold their endpoint, optional API key and default model. TTS also has a provider-specific `default_voice`; there are no speech model-profile rows.
- The install-wide active config has three optional fields: `stt_provider_id`, `tts_provider_id` and `tts_voice`. No configured provider is a supported steady state.
- An agent's `tts_voice` overrides the install voice, which overrides the selected provider's default voice. Voice identifiers come from that provider; changing the provider can invalidate a previously selected voice.
- Provider writes and changing the install defaults require an admin. Authenticated users can read masked provider/config facts and use the audio surface. A role floor does not bypass the live route's authorization or a tool's effective approval policy.
- Audio is ephemeral. The persisted record is text, not the recording or the synthesized audio.

## How to

1. An admin registers the STT and TTS provider rows with the endpoint's actual model names. Probe a draft before selecting it; an unreachable provider is not a usable default.
2. Read the active speech config, preserve the other fields, and select the desired `stt_provider_id` and `tts_provider_id`. Enumerate voices for that TTS provider before choosing `tts_voice`.
3. Read the config back and check the speech capabilities. In the session composer a person records audio, reviews the transcribed draft and sends the text. Reply playback uses the selected TTS provider and the agent's voice override when set.
4. If a provider is warming up, respect its retriable error and back off. Fix an invalid model, voice, endpoint or credential instead of retrying unchanged input.

## Tool calls

The exact provider, config, enumeration and audio operations are linked below. Use `system__describe_tool` to inspect their live arguments, role and approval facts before calling them. A binary or streamed result is the native route's result, not an invented JSON audio envelope. This page preserves the configuration concept; recording and playback remain human-facing, rather than an agent-callable voice-onboarding runbook.

## Gotchas

- Replacing the active config replaces all three fields; preserve the defaults you are not changing.
- A TTS stream that has emitted audio is not retried from the beginning: concatenating partial syntheses produces an invalid reply.
- The browser talks to Primer's audio proxy. It does not receive the upstream provider URL or API key.
- A provider row alone does not enable recording or playback. Select it, verify the live capabilities and test the actual composer behavior.

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`audio__list_audio_models`](reference/tools/audio/list_audio_models) | List the models the active speech providers currently offer, per family. Feeds the catalog's model pickers. | user |
| [`audio__list_audio_voices`](reference/tools/audio/list_audio_voices) | List the voices a text-to-speech provider offers. Defaults to the active provider; pass provider_id to inspect another row. | user |
| [`audio__synthesize_speech`](reference/tools/audio/synthesize_speech) | Synthesise speech through the active text-to-speech provider. JSON in, chunked audio out as it is synthesised. | user |
| [`audio__transcribe_audio`](reference/tools/audio/transcribe_audio) | Transcribe an uploaded audio segment through the active speech-to-text provider. Multipart in, JSON out. | user |
| [`speech__get_active_speech_config`](reference/tools/speech/get_active_speech_config) | Read the singleton active speech config. | user |
| [`speech__list_stt_types`](reference/tools/speech/list_stt_types) | Provider-type metadata for the catalog's speech-to-text form. | user |
| [`speech__list_tts_types`](reference/tools/speech/list_tts_types) | Provider-type metadata for the catalog's text-to-speech form. | user |
| [`speech__put_active_speech_config`](reference/tools/speech/put_active_speech_config) | Replace the singleton active speech config. | admin |
| [`speech__test_stt_provider`](reference/tools/speech/test_stt_provider) | Test a draft speech-to-text config with a live GET /models round trip. Returns {ok, models} or {ok=false, error}. | admin |
| [`speech__test_tts_provider`](reference/tools/speech/test_tts_provider) | Test a draft text-to-speech config with a live GET /audio/voices round trip (falling back to GET /models). Returns {ok, voices} or {ok=false, error}. | admin |
| [`stt_providers__create`](reference/tools/stt_providers/create) | Create SpeechToTextProvider. | admin |
| [`stt_providers__delete`](reference/tools/stt_providers/delete) | Delete SpeechToTextProvider. | admin |
| [`stt_providers__find`](reference/tools/stt_providers/find) | Find SpeechToTextProvider with predicate. | admin |
| [`stt_providers__get`](reference/tools/stt_providers/get) | Get SpeechToTextProvider by id. | user |
| [`stt_providers__list`](reference/tools/stt_providers/list) | List SpeechToTextProvider. | user |
| [`stt_providers__update`](reference/tools/stt_providers/update) | Replace SpeechToTextProvider. | admin |
| [`tts_providers__create`](reference/tools/tts_providers/create) | Create TextToSpeechProvider. | admin |
| [`tts_providers__delete`](reference/tools/tts_providers/delete) | Delete TextToSpeechProvider. | admin |
| [`tts_providers__find`](reference/tools/tts_providers/find) | Find TextToSpeechProvider with predicate. | admin |
| [`tts_providers__get`](reference/tools/tts_providers/get) | Get TextToSpeechProvider by id. | user |
| [`tts_providers__list`](reference/tools/tts_providers/list) | List TextToSpeechProvider. | user |
| [`tts_providers__update`](reference/tools/tts_providers/update) | Replace TextToSpeechProvider. | admin |

## Related

[Providers](providers), [agent definitions](agents/create-and-edit), [human interactions](human-in-the-loop), [human-only boundaries](not-agent-callable).
