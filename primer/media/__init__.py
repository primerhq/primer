"""Layer-pure media helpers shared by channel adapters and the agent turn loop.

Nothing platform- or channel-specific lives here -- only behaviour over
:class:`primer.model.chat.Part`/``Message`` and the
:class:`primer.int.artifact_storage.ArtifactStorage` interface, so both
``primer.channel`` (inbound uploads, outbound relay) and ``primer.agent``
(prompt-time hydration) can depend on it directly instead of one reaching
into the other.
"""

from __future__ import annotations
