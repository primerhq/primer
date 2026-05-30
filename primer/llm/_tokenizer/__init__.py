"""Per-provider token counter modules.

Each module exports a single function that estimates the prompt token
count for ``messages`` (+ optional ``tools``). The :class:`LLM`
adapter for the corresponding provider delegates ``count_tokens`` to
its module. ``char_fallback`` is the universal floor used when a
native counter raises.
"""

from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback

__all__ = ["count_tokens_char_fallback"]
