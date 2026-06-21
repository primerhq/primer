"""ChannelTriggerConfig + TriggerKind.CHANNEL."""

from datetime import datetime, timezone

from primer.model.trigger import ChannelTriggerConfig, Trigger, TriggerKind


def test_channel_trigger_kind_and_config():
    assert TriggerKind.CHANNEL.value == "channel"

    cfg = ChannelTriggerConfig(provider_id="channel-provider-1")
    assert cfg.kind == "channel"
    assert cfg.channel_id is None

    t = Trigger(
        id="tr-c",
        slug="tr-c",
        name="ch",
        config=ChannelTriggerConfig(
            provider_id="channel-provider-1", channel_id="channel-1"
        ),
        created_at=datetime.now(timezone.utc),
    )
    rt = Trigger.model_validate(t.model_dump())
    assert rt.config.kind == "channel"
