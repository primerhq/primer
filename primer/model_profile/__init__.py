"""Model-profile resolution.

A profile names one (provider, model) pair plus its API-level config; the
model itself lives in :mod:`primer.model.model_profile`. This package holds
the resolution logic that turns a profile id into the concrete facts a turn
needs.
"""

from primer.model_profile.resolver import ResolvedModel, resolve_llm, resolve_model

__all__ = ["ResolvedModel", "resolve_llm", "resolve_model"]
