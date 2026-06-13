"""Provider-agnostic chat input and streaming-output types.

These models define the unified interface that every LLM provider adapter in
this project must accept (inputs) and emit (output stream events). The shapes
were derived from comparative SDK research in
``research/provider_interface.md``, which also documents the per-provider
mapping rules adapters must follow.

The interface is layered:

* **Universal types** are the primary surface. Adapters MUST map
  provider-specific signals onto these whenever a reasonable mapping exists
  (e.g. fold OpenAI ``response.reasoning_summary_text.delta`` into
  :class:`ReasoningDelta`; fold Anthropic ``stop_reason="refusal"`` into a
  :class:`Done` with ``stop_reason="content_filter"``).
* **Extended types** carry provider-specific information that has no clean
  universal equivalent (raw reasoning trace, server-side tool lifecycles,
  citations, logprobs, safety ratings, audio/video input). They are reached
  through a single wrapper at each level — :class:`ExtendedPart` for inputs
  and :class:`ExtendedEvent` for outputs — both exposing the underlying
  payload via an ``extended`` property. Consumers that only care about the
  universal interface can ignore extended content with one pattern-match
  arm.

Two top-level discriminated unions are exported:

* :data:`Part` — one element of a :class:`Message` ``parts`` list. Members:
  :class:`TextPart`, :class:`ImagePart`, :class:`DocumentPart`, and
  :class:`ExtendedPart`.
* :data:`StreamEvent` — one event yielded by a provider's ``stream()``
  generator. Members: the universal lifecycle / content events plus
  :class:`ExtendedEvent`.

Adapters MUST prefer the universal type whenever a clean mapping exists; use
the extended wrapper only when no reasonable subset equivalent captures the
signal without information loss.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

from primer.model.common import Describeable


def _decode_b64_if_str(v: Any) -> Any:
    """BeforeValidator for ``data: bytes`` fields on multimodal parts.

    Pydantic's default ``bytes`` field treats a string input as UTF-8
    text — so a JSON payload like ``{"data": "JVBER..."}`` (the shape
    the chat WS frame uses) ends up with ``data`` set to the literal
    bytes of the base64 string instead of the decoded file content.
    The adapter then base64-encodes that string AGAIN, sending
    double-encoded garbage to the LLM provider, which 400s with
    ``invalid_union`` or similar.

    This validator decodes a base64 string input back to raw bytes
    before Pydantic's bytes validator runs. ``bytes`` inputs pass
    through unchanged (existing tests construct parts with raw bytes
    directly, e.g. ``DocumentPart(data=b'%PDF-1.4')``). Any input
    that isn't a string or bytes is left alone so Pydantic's normal
    type error surfaces.
    """
    if isinstance(v, str):
        try:
            return base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError):
            # Not valid base64 — let Pydantic's default validator
            # produce the canonical error rather than guessing.
            return v
    return v


def _encode_b64_for_json(v: bytes | None) -> str | None:
    """PlainSerializer for ``data: bytes`` fields in JSON output.

    Pydantic's default JSON serializer for ``bytes`` tries to UTF-8
    decode the value — which crashes with ``UnicodeDecodeError`` on
    real binary content (a PDF, a PNG header, anything past plain
    ASCII text). The chat runner persists Part rows via
    ``model_dump(mode='json')``, so without this serializer any
    non-text attachment makes the WS turn explode.

    Pairs with :func:`_decode_b64_if_str` to give the field a clean
    base64 round-trip through JSON storage + the WS wire frame.
    """
    if v is None:
        return None
    return base64.b64encode(v).decode("ascii")


# ===========================================================================
# Input content parts
# ===========================================================================


# ---- Universal input parts --------------------------------------------------


class TextPart(BaseModel):
    """Plain UTF-8 text content."""

    type: Literal["text"] = Field(
        default="text",
        description="Discriminator tag identifying this part as text.",
    )
    text: str = Field(
        ...,
        description="The text content of this part.",
    )


class _BinarySourceMixin(BaseModel):
    """Validation shared by binary-bearing parts (image, document, audio, video).

    Each such part must carry the payload via at least one of ``data``,
    ``url``, ``file_id``, or ``artifact_id``; the adapter chooses which surface
    to forward based on the target provider.
    """

    artifact_id: str | None = Field(
        default=None,
        description=(
            "Reference to a stored artifact (chat media bytes) in the "
            "ArtifactStorage backend. The bytes are rehydrated into ``data`` "
            "at turn time and at outbound-relay time; LLM provider adapters "
            "never see this field."
        ),
    )

    @model_validator(mode="after")
    def _require_one_source(self) -> "_BinarySourceMixin":
        if not (self.data or self.url or self.file_id or self.artifact_id):  # type: ignore[attr-defined]
            raise ValueError(
                "at least one of 'data', 'url', 'file_id', or 'artifact_id' "
                "must be provided"
            )
        return self


class ImagePart(_BinarySourceMixin):
    """An image attachment.

    Supply the image via raw ``data`` (bytes), a public ``url``, or a
    provider-side ``file_id``. ``mime_type`` is required by Anthropic and
    Google when ``data`` is used; OpenAI ignores it but it is harmless to
    include.
    """

    type: Literal["image"] = Field(
        default="image",
        description="Discriminator tag identifying this part as an image.",
    )
    data: Annotated[
        bytes | None,
        BeforeValidator(_decode_b64_if_str),
        PlainSerializer(_encode_b64_for_json, return_type=str, when_used="json"),
    ] = Field(
        default=None,
        description="Raw image bytes. Pydantic accepts base64-encoded strings.",
    )
    url: str | None = Field(
        default=None,
        description="Public URL or 'data:' URI pointing at the image.",
    )
    mime_type: str | None = Field(
        default=None,
        description="MIME type of the image (e.g. 'image/png'). Required by some providers when 'data' is used.",
    )
    file_id: str | None = Field(
        default=None,
        description="Opaque provider-side file identifier. ID space is not portable across providers.",
    )
    detail: Literal["low", "high", "auto"] | None = Field(
        default=None,
        description="OpenAI-only rendering hint; ignored by other providers.",
    )


class DocumentPart(_BinarySourceMixin):
    """A document attachment, typically a PDF.

    Supply the document via raw ``data`` (bytes), a public ``url``, or a
    provider-side ``file_id``. Set ``mime_type`` to ``'application/pdf'``
    when sending PDFs; other types may or may not be accepted depending on
    the target provider.
    """

    type: Literal["document"] = Field(
        default="document",
        description="Discriminator tag identifying this part as a document.",
    )
    data: Annotated[
        bytes | None,
        BeforeValidator(_decode_b64_if_str),
        PlainSerializer(_encode_b64_for_json, return_type=str, when_used="json"),
    ] = Field(
        default=None,
        description="Raw document bytes. Pydantic accepts base64-encoded strings.",
    )
    url: str | None = Field(
        default=None,
        description="Public URL pointing at the document.",
    )
    mime_type: str | None = Field(
        default=None,
        description="MIME type of the document (commonly 'application/pdf').",
    )
    file_id: str | None = Field(
        default=None,
        description="Opaque provider-side file identifier. ID space is not portable across providers.",
    )
    filename: str | None = Field(
        default=None,
        description="OpenAI-only filename hint; ignored by other providers.",
    )


# ---- Universal tool round-trip parts ----------------------------------------


class ToolCallPart(BaseModel):
    """Records a tool call the model made in a previous turn.

    Universal across all four chat providers (OpenAI, Anthropic, Google,
    Ollama). Appears as a part inside an ``assistant``-role
    :class:`Message` to preserve the model's tool-call request when
    rebuilding the message history for a follow-up turn.

    The ``id`` field correlates with a matching :class:`ToolResultPart`
    in a subsequent ``tool``-role message; adapters use it to wire the
    request back to its result on the provider's wire format.

    Adapter wrapping:

    * OpenAI Responses: extracted as a top-level ``function_call`` input
      item (not nested under the assistant message).
    * Anthropic: emitted as a ``tool_use`` content block inside the
      assistant message.
    * Google GenAI: emitted as a ``Part(function_call=FunctionCall(id=, name=, args=))``
      inside the model's content.
    * Ollama: emitted on the assistant message's parallel ``tool_calls``
      field.
    """

    type: Literal["tool_call"] = Field(
        default="tool_call",
        description="Discriminator tag identifying this part as a tool call the assistant made.",
    )
    id: str = Field(
        ...,
        min_length=1,
        description="Identifier correlating this call with its later ToolResultPart.",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Name of the tool the model invoked.",
    )
    arguments: dict[str, Any] = Field(
        ...,
        description="Parsed arguments object the model passed to the tool.",
    )


class ToolResultPart(BaseModel):
    """Records the result of executing a previously-requested tool call.

    Universal across all four chat providers. Appears as a part inside a
    ``tool``-role :class:`Message` to feed the tool's output back to the
    model on the next turn. The ``id`` MUST match the originating
    :class:`ToolCallPart`'s ``id``.

    Adapter wrapping:

    * OpenAI Responses: extracted as a top-level ``function_call_output``
      input item (not nested under any message role).
    * Anthropic: emitted as a ``tool_result`` content block inside a
      synthesised user-role message.
    * Google GenAI: emitted as a ``Part(function_response=FunctionResponse(id=, name=, response=))``
      inside a user-role content.
    * Ollama: emitted as a separate ``tool``-role message with the
      output as ``content``.

    The ``error`` flag lets the caller signal an execution failure (the
    tool ran but returned an error, or the user denied the call); the
    output string carries a human-readable explanation. Distinct from a
    transport-level :class:`Error` event, which represents a stream
    failure rather than a tool failure.
    """

    type: Literal["tool_result"] = Field(
        default="tool_result",
        description="Discriminator tag identifying this part as a tool execution result.",
    )
    id: str = Field(
        ...,
        min_length=1,
        description="Identifier matching the originating ToolCallPart's id.",
    )
    output: str = Field(
        ...,
        description="Tool execution output as a string. Adapters that need structured payloads serialise to JSON.",
    )
    error: bool = Field(
        default=False,
        description="True if the output represents a tool execution failure or denial rather than a successful result.",
    )
    media: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Raw non-text content blocks the tool returned (e.g. MCP "
            "image/audio/embedded-resource blocks). Carried from "
            "ToolCallResult.extended so callers can surface tool-produced "
            "media; ignored by LLM adapters (they read ``output``)."
        ),
    )


class ToolCallResult(BaseModel):
    """Result of executing a tool through a :class:`primer.int.ToolsetProvider`.

    Mirrors the shape of MCP's tool-call response so downstream consumers
    can convert directly to a :class:`ToolResultPart` for the next chat
    turn::

        ToolResultPart(id=call.id, output=result.output, error=result.is_error)

    Distinct from :class:`ToolResultPart`:

    * :class:`ToolResultPart` lives *inside* a chat ``Message`` and feeds
      the model's next turn.
    * :class:`ToolCallResult` is the value returned from a single
      ``ToolsetProvider.call`` invocation — it carries the same ``output``
      / error signal but adds a free-form ``extended`` slot for adapters
      that surface richer payloads (MCP image / audio / embedded resource
      content arrays).
    """

    output: str = Field(
        ...,
        description=(
            "Tool execution output as a single string. Adapters that "
            "receive structured / multi-content payloads concatenate text "
            "parts and serialise the rest as JSON inside this string."
        ),
    )
    is_error: bool = Field(
        default=False,
        description="True if the tool reported an execution failure.",
    )
    extended: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Provider-specific extras the adapter chose to surface "
            "(e.g. raw MCP content array). Open for callers that want "
            "richer access; safely ignored by callers that only need "
            "``output``."
        ),
    )


# ---- Extended input parts (wrapped via ExtendedPart) ------------------------


class AudioPart(_BinarySourceMixin):
    """An audio attachment. Reachable only through :class:`ExtendedPart`.

    Audio input is not universally supported, which is why this type lives
    behind :class:`ExtendedPart` rather than alongside the universal parts.
    Provider support:

    * OpenAI Responses accepts inline base64 audio in ``mp3`` or ``wav``
      format only (via ``ResponseInputAudio``).
    * Google GenAI accepts arbitrary audio mime types via ``inline_data``
      or ``file_data``.
    * Anthropic and Ollama do not accept audio input — adapters for those
      providers must reject this part (e.g. raise an unsupported-content
      error rather than silently dropping it).
    """

    type: Literal["audio"] = Field(
        default="audio",
        description="Discriminator tag identifying this extended part as audio.",
    )
    data: Annotated[
        bytes | None,
        BeforeValidator(_decode_b64_if_str),
        PlainSerializer(_encode_b64_for_json, return_type=str, when_used="json"),
    ] = Field(
        default=None,
        description="Raw audio bytes. Pydantic accepts base64-encoded strings.",
    )
    url: str | None = Field(
        default=None,
        description="Public URL or provider-side file URI pointing at the audio.",
    )
    mime_type: str | None = Field(
        default=None,
        description="MIME type of the audio (e.g. 'audio/mpeg', 'audio/wav'). OpenAI is restricted to mp3/wav.",
    )
    file_id: str | None = Field(
        default=None,
        description="Opaque provider-side file identifier. ID space is not portable across providers.",
    )


class VideoPart(_BinarySourceMixin):
    """A video attachment. Reachable only through :class:`ExtendedPart`.

    Video input is supported only by Google GenAI (via ``inline_data`` or
    ``file_data``, optionally annotated with ``video_metadata`` for
    clipping / fps). All other adapters must reject this part.

    Use the optional ``start_offset`` / ``end_offset`` / ``fps`` fields
    to clip or downsample the video on the provider side; these map onto
    Google's ``VideoMetadata`` and are silently ignored by adapters that
    have no equivalent.
    """

    type: Literal["video"] = Field(
        default="video",
        description="Discriminator tag identifying this extended part as video.",
    )
    data: Annotated[
        bytes | None,
        BeforeValidator(_decode_b64_if_str),
        PlainSerializer(_encode_b64_for_json, return_type=str, when_used="json"),
    ] = Field(
        default=None,
        description="Raw video bytes. Pydantic accepts base64-encoded strings.",
    )
    url: str | None = Field(
        default=None,
        description="Public URL or provider-side file URI pointing at the video.",
    )
    mime_type: str | None = Field(
        default=None,
        description="MIME type of the video (e.g. 'video/mp4').",
    )
    file_id: str | None = Field(
        default=None,
        description="Opaque provider-side file identifier. ID space is not portable across providers.",
    )
    start_offset: str | None = Field(
        default=None,
        description="Start of the clip to send (duration string, e.g. '10s'). Provider-side trim hint.",
    )
    end_offset: str | None = Field(
        default=None,
        description="End of the clip to send (duration string, e.g. '60s'). Provider-side trim hint.",
    )
    fps: float | None = Field(
        default=None,
        gt=0,
        description="Target frames-per-second for sampling the video on the provider side.",
    )


ExtendedInputContent = Annotated[
    AudioPart | VideoPart,
    Field(discriminator="type"),
]
"""Discriminated union of every extended input part.

Always reached via :attr:`ExtendedPart.extended`. New extended part types
are added here; consumers gain access to them automatically by matching on
:class:`ExtendedPart`.
"""


class ExtendedPart(BaseModel):
    """Wrapper carrying a non-universal input part (audio, video).

    The unified :data:`Part` union exposes only the categories every
    surveyed provider supports as first-class members (text, image,
    document). Modalities that some providers cannot ingest are wrapped in
    this single envelope so universal-only consumers can identify and skip
    them with one pattern-match arm.

    Construct as ``ExtendedPart(extended=AudioPart(...))``; access the
    payload via ``part.extended``.
    """

    type: Literal["extended"] = Field(
        default="extended",
        description="Discriminator tag identifying this part as an extended-content wrapper.",
    )
    extended: ExtendedInputContent = Field(
        ...,
        description="The wrapped extended input content (audio, video).",
    )


# ---- Top-level Part union and Message ---------------------------------------


Part = Annotated[
    TextPart | ImagePart | DocumentPart | ToolCallPart | ToolResultPart | ExtendedPart,
    Field(discriminator="type"),
]
"""One element of a :class:`Message`'s ``parts`` list.

Universal members are the primary surface — adapters should always
prefer one of these when a reasonable mapping exists:

* :class:`TextPart`, :class:`ImagePart`, :class:`DocumentPart` —
  content modalities accepted by every chat backend.
* :class:`ToolCallPart` — assistant's tool call from a previous turn,
  preserved when rebuilding history. Belongs in ``assistant``-role
  messages.
* :class:`ToolResultPart` — result of executing a previously-requested
  tool call. Belongs in ``tool``-role messages.

Provider-specific content (audio, video, future additions) is reached
through :class:`ExtendedPart`.
"""


class Message(BaseModel):
    """A single chat message.

    Roles map directly onto each provider:

    * ``user`` — human input.
    * ``assistant`` — model output (text, tool calls, etc.).
    * ``system`` — system instruction. Surfaced by adapters via the
      top-level ``system`` parameter (Anthropic) or
      ``system_instruction`` (Google); inlined as a message for OpenAI
      and Ollama.
    * ``tool`` — carries one or more :class:`ToolResultPart`s produced
      after executing the model's tool calls. Adapters lift to the
      provider-specific surface (OpenAI top-level
      ``function_call_output``, Anthropic user-role ``tool_result``
      block, Google user-role ``function_response`` part, Ollama
      ``tool``-role message).
    """

    role: Literal["user", "assistant", "system", "tool"] = Field(
        ...,
        description="Speaker role for this message.",
    )
    parts: list[Part] = Field(
        ...,
        min_length=1,
        description="Ordered content parts that make up this message.",
    )


# ---- Tool definitions and choice -------------------------------------------


class ToolExample(BaseModel):
    """One worked example of calling a tool, validated against its args_schema.

    Rendered into ``Tool.description`` for the LLM and kept structured so a
    conformance test can re-validate ``args`` against the tool's schema.
    """

    args: dict[str, Any] = Field(
        ..., description="A valid argument object for this tool."
    )
    returns: str | None = Field(
        default=None,
        description="Short illustrative outcome, e.g. '201 plus the stored row'.",
    )
    note: str | None = Field(
        default=None, description="Optional one-line caveat for this example."
    )


class Tool(Describeable):
    """A function tool the model may invoke during a chat turn.

    Lowest-common-denominator across the four surveyed providers: each
    tool is identified by a string ``id``, carries a free-form
    ``description`` (both inherited from :class:`Describeable`), a
    ``toolset_id`` linking it to the :class:`Toolset` it belongs to,
    and a JSON Schema describing its argument object. Adapters wrap the
    same shape into provider-specific envelopes:

    * OpenAI Responses: ``{type: "function", name=id, description, parameters=args_schema}``.
    * Anthropic: ``{name=id, description, input_schema=args_schema}``
      (rename of the schema key).
    * Google GenAI: nested under
      ``Tool(function_declarations=[FunctionDeclaration(name=id, description=..., parameters_json_schema=args_schema)])``.
    * Ollama: older Chat Completions nesting,
      ``{type: "function", function: {name=id, description, parameters=args_schema}}``.

    The :attr:`id` is the wire-level identifier the model uses to
    invoke the tool — it appears as the ``name`` field in
    :class:`ToolCallPart` and in every provider's tool-call payload.
    The :attr:`toolset_id` ties the tool back to its origin so the
    application can route :class:`ToolCallPart` invocations to the
    correct :class:`Toolset` for execution.

    For Pydantic-defined arguments, callers can derive the schema:
    ``args_schema=MyArgsModel.model_json_schema()``.

    Wire compatibility: the JSON key on both input and output is
    ``"schema"`` (preserved via Pydantic aliases) so existing REST
    clients, the operator console, and the E2E test surface keep
    working. Python code reads/writes ``args_schema``; constructors
    accept either name (``populate_by_name=True``).
    """

    # ``populate_by_name`` lets callers pass either the Python field name
    # (``args_schema``) or the JSON wire alias (``schema``) to the
    # constructor. ``serialize_by_alias`` makes ``model_dump()`` /
    # ``model_dump_json()`` default to the alias so the REST surface and
    # operator console keep seeing ``"schema"`` without every callsite
    # having to pass ``by_alias=True``.
    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

    toolset_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the Toolset this tool belongs to (matches Toolset.id).",
    )
    args_schema: dict[str, Any] = Field(
        ...,
        validation_alias="schema",
        serialization_alias="schema",
        description="JSON Schema describing the tool's argument object.",
    )
    examples: list[ToolExample] = Field(
        default_factory=list,
        exclude=True,
        description=(
            "Structured worked examples, rendered into `description` and "
            "validated against `args_schema`. In-memory metadata only; "
            "excluded from serialization (not sent over the wire)."
        ),
    )


ToolChoice = Literal["auto", "required", "none"] | str
"""How the model should decide whether to invoke tools.

* ``"auto"`` — model decides whether to call any tool.
* ``"required"`` — model must call at least one tool.
* ``"none"`` — model must not call any tool.
* any other string — name of a specific tool the model is forced to call.

Adapter mapping is documented in
``research/abc_interface.md`` (the "Tool choice design" section). Note
that the Ollama adapter ignores any tool choice — Ollama's chat API
does not expose a tool-choice parameter, so the model decides
regardless of what the caller passes.
"""


# ===========================================================================
# Output stream events
# ===========================================================================


StopReason = Literal[
    "stop",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "content_filter",
    "error",
    "other",
]
"""Normalised stop reason emitted on :class:`Done`.

Adapters should map provider-specific reasons onto this set and preserve the
original string in :attr:`Done.raw_reason` for callers that need full
fidelity (Google in particular has 16 distinct finish reasons that collapse
into this 7-value set).
"""


# ---- Universal stream events ------------------------------------------------


class StreamStart(BaseModel):
    """Emitted once at the start of a stream.

    Synthesised by adapters that don't have a native start event (Google's
    SDK only emits per-chunk ``GenerateContentResponse``s; the adapter fires
    this on the first chunk).
    """

    type: Literal["stream_start"] = Field(default="stream_start")
    request_id: str | None = Field(
        default=None,
        description="Provider-assigned request identifier, if available.",
    )
    model: str = Field(
        ...,
        description="Provider-side model name servicing the request.",
    )


class TextDelta(BaseModel):
    """An incremental chunk of assistant text.

    The ``text`` field is a delta — concatenate successive deltas at the same
    ``index`` to reconstruct the full block.
    """

    type: Literal["text_delta"] = Field(default="text_delta")
    text: str = Field(
        ...,
        description="Incremental text to append to the block at 'index'.",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index assigned by the adapter.",
    )


class ReasoningDelta(BaseModel):
    """An incremental chunk of model reasoning / extended thinking.

    Maps to the user-facing reasoning channel for each provider:
    OpenAI ``reasoning_summary_text`` deltas, Anthropic
    ``thinking_delta`` events, Google ``Part(thought=True)`` chunks, and
    Ollama ``message.thinking`` fragments. Adapters with two reasoning
    channels (OpenAI's raw vs. summary) map the user-facing one here and
    emit the raw trace via :class:`RawReasoningDelta` inside an
    :class:`ExtendedEvent`.
    """

    type: Literal["reasoning_delta"] = Field(default="reasoning_delta")
    text: str = Field(
        ...,
        description="Incremental reasoning text to append to the block at 'index'.",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index assigned by the adapter.",
    )
    signature: str | None = Field(
        default=None,
        description="Anthropic-only cryptographic signature paired with the reasoning block.",
    )


class ToolCallStart(BaseModel):
    """Announces that the model has started emitting a client-side tool call.

    "Client-side" means the tool is one the application registered and is
    expected to execute itself (the model emits the call, the application
    runs it, the result is sent back as a tool-result message). Distinct
    from :class:`ServerToolCallStart`, which fires for tools the provider
    runs internally.
    """

    type: Literal["tool_call_start"] = Field(default="tool_call_start")
    id: str = Field(
        ...,
        description="Adapter-assigned identifier used to correlate subsequent delta/end events.",
    )
    name: str = Field(
        ...,
        description="Name of the tool the model is invoking.",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index assigned by the adapter.",
    )


class ToolCallDelta(BaseModel):
    """An incremental chunk of a tool call's JSON-encoded arguments.

    ``arguments_delta`` carries a fragment of the arguments JSON string;
    callers must concatenate fragments at the same ``id`` and parse the
    result as JSON. Google's and Ollama's adapters emit a single delta
    containing the full arguments JSON because their SDKs deliver parsed
    args atomically.
    """

    type: Literal["tool_call_delta"] = Field(default="tool_call_delta")
    id: str = Field(
        ...,
        description="Identifier matching the originating ToolCallStart event.",
    )
    arguments_delta: str = Field(
        ...,
        description="Partial JSON fragment to concatenate with prior deltas for this id.",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index assigned by the adapter.",
    )


class ToolCallEnd(BaseModel):
    """Completes a client-side tool call and exposes the fully-parsed argument object."""

    type: Literal["tool_call_end"] = Field(default="tool_call_end")
    id: str = Field(
        ...,
        description="Identifier matching the originating ToolCallStart event.",
    )
    arguments: dict[str, Any] = Field(
        ...,
        description="Parsed JSON arguments object the model wants to invoke the tool with.",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index assigned by the adapter.",
    )


class MediaDelta(BaseModel):
    """An incremental chunk of generated media (audio or image bytes).

    Universal in the sense that it represents a content modality of the
    model's output; only some providers actually produce it. OpenAI's
    audio deltas and image-generation tool, and Google's audio /
    image-output models emit this. Anthropic and Ollama never do.
    """

    type: Literal["media_delta"] = Field(default="media_delta")
    kind: Literal["audio", "image"] = Field(
        ...,
        description="Modality of the media chunk.",
    )
    data: bytes = Field(
        ...,
        description="Raw media bytes for this chunk.",
    )
    mime_type: str = Field(
        ...,
        description="MIME type of the media (e.g. 'audio/mpeg', 'image/png').",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index assigned by the adapter.",
    )


class Usage(BaseModel):
    """Token-accounting telemetry.

    Anthropic and Google emit usage on every chunk with cumulative counts;
    OpenAI emits a single terminal value; Ollama emits final counters on
    the ``done=True`` chunk. The ``cumulative`` flag tells the consumer
    how to interpret the numbers.
    """

    type: Literal["usage"] = Field(default="usage")
    input_tokens: int = Field(
        ...,
        ge=0,
        description="Tokens consumed from the request prompt.",
    )
    output_tokens: int = Field(
        ...,
        ge=0,
        description="Tokens produced in the assistant response.",
    )
    cached_input_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Subset of input_tokens served from a prompt cache, if reported.",
    )
    reasoning_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Tokens spent on hidden reasoning, if reported.",
    )
    cumulative: bool = Field(
        ...,
        description="True if counts are running totals; False if they are this-event deltas or final-only.",
    )


class Done(BaseModel):
    """Emitted exactly once at the end of a successful stream."""

    type: Literal["done"] = Field(default="done")
    stop_reason: StopReason = Field(
        ...,
        description="Normalised reason the stream ended.",
    )
    raw_reason: str = Field(
        ...,
        description="Original provider-supplied stop reason string for callers needing full fidelity.",
    )


class Error(BaseModel):
    """Emitted when an error is detected on or before stream completion.

    OpenAI surfaces these mid-stream as native events; Anthropic, Google,
    and Ollama raise exceptions, which the adapter wraps into a terminal
    Error before closing the iterator.
    """

    type: Literal["error"] = Field(default="error")
    code: str | None = Field(
        default=None,
        description="Provider error code, if available.",
    )
    message: str = Field(
        ...,
        description="Human-readable error message.",
    )
    fatal: bool = Field(
        ...,
        description="True if no further events will follow this one.",
    )


# ---- Extended stream events (wrapped via ExtendedEvent) ---------------------


class RawReasoningDelta(BaseModel):
    """Incremental chunk of unsummarised internal reasoning trace.

    Reachable only through :class:`ExtendedEvent`. Some providers expose
    two parallel reasoning channels: a user-facing summary (mapped to
    :class:`ReasoningDelta`) and a more detailed raw trace mapped here.
    Currently emitted only by the OpenAI Responses adapter for reasoning
    models, which exposes the raw channel via
    ``response.reasoning_text.delta`` events distinct from the summary
    channel ``response.reasoning_summary_text.delta``.

    Adapters whose providers don't separate the two leave this stream
    silent.
    """

    type: Literal["raw_reasoning_delta"] = Field(default="raw_reasoning_delta")
    text: str = Field(
        ...,
        description="Incremental raw-reasoning text to append to the block at 'index'.",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index assigned by the adapter.",
    )


class RefusalDelta(BaseModel):
    """Incremental chunk of a model refusal explanation streamed on a dedicated channel.

    Reachable only through :class:`ExtendedEvent`. OpenAI Responses streams
    refusal text separately from regular content via
    ``response.refusal.delta`` / ``response.refusal.done`` events; the
    adapter surfaces those fragments here. Other providers signal refusal
    only via :class:`Done` with ``stop_reason="content_filter"`` and never
    emit this event.
    """

    type: Literal["refusal_delta"] = Field(default="refusal_delta")
    text: str = Field(
        ...,
        description="Incremental refusal text to append to the block at 'index'.",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index assigned by the adapter.",
    )


class ServerToolCallStart(BaseModel):
    """A built-in server-side tool invocation has begun.

    Reachable only through :class:`ExtendedEvent`. Server-side tools execute
    on the provider's infrastructure rather than being handed to the client
    for execution. Examples by provider:

    * OpenAI Responses: ``web_search_call``, ``file_search_call``,
      ``code_interpreter_call``, ``image_generation_call``, ``mcp_call``.
    * Anthropic Messages: ``server_tool_use`` (web search, web fetch,
      code execution, bash, text editor) results.
    * Google GenAI: tools-driven actions like grounding lookups and code
      execution.

    The ``tool_name`` is provider-specific but conventionally lowercase
    (e.g. ``"web_search"``, ``"code_interpreter"``).
    """

    type: Literal["server_tool_call_start"] = Field(default="server_tool_call_start")
    id: str = Field(
        ...,
        description="Adapter-assigned identifier used to correlate subsequent delta/end events.",
    )
    tool_name: str = Field(
        ...,
        description="Name of the server-side tool being invoked (e.g. 'web_search', 'code_interpreter').",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index assigned by the adapter.",
    )


class ServerToolCallDelta(BaseModel):
    """Incremental sub-content from an in-progress server-side tool call.

    Reachable only through :class:`ExtendedEvent`. Some server tools stream
    partial content while running: OpenAI's ``code_interpreter`` streams
    the executable code via ``code.delta`` events; ``web_search`` may emit
    intermediate progress markers. The ``text`` field carries the
    incremental fragment when the underlying tool exposes one.
    """

    type: Literal["server_tool_call_delta"] = Field(default="server_tool_call_delta")
    id: str = Field(
        ...,
        description="Identifier matching the originating ServerToolCallStart event.",
    )
    text: str | None = Field(
        default=None,
        description="Incremental sub-content from the server tool (code, query, etc.) when available.",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index assigned by the adapter.",
    )


class ServerToolCallEnd(BaseModel):
    """Completion of a server-side tool call.

    Reachable only through :class:`ExtendedEvent`. The ``result`` field
    carries the tool's output as a provider-specific object (search hits,
    code execution output, generated image references, etc.). Consumers
    must inspect the originating ``tool_name`` to interpret it.
    """

    type: Literal["server_tool_call_end"] = Field(default="server_tool_call_end")
    id: str = Field(
        ...,
        description="Identifier matching the originating ServerToolCallStart event.",
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="Provider-specific result payload from the server tool.",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index assigned by the adapter.",
    )


class Citation(BaseModel):
    """A citation or annotation attaching a section of assistant output to an external source.

    Reachable only through :class:`ExtendedEvent`. All four surveyed
    providers expose citations in different shapes; this type is the union
    of the populated fields:

    * OpenAI Responses annotations: ``url_citation`` / ``file_citation`` /
      ``container_file_citation``.
    * Anthropic Messages: ``CitationsDelta`` with ``char_location`` /
      ``page_location`` / ``content_block_location``.
    * Google GenAI: ``citation_metadata.citation_sources`` plus
      ``grounding_metadata.grounding_chunks`` for retrieval-grounded
      responses.
    * Ollama: not supported.

    Adapters populate whatever fields the provider exposes and leave the
    rest as ``None``.
    """

    type: Literal["citation"] = Field(default="citation")
    source_url: str | None = Field(
        default=None,
        description="URL of the cited source when it is a web resource.",
    )
    source_title: str | None = Field(
        default=None,
        description="Human-readable title of the cited source.",
    )
    source_id: str | None = Field(
        default=None,
        description="Opaque provider-side identifier for the source (file id, document id).",
    )
    quoted_text: str | None = Field(
        default=None,
        description="Excerpt from the source that is being cited, when the provider exposes one.",
    )
    start_index: int | None = Field(
        default=None,
        ge=0,
        description="Character offset in the response text where this citation begins.",
    )
    end_index: int | None = Field(
        default=None,
        ge=0,
        description="Character offset in the response text where this citation ends (exclusive).",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index of the output block this citation annotates.",
    )


class LogprobAlternative(BaseModel):
    """An alternative token the model considered, with its log-probability.

    Embedded inside :class:`TokenLogprob`; not a top-level stream event.
    """

    token: str = Field(
        ...,
        description="The alternative token text.",
    )
    logprob: float = Field(
        ...,
        description="Log-probability the model assigned to this alternative.",
    )


class TokenLogprob(BaseModel):
    """Log-probability information for one chosen token.

    Embedded inside :class:`Logprobs`; not a top-level stream event.
    """

    token: str = Field(
        ...,
        description="The chosen token text.",
    )
    logprob: float = Field(
        ...,
        description="Log-probability the model assigned to this token.",
    )
    top_alternatives: list[LogprobAlternative] | None = Field(
        default=None,
        description="Other tokens the model ranked highly at this position, when requested.",
    )


class Logprobs(BaseModel):
    """Per-token log-probability information for a recently emitted text segment.

    Reachable only through :class:`ExtendedEvent`. Emitted by adapters
    whose providers expose token-level logprobs: OpenAI Responses
    (``ResponseTextDeltaEvent.logprobs`` when ``top_logprobs`` is set on
    the request) and Google GenAI (``response_logprobs``). Each entry
    corresponds to one token of the most recent :class:`TextDelta`(s) at
    the same ``index``.

    Adapters whose providers don't expose logprobs leave this stream
    silent.
    """

    type: Literal["logprobs"] = Field(default="logprobs")
    tokens: list[TokenLogprob] = Field(
        ...,
        description="Per-token logprob entries for the recent text segment at 'index'.",
    )
    index: int = Field(
        ...,
        ge=0,
        description="Flat per-block index of the text block these logprobs annotate.",
    )


class SafetyRatings(BaseModel):
    """Per-category safety ratings for the response.

    Reachable only through :class:`ExtendedEvent`. Currently emitted only
    by the Google GenAI adapter, exposing categories like
    ``HARM_CATEGORY_HATE_SPEECH``, ``HARM_CATEGORY_HARASSMENT``,
    ``HARM_CATEGORY_DANGEROUS_CONTENT`` etc. with probability levels
    (``NEGLIGIBLE``, ``LOW``, ``MEDIUM``, ``HIGH``). Adapters whose
    providers don't expose granular safety ratings leave this stream
    silent.
    """

    type: Literal["safety_ratings"] = Field(default="safety_ratings")
    ratings: dict[str, str] = Field(
        ...,
        description="Mapping from harm category to probability level.",
    )
    index: int | None = Field(
        default=None,
        ge=0,
        description="Flat per-block index when the rating applies to a specific output block; None if it applies to the whole response.",
    )


class _ExecutorToolResult(BaseModel):
    """Synthetic event: an agent executor fed a tool result back to the LLM.

    Reachable only through :class:`ExtendedEvent`. The agent executor
    (``primer.agent.AgentExecutor`` / ``WorkspaceAgentExecutor``)
    emits one of these per :class:`ToolResultPart` it sends to the
    LLM so streaming-tap subscribers can render the tool round-trip
    as part of the normal stream channel. Not produced by any LLM
    adapter -- only the executor emits it.

    Lives in this module (rather than ``primer.agent.events``) to
    keep the :data:`ExtendedStreamContent` discriminated union
    self-contained and avoid a chat -> agent module cycle.
    ``primer.agent.events`` re-exports the type for clarity at the
    agent-side import site.
    """

    type: Literal["executor_tool_result"] = Field(
        default="executor_tool_result",
        description="Discriminator tag identifying this as a synthetic executor tool-result event.",
    )
    call_id: str = Field(
        ...,
        min_length=1,
        description="Identifier matching the ToolCallPart whose result this represents.",
    )
    output: str = Field(
        ...,
        description="The tool's output as fed back to the LLM (post-truncation if applicable).",
    )
    error: bool = Field(
        default=False,
        description="True if the tool reported an execution failure or denial.",
    )


class _GraphNodeEvent(BaseModel):
    """Synthetic event: a graph executor forwarded a child node's stream event.

    Reachable only through :class:`ExtendedEvent`. The graph
    executor (``primer.graph.GraphExecutor`` /
    ``WorkspaceGraphExecutor``) wraps every event produced by a
    child agent executor so streaming-tap subscribers can correlate
    by graph node + iteration. Not produced by any LLM adapter --
    only the graph executor emits it.

    Lives in this module (rather than ``primer.graph.events``) for
    the same reason as :class:`_ExecutorToolResult` -- keeping the
    :data:`ExtendedStreamContent` union self-contained and avoiding
    a chat -> graph module cycle.
    """

    type: Literal["graph_node_event"] = Field(
        default="graph_node_event",
        description="Discriminator tag identifying this as a graph-forwarded child event.",
    )
    node_id: str = Field(
        ...,
        min_length=1,
        description="Within-graph node id that produced the wrapped event.",
    )
    iteration: int = Field(
        ...,
        ge=0,
        description="Graph iteration during which the event was produced.",
    )
    inner_type: str = Field(
        ...,
        description="The wrapped event's ``type`` discriminator value.",
    )
    inner_payload: dict[str, Any] = Field(
        ...,
        description=(
            "The wrapped :class:`StreamEvent` serialised as a dict "
            "via ``model_dump(mode='json')``. Subscribers that want "
            "the typed event can re-validate via "
            "``TypeAdapter(StreamEvent).validate_python({type: inner_type, **inner_payload})``."
        ),
    )


ExtendedStreamContent = Annotated[
    RawReasoningDelta
    | RefusalDelta
    | ServerToolCallStart
    | ServerToolCallDelta
    | ServerToolCallEnd
    | Citation
    | Logprobs
    | SafetyRatings
    | _ExecutorToolResult
    | _GraphNodeEvent,
    Field(discriminator="type"),
]
"""Discriminated union of every extended stream-event payload.

Always reached via :attr:`ExtendedEvent.extended`. New extended event types
are added here; consumers gain access to them automatically by matching on
:class:`ExtendedEvent`.
"""


class ExtendedEvent(BaseModel):
    """Wrapper carrying a non-universal stream event.

    The unified :data:`StreamEvent` union exposes only the events every
    adapter is expected to emit (lifecycle, content, tool calls, media,
    usage). Provider-specific events that don't have a clean universal
    equivalent are wrapped in this single envelope so consumers who only
    care about the universal interface can identify and skip them with
    one pattern-match arm.

    Construct as ``ExtendedEvent(extended=Citation(...))``; access the
    payload via ``event.extended``.
    """

    type: Literal["extended"] = Field(
        default="extended",
        description="Discriminator tag identifying this event as an extended-content wrapper.",
    )
    extended: ExtendedStreamContent = Field(
        ...,
        description="The wrapped extended stream-event payload.",
    )


# ---- Top-level StreamEvent union --------------------------------------------


StreamEvent = Annotated[
    StreamStart
    | TextDelta
    | ReasoningDelta
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
    | MediaDelta
    | Usage
    | Done
    | Error
    | ExtendedEvent,
    Field(discriminator="type"),
]
"""One event yielded by a provider's ``stream()`` generator.

Universal members are emitted by every adapter capable of producing the
underlying signal. Provider-specific events are reached through
:class:`ExtendedEvent`; consumers that don't care about extended content
can ignore the entire branch with one pattern-match arm.
"""


# ===========================================================================
# Output → Input conversion
# ===========================================================================


def output_to_message(events: Iterable[StreamEvent]) -> Message:
    """Build an assistant :class:`Message` from a stream of output events.

    The default converter for round-tripping the model's output back into
    the next turn's input history. Lets the caller take a stream of
    :data:`StreamEvent`s, append the resulting :class:`Message` to the
    chat history, append a separate ``tool``-role message carrying the
    :class:`ToolResultPart`s, and re-invoke the LLM with the updated
    history — uniform across every provider adapter.

    Processed events:

    * :class:`TextDelta` — accumulated by ``index`` (multiple deltas
      with the same index concatenate into one :class:`TextPart`).
    * :class:`ToolCallStart` — opens a new :class:`ToolCallPart` keyed
      by ``id``.
    * :class:`ToolCallEnd` — supplies the parsed ``arguments`` dict for
      the matching :class:`ToolCallPart`.
    * :class:`ToolCallDelta` — ignored. The End event already carries
      the fully-parsed arguments dict; replaying the partial JSON
      fragments is unnecessary.

    Ignored events (not round-tripped by default):

    * Lifecycle (:class:`StreamStart`, :class:`Done`, :class:`Error`).
    * Telemetry (:class:`Usage`).
    * :class:`ReasoningDelta` — preserving reasoning across turns is
      provider-specific (Anthropic needs cryptographic signatures,
      Google needs ``thought_signature``, OpenAI needs reasoning items
      with ``encrypted_content``). The default converter drops it; a
      reasoning-aware adapter can post-process the events before
      conversion if needed.
    * :class:`MediaDelta` — produced media (audio bytes, generated
      images) is not typically round-tripped on the input side.
    * :class:`ExtendedEvent` — provider-specific extras (citations,
      logprobs, server-tool lifecycles, refusals, safety ratings, raw
      reasoning) have no universal input mapping.

    Parts in the returned :class:`Message` appear in **first-appearance
    order** — text and tool-call blocks are interleaved according to
    when each new ``index`` (for text) or ``id`` (for tool call) first
    appeared in the stream.

    Parameters
    ----------
    events
        Iterable of stream events. Typically the caller buffers an
        async stream into a list first
        (``events = [e async for e in llm.stream(...)]``) and passes
        that list here.

    Returns
    -------
    Message
        :class:`Message` with ``role="assistant"`` and parts derived
        from the stream.

    Raises
    ------
    ValueError
        If the event stream contains no convertible events (no
        :class:`TextDelta` and no :class:`ToolCallStart`). Callers
        should handle empty or error-only streams before invoking this
        converter.
    """
    text_buffers: dict[int, list[str]] = {}
    tool_call_names: dict[str, str] = {}
    tool_call_args: dict[str, dict[str, Any]] = {}

    # Track first-appearance order with unified keys: ("text", index) or ("tool", id).
    # Insertion-ordered list preserves the order text/tool blocks began streaming.
    parts_order: list[tuple[Literal["text", "tool"], int | str]] = []

    for event in events:
        if isinstance(event, TextDelta):
            if event.index not in text_buffers:
                text_buffers[event.index] = []
                parts_order.append(("text", event.index))
            text_buffers[event.index].append(event.text)
        elif isinstance(event, ToolCallStart):
            if event.id not in tool_call_names:
                parts_order.append(("tool", event.id))
            tool_call_names[event.id] = event.name
        elif isinstance(event, ToolCallEnd):
            tool_call_args[event.id] = event.arguments
        # All other events (StreamStart, Done, Error, Usage, ReasoningDelta,
        # MediaDelta, ToolCallDelta, ExtendedEvent) are intentionally ignored.

    parts: list[Part] = []
    for kind, key in parts_order:
        if kind == "text":
            assert isinstance(key, int)
            parts.append(TextPart(text="".join(text_buffers[key])))
        else:  # "tool"
            assert isinstance(key, str)
            parts.append(
                ToolCallPart(
                    id=key,
                    name=tool_call_names[key],
                    arguments=tool_call_args.get(key, {}),
                )
            )

    if not parts:
        raise ValueError(
            "no convertible events found in stream "
            "(need at least one TextDelta or ToolCallStart)"
        )

    return Message(role="assistant", parts=parts)
