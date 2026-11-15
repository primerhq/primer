"""The claude_code agent-runtime provider: drives the operator's claude CLI.

See the per-step process in ``primer/llm/claude_code/adapter.py``; the rest of
this package is the wire (``wire``), the replay (``history``), the tool bridge
(``bridge``), the child lifecycle (``process``), the event translation
(``translate``) and the host status check (``status``).
"""

from primer.llm.claude_code.adapter import ClaudeCodeLLM

__all__ = ["ClaudeCodeLLM"]
