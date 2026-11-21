"""Only an API-capable holder of the listener role may open inbound connections."""

from typing import TYPE_CHECKING

from primer.bus.scheduler_tasks import _BackgroundTask
from primer.channel.retirement import finish_cleanup, retry_cleanup


if TYPE_CHECKING:
    from primer.api.registries.channel_registry import ChannelRegistry


class ChannelListenerOwner(_BackgroundTask):
    role = "channel-listeners"

    def __init__(self, registry: "ChannelRegistry") -> None:
        super().__init__(name=self.role)
        self._registry = registry

    async def _run(self) -> None:
        try:
            await self._registry.set_inbound(True)
            await self._registry.watch(inbound=True)
        finally:
            # The supervisor drains this cleanup before releasing its lease.
            await finish_cleanup(retry_cleanup(lambda: self._registry.set_inbound(False), self.role))
