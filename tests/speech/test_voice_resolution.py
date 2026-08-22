"""Voice resolution order: agent override then global default (S4 P1 Task 11)."""

from __future__ import annotations

from primer.model.agent import Agent, AgentModel
from primer.speech.resolution import resolve_tts_voice


def _agent(**overrides) -> Agent:
    body = {
        "id": "agent-a",
        "description": "test agent",
        "model": AgentModel(profile_id="prov--m"),
    }
    body.update(overrides)
    return Agent(**body)


def test_agent_override_wins_over_the_global_default() -> None:
    assert (
        resolve_tts_voice(
            agent_tts_voice="am_adam",
            active_voice="af_heart",
            provider_default_voice="af_bella",
        )
        == "am_adam"
    )


def test_the_global_default_wins_when_the_agent_has_no_override() -> None:
    assert (
        resolve_tts_voice(
            agent_tts_voice=None,
            active_voice="af_heart",
            provider_default_voice="af_bella",
        )
        == "af_heart"
    )


def test_the_provider_row_default_is_the_last_resort() -> None:
    assert (
        resolve_tts_voice(
            agent_tts_voice=None,
            active_voice=None,
            provider_default_voice="af_bella",
        )
        == "af_bella"
    )


def test_empty_strings_do_not_shadow_a_real_default() -> None:
    assert (
        resolve_tts_voice(
            agent_tts_voice="",
            active_voice="",
            provider_default_voice="af_bella",
        )
        == "af_bella"
    )


def test_nothing_configured_resolves_to_none() -> None:
    assert (
        resolve_tts_voice(
            agent_tts_voice=None, active_voice=None, provider_default_voice=None,
        )
        is None
    )


def test_agent_tts_voice_defaults_to_none_and_round_trips() -> None:
    assert _agent().tts_voice is None
    assert _agent(tts_voice="am_adam").tts_voice == "am_adam"


def test_agent_tts_voice_survives_a_json_round_trip() -> None:
    agent = _agent(tts_voice="am_adam")
    assert Agent.model_validate(agent.model_dump()).tts_voice == "am_adam"
