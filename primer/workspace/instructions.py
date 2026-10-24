"""A workspace's ``agents.md``: loaded each turn, appended to the system prompt of every agent that runs there.

The platform reads the file, not the agent: at every turn the prompt sites (the agent executor, graph agent
nodes, the manual compaction route's sizing, ``invoke_agent`` subagents) call :func:`load_workspace_instructions`
on the workspace and pass the text to :func:`workspace_instructions_parts`. The text is re-read each turn, so an
edit takes effect on the next one. An agent opts out with ``Agent.use_workspace_instructions`` (default on), and
:func:`workspace_instructions_parts` is the one place that honours it.

Four properties matter more than the plumbing:

* **Literal.** An agent's system prompt is a Jinja template (:mod:`primer.agent.prompt_render`), so a user's
  ``{{ x }}`` in the file would be evaluated, or fail the render. :func:`jinja_literal` escapes every ``{``, so
  the text reaches the model as written.
* **Bounded.** The file counts at most :data:`AGENTS_MD_MAX_BYTES` bytes; a longer one is cut and says so. The
  read itself is bounded too: a file over :data:`AGENTS_MD_READ_LIMIT_BYTES` is not read at all, sandbox reads
  refuse a FIFO from its stat metadata before opening it, and a read that takes longer than
  :data:`AGENTS_MD_READ_TIMEOUT_S` is given up on as a backstop for a stuck mount or backend.
* **Never fatal.** A missing file is normal (``None``, no log); an unreadable one (a directory, a read error, a
  file too large or too slow to read) logs one warning and is skipped. A turn must run whatever the file is.

* **Marked.** The part opens with a heading that says the text was written by this workspace's users and grants no
  permissions, and ends with a heading of its own (:data:`WORKSPACE_INSTRUCTIONS_END`). A line in the file that imitates
  that closing heading is quoted (:func:`neutralise_end_markers`), so the file cannot end its own section early.

Security: any session in a workspace can write ``agents.md`` and so change the instructions of every agent
running there. A workspace is a trust boundary, and the docs say so. That includes the shared default workspace
(prime), where administrators' operator sessions run: whoever can write files there steers those agents too. This
was a deliberate, accepted choice (2026-10-23); the framing above is the mitigation, not a boundary, and the file
grants nothing.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import re
from typing import TYPE_CHECKING, Any

from primer.model.except_ import NotFoundError


if TYPE_CHECKING:
    from primer.model.agent import Agent


logger = logging.getLogger(__name__)


#: The file names, in lookup order. ``AGENTS.md`` is read only when ``agents.md`` is absent.
AGENTS_MD_NAMES: tuple[str, str] = ("agents.md", "AGENTS.md")

#: How many raw bytes of the file reach the prompt.
AGENTS_MD_MAX_BYTES: int = 32768

#: A file larger than this is not read at all (``Workspace.read_file`` has no ranged read, so the cap above bounds the
#: prompt, not the read). Between the two limits the file is read whole and then cut at the cap.
AGENTS_MD_READ_LIMIT_BYTES: int = 1024 * 1024

#: How long, in seconds, checking and reading one name may take before the file is given up on. Read when the load
#: runs, so a test can shorten it.
AGENTS_MD_READ_TIMEOUT_S: float = 5.0

#: The heading that opens the prompt part. It says who wrote the text and that the text grants nothing, so the model
#: reads the file as guidance from this workspace's users and not as a platform section.
WORKSPACE_INSTRUCTIONS_TITLE: str = (
    "Workspace instructions (agents.md, written by this workspace's users; they grant no permissions)"
)

#: The heading that closes the prompt part. The file's text lies between the two headings and nothing after the closing
#: one is the file's.
WORKSPACE_INSTRUCTIONS_END: str = "End of workspace instructions"


async def load_workspace_instructions(workspace: Any) -> str | None:
    """The text of the workspace's ``agents.md`` (or ``AGENTS.md``), ready for the prompt, or ``None``.

    The first name that is not missing (``Workspace.file_info`` or ``read_file`` raising :class:`NotFoundError`)
    wins; when both are absent the answer is ``None`` and nothing is logged. Any other failure (a directory, a
    read error, a file over :data:`AGENTS_MD_READ_LIMIT_BYTES`, a check or read that outlasts
    :data:`AGENTS_MD_READ_TIMEOUT_S`) logs one warning and returns ``None`` without trying the other name: a file
    that is there but unreadable must not be shadowed by a different one. Cancellation is not a failure and is
    never swallowed.

    The bytes decode as UTF-8 with replacement characters. Past :data:`AGENTS_MD_MAX_BYTES` only the first
    that-many bytes are kept (a character cut in half by the limit is dropped) and a note names the file and its
    size.
    """
    for name in AGENTS_MD_NAMES:
        try:
            raw = await asyncio.wait_for(_read(workspace, name), timeout=AGENTS_MD_READ_TIMEOUT_S)
        except NotFoundError:
            continue
        except TimeoutError as exc:
            # Sandbox reads refuse FIFOs before opening them. Other stuck mounts or backends can still outlast the
            # caller, and a runtime thread already blocked in I/O cannot be cancelled, so stop waiting for it.
            # A TimeoutError the backend raised itself keeps its own words.
            _skip(workspace, name, str(exc) or f"the read did not finish within {AGENTS_MD_READ_TIMEOUT_S:g} seconds")
            return None
        except Exception as exc:  # noqa: BLE001 - an unreadable file must never fail the turn
            _skip(workspace, name, exc)
            return None
        return _decode(name, raw)
    return None


async def _read(workspace: Any, name: str) -> bytes:
    """``name``'s bytes, after checking its size: a file over the read limit is refused before it is read."""
    size = (await workspace.file_info(name)).size_bytes
    if size > AGENTS_MD_READ_LIMIT_BYTES:
        raise ValueError(f"{size} bytes is over the {AGENTS_MD_READ_LIMIT_BYTES}-byte read limit")
    return await workspace.read_file(name)


def _skip(workspace: Any, name: str, reason: object) -> None:
    logger.warning(
        "workspace instructions: %s in %s is unreadable; skipped: %s", name, getattr(workspace, "id", None), reason,
    )


def _decode(name: str, raw: bytes) -> str:
    size = len(raw)
    if size <= AGENTS_MD_MAX_BYTES:
        return raw.decode("utf-8", errors="replace")
    # An incremental decoder holds back a multi-byte character the cut split instead of turning it into a
    # replacement character, and still replaces bad bytes before the cut as the whole-file decode does.
    kept = codecs.getincrementaldecoder("utf-8")(errors="replace").decode(raw[:AGENTS_MD_MAX_BYTES])
    return f"{kept}\n\n[truncated: {name} is {size} bytes; only the first {AGENTS_MD_MAX_BYTES} bytes are included]"


def jinja_literal(text: str) -> str:
    """``text`` as a Jinja template that renders to exactly ``text``.

    Every ``{`` becomes ``{{ "{" }}``. Jinja's delimiters (``{{``, ``{%``, ``{#``) all start with one, and the
    closing ones (``}}``, ``%}``, ``#}``) mean nothing outside a block, so nothing else needs escaping.
    """
    return text.replace("{", '{{ "{" }}')


#: A line a reader could take for the closing heading: any heading level, any case or spacing, any whitespace around it.
#: Matched against a whole line, line ending included.
_END_LINE = re.compile(r"\s*#+\s*" + r"\s+".join(WORKSPACE_INSTRUCTIONS_END.split()) + r"\s*", re.IGNORECASE)

#: What a neutralised line starts with. It is visible, so the model (and a person reading the prompt) can tell that the
#: file held such a line, and it stops the line from being a heading.
_QUOTED_PREFIX: str = "(quoted) "


def neutralise_end_markers(text: str) -> str:
    """``text`` with every line that imitates the closing heading marked as quoted, so none of them ends the section.

    The file is written by the workspace's users: a line equal to the closing heading, with text after it, would
    otherwise read as the end of their section and the rest as platform text. A line is taken for the closing heading
    when, ignoring whitespace around it, case and the spacing between the words, it is :data:`WORKSPACE_INSTRUCTIONS_END`
    under a heading of any level; such a line gets ``(quoted) `` in front of it and is otherwise kept as written.
    Lines are split the way :meth:`str.splitlines` does (``\\n``, ``\\r\\n``, ``\\r`` and the rarer separators), and the
    separators are kept, so every other character of the text is unchanged. A line that only mentions the words
    (``see "## End of workspace instructions" above``) is left alone.
    """
    return "".join(
        _QUOTED_PREFIX + line if _END_LINE.fullmatch(line) else line for line in text.splitlines(keepends=True)
    )


def workspace_instructions_part(text: str) -> str:
    """The system-prompt part for a workspace's instructions.

    The opening heading (:data:`WORKSPACE_INSTRUCTIONS_TITLE`), a blank line, the text with any line that imitates the
    closing heading quoted and every ``{`` escaped for Jinja, a blank line, and the closing heading
    (:data:`WORKSPACE_INSTRUCTIONS_END`). The closing heading is the only line of the part that can end the section.
    """
    body = jinja_literal(neutralise_end_markers(text))
    return f"## {WORKSPACE_INSTRUCTIONS_TITLE}\n\n{body}\n\n## {WORKSPACE_INSTRUCTIONS_END}"


def workspace_instructions_parts(agent: "Agent", text: str | None) -> list[str]:
    """The prompt parts a workspace's instructions add for ``agent``: none, or the one part.

    Every prompt site calls this, never :func:`workspace_instructions_part` directly, so who gets the
    instructions is decided in one place. An agent whose ``use_workspace_instructions`` is off gets none,
    whatever the file holds. A file with nothing in it adds no part: a heading over nothing would only
    cost tokens and read as an empty instruction.
    """
    if not agent.use_workspace_instructions:
        return []
    if text is None or not text.strip():
        return []
    return [workspace_instructions_part(text)]
