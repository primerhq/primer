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

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator


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
    ``url``, or ``file_id``; the adapter chooses which surface to forward
    based on the target provider.
    """

    @model_validator(mode="after")
    def _require_one_source(self) -> "_BinarySourceMixin":
        if not (self.data or self.url or self.file_id):  # type: ignore[attr-defined]
            raise ValueError(
                "at least one of 'data', 'url', or 'file_id' must be provided"
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
    data: bytes | None = Field(
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
    data: bytes | None = Field(
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
    data: bytes | None = Field(
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
    data: bytes | None = Field(
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
    TextPart | ImagePart | DocumentPart | ExtendedPart,
    Field(discriminator="type"),
]
"""One element of a :class:`Message`'s ``parts`` list.

Universal members (:class:`TextPart`, :class:`ImagePart`,
:class:`DocumentPart`) are the primary surface — adapters should always
prefer one of these when a reasonable mapping exists. Provider-specific
content (audio, video, future additions) is reached through
:class:`ExtendedPart`.
"""


class Message(BaseModel):
    """A single chat message.

    Roles map directly onto each provider: ``user`` and ``assistant`` are
    universal; ``system`` is treated as a system instruction by adapters
    (Anthropic surfaces this via the top-level ``system`` parameter,
    Google via ``system_instruction``).
    """

    role: Literal["user", "assistant", "system"] = Field(
        ...,
        description="Speaker role for this message.",
    )
    parts: list[Part] = Field(
        ...,
        min_length=1,
        description="Ordered content parts that make up this message.",
    )


# ---- Tool definitions and choice -------------------------------------------


class Tool(BaseModel):
    """A function tool the model may invoke during a chat turn.

    Lowest-common-denominator across the four surveyed providers: each
    tool has a name, a free-form description, and a JSON Schema for its
    arguments. Adapters wrap the same shape into provider-specific
    envelopes:

    * OpenAI Responses: ``{type: "function", name, description, parameters}``.
    * Anthropic: ``{name, description, input_schema: parameters}``
      (rename of the parameters key).
    * Google GenAI: nested under
      ``Tool(function_declarations=[FunctionDeclaration(name=..., description=..., parameters_json_schema=parameters)])``.
    * Ollama: older Chat Completions nesting,
      ``{type: "function", function: {name, description, parameters}}``.

    For Pydantic-defined arguments, callers can derive the schema:
    ``parameters=MyArgsModel.model_json_schema()``.
    """

    name: str = Field(
        ...,
        min_length=1,
        description="Unique tool identifier the model uses to invoke it.",
    )
    description: str = Field(
        ...,
        description="Free-form description of what the tool does and when to use it.",
    )
    parameters: dict[str, Any] = Field(
        ...,
        description="JSON Schema describing the tool's argument object.",
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


ExtendedStreamContent = Annotated[
    RawReasoningDelta
    | RefusalDelta
    | ServerToolCallStart
    | ServerToolCallDelta
    | ServerToolCallEnd
    | Citation
    | Logprobs
    | SafetyRatings,
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
