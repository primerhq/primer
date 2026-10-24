# Model Providers

## 1. Purpose

The model-providers subsystem is the adapter layer that converts Primer's universal, provider-agnostic model interfaces into the wire shapes of concrete vendor SDKs. It owns three model families: streaming chat LLMs (`primer/llm/`), text embedders (`primer/embedder/`), and cross-encoder rerankers (`primer/cross_encoder/`). Each adapter binds to one configured provider row at construction time, validates that the row's type and config class match, lazily constructs the underlying SDK client on first use, and translates the universal `Message` / `Tool` / `EmbeddingPart` types into and out of the provider's native format.

Callers (the agent loop, the ingest path, the collections subsystem) depend only on the abstract base classes `primer.int.LLM`, `primer.int.Embedder`, and `primer.int.CrossEncoder`; they never import a concrete adapter. The `ProviderRegistry` (`primer/api/registries/provider_registry.py`) is the single construction seam: it builds and caches one adapter per provider row, threads in the shared `RateLimiter` and `trace_llm_io` flag, and calls `aclose()` when a row is invalidated. This document covers the adapters and their shared contract; the coordinator's distributed rate-limiting and invalidation machinery is documented under the provider-pattern and coordinator design docs and is referenced here rather than restated.

## 2. Conceptual model

A provider row (`LLMProvider`, `EmbeddingProvider`, or `CrossEncoderProvider` in `primer/model/provider.py`) is a configuration record: which backend (`provider` enum), which models are permitted (`models`), the backend-specific connection details (`config`, a discriminated union), and the concurrency budget (`limits`). An adapter is the live object built from that row. The adapter serves every model the row declares; model selection happens per call, validated against `provider.models` with a `ModelNotFoundError` raised when the caller passes an unknown name.

The universal types the adapters translate are defined in `primer/model/chat.py` and `primer/model/embedding.py`: `Message` (a role plus a list of `Part`s), `Tool`, `ToolChoice`, the `StreamEvent` union that `LLM.stream` yields, and `EmbeddingPart` / `EmbedResponse` for embedders. Provider-specific knobs that do not map cleanly across vendors ride in an open-ended `extended: dict[str, Any]` slot the adapter interprets and silently drops unknown keys from.

```mermaid
erDiagram
    LLMProvider ||--|| LLMProviderConfig : "carries (union)"
    LLMProvider ||--o{ LLMModel : "permits"
    LLMProvider ||--|| Limits : "budgets"
    LLMProvider ||--|| LLM : "built into (cached)"
    EmbeddingProvider ||--|| EmbeddingProviderConfig : "carries (union)"
    EmbeddingProvider ||--|| Embedder : "built into (cached)"
    CrossEncoderProvider ||--|| CrossEncoder : "built into (cached)"
    LLM ||--|| RateLimiter : "acquires slot from"
    LLM ||--|| count_tokens_module : "delegates token count to"
    ProviderRegistry ||--o{ LLM : "constructs + caches"
    ProviderRegistry ||--o{ Embedder : "constructs + caches"
    ProviderRegistry ||--o{ CrossEncoder : "constructs + caches"
```

Six LLM backends ship today (`primer/model/provider.py` `LLMProviderType`): `openresponses`, `openchat`, `gemini`, `anthropic`, `ollama`, `openrouter`. Three embedder backends (`EmbeddingProviderType`): `huggingface`, `openai`, `gemini`. One cross-encoder backend (`CrossEncoderProviderType`): `huggingface`. The package re-exports five LLM adapters in `primer/llm/__init__.py.__all__` (`AnthropicLLM`, `GeminiLLM`, `OllamaLLM`, `OpenChatLLM`, `OpenResponsesLLM`); `OpenRouterLLM` is importable from `primer/llm/openrouter.py` but is not in `__all__`.

## 3. Architecture patterns implemented

- **Thin ABC, fat adapter.** `primer.int.LLM` declares `stream`, `count_tokens` and `aclose` as abstract, plus `count_tokens_detailed` with a default (see below) -- it lost `list_models` when `ModelProfile` rows became the registry of what an LLM provider serves; `primer.int.Embedder` declares three (`list_models`, `embed`, `aclose`) and keeps its own `models[]`. `aclose()` defaults to a no-op on both bases so adapters that hold no resources inherit cheaply. The signatures were derived from the cross-SDK comparison in `research/abc_interface.md` and `research/embedding_interface.md`.
- **Validate-then-stream construction.** Every adapter `__init__` checks `provider.provider` against its expected enum and `isinstance(provider.config, …)` against its expected config class, raising `ConfigError` on a mismatch. See `AnthropicLLM.__init__`, `HuggingFaceEmbedder.__init__`.
- **Lazy SDK client.** The SDK client (`AsyncAnthropic`, `AsyncOpenAI`, `google.genai.Client`, `ollama.AsyncClient`, `SentenceTransformer`) is constructed on first use via `_get_client()` / `_get_model()` and cached on the instance, so importing or constructing an adapter never opens a connection.
- **Shared `RateLimiter` instead of a per-adapter semaphore.** Concurrency is mediated through an injected `RateLimiter` (`primer.int.coordinator`) keyed `llm:{provider.id}` / `embedder:{provider.id}` / `cross_encoder:{provider.id}` with `max_concurrency` from `provider.limits`. When no limiter is injected the adapter falls back to `primer.coordinator.in_memory.InMemoryRateLimiter`. `tests/llm/test_adapters_no_local_semaphore.py` is a source-level pin asserting no adapter constructs its own `asyncio.Semaphore`.
- **Shared per-SDK exception classifiers.** `primer/common/openai_errors.py`, `anthropic_errors.py`, `google_errors.py`, and `mcp_errors.py` each expose one `classify_*_exception` function mapping the vendor SDK hierarchy onto the `primer.model.except_.PrimerError` tree. Adapters that wrap the same SDK (OpenResponses, OpenChat, OpenRouter, OpenAIEmbedder all wrap `openai`) share one classifier so the universal error surface does not drift.
- **Shared OpenAI-family helper modules.** `primer/llm/_openai_common.py` (`build_sampling_params(target=…)`) is shared by the Responses and Chat Completions adapters; `primer/llm/_openai_compat.py` (request/response shaping for Chat Completions) is shared by `OpenChatLLM` and `OpenRouterLLM`. Both modules are pure and do no network IO.
- **Flavor strategy table over enum-per-flavor.** OpenAI-compatible adapters carry a frozen `_FlavorPolicy` dataclass selected from a `_POLICY_BY_FLAVOR` table keyed by the config's flavor enum, so a new OpenAI-compatible server lands as one table row rather than a new adapter class.
- **Native token counters are groundwork, not yet on a turn path.** `count_tokens` is a mandatory abstract method backed by per-provider modules under `primer/llm/_tokenizer/`, and `primer.llm.counting.count_prompt_tokens` is the one wrapper meant to call it (see "Token counter contract" below). Nothing in the live turn path calls either yet: agent compaction still decides on a character heuristic (`CompactionStrategy.maybe_compact`), and `compaction_mixin.should_compact`, the only caller, has no production caller.
- **Observability woven into every stream.** Each `stream()` opens an OpenTelemetry span `llm.stream` and increments Prometheus counters (`llm_tokens_total`, `llm_failure_total`, `llm_duration_seconds`); the cross-cutting instrumentation is identical across adapters.

## 4. Code layout

| Path | Responsibility |
| --- | --- |
| `primer/int/llm.py` | `LLM` ABC: `stream`, `count_tokens`, `count_tokens_detailed` (default), `aclose`. |
| `primer/int/embedder.py` | `Embedder` ABC: `list_models`, `embed`, `aclose`. |
| `primer/int/cross_encoder.py` | `CrossEncoder` ABC for rerankers. |
| `primer/int/coordinator.py` | `RateLimiter` / `RateLimiterLease` ABCs and the `Coordinator` bundle. |
| `primer/coordinator/in_memory.py` | `InMemoryRateLimiter` fallback used when no limiter is injected. |
| `primer/model/provider.py` | Provider entities, provider-type enums, flavor enums, config classes, the `LLMProvider._coerce_config_to_provider` validator. |
| `primer/model/except_.py` | `PrimerError` hierarchy every classifier maps into. |
| `primer/llm/__init__.py` | Re-exports the five named LLM adapters. |
| `primer/llm/anthropic.py` | `AnthropicLLM` over `anthropic.AsyncAnthropic` Messages API. |
| `primer/llm/gemini.py` | `GeminiLLM` over `google.genai` `generate_content_stream`. |
| `primer/llm/ollama.py` | `OllamaLLM` over `ollama.AsyncClient` chat. |
| `primer/llm/openresponses.py` | `OpenResponsesLLM` over the OpenAI Responses API. |
| `primer/llm/openchat.py` | `OpenChatLLM` over the OpenAI Chat Completions API. |
| `primer/llm/openrouter.py` | `OpenRouterLLM` over the OpenRouter gateway (Chat Completions wire). |
| `primer/llm/_openai_common.py` | `build_sampling_params(target=…)` shared by Responses + Chat Completions. |
| `primer/llm/_openai_compat.py` | Chat Completions request/response shaping shared by OpenChat + OpenRouter. |
| `primer/llm/_trace.py` | `_serialize_messages` for the `trace_llm_io` span payload. |
| `primer/llm/_tokenizer/` | Per-provider token counters (`anthropic`, `gemini`, `hf`, `openai`, `char_fallback`), the offline tiktoken loader (`_tiktoken_offline`), the counter executor (`_executor`) and the vocabulary pins (`vocab_pins`). |
| `primer/llm/counting.py` | `count_prompt_tokens`: the one wrapper that owns timeout, fallback, labelling and negative caching. |
| `primer/model/token_count.py`, `primer/model/media_tokens.py` | `TokenCount` (a count plus what stands behind it) and the shared flat estimates for media blocks. |
| `primer/common/openai_errors.py` | `classify_openai_exception`, shared by every `openai`-SDK adapter. |
| `primer/common/anthropic_errors.py` | `classify_anthropic_exception`. |
| `primer/common/google_errors.py` | `classify_google_exception`. |
| `primer/llm/_failure.py` | `describe_failure`, `provider_label`, `scrub`: the message of a classified model-call failure. |
| `primer/embedder/openai.py` | `OpenAIEmbedder` (flavors OPENAI / LMSTUDIO / OTHER). |
| `primer/embedder/gemini.py` | `GeminiEmbedder` over `google.genai` `embed_content`. |
| `primer/embedder/huggingface.py` | `HuggingFaceEmbedder` over local sentence-transformers. |
| `primer/embedder/_prompts.py` | Model-family query/document prompt registry (BGE, E5, nomic-embed-text), shared by HuggingFace + OpenAI embedders. |
| `primer/cross_encoder/huggingface.py` | `HuggingFaceCrossEncoder` local reranker. |
| `primer/api/registries/provider_registry.py` | `ProviderRegistry`: per-row adapter cache, factory dispatch, rate-limiter binding, invalidation, `aclose`. |
| `primer/api/routers/providers.py` | Provider CRUD plus the `_discover_models` live-probe endpoint and the draft `_test` routes of the embedding and cross-encoder classes. |
| `primer/common/hf_hub.py` | `check_hf_models`: the Hugging Face Hub model check (`HfModelCheck`, `hf_endpoint`, `HF_REPO_ID_RE`). |
| `primer/api/routers/hf_hub.py` | `POST /v1/huggingface/_check_models`, the route over the Hub check. |
| `primer/toolset/_system_probes.py` | The `system` tools that probe a provider before it exists and store nothing: `check_huggingface_models`, `test_embedding_provider`, `test_cross_encoder_provider`. |
| `ui/components/provider-catalog.jsx` | The console's Providers page: the catalogue of every provider class, opening the Register provider overlay. |
| `ui/components/provider-form.jsx` | The one parameterized provider form, drawn from each class's `_types` answer. |
| `ui/components/providers/` | The Register provider overlay (`register-overlay.jsx`) and its per-class pages; `register-llm.jsx` registers an LLM provider and imports its models as LLM profiles. |

## 5. Data model

### Limits

`Limits` (in `primer/model/provider.py`) carries two fields:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `max_concurrency` | `PositiveInt` | required | Maximum in-flight requests held at once via the shared `RateLimiter`. |
| `request_timeout_seconds` | `float \| None` | `300.0` | Per-event inactivity timeout for LLM streaming calls. If no event arrives within this window the stream is aborted with `ProviderTimeoutError`. `None` disables it. See section 9 for enforcement details. |

### Provider entities

`LLMProvider`, `EmbeddingProvider`, and `CrossEncoderProvider` (`primer/model/provider.py`) all extend `Identifiable` and carry `provider` (the backend enum), `models` (a non-empty list of `LLMModel` / `EmbeddingModel` / `CrossEncoderModel`), `config` (a discriminated config union), and `limits` (a `Limits` with `max_concurrency` and `request_timeout_seconds`).

The `LLMProvider.config` union is `OpenResponsesConfig | OpenChatConfig | GoogleConfig | AnthropicConfig | OllamaConfig | OpenRouterConfig`. Because `OpenResponsesConfig` and `OpenChatConfig` share the `_HttpApiKeyConfig` shape and overlapping flavor values (`openai`, `other`), Pydantic's first-match-wins union dispatch would silently coerce an `openchat` row into `OpenResponsesConfig`. The `_coerce_config_to_provider` model-validator (mode `before`) defends against this by selecting the concrete config class from a `provider`-enum lookup before validation runs. `OpenRouterConfig` is not an `_HttpApiKeyConfig` subclass; it sets `ConfigDict(extra="forbid")`, hard-codes the base URL (no `url` field), requires `api_key`, and adds optional `app_name` / `app_url` attribution fields. The two defenses (validator plus `extra="forbid"`) are the same union-disambiguation problem at two layers. `EmbeddingProvider.config` (`OpenAIConfig | HuggingFaceConfig | GoogleConfig`) has the same `_coerce_config_to_provider` before-validator (ticket 01a11cdf): without it an `openai` row whose url was invalid or missing failed `OpenAIConfig` and `HuggingFaceConfig` (no token) and validated as a `GoogleConfig` with the url DROPPED (a row with no endpoint could be saved, and `_discover_models` sent the raw typed string to httpx), and a `huggingface` row with only a url validated as an `OpenAIConfig` (pydantic's smart mode keeps the member that sets the most fields). Keyed by the provider, the error is the config class's own, at the field (its loc is the field alone, `url`, as the LLM validator's is), and a config OBJECT of another class is refused by name. `HuggingFaceConfig.token` is OPTIONAL (default `None`, as `HuggingFaceCrossEncoderConfig.token`): a public local model such as `all-MiniLM-L6-v2` needs no token, `config: {}` is a valid huggingface embedder, and the adapter passes no token to `SentenceTransformer` for a missing or empty one (the validator first enforced the field's old `Field(...)` at create, where main never validated the config, and broke every client that creates a local embedder with `config: {}`; found by the e2e job). Rows stored that way before the validator existed are repaired at boot by migration 8 (`primer/storage/migrations/m008_embedding_provider_repair.py`): an `openai` row without a usable url keeps its key and gets the base URL `https://base-url-not-set.invalid/` (reserved `.invalid` domain: it says what to fix; https because the row keeps its stored key and a use sends `Authorization: Bearer <key>`, which over plain http would go to whatever resolves the name (a k8s search list, an NXDOMAIN-hijacking resolver), while no CA can issue a certificate for `.invalid`, so the TLS handshake cannot succeed and fails before any header is sent; the row is kept, not deleted, because a collection may name it), and a row that already validates is not touched (a `huggingface` row of main's shape, a stray url and key, reads fine: the HuggingFace token is optional, see below); without the repair one such row answered 500 on the whole `GET /v1/embedding_providers` and on get, put and delete by id. The log names the row id and provider, never the config.

**A Base URL's password is masked like a secret** (ticket 01a11cdf part 3). A provider `url` may carry `user:password@` (a reverse proxy in front of the server): httpx sends it as Basic auth. The `url` of an LLM (`_HttpApiKeyConfig`: openresponses and openchat; `OllamaConfig`), embedding (`OpenAIConfig`) and speech (`SpeechToTextConfig`, `TextToSpeechConfig`) config is a `MaskedUserinfoUrl` (`primer/model/masked_url.py`, re-exported by `primer/model/providers/_shared.py`; the workspace template's `kind=url` file source uses it too, ticket 01a11d32, see `workspaces.md`): an `HttpUrl` whose JSON-mode dump writes `http://svc:**********@host/v1` (`primer/common/url_userinfo.py` `mask_userinfo`: the username stays only when a password is present, a lone userinfo such as `https://TOKEN@host` is masked whole, scheme, host, port, path, query and fragment are never touched; known limit: a token kept in the USERNAME slot with a dummy password, `https://<token>:x-oauth-basic@github.com/`, therefore shows the token). So `GET`, the list, the `POST`/`PUT` responses, the CRUD events (`redact_payload` masks a string leaf that is a URL with userinfo (a password that holds whitespace included, which `httpx` accepts), and a credentialed URL inside a longer text through `redact_url_secrets`; events written before are served masked too, they are redacted on read, while the stored rows and the session transcripts keep what they hold) and the system tools' results (`get_llm_provider`, `create_*`, `update_*`) serve it masked, while a python-mode dump and the object itself keep the real URL (the adapters and the probes read those; the saved-provider probe reads the stored row) and `dump_for_storage` keeps it for the row (`STORAGE_DUMP_CONTEXT`, and only that exact context: another key, `{"storage": False}` or a non-dict masks). The serializer is registered `when_used="always"` with NO `return_type` and no return annotation, and returns the value unchanged for a python-mode dump: a `return_type=str` serializer made pydantic 2.13 warn on every python-mode dump (`PydanticSerializationUnexpectedValue`, with the URL and its password in the text, to stderr and past the log filter; `discover_saved_llm_models` does that dump on every `GET /v1/setup/state`), and an `Any` annotation would turn the field into `{}` in the OpenAPI document (a test pins the schema as a uri string). `preserve_masked_secrets` puts the stored credential back when a full-replace `PUT` sends the served mask back (`restore_userinfo`), and ONLY for the URL it was served for: the scheme, host and port equal the stored URL's and the username is the same (a changed path or query with the mask left alone keeps the password; the default port spelled out is the same origin). A real new password and a removed credential carry no mask and are the person's change. Any URL that still carries the mask and cannot be restored (another host, port, scheme or username, a stored URL with no credential of that kind, a provider type that changed so there is no stored config of that shape) is REFUSED with a 422 `url: re-enter the password: the stored one is kept only for the same host and user` (the system update tool answers the same as a `validation-error` result): the stored credential is never given to a host the update names (an admin-started agent run can call `update_llm_provider`), and the literal mask is never stored as the password. A restored URL over the 2083-character limit is a 422 too. **A masked secret is kept only for the origin it was stored for** (ticket 01a1212a, the twin of the rule above for a secret): `preserve_masked_secrets` restores a stored `SecretStr` from a served mask only while the config still points at the origin it was stored for, so an update of `{url: "https://attacker.example/v1", api_key: "<the mask>"}` can no longer store the real key next to another host (an admin, or an agent run an admin started, can call `update_llm_provider`, `update_embedding_provider` and the speech and artifact tools, and the next probe or call would send the key there). A model's origin is the scheme, host and port of its `url` / `base_url` / `endpoint_url` / `apiserver_url` / `discovery_url` / `git_url` / `resource_uri` / `token_endpoint` (`_ORIGIN_URL_FIELDS` in `primer/model/common.py`, compared by `origin_of` in `primer/common/origin.py`: the case of scheme and host, the default port spelled out, the path, the query and the userinfo do not count, and a value that is not a URL is its own origin, and so is one whose authority holds a backslash or whitespace, because parsers disagree about its host; the authority is judged twice, on the text and on the netloc `urlsplit` itself read, and a value that holds a tab, CR or LF anywhere is compared as text, because a tab in the scheme separator (`https:<TAB>//attacker.example\@home.example/x://y`) put the first `://` in the path while `urlsplit` read the stored host and a WHATWG client read `attacker.example`), or the host and `port` of its `hostname`; a model nested in it (an MCP toolset's headers and OAuth client secret under its `url`) is bound to the origin of the model it sits in, through a flag threaded down the nested models, lists and dicts. A served mask under another origin is REFUSED with a 422 `api_key: re-enter the secret: the stored one is kept only for the same origin (scheme, host and port)` (a `validation-error` result from the system tools), naming no secret, for EVERY caller, an admin included: a run an admin started is an admin caller, so "an admin may repoint a toolset and keep its secrets" is gone and an admin who moves the host re-enters the secrets (a changed path keeps them). A key the person typed is theirs and is stored as sent wherever the URL points; an endpoint that appears or goes away counts as a move; a model with no origin (a Hugging Face token, the web-search keys) is unaffected. The families pinned in `tests/model/test_masked_secrets_are_bound_to_their_origin.py` are the LLM, embedding and speech configs (`url` + `api_key`), the S3 artifact store (`endpoint_url`), the Kubernetes service account (`apiserver_url`), a harness and its dependencies (`git_url`), an OIDC provider (`discovery_url`), an HTTP MCP toolset (`url`) and the Postgres-shaped configs (the vector stores and the storage provider: `hostname` + `port`); any other model that holds a secret beside such a field follows the same rule. Where there is no stored value of the same shape to restore from (the config class changed, a dict key is new, a list changed length) a served mask, a password or a key, is REFUSED too; a mask for a field of the same class that held no secret stays the accepted limit of `preserve_masked_secrets` (it is stored as sent). **The OIDC providers are not written through `preserve_masked_secrets`**: their router runs `_preserve_client_secret_if_blank`, which treats the mask, `""`, `null` and an omitted `client_secret` as "unchanged", and it now restores the stored secret only for the same `discovery_url` origin (the login flow POSTs `client_id:client_secret` as Basic auth to the `token_endpoint` of the document it fetches, so a blank secret under another host would have handed the secret to it): a blank secret under another scheme, host or port is a 422 `client_secret: re-enter the secret: ...` and the row is untouched, the same origin with another path keeps it, a typed secret is stored (`tests/api/test_oidc_client_secret_origin.py`). Four entries of the family table are NOT written through the helper, and the table's `HOW` map says what writes each: the OIDC provider and the harness git token are written by a route with a rule of its own (the harness `PUT` runs `apply_git_token_update`, which compares `git_url` exactly and is stricter), and a dependency's git token (a `DependencyRef` comes only from `harness.yaml`) and the storage provider (it comes from the config file) by no route at all; a test fails if an entry has no `HOW` line. **A create refuses a served mask**: `POST` (`make_crud_router`) and `create_*` call `refuse_served_masks` before the create hooks (so do the hand-written creates: the workspaces toolset's `create_workspace_provider` / `create_workspace_template` handler, `POST /v1/harnesses` and the harness toolset's `harness__register`, and `POST /v1/workspaces` / `create_workspace` for `overrides.env`, which the workspace row keeps and serves masked; `create_python_toolset` takes a source and a timeout and no secret), so a body that carries a mask a GET serves, as a URL's `**********` password or as a secret (`**********` or `**********` and the last four characters), is a 422 / `validation-error`; the copy-a-provider move (`get_*` and then `create_*` under a new id) used to store the masks as the values. Never dump a provider row with `serialize_as_any=True` or `SerializeAsAny`: pydantic 2.13 skips a field's own serializer then, so the URL password and the `api_key` would be served in clear (a static test fails on it). Anything that fingerprints or compares a provider row must use the storage form: the served form is the same for two URLs that differ only by password (nothing in `primer/` does; checked). The console's Test button on an UNSAVED draft sends what is typed, so a draft opened from a saved row with the masked URL probes with the mask, as it does with a masked `api_key`; the saved-provider probe is the one that authenticates. The remaining URL fields that can carry credentials are now the `str` flavour of the same rule (ticket 01a1294c, the 01a11d32 remainder): the MCP toolset's `HttpConfig.url` and the OIDC provider's `discovery_url` are a `MaskedUserinfoStr` (a plain `str` with the same `_serialize_url` serializer, `primer/model/masked_url.py`: these URLs are compared and keyed as typed, and they are not necessarily `HttpUrl`s, a git URL among them being a `file://` under the operator opt-in), OpenRouter's `app_url` is a `MaskedUserinfoUrl | None`, and the harness `git_url` and its resolved dependencies are the `MaskedGitUrl` alias of that `str` flavour (see `harness.md`). A served mask on any of them is restored, or refused with a 422 re-enter-the-password, by the same origin rule as the Base URLs above, name-gated to the fields in `_ORIGIN_URL_FIELDS`; the OIDC router restores `discovery_url` in its `_on_pre_update` (through `restore_served_masked_url`) before the client-secret preserve, and the toolset's repoint check restores before it compares the endpoint, so a same-origin `url` mask round-trips to the stored URL and is not a move (a refusal there still counts as a repoint).

Across the HTTP-keyed configs `api_key` is `Optional[SecretStr]` defaulting to `None`, so operators can register endpoints fronted by an auth-injecting proxy; a real provider that needs the key surfaces a 401 at call time rather than the schema rejecting the row. `OpenRouterConfig.api_key` is the exception (required), since OpenRouter is always remote and authenticated. The `EmbeddingProvider.config` union is `OpenAIConfig | HuggingFaceConfig | GoogleConfig`; `GoogleConfig` backs both the Gemini LLM and the Gemini embedder.

Flavor discriminators live on the config rather than the provider enum: `OpenResponsesFlavor` (OPENAI / LMSTUDIO / OTHER), `OpenChatFlavor` (OPENAI / LMSTUDIO / OLLAMA / VLLM / OTHER), `OpenAIEmbeddingFlavor` (OPENAI / LMSTUDIO / OTHER). The adapter resolves each flavor to a frozen `_FlavorPolicy`; only `require_api_key` is consulted at runtime today (the OpenResponses policy also carries `drop_encrypted_reasoning` and `expect_reasoning_under_store_true` as forward-compat scaffolding).

`require_api_key` is `True` for the OPENAI flavor only. Every other flavor, including the OTHER catch-all, tolerates a missing key and lets the upstream 401 surface at call time, which is the same contract the schema-level `Optional[SecretStr]` expresses. OTHER is the flavor an operator lands on for any OpenAI-compatible server that is not explicitly modelled, and that population is dominated by self-hosted backends (vLLM, SGLang, llama.cpp, TGI, LiteLLM) which are commonly unauthenticated, so "unknown server" cannot be read as "authenticated server". This matters most on the Responses API, where OTHER is the only flavor available for a non-LM-Studio self-hosted endpoint.

The streaming surface is a small state machine. `LLM.stream` is an async generator that always yields exactly one terminal event. A per-stream `_StreamState` dataclass tracks request id, model, accumulated tool-call argument fragments, token counts, and the final stop reason; a pure `_translate_event` / `_translate_chunk` dispatch maps each SDK event onto the universal `StreamEvent` union (`primer/model/chat.py`).

```mermaid
stateDiagram-v2
    [*] --> StreamStart : first SDK event
    StreamStart --> Streaming
    Streaming --> Streaming : TextDelta / ReasoningDelta / ToolCallStart / ToolCallDelta / ToolCallEnd / MediaDelta / ExtendedEvent
    Streaming --> Usage : terminal SDK event with token counts
    Usage --> Done : success
    Streaming --> Done : success (no usage)
    Streaming --> Error : mid-stream exception (fatal=True)
    Done --> [*]
    Error --> [*]
```

### Aggregated model profiles

Aggregation lives on the profile row, not on a provider kind: a
`ModelProfile` with `kind: "aggregated"` names two or more member profile
ids in `members`, and the order is the failover chain. The row carries no
model of its own - `provider_id`, `model_name` and `context_length` are
null on it - and `resolve_model` (`primer/model_profile/resolver.py`)
reports the SMALLEST context length over the resolved members as the
profile's window, so a caller never overpromises it. (The rest of the row
shape, including `config` and `harness_id`, is the same as a single
profile's; see "Model profiles" below.)

The three routing fields ride on the row with these defaults (from
`primer/model/model_profile.py`):

- `strategy`: `sequential` (default; always start at members[0]) or
  `round_robin` (rotate the starting member once per stream call;
  per-process, load-spreading).
- `failover_point`: `before_first_token` (default; never re-emits tokens)
  or `mid_stream` (opt-in; may duplicate already-shown tokens).
- `failover_on`: `transient` (rate-limit / 5xx / timeout / network) or
  `transient_and_config` (default; also auth / bad-request).

Every write is checked at WRITE time by the `model_profiles` router and by
the `create_model_profile` / `update_model_profile` system tools, which
share `primer/model_profile/checks.py`; each refusal is a 422 carrying
`{error, field, message}` (the tools answer `validation-error` with the
same codes):

- `aggregation_too_small` (field `members`): fewer than two members.
- `self_reference` (field `members`): the profile names itself.
- `duplicate_member` (field `members`): a member id appears twice.
- `member_not_found` (field `members`): a member id names no stored
  profile.
- `nested_aggregation` (field `members`): a member is itself
  `kind="aggregated"`.
- `member_of_another_aggregate` (field `kind`, on update): the row becomes
  an aggregate while another aggregate still lists it as a member.
- `provider_not_found` (field `provider_id`): a single profile's provider
  names no stored LLMProvider.

**Who may read the profiles.** The `model_profiles` router and the
`model_profile` system tools are admin-only, because a profile is provider
configuration. An agent must name a profile and any signed-in user may write
one, so `GET /v1/model_profiles/choices` (user tier, included before the CRUD
router so the literal path is not read as a profile id) and the user-tier
`system::list_model_profile_choices` tool list every profile as a
`ModelProfileChoice`, `{id, description, kind, context_length}`, sorted by id
and paged like the other lists (`limit` with `offset` or `cursor`). Both read
through `primer/model_profile/choices.py`, so they cannot disagree. The
provider, the model name, the members and the config are never served
(audit G3-02); `context_length` is the stored one, null for an aggregated
profile. The console's agent form fills its Model profile dropdown from this
route for every role. A profile whose id is literally `choices` is not
reachable through `GET /v1/model_profiles/{id}` (the list, find and the
tools still reach it).

`AggregatedLLM` (`primer/llm/aggregated.py`) is the adapter an aggregated
profile resolves to. It resolves each member per `stream` call through the
`resolve_member` callable (bound by `resolve_llm` to itself, so a member
resolves recursively by profile id, and editing a member is picked up on
the next call), and the `ProviderRegistry` keeps ONE `AggregatedLLM`
instance per profile id, so a `round_robin` cursor persists across calls;
the router's update and delete hooks drop that instance
(`invalidate_aggregated_llm`) so the next resolve rebuilds it against the
row's current shape. `AggregatedLLM.aclose()` is a no-op: downstream
adapters are owned and cached by the registry.

**Two failover channels.** A member surfaces a failure two ways, and the
aggregated adapter handles each:

- Connect phase (before the first event): the member RAISES a typed
  exception. Matched by class - the reliable channel.
- Mid-stream (after at least one event): the member usually YIELDS a
  terminal `Error(fatal=True)`, but a mid-stream timeout is RAISED instead.
  The adapter inspects the FIRST event before forwarding anything, so a
  first-event fatal error fails over without emitting tokens. Yielded errors
  carry a `code` that is often null for transient failures, so yielded-error
  eligibility is best-effort by code (known timeout codes and null are
  always eligible; other codes are eligible only under
  `transient_and_config`).

**The commit point is the first event of any kind.** The failover window
closes at the first event the member yields, including a `StreamStart`. In
practice rate-limit and auth failures RAISE at connect, before any event,
so they fail over cleanly; a fatal error that arrives only after the first
event is surfaced (default mode) rather than retried.

**Class coarsening on the yielded channel.** Because the classify_*
functions erase the exception class to `code=null` for rate-limit, server,
auth, and network failures, the yielded channel cannot honor the
`transient` policy's "exclude auth" guarantee: a null-code fatal error is
treated as eligible under either policy. This is safe because the
yielded-error failover window ends before any token is emitted, and auth
failures also RAISE at connect (where the class is preserved and `transient`
correctly excludes them). The all-members-failed error is an aggregated
`RateLimitError` whose message lists each member and its failure class;
that per-member class summary is the coarsened record of what went wrong.
The one exception is a pool in which EVERY member failed because the prompt
did not fit the context window (`primer.common.context_overflow`): that is
not "rate limited, retry later", and the executor's overflow recovery keys on
it, so the last member's own `BadRequestError` is re-raised instead (for a
member that YIELDED its 400, one is built from the `Error`). Any other mix of
failures, a rate limit or a missing member among them, stays the aggregated
`RateLimitError`. An overflow on a single member still fails over to the next.
Because the re-raised `BadRequestError` is what the executor's hard-overflow
recovery catches, an aggregated pool takes the same recovery as an eager
adapter: the replay continues the turn, so no tool runs twice (see
`docs/dev/subsystems/agents.md`).

**Safety constraint.** Clean failover is only possible before the first
non-terminal event reaches the subscriber; retrying after partial output
was streamed would re-emit shown tokens. That is why `before_first_token`
is the default. Under `mid_stream`, both a yielded fatal eligible Error and
a raised eligible exception (e.g. a mid-stream timeout) after tokens were
shown restart on the next member, and the already-streamed tokens may be
duplicated in the output; this is the documented trade-off of the mode.

**No nesting.** A member that resolves to another aggregated profile (or
to itself) is refused at write time by the router and the system tools
(`nested_aggregation`, `member_of_another_aggregate`, above) and refused
again at resolve time with `BadRequestError`.

### Model profiles

`LLMProvider.models[]` no longer exists. What a provider can serve is
expressed by :class:`~primer.model.model_profile.ModelProfile` rows
pointing at it, each naming one `(provider, model)` pair plus its
API-level config. Several profiles may share a model name; that is the
point, and it is what lets one model be registered twice with different
reasoning settings. An Agent references a profile id, and session
create may override it per run.

**Every call made under a profile applies its config.** A profile's
`config` carries `reasoning` (a vendor-neutral level: `off`, `minimal`,
`low`, `medium`, `high`, or null for the vendor default) and `extended`
(provider-specific knobs, passed through). Every model call made under a
profile sends it: each call of an agent turn (`primer/agent/loop.py`, which
sessions, subagents and graph agent nodes all run) and each call of both
compaction summarisers (`primer/agent/compaction.py`), an in-turn compaction
and the manual Compact route (`POST /workspaces/{ws}/sessions/{sid}/compact`,
which passes the resolved profile's `config` through `force_compact` and
`apply_compaction` in `primer/agent/compaction_mixin.py`). The call site hands
the resolved profile's config (`ResolvedModel.config`) and the call's model
to `profile_extended(llm, config, model=...)` in `primer/llm/_reasoning.py`,
which asks the adapter to translate it (`LLM.profile_extended(config,
model=...)`): the right shape is a property of the model as well as of the
provider, so each adapter's mapping in `_reasoning.py` takes its flavor and
the model. The result goes out as `stream(extended=...)` through
`profile_stream`. A call with no config, or a config that asks for nothing,
sends no `extended` at all, exactly as before. The adapter's whitelist then
projects `extended` onto the SDK request and DEBUG-logs any key it does not
know. On a key collision the typed `reasoning` level wins over the same key
in `extended`. `RetryingLLM` forwards the translation to the adapter it
wraps; an adapter with no mapping forwards `extended` and logs the level
once at DEBUG as not applied. The approval gate's LLM judge is not a profile
call (its policy names a provider and a model), so no profile config applies
to it.

**A setting never fails a call.** A model table can be wrong or behind, and
a level can be set on a model that cannot honour it, so `profile_stream`
guards stream open. A refusal is a `BadRequestError` raised before the first
event by an eager adapter, or a fatal first event, which is how the
lazily-opened Ollama and Gemini adapters report a 400; an OpenAI-compatible
server's 422 (vLLM rejecting a body field) counts like a 400, because
`classify_openai_exception` maps it to `BadRequestError` too. A refusal
counts only when its message names a setting that call actually sent: the
setting's key, its wire alias, or that setting's own refusal wording, as
listed in `_SETTING_WORDS` (for example `reasoning.effort` for
`reasoning_effort`, `budget_tokens` for `thinking`, "does not support
thinking" for Ollama's `think`). So a 400 about something else (OpenAI's
moderation text, which links to its reasoning guide) is never retried, and
neither is a profile that sent only a `seed` on a 400 that mentions
thinking. When a refusal counts, the call is made once more exactly as it
would be without the profile's settings, and the refusal is logged once at
DEBUG. When that retry works, the adapter instance (a weak key, never its
`id()`) calls that model without those settings for the rest of the
process, so a refused setting costs one failed request, not one per call;
when the retry fails too, nothing is remembered and the failure is the
call's own. A failure after the first event is never
retried, because output has already reached the consumer. Where a model has
no control for a level at all, the mapping sends nothing and logs that once
at DEBUG.

**Per model.** `ModelProfileConfig.reasoning` is mapped per adapter and per
model in `primer/llm/_reasoning.py`, the same normalisation the adapters
already do for stop reasons. Where a model has no true off, the closest
setting is used:

| Adapter, model | Wire shape | OFF sends |
| --- | --- | --- |
| `anthropic`, Claude 4.5 and earlier (Sonnet 3.7, Opus 4 to 4.5, Sonnet 4 and 4.5, Haiku 4.5) | `thinking: {type: enabled, budget_tokens}` (MINIMAL 1024, LOW 4096, MEDIUM 16384, HIGH 32768), never `effort` | `thinking: {type: disabled}` |
| `anthropic`, Claude Opus 4.6, Sonnet 4.6 | `thinking: {type: adaptive}` and `output_config.effort` (MINIMAL and LOW `low`, MEDIUM `medium`, HIGH `high`) | nothing (thinking is off when omitted) |
| `anthropic`, Claude Opus 4.7, 4.8 and 5, Sonnet 5, Haiku 5.5 | adaptive and `output_config.effort` | `thinking: {type: disabled}` |
| `anthropic`, Claude Sonnet 5.5 | adaptive and `output_config.effort` | `thinking: {type: between_tools}` (it refuses `disabled`) |
| `anthropic`, Claude Opus 5.5, Fable, Mythos | adaptive and `output_config.effort` | no `thinking`, and `output_config.effort: low` (thinking cannot be turned off) |
| `anthropic`, Claude 3.5 and earlier, or a model not in the table | nothing | nothing |
| `gemini`, 2.5 | `thinking_config.thinking_budget` within the model's range (Pro 128 to 32768, Flash up to 24576, Flash-Lite 512 to 24576) | `0`; on 2.5 Pro, which cannot turn thinking off, its lowest budget `128` |
| `gemini`, 3 and later | `thinking_config.thinking_level` (never a budget, never both), clamped to the levels the model documents: a level it lacks becomes the nearest it has, a tie going to the higher (3 Pro has only `low` and `high`, so MEDIUM sends `high`; 3.7 and 3.8 Flash and 4 Argon have no `minimal`; the 3.1 Flash-Lite image model has `minimal` and `high`, a Pro image model only `high`); a model not in the table is taken to have `low`, `medium` and `high`; each clamp is logged once at DEBUG | the model's lowest level: `minimal` where it has one (3 Flash, 3.5 Flash-Lite, 3.6 Flash), otherwise `low` (3 Pro, 3.1 Pro, 3.7 and 3.8 Flash, 4 Argon), `high` on a Pro image model |
| `gemini`, 2.0 and earlier, or a model not in the table | nothing | nothing |
| `openresponses`, `openchat` (openai): o-series and codex models | `reasoning_effort` (`reasoning.effort` on Responses), from `low` | `low` |
| same, gpt-5 | from `minimal` | `minimal` |
| same, gpt-5.1 to 5.x | `none`, then from `low` | `none` |
| same, GPT-6 and later (gpt-6-astra has no `none`, gpt-6.1-sol neither `none` nor `minimal`) | from `low` | `low` |
| same, gpt-5-pro | `high` only | `high` |
| same, a model that does not reason (gpt-4o, gpt-4.1) | nothing | nothing |
| same, o1-mini and o1-preview (they reason, but predate the effort parameter) | nothing | nothing |
| same, a model not in the table (a deployment name, a self-hosted model) | `reasoning_effort` from `minimal` | `minimal` |
| `openchat` (vllm/lmstudio/other) | `chat_template_kwargs.enable_thinking` (request body) | `false` |
| `openchat` (ollama) | `reasoning_effort` (`minimal` to `high`; Ollama's `/v1` reads it and ignores `chat_template_kwargs`) | `none` |
| `ollama` | `think` | `false` |
| `openrouter` | `reasoning.effort` (request body) | `none` |

Models are matched by their ids: `claude-<family>-<major>[-<minor>]` with any
date or `@date` suffix, the older `claude-3-x-<family>`, and Fable and Mythos
by name; `gemini-<major>.<minor>-<pro|flash|flash-lite>`; OpenAI ids with a
fine-tune or provider prefix stripped. What no table can predict is the
stream-open guard's: OpenRouter's mandatory-reasoning models refuse
`effort: "none"`, Ollama answers `think` (or, on its `/v1`, a
`reasoning_effort`) on a model without the thinking capability with a 400
("does not support thinking"), and a deployment name can hide an OpenAI model that
does not reason. Each costs one failed request per process, and the model
then runs without the setting: that is logged at WARNING, once, naming the
provider, the model and the settings dropped (never their values), because
it silently turns a profile's reasoning off for that model until the
provider's adapter is rebuilt (a restart or an edit of the provider).

**Onto the wire.** The openai SDK refuses a keyword it does not know, so a
knob that is not one of its parameters travels in `extra_body`: `openchat`
sends `reasoning_effort` as the SDK parameter and `chat_template_kwargs` in
the request body, and `openrouter` sends its `reasoning` object in the body.
`openresponses` turns `reasoning_effort` into `reasoning.effort`, `gemini`
folds `thinking_budget` or `thinking_level` into `thinking_config` (keeping
the level when both arrive), `ollama` sends `think` at the top level and
`anthropic` sends the `thinking` block and `output_config`.

A forced `tool_choice` (`required` or a named tool) is sent as the caller
chose it on every wire. `openchat` used to turn it into `auto` on real OpenAI
whenever a reasoning effort was sent, on a premise no vendor source states
(audit G2-11); no primer call site forces a choice anyway (the agent loop and
compaction send `auto`). A refusal of a profile setting reaches the
stream-open guard like any other.

**OpenAI reasoning models: the rest of the request.** On the `openai` flavor
of `openchat` and `openresponses`, a model that reasons (o-series, codex,
gpt-5 and later, pro, and o1-mini and o1-preview, which take no effort but
reason all the same; `openai_reasons` in `primer/llm/_reasoning.py`, a
question kept apart from which effort the model takes) gets the request
OpenAI accepts whether or not the profile sets a reasoning level, because
these models reason by default.
`fit_openai_reasoning_request` in `primer/llm/_openai_common.py` sends the
Chat Completions `max_tokens` as `max_completion_tokens`, and drops `temperature`
and `top_p` on both wires unless the effort is `none` on a model that has a
true `none` (gpt-5.1 to 5.x, `openai_has_none`; OpenAI accepts them only
then). Every other reasoning family starts at `minimal` or `low` (gpt-5-pro
takes only `high`) and refuses them at every effort, so a `none` that a
profile's `extended` sends to one of them does not keep them (ticket
01a16b64). A call that sends no effort to gpt-5.1 to 5.x loses them too, on
purpose (ticket 01a16b64): those models default to `none`, but primer does
not count on a default it did not send (OpenAI sets it per model and can
change it), and a dropped knob costs only determinism where a refused one
fails the call. A profile that wants `temperature` and `top_p` on those
models sets its reasoning to OFF, which sends `none`. This covers
compaction's `temperature: 0.0` and 4096-token cap and an agent's
`temperature` and `max_output_tokens`. Each change is logged once
per adapter, provider and model at DEBUG. A model that does not reason
(gpt-4o) is sent as shaped. A model the table does not know (a deployment
name) keeps the earlier rule: on Chat Completions its `temperature` and
`top_p` are dropped only when an effort other than `none` is sent, and
`max_tokens` stays; on the Responses API it is sent as shaped. The
self-hosted flavors and OpenRouter keep their sampling knobs unchanged.

**Anthropic: the rest of the request.** Thinking has rules the call's other
settings can break, so `_fit_thinking` and `_fit_sampling` in
`primer/llm/anthropic.py` make such a request valid instead of letting it
fail, each kind of adjustment logged once at DEBUG. A call that forces a
tool runs without thinking. Manual thinking (Claude 4.5 and earlier) needs a
budget of at least 1024 tokens that stays below `max_tokens`, so a budget
that does not fit is lowered to half of `max_tokens` (not under 1024), and
when even that does not fit the call runs without thinking; manual thinking
also needs the final assistant turn to start with a thinking block, which
primer's history never carries (it does not send thinking blocks back), so a
call that continues a tool round runs without it. Adaptive thinking has
neither rule and thinks on every call of a tool round. While thinking is on,
manual or adaptive, `temperature` other than 1, `top_k` and `top_p` below
0.95 are dropped. And a model that takes no sampling knobs at all (Claude
Opus 4.7 and later, Sonnet 5 and later, Haiku 5.5, Fable, Mythos) loses
`temperature`, `top_p` and `top_k` on every call, profile or not:
compaction asks for temperature 0, and those models answer any sampling knob
with a 400.

**Aggregated profiles.** Each member's call carries that member's own
profile config, translated for the member's own adapter, so a pool keeps
every member's settings through a failover. The aggregated profile's own
`config` is not applied (its members can be different kinds of provider,
so there is no one wire to translate it for); a non-empty one is logged
once at DEBUG. A per-call `extended` from the caller (no call site passes
one today) wins over a member's on a key collision.

**vLLM is asymmetric and this was verified against a live server, not
assumed.** On Chat Completions, `chat_template_kwargs.enable_thinking:
false` works: the response carries `reasoning: null` and real content. On
the Responses endpoint, that key, `extra_body.chat_template_kwargs`, a
top-level `enable_thinking`, and a native `reasoning.effort` are all
accepted WITHOUT error and all ignored, with reasoning emitted regardless.
So `openresponses` + `vllm` maps nothing and says so once per process at
DEBUG, naming the `openchat` provider type as the way to get the control.
An operator who needs reasoning control against vLLM must use `openchat`.

### Transport retries

Every adapter except `aggregated` is wrapped in
:class:`~primer.llm.retrying.RetryingLLM` by the registry factory. It
replays a stream that failed at the TRANSPORT level (`NetworkError`,
`ProviderTimeoutError`, `ServerError`, `RateLimitError`) and never replays
one the API explicitly rejected, because that reproduces the same rejection
and only delays the error. Retries stop at the first streamed event: once
output has reached the consumer, re-running would duplicate tokens.
Backoff is exponential with full jitter, tuned by `Limits.max_retries` /
`retry_backoff_seconds` / `retry_backoff_max_seconds`.

`aggregated` is excluded deliberately: it already fails over across
members, and wrapping it would retry the whole pool before trying the next
member. Its members resolve through the same factory, so each is wrapped
individually.

### Baked tokenizer vocabularies

The OpenAI-family token counter needs two tiktoken vocabularies (`o200k_base`, `cl100k_base`). tiktoken's own loader fetches them over the network, with no timeout, whenever its cache misses or fails its hash check, whatever `TIKTOKEN_CACHE_DIR` says. The Docker image therefore bakes them: a `tokenizer-vocab` stage in the `Dockerfile` runs `scripts/bake_tokenizers.py`, which downloads each file under one hard wall-clock deadline (the download runs on a daemon thread the script stops waiting for, so a slow DNS lookup, a stalled TLS handshake or a server trickling one byte at a time cannot hold the build), verifies its sha256 against the pins in `primer/llm/_tokenizer/vocab_pins.py` (directly, not through tiktoken) and writes it atomically in tiktoken's own cache layout (`sha1(url)` file names). The final stage (below both `uv sync` layers, so a pin or script change never invalidates the dependency install) copies the directory to `/opt/primer/tiktoken-cache`, sets `TIKTOKEN_CACHE_DIR` to it and re-runs the script with `--check`, which verifies bytes on disk and never touches the network. `python3 /opt/primer/bake/bake_tokenizers.py --check` also works inside a running container as a readiness check. A stock `tiktoken.get_encoding` loads both encodings offline from that directory.

The pins mirror `tiktoken_ext.openai_public`; `tests/tooling/test_bake_tokenizers.py` fails if the installed tiktoken disagrees, so a tiktoken bump that moves a vocabulary is caught in CI, not at runtime. Re-pin by updating `vocab_pins.py` from the new `openai_public` source. The full image build itself runs only at release (`release.yml`, after the PyPI publish); `docker build --target tokenizer-vocab .` exercises the bake alone in seconds. Installs outside the image (pip, `uv run primer api`) do not get the baked directory.

### Token counter contract

A counter is a function that is meant to raise when it cannot count; the wrapper is what a turn calls. Nothing in the live turn path calls either yet.

- **Counters raise; they do not estimate.** `LLM.count_tokens_detailed` returns a `TokenCount` (`total`, `exact`, `estimated_components`, `encoding`, `declared`) or raises `TokenCounterUnavailable` (`primer/model/except_.py`; `transient` says whether a retry can help) or a mapped provider error. `exact` is True only for the model's own tokenizer or a vendor count endpoint; an OpenAI encoding applied to an unknown model is `exact=False`, and media blocks are reported as an estimated component (flat constants in `primer/model/media_tokens.py`, shared with the compaction heuristic). `declared` is False only on the ABC default, which wraps `count_tokens` and vouches for nothing: the wrapper reports such a number as `source=estimate`, `outcome=legacy_counter`, never as native. Every shipped counter (OpenAI family, Anthropic, Gemini, HF, aggregated) raises on failure and declares what it counted. A test (`tests/llm/test_counting_not_wired.py`) keeps a returned heuristic out of the three formerly swallowing counters once anything wires the wrapper.
- **`count_prompt_tokens` never raises** (`CancelledError` aside) and never blocks past a backstop. It returns a `CountResult` whose `source` is `native`, `native_approx`, `native_plus_estimated` or `estimate`, and whose `outcome` says why an estimate was used (`legacy_counter` for an adapter that declares nothing). Transient failures (timeout, network, 5xx, 429, 408, 425, a counter queue that did not start) are negative-cached per `(provider, model)` for 60 s with one WARNING per window; a deterministic rejection (400, auth, unsupported content, any other unclassified 4xx such as 413 or 422) is reported and never cached, so one conversation's failure cannot disable counting for every session on a model. Anything unexpected is `fallback_bug`: an ERROR with the traceback, a counter the test suite asserts stays at zero, never a count.
- **tiktoken never fetches.** `primer/llm/_tokenizer/_tiktoken_offline.py` reads the vocabulary from the cache directory (resolved as tiktoken resolves it), verifies its sha256 against the pins, builds the `Encoding` from the verified bytes by running tiktoken's own constructor against a copy of its globals, memoises successes, and the failures that cannot cure themselves (file absent, hash mismatch, unbuildable, no cache directory), for the life of the process (a transient OS error such as EMFILE, EACCES or EIO is not memoised and is retried on the next count), and raises `TokenCounterUnavailable` when anything is missing. It never calls `tiktoken.get_encoding` and leaves a bad file where it is. Counting uses `encode_ordinary`, because `encode` raises on content that spells a special token (`<|endoftext|>` in a web page a tool fetched).
- **Synchronous counters run off the event loop** on a dedicated two-thread executor (`primer/llm/_tokenizer/_executor.py`) so a burst of counts cannot starve the shared default pool; a count that has not started within the queue-wait bound is a transient `TokenCounterUnavailable`.
- **Aggregated profiles** count with the first member that can and never claim the result is exact (the member that counted may not be the one that serves). When none can, the failure is `TokenCounterUnavailable`, not `ConfigError`.
- **What each counter sends, and how it is bounded.**

  | Adapter | Counter | Bound | System text | Tools | Media | Result |
  | --- | --- | --- | --- | --- | --- | --- |
  | OpenAI / OpenChat / OpenRouter | tiktoken, offline | CPU only, counter executor | counted | counted | flat estimate | `exact` only for a model in `_MODEL_TO_ENCODING` |
  | Anthropic | `messages.count_tokens` | 3 s per attempt and 3 s for the whole call (`asyncio.wait_for`), `max_retries=0`: the SDK's `timeout` is applied to each connect, write, read and pool wait separately, so a server that keeps trickling bytes is never cut off by it | the `system` parameter | sent | stripped, flat estimate | exact, `media` estimated |
  | Gemini | `models.count_tokens` | 3 s per attempt (`http_options`) and 3 s for the whole call (`asyncio.wait_for`): google-genai's aiohttp path sleeps 1 to 10 s and retries once after a connection error, so the per-attempt timeout alone is not a bound | estimated, never sent | estimated, never sent | dropped, flat estimate | exact over contents, `system`/`tools`/`media` estimated |
  | Ollama | local `AutoTokenizer` | CPU only, counter executor | counted | counted | flat estimate | never exact; unavailable unless a repo-id tokenizer is cached |

  Gemini never sends `system_instruction`, `tools` or `generation_config` on a count: the Developer-API client raises `ValueError` for all three. Anthropic never sends an empty base64 placeholder for media. Both vendor counters raise on failure; neither falls back inside the adapter.
- **Metrics** (`llm_count_tokens_total`, `llm_count_tokens_seconds`, `llm_tokenizer_ready`) are described in `docs/dev/architecture/observability.md`.

### What `Usage.input_tokens` means

`Usage.input_tokens` is the whole prompt the provider processed, cached tokens included; `cached_input_tokens` is a subset of it. Anything that reads provider usage as the size of the prompt depends on each adapter honouring that, so the mapping is pinned from payloads in `tests/llm/test_usage_semantics.py`:

| Kind | Source field | Includes cached tokens? | Status |
| --- | --- | --- | --- |
| Anthropic | `usage.input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens` | the API's `input_tokens` EXCLUDES them (documented); the adapter adds them back | pinned |
| Gemini | `prompt_token_count` | yes (documented) | pinned |
| OpenResponses | `input_tokens`, cached from `input_tokens_details` | yes | pinned |
| OpenChat, OpenRouter | `prompt_tokens` | OpenAI spec: yes; LM Studio, llama.cpp, vLLM and OpenRouter not verified | mapping pinned, semantics unverified |
| Ollama | `prompt_eval_count` | docs list `prompt_eval_cached_count` separately; reports say a warm cache lowers it and a prompt past `num_ctx` is silently truncated and reported post-truncation | mapping pinned, not reproduced, do not trust as the prompt size |

Cost note: `Usage.input_tokens` (and so `llm_profile_tokens_total{direction="in"}`) now counts Anthropic cache reads and cache creation at FULL weight, because it is the size of the prompt. Providers bill cache reads at a fraction, so any cost derived from `input_tokens` alone over-costs cached traffic; subtract or discount `cached_input_tokens`. primer does not enable Anthropic prompt caching today, so no deployment sees a difference yet.

## 6. Lifecycle

An adapter is built lazily by `ProviderRegistry` on first lookup of a provider row, cached under the row id, and dropped (with `aclose()`) when the row is invalidated. A single `stream()` call walks the validate, translate, acquire, iterate, classify sequence below. Pre-stream exceptions are classified and re-raised; once the iterator has opened, mid-stream exceptions are classified and yielded as a terminal `Error(fatal=True)` so the consumer's `async for` always closes cleanly.

```mermaid
sequenceDiagram
    participant Caller as Agent runner
    participant Reg as ProviderRegistry
    participant Adp as LLM adapter
    participant RL as RateLimiter
    participant SDK as Vendor SDK

    Caller->>Reg: get_llm(provider_id)
    Reg->>Adp: construct (validate type+config), inject rate_limiter
    Reg-->>Caller: cached adapter
    Caller->>Adp: stream(model, messages, ...)
    Adp->>Adp: validate model in provider.models (else ModelNotFoundError)
    Adp->>Adp: translate messages / tools / response_format
    Adp->>RL: acquire(llm:{id}, max_concurrency)
    RL-->>Adp: lease
    Adp->>SDK: create(stream=True, ...)
    alt pre-stream failure
        SDK-->>Adp: SDK exception
        Adp->>Caller: raise classify_*_exception(exc)
    else streaming
        loop per SDK event
            SDK-->>Adp: raw event
            Adp->>Caller: yield translated StreamEvent
        end
        Adp->>Caller: yield Usage, then Done
    end
    Adp->>RL: release lease (context exit)
    Caller->>Reg: invalidate(provider_id)
    Reg->>Adp: aclose() (close httpx pool)
```

Embedders follow the mirror flow: validate model, map every `EmbeddingPart` to its input type before acquiring the lease (so unsupported parts fast-fail without holding a slot), acquire, call the SDK, classify exceptions, and translate the response into `EmbedResponse`. The local `HuggingFaceEmbedder` and `HuggingFaceCrossEncoder` differ only in that the SDK call is a synchronous `SentenceTransformer.encode` / `.predict` wrapped in `asyncio.to_thread` so the event loop is never blocked by weight loading.

- Aggregated providers resolve members lazily per call via
  `ProviderRegistry.get_llm`, so a member edit (which invalidates that
  member's own cache entry) is picked up on the next aggregated call with
  no cross-invalidation wiring.

## 7. Persistence

Provider rows (`LLMProvider`, `EmbeddingProvider`, `CrossEncoderProvider`) are persisted as CRUD-able entities through the storage layer and edited via `primer/api/routers/providers.py`; the adapters themselves hold no durable state. The one persistence concern the adapters touch is auto-bootstrap: on first boot the `BootstrapRunner` (`primer/bootstrap/runner.py`, `primer/bootstrap/defaults.py`) seeds a reserved HuggingFace embedder row (id `huggingface`, empty `SecretStr` token so public Hub models load with zero configuration) and a reserved HuggingFace cross-encoder row (id `huggingface-ce`). The provider registry reserves those ids (`RESERVED_EMBEDDER_IDS`, `RESERVED_CROSS_ENCODER_IDS`) so operators cannot shadow the built-in adapters. LLM providers have no reserved ids and are always operator-provided; OpenRouter rows in particular are added explicitly with no bootstrap. Token-counter caches (LRU keyed on content hashes for the network counters, a per-process tokenizer cache for HuggingFace) are in-memory only and never persisted.

## 8. Public surfaces

The abstract surface callers consume:

- `LLM` has no `list_models()`: what an LLM provider serves is its `ModelProfile` rows, not a list on the adapter, so the question is answered from storage (`GET /v1/llm_providers/{id}/models`) or by a live probe (`GET /v1/llm_providers/{id}/discovered_models`). The embedder and cross-encoder families still carry their own `models[]` and keep `list_models()`. `LLM.stream(model, messages, temperature, top_p, max_output_tokens, stop, response_format, tools, tool_choice, extended)` is the async generator. `LLM.count_tokens(model, messages, tools)` returns a best-effort prompt estimate. `LLM.aclose()` releases the SDK client.
- `Embedder.list_models()` and `Embedder.embed(model, inputs, output_dimensions, config)` returning `EmbedResponse`.
- `CrossEncoder` exposes the reranker surface.

The construction surface is `ProviderRegistry` (`primer/api/registries/provider_registry.py`). Its `_build_default_llm_factory` dispatches each `LLMProviderType` to its adapter, forwarding `rate_limiter` and `trace_llm_io`; `_build_default_embedder_factory` and `_build_default_cross_encoder_factory` do the same for their families. `bind_rate_limiter` rebuilds the factories with the coordinator's limiter once it is available.

The `system::fetch_llm_provider_models` tool calls the same saved-provider discovery function as REST instead of asking an LLM adapter. The LLM ABC deliberately has no `list_models()` after model profiles replaced the provider-row allowlist. Sharing the saved probe keeps the tool's upstream response, stored-secret handling and probe-state stamps aligned with REST. An unexpected `AttributeError` from a provider catalogue integration is logged and returned by the tool as `type=provider-error`, never raised through tool dispatch.

The REST surface (`primer/api/routers/providers.py`) carries provider CRUD plus two live-probe endpoints sharing one dispatch (`_probe_llm_models`): `POST /v1/llm_providers/_discover_models` takes a DRAFT config, for the create form before anything is persisted, and `GET /v1/llm_providers/{id}/discovered_models` reads the SAVED row's config server-side, for the detail page. The second exists because secrets are redacted on the wire: replaying a config the console fetched would authenticate upstream with `"**********"`. It also unwraps `SecretStr` before probing, since the helpers interpolate config values straight into an `Authorization` header. Its outcome is stamped on the row as `last_probe_at`, `last_probe_ok` and `last_error`, and only those three fields are written (`_stamp_saved_probe`, a field-scoped `patch_if` guarded on the provider kind and, where the config has one, on its `url` as stored): the probe can run for seconds, and an edit saved meanwhile (a rotated key, a new URL, new limits) is kept, where the earlier whole-row write reverted it (audit G2-10); a row deleted, given another kind or pointed at another URL meanwhile is not stamped, because the probe said nothing about the new endpoint (ticket 01a16b64). The system tool `fetch_llm_provider_models` runs the same function. Every message a probe builds from text a library produced is built by `_probe_failure`, which masks URL-borne credentials (`redact_url_secrets`) first (the speech `_test` routes and the web search and web fetch `_test` routes keep a draft that did not validate out of their text through `draft_error` in `primer/api/routers/_probe_text.py`, which is pydantic's layout WITHOUT its input, and `validation_detail`, shared with `_validation_detail` here; their probe failures are described below) (the Gemini key arm, a 401 or 403 and the HTTP 400 `INVALID_ARGUMENT` whose `ErrorInfo` reason is `API_KEY_INVALID`, which is how Google answers a bad key, and the "not supported" arms carry text of ours, with no URL, and are plain `BadRequestError`s): httpx prints the whole request URL in its error, so a Base URL `http://user:pw@host/v1` would otherwise come back as `for url 'http://user:pw@host/v1/models'` in the 400, in the `last_error` stamped on the saved row (kept until the next probe, so a row stamped before this change keeps its old text until it is probed again) and in the `detail` of the `llm_provider` predicate of `GET /v1/setup/state`; it reads `http://[REDACTED]@host/v1/models` now (ticket 01a11c0d-dd9a). The configured KEY is masked too (ticket 01a11eda-2031): a key pasted with a trailing newline, space or tab is not a valid header value, h11 refuses it with `LocalProtocolError: Illegal header value b'Bearer sk-...\n'` and the library's message holds the key whole, which `_probe_failure` (URL credentials only) does not see. `_probe_llm_models` validates the draft into a stub provider and re-raises a probe's 400 through `_reraise_masked`, which runs the message through `describe_failure` with the stub (the configured key in its three forms, a Base URL password, Bearer and Basic tokens; a message with no credential is returned as it was); the embedding `_discover_models` does the same, and so does everything built on `_probe_llm_models`: the saved row's `last_error` and the setup predicate. The draft `_test` routes of the speech (`stt_providers`, `tts_providers`) and web (`web_fetch_providers`, `web_search_providers`) providers return `{"ok": false, "error": ...}` through `scrubbed_event_text` with the draft, and the audio model pickers' probe logs its failure the same way. The upstream body a probe quotes (an `HTTPStatusError`) is masked BEFORE it is cut (describe_failure caps after masking), so a key that straddles the cut keeps no prefix. A row stored before this change keeps the key in its `last_error`; `LLMProvider` scrubs it with the row's own config whenever it is loaded (an `after` validator calling `scrub`), the author's choice over a one-off migration that would have had to guess which stored texts held a key. An MCP toolset has HEADER values, not a provider's key: `McpToolsetProvider._classified` masks them (`primer.llm._failure.mask_values`, the same forms, including the `repr` and JSON forms of a value that holds quotes) and URL credentials at each `classify_mcp_exception` site, so `GET /v1/toolsets/{id}/tools` (502), the `unavailable_reason` `GET /v1/tools` serves to every user, the `list_toolset_tools` tool and the catalogue's log are covered. Not covered: a session-enter failure inside `McpToolsetProvider.call` is not classified there and reaches the tool manager raw (which masks URL and Bearer credentials, not a custom header's value); a non-ASCII key answers 500 on some probes (no leak); the web_search and web_fetch TOOLS (the runtime, not the `_test` routes) echo the active provider's key (its own ticket). Also not covered: the semantic-search `_test` (a database password, not a key), which has its own ticket (01a11eda-29a9). A draft that fails validation is described by `_validation_detail`, pydantic's own layout (`N validation error(s) for <Model>`, each field on its own line with the reason indented under it, `[type=..., input_type=...]`) WITHOUT `input_value`: pydantic cuts a long input to its first 25 and last 24 characters, which removes the `@` and leaves a slice of a password readable, and a raw `/`, `?` or `#` in a password ends the userinfo for any URL-shaped mask, so the typed value is never printed. Note the pair `GET /{id}/models` (what is REGISTERED here, derived from `ModelProfile` rows) versus `GET /{id}/discovered_models` (what the upstream OFFERS) -- the difference is exactly what the console's Fetch action turns into new profiles. The probe has per-backend arms. The Ollama arm probes `ollama.AsyncClient.list` and reads each model's real context window from `/api/show` (one call per model; a model whose window it cannot read is listed without one, never with a seeded default); the OpenRouter arm uses a plain `httpx.AsyncClient` against `/models` (not the openai SDK, which strips OpenRouter-specific catalogue fields like pricing and modality) and takes `context_length` from the catalogue, which carries it verbatim. The Anthropic arm lists the models through Anthropic's own list-models API (`_discover_anthropic_models`), and the Gemini arm through ListModels (`_probe_gemini_models`). An LLM provider has no `models[]`, so its form has no models table: the console turns a fetch result into LLM profiles in the Register provider overlay (`ui/components/providers/register-llm.jsx`: page 2 of a new provider, and "Import more models" on a saved one, which probes through `discovered_models` and lists only the models that have no profile yet), synthesising ids with `PC_profileIdFor`, the `<provider>--<model-slug>` rule of the m002 migration, so a created profile collides with rather than duplicates a migrated one. A second profile for a model already registered (the same model with another `config`) is created over `POST /v1/model_profiles` or `system::create_model_profile`.

The Hugging Face tokens of the embedding and cross-encoder providers (`config.token`) are optional and needed only for gated or private repositories: `HuggingFaceConfig.token` is `ApiKeySecret | None`, an empty config is a valid row (the reserved `huggingface` embedder is seeded with an empty token), and `GET /v1/embedding_providers/_types` serves the embedding token as `required: false` with the label "API token (optional)", as the cross-encoder entry always did.

`POST /v1/embedding_providers/_discover_models` is live for two kinds, and `GET /v1/embedding_providers/_types` serves `discoverable: true` for exactly those two. `openai` lists every model the server's `/models` answers, embedding models or not. `gemini` lists the `embedContent` models of Google's ListModels: `_probe_gemini_models(config, method="embedContent")` calls `_discover_gemini_models(draft, method="embedContent")` (`primer/llm/gemini.py`), which keeps the entries whose `supportedGenerationMethods` contains `method`, and the LLM arm of `_dispatch_llm_probe` calls the same function with `method="generateContent"`, so the two forms share one body and one set of failure texts (the 401/403 words, the status and the network error). Both embedding arms re-raise through `_reraise_masked` with the stub, so a configured key is masked out of the 400 like everywhere else. `huggingface` has no inventory to list (the operator names its models), so it answers 400 here and stays `discoverable: false`.

Creating or changing an MCP toolset on the `stdio` transport, or a python toolset, is admin-only, although the toolset routes and the `create_toolset` / `update_toolset` system tools sit on the user tier: a stdio toolset names a command the primer process launches on the server host when the toolset is probed or called, and a python toolset's source runs on the server host (`LocalHardenedRunner`, a child of the API or worker process), which is system configuration of the same class as the provider rows. An existing http / sse MCP toolset with OAuth also reserves any change to its URL or OAuth block, including `resource_uri`, `token_endpoint`, scopes, grant and client settings, to an admin. An accepted change deletes the shared sign-in; an unchanged or refused save keeps it. The origin-bound fields are `url`, `resource_uri` and `token_endpoint`. Separately, the stored secrets follow only to the same origin (scheme, host and port): an admin who moves the host with the mask left alone is refused too (422, `validation-error` from the tool: "re-enter the secret"), and may still change the path. The rule is one predicate, `toolset_admin_reason(entity, existing=None)` in `primer/toolset/toolset_checks.py`, which answers the reason or `None`: the stdio and python checks count either side of an update (so a user can neither turn an http toolset into a stdio or python one nor edit one, even to point it at http), and `oauth_settings_changed` compares the restored copy's URL and whole OAuth block (the transport and extra headers do not count; a refusal while restoring the copy counts as changed). The admin gate answers `forbidden` before a write for a caller below admin. For an admin, the handler then applies the separate masked-secret origin guard and may answer 422 or `validation-error`; `update_*` also answers a refusal raised by `admin_when` instead of raising it out of the tool. A delete launches nothing and is not gated: deleting a stdio or a python toolset stays user-tier, a conscious decision (removing code from the server host grants no power over it; the lead scoped A-02, and then AUTHZ-01, to creating and changing). Both writers apply it. The REST pre-write hooks (`_toolset_on_pre_create` / `_toolset_on_pre_update`) call `require_admin(request)` first, before the reserved-id check, the reachability probe and the restoring of masked secrets, and answer 403 `forbidden_role` with the reason as the detail. The system tools cannot lean on the tool manager's floor, which compares only the tool's static `required_role`, so `_crud_tools_for` takes an `admin_when` predicate (which may answer the reason as a string, then used as the refusal message) and its create / update handlers read the run's identity from `ToolContext.initiated_by` under the floor's own predicate (`primer.authz._role_allows`: an admin, or the internal `system` actor; a trigger-fired run is ranked by its owners' roles, see `docs/dev/subsystems/triggers.md` section 9) and answer `type=forbidden` before any guard or write. A call that carries no `ToolContext` has no known role and is refused: the `/v1/mcp` endpoint dispatches handlers without one, so an MCP client cannot create a stdio or python toolset through the system tool at all (an admin uses `POST /v1/toolsets`). The dedicated `create_python_toolset` / `update_python_toolset_source` tools are statically `required_role="admin"`, which agrees. The command allowlist (`mcp_stdio_allowed_commands`) is unchanged and still applies on top. On top of the role rule, an admin's run is approval-gated: a seeded Rego policy on `system::create_toolset` and `update_toolset` parks every body that is not exactly an http, sse or internal toolset (ticket 01a16a29; [bootstrap-operator](bootstrap-operator.md) section 5), so a model cannot add code to the server host without a person deciding it. Pinned by `tests/api/test_toolset_stdio_admin.py`, `tests/toolset/test_system_toolset_stdio_admin.py`, `tests/api/test_toolset_write_privilege.py` and `tests/toolset/test_system_toolset_write_privilege.py`; a test that writes such a toolset through a system tool says who is writing with `tests/_support/caller.py`.

Inbound MCP is the mirror of the outbound MCP toolset client and is a peer surface to this subsystem: `primer/mcp/server.py` builds an `mcp.server.lowlevel.Server` exposing Primer's own tool catalogue over Streamable HTTP at `/v1/mcp`, gated by `is_exposable` filtering (`primer/mcp/safety.py`, denying `yielding_unsupported` and `needs_session`), a cookie-only exposure-config rule, and the `primer/agent/tool_manager.py` `invoke_one` helper that bypasses the approval gate and workspace dispatch. Full detail lives in the MCP and rest-api docs.

### Hugging Face model check

`POST /v1/huggingface/_check_models` (admin; `primer/api/routers/hf_hub.py`, mounted beside the provider `_types` routers) tells an operator, before a Hugging Face embedding or cross-encoder provider is registered, whether each model it names exists and whether the token can pull it. A Hugging Face provider has no model inventory to list (its models are named by the operator and pulled from the Hub the first time they are used), so a typo, a gated repository or a refused token otherwise shows up at the first embed, in a worker, long after the form was saved. The body is `{"models": [<name>, ...], "token": <string or null>}` with 1 to 50 names (an empty or longer list is a 422); the answer is `{"models": [{"name", "ok", "status", "detail"}, ...]}`, one entry per name in the order given. It stores nothing. A model the Hub refuses is a result inside a 200 answer, not an error status. The `check_huggingface_models` system tool (admin, `primer/toolset/_system_probes.py`) takes the same arguments and returns the same body.

The helper is `check_hf_models` in `primer/common/hf_hub.py`. It makes the request `huggingface_hub`'s `HfApi.auth_check` makes, `GET {endpoint}/api/models/{name}/auth-check`, with `Authorization: Bearer <token>` only when a token was given, one model after another (10 seconds each; once a request finds the Hub out of reach, which is a connection that could not be made or kept, a timeout, DNS or a proxy, the names after it get the same `error` without a request instead of a timeout apiece), and maps the answer the way the library's `hf_raise_for_status` does: the `X-Error-Code` and `X-Error-Message` headers are read before the status, because a gated repository, a disabled one and a refused token all arrive as a 401 or a 403 and only the headers tell them apart.

| Hub answer | `status` | `ok` | `detail` |
| --- | --- | --- | --- |
| 2xx | `ok` | true | Can be pulled |
| `X-Error-Code: GatedRepo` | `gated` | false | Gated: accept the model's terms on the Hub with the token's account |
| `X-Error-Message: Access to this resource is disabled.` | `disabled` | false | Disabled on the Hub |
| 401 with `X-Error-Message: Invalid credentials in Authorization header` | `bad_token` | false | The Hub refused the token |
| `X-Error-Code: RepoNotFound`, any 404, any other 401 | `not_found` | false | Not found, or private and the token cannot see it |
| any other status | `error` | false | The Hub answered HTTP (the status) |
| the request failed (a network failure, a timeout, anything the HTTP library raised) | `error` | false | Could not reach (the host) (the exception type) |
| the name is not a model id | `invalid_name` | false | Not a Hugging Face model id (namespace/name); no request is made |
| the token cannot be a header value (a space, a line break, a non-ASCII character) | `bad_token` | false | The token has a space, a line break or another character that cannot be sent; paste it again; no request is made |

- **The endpoint is `HF_ENDPOINT`**, the variable `huggingface_hub`, `transformers` and `sentence-transformers` already honour, read on every call, so a deployment behind a mirror checks the mirror it will pull from (default `https://huggingface.co`; a trailing slash is dropped). Up to three redirects are followed, as the libraries that later pull the model follow them (a renamed repository; a longer chain is that name's `error`), and the `Authorization` header is dropped by the HTTP client on a redirect to another origin. The `endpoint=` argument of `check_hf_models` is for tests: no route or tool passes it, because an endpoint taken from a request would send the token to any host.
- **No new dependency.** The check is one GET with `httpx`, which is core; `huggingface_hub` ships with the optional `huggingface` extra, which an API pod may not have.
- **A name never reaches the request path unchecked.** `HF_REPO_ID_RE` allows `name` or `namespace/name` (ASCII letters, digits, `.`, `_` and `-`, starting with a letter or a digit, 96 characters each), and a name with `..` or `--` fails as well, as in the library's `validate_repo_id`; only a name that passes is put into the URL, so a name cannot add a path segment, a query or an escape.
- **The token is a header and nothing else.** It is never in the URL (httpx logs every request URL), never in a `detail` (the sentences are fixed; a failure names the host and the exception TYPE, never the library's text, which can quote the request), and the response model has no field for it. The host in a sentence is the endpoint's hostname, never its `user:password@`.
- **The helper never raises.** Every name gets a result, so one bad name or an unreachable Hub does not hide the verdict on the others. The HTTP client is built inside the per-name `try`, so even a proxy variable httpx cannot parse is a result.
- **No ambient credentials.** A check with no token asks anonymously. The libraries that later pull the model also fall back to an `HF_TOKEN` in the server's environment (or a cached login) when the row has no token; the check does not, so a gated model can read `gated` here and still load on a server that has one.

### Draft tests of embedding and cross-encoder providers

`POST /v1/embedding_providers/_test` and `POST /v1/cross_encoder_providers/_test` (admin; on the `_types` routers of `primer/api/routers/providers.py`, mounted before the CRUD routers) answer, for a provider nobody has saved, whether it would work. Before them the console's Test on these classes answered 405: the literal path fell through to the CRUD `/{id}` route, which has no POST. They follow the speech and web `_test` routes: always HTTP 200, `{"ok": true, ...}` or `{"ok": false, "error": ...}`, nothing stored. The cores are `embedding_draft_test(draft)` and `cross_encoder_draft_test(draft)`; the `test_embedding_provider` and `test_cross_encoder_provider` system tools (admin, `primer/toolset/_system_probes.py`) call the same cores and return the same bodies, so an agent can ask what the console asks.

The body is a draft row with every key optional: `provider`, `config`, `models` (`id` and `limits` are accepted and ignored). It is validated into a transient row of the real model class, so a draft that cannot be a row is `{"ok": false, "error": "invalid draft: ..."}` with pydantic's layout of the error WITHOUT its input (`draft_error`: the field is named, the typed value, a key or a Base URL with its password, is never printed). A missing `config` is an empty one, so an `openai` draft is told it needs a `url` while a Hugging Face or Gemini draft, whose keys are optional, is valid; a cross-encoder draft may leave `provider` out (the one kind is `huggingface`). Then, by kind:

| Kind | What it does | Answer |
| --- | --- | --- |
| `openai` | `GET <url>/models` with the key, the probe behind `_discover_models` (models that are not embedders are listed too) | `{"ok": true, "models": [names]}` |
| `gemini` | Google's ListModels kept to the `embedContent` models, the probe behind `_discover_models` | `{"ok": true, "models": [names]}` |
| `huggingface`, embedding and cross encoder | the Hub check above of each model the DRAFT names, with `config.token` | `{"ok": <all passed>, "models": [names that passed], "checks": [...]}`, plus `"error": "N of M models failed the Hub check: <name> (<status>), ..."` when any failed |

A Hugging Face draft with no model is `invalid draft: models: give at least one model name to check`, and one with more than 50 is `invalid draft: models: at most 50 model names can be checked at once`. An upstream failure is `{"ok": false, "error": <text>}` with the configured key and the password of a Base URL masked out of it (`scrubbed_event_text` with the validated config, as the speech `_test` routes do). The probes are handed `_unwrap_secrets(config.model_dump(mode="python"))`, the key as typed: a draft opened from a saved row holds the served mask (`**********`) and would authenticate with it, so an edit never sends its draft here (the saved-provider routes read the stored secret).

## 9. Internal contracts

- **Per-event inactivity timeout (`Limits.request_timeout_seconds`).** Each LLM adapter reads `provider.limits.request_timeout_seconds` at construction time and stores it as `self._request_timeout_seconds`. During streaming the adapter passes the SDK iterator through `primer.llm._timeout._iter_with_timeout`, which wraps every `__anext__` call with `asyncio.timeout(seconds)`. If no event arrives within the window `asyncio.TimeoutError` is raised, the adapter catches it before the generic `except Exception` clause, and re-raises it as `primer.model.except_.ProviderTimeoutError` (a `ProviderError` subclass). This propagates out of the async generator to the agent loop, which catches it as a generic `Exception` and records it as an error row / turn failure, releasing the concurrency slot and ending the turn cleanly. `None` disables the timeout entirely. The default is 300 s. LM Studio guidance: LM Studio can stall mid-generation on large models or low-memory hardware; 300 s covers most real runs. Lower to 60 s for faster failure detection if hardware is fast enough.
- **Always exactly one terminal event.** Every successful `stream()` ends with one `Done`; every failed stream ends with one `Error(fatal=True)`. Pre-stream exceptions (the iterator never opened) re-raise the classified `PrimerError` instead. Exception: a timeout raises `ProviderTimeoutError` out of the generator rather than yielding a terminal event, because `asyncio.TimeoutError` fires asynchronously and the generator is already unwinding.
- **An OpenResponses `error` event is a notice that is followed by `response.failed` (ticket 01a11c07).** `OpenResponsesLLM` yields an `error` stream event as `Error(fatal=False)` and keeps reading. Per the Open Responses specification (its Errors section), an error incurred while streaming "will be followed by a `response.failed` event", which the adapter turns into `Done(stop_reason="error", raw_reason="failed")`; so one more event follows the `Error`, which is what `fatal=False` says, and this is the one adapter whose failed stream can end in a `Done` after an `Error`. That is per the spec, NOT observed against a real provider (the repo has no trace of one). The agent loop holds the first `Done` or `Error` of a call as its terminal whatever `fatal` says, so a non-fatal `Error` fails the turn today and never recovers it; reopen only if a provider trace shows an `error` followed by a successful completion.
- **Stop-reason normalisation.** Each adapter maps its vendor finish reason onto the universal `StopReason`. The shared rule across Anthropic, Gemini, Ollama, and the Chat Completions adapters: a natural stop collapses to `tool_use` when the stream emitted any tool call, otherwise `stop`, so downstream callers get a consistent signal to dispatch tools. Unknown reasons collapse to `other`. `tool_turn_cap` is in the same `StopReason` literal but no adapter produces it: the agent loop sets it on the `Done` of the round that tripped `Agent.max_tool_turns` (the model's own reason stays in `raw_reason`; see `docs/dev/subsystems/agents.md`). The Chat Completions adapters (`openchat`, `openrouter`; the shared code is `_map_finish_reason` in `primer/llm/_openai_compat.py`) did NOT apply that rule until it was fixed: they mapped the server's `finish_reason` alone, so a server that is not OpenAI (LM Studio, llama.cpp, vLLM, some gateways) that ends a tool round with `"stop"` was recorded as `Done(stop_reason="stop")`, which the readers that take a `done` that is not `tool_use` for the end of a turn (the persisted `done` record, `scripts/analyse_estimate_ratio.py`) read as the turn ending. Now only a `"stop"` is reinterpreted (to `tool_use` when the stream emitted a tool call); `"tool_calls"` and the legacy `"function_call"` are `tool_use`; `"length"` with tool calls stays `max_tokens` (the call may be truncated, which is not a clean tool round); `content_filter` and unknown reasons stay what they were (`raw_reason` keeps the server's string). Gemini, Ollama and the Responses adapter already looked at the calls.
- **Tool-call id synthesis.** Adapters whose protocol omits stable tool-call ids (Gemini, Ollama) synthesise `call_{index}` so the universal `ToolCallStart` / `ToolCallDelta` / `ToolCallEnd` triple and round-trip `ToolResultPart` correlation have an id to pair on.
- **Chat Completions tool calls are tracked by stream index, and a new id on a live index starts a new call.** `_translate_chunk` (`primer/llm/_openai_compat.py`, shared by `openchat` and `openrouter`) keeps one in-progress call per `index` of the stream. A gateway that numbers EVERY parallel call with the same index (they are streamed one after the other, each header carrying its own id and the argument chunks that follow carrying none) used to lose the second call: its header was dropped and its arguments were appended to the first's, which then ended with `{}`. Now a header with another id on an index already in progress ends the call in progress (`ToolCallEnd`) and starts the new one; a chunk that repeats the same id (some servers send the id and name on every chunk) is a continuation. Not handled: parallel calls that share an index AND interleave their argument chunks, which cannot be told apart without ids on the chunks, and a new call whose id and name arrive in different chunks. Which gateways number parallel calls per index (vLLM, LM Studio, the Ollama flavors, OpenRouter routes) is UNVERIFIED: the fix is defensive and rests on the wire shape, not on a live capture.
- **`response_format` translation.** OpenAI, Gemini, and Ollama have native structured-output surfaces; the adapter routes the Pydantic class or dict schema to them. Anthropic has no JSON mode, so `AnthropicLLM` emulates it with a forced single synthetic tool named `structured_output`, and raises `ConfigError` if `response_format` is combined with caller-supplied tools or an explicit `tool_choice`.
- **Unsupported parts raise, never drop.** An adapter that cannot transmit a `Part` modality raises `UnsupportedContentError` rather than silently dropping it, so input/output index correspondence is never corrupted. Embedders run this check before acquiring a rate-limit slot.
- **Assistant history replays as string content on the Responses API.** `_finalize_message_item` (`primer/llm/openresponses.py`) collapses a text-only assistant item to `{"role": "assistant", "content": "<text>"}` before it is appended to the `input` list. A list of `output_text` parts matches only the `ResponseOutputMessageParam` union arm, which also requires `id` and `status`; real OpenAI infers those but a strict reimplementation of the schema rejects every arm and returns a 400 enumerating the whole union. String content matches `EasyInputMessageParam`, which accepts `role="assistant"`, and validates against both. Tool calls are split into separate `function_call` items before this runs, so the text-only case is the common one and the collapse is lossless; an assistant message carrying non-text parts keeps the list form.
- **Unknown extended kwargs are dropped with one DEBUG line.** Each adapter whitelists the extended keys its wire format accepts and logs the dropped remainder once, so an operator diagnosing "my knob is not taking effect" has a discoverable signal.
- **A counter raises; the wrapper keeps a turn safe.** No adapter returns a heuristic number from `count_tokens`; a failure is a mapped provider error or `TokenCounterUnavailable`, and `primer.llm.counting.count_prompt_tokens` is the one place that turns it into a labelled estimate (see "Token counter contract"). The per-adapter wiring: OpenResponses, OpenChat and OpenRouter call `count_tokens_openai_detailed` (tiktoken, offline, off the loop); Anthropic calls `count_tokens_anthropic_detailed` (the vendor endpoint); Gemini calls `count_tokens_gemini_detailed` (the vendor endpoint over the contents only); Ollama runs `count_tokens_hf_detailed` (a local `AutoTokenizer`) on the counter executor. `transformers` ships in the optional `huggingface` extra and is imported lazily, so an install without it reports the Ollama counter as unavailable instead of returning a number.
- **A failed model call names the provider and carries the provider's own words (C-024).** The classifiers choose the exception class, `code` and `status_code`; the message used to be a fixed vendor label ("OpenAI server error"), which dropped what the provider said and called every OpenAI-compatible endpoint OpenAI. Each adapter now passes the classified error through `describe_failure` (`primer/llm/_failure.py`), which keeps all three and rewrites the message of the four shapes that lost the provider's text (server error, rate limit, authentication, network): `Model provider 'lm-studio-box' (openchat/lmstudio) had a server error (HTTP 500): upstream exploded`. The label is the configured provider id and the backend kind (plus the flavor of an OpenAI-compatible one); the text is the body's `error.message` (else a bounded slice of the body: nested `error` objects are walked iteratively, at most 8 levels, and a body that nests deeper or has no message is dumped to a depth of 4), whitespace collapsed, non-whitespace control characters (NUL, ESC) stripped, and capped at 300 characters; a network failure ALWAYS takes the transport's own reason (the exception it was raised from, or the context it was raised in unless `from None` suppressed it, else ollama's `.error`, else a builtin `ConnectionError`'s text: ollama's "Failed to connect to Ollama..." is raised `from None`, so it is the sentence, not the transport's "All connection attempts failed"), never the SDK's class label, and a body nobody can read leaves the sentence without its text instead of crashing the classification. A stream `Error` EVENT (the OpenResponses `error` event: no exception, so nothing for `describe_failure` to rewrite) is scrubbed by `scrubbed_event_text` with the same rules (the configured credentials included) and capped like an untouched message, and the session record writer redacts URL credentials of every ERROR record whatever produced it (security ticket 01a11f35-ad20). A 4xx the provider rejected, a mid-stream provider error and any other shape keep the SDK's message, because `primer.common.context_overflow` reads it; it is masked and capped at 4000 characters, so a multi-megabyte body cannot land whole in the session record. The text is normalised FIRST (control characters such as NUL and ESC stripped, and for the provider's own text whitespace collapsed), then cut to the first 64,000 characters of the normalised text, then credentials are masked, and only then is it capped (300 characters, 4000 for an untouched message). That order is what keeps a secret from being cut in half and left partly readable: the message shows the first characters of the normalised text, so anything it can show lies far inside the prefix that is scrubbed (cut before normalising, a key echoed behind padding that straddled the limit lost its first characters to the cut and the collapse brought the rest to the front), and a NUL inside an echoed key does not defeat the exact match. Stripping uses `str.translate` (linear, about 0.05 s for 32 MB) and the collapse reads the words lazily and stops once they fill the scan limit (`_collapse`), so a 32 MB body is never split whole. Masked: the provider's configured API key and Base URL password as exact values (4 or more characters; each in the forms the text can show it: as configured, with its controls stripped and whitespace collapsed (the text is normalised first), and escaped the way a `repr` prints it (a tab as `\t`); a value of 8 or more is masked wherever it appears, one of 4 to 7 only as a token, where it is not part of a longer run of letters or digits, so a short key does not blank a number the overflow veto reads; a `%XX` or `\uXXXX` escape, or one a Python `repr` prints (`\n`, `\t`, `\x1b`), before it counts as a boundary, so `Bearer%20sk-1234` and `...\nsk-1234` are masked), the `Authorization: Basic` token a Base URL with credentials is sent as (`base64(user:password)`, exact, padded and unpadded, so a bare echo of it is masked too), any other `Basic <token>` whose token base64-decodes to printable text containing a `:` (credentials primer did not configure; the words after "Basic" in prose do not decode to one and are left alone), URL-borne credentials (`redact_url_secrets`: query keys, Telegram tokens, the `user:password@` of a URL) and `Bearer` tokens. A configured key that is one of the keyless placeholders a local server accepts (`EMPTY`, `none`, `dummy`, `ollama`, `lm-studio`, `no-key-required`, `not-needed`, `sk-no-key-required`, any case) is not a secret and is never masked, so it cannot blank ordinary words; a key of 1 to 3 characters is below the floor and is not masked. Known limits: a configured key of 4 to 7 characters that a provider echoes GLUED to letters or digits (`xsk-1234`, `Bearersk-1234`) is not masked, because the same rule is what spares the numbers the overflow veto reads; a secret the provider echoes in a form other than those above (re-encoded, split across words, hashed) is not recognised; a `Basic` token that decodes to printable text without a `:` is left alone (the guard that spares prose such as "Basic Indiana plan"); a credential that primer was not configured with and that is not a `Bearer` / `Basic` token, a URL credential or a query key is not recognised; and the message is a courtesy, not a log: it carries at most 300 characters (4000 untouched) of what the provider said. The embedders and the speech adapters share `classify_openai_exception` and keep their old labels (follow-up ticket 01a11d5a-9e30). Pinned by `tests/llm/test_failure_messages.py` (all six adapters, with real SDK exceptions) and `tests/llm/test_failure_description.py`.

- **An error `code` is an identifier, the sentinel or nothing (ticket 01a11fbc-ffea).** A provider can send any text as its code: an OpenResponses `error` event's `code` and the OpenAI SDK exception's `code` (the body's `error.code`) used to be passed through whole, unbounded and with any character, into the ERROR record, `ended_detail`, `last_turn_error`, the `session.turn_failed` payload and, for a graph, the `X-Primer-Graph-Ended-Detail` git trailer. `primer.common.error_codes.safe_code` is the one rule: a code survives only when it is `[A-Za-z0-9_.:-]{1,64}` (every code of ours is, and so are a provider's real ones, `context_length_exceeded` included, and a JSON-RPC number as text; an int a proxy sends is read as its text), else it is the fixed sentinel `provider_error` (`UNSAFE_CODE`); an empty code is no code. NOT `None`: a failure with no code is classified by its CLASS (a 5xx is `server_error`, which RESTS an interactive session and is transient for strict aggregated failover), while a code that is SET and that nobody classified ENDS the session and is only config-eligible for failover, so a hostile code must stay a set, unclassified code or it would flip the rule it is judged by (`provider_error` is in neither set). It is enforced in the two places every code passes: `PrimerError.__init__` (so every classifier, and `str(exc)`, see only the safe value) and the `Error` stream event's `code` validator, which runs in `mode="before"` because the field is `str | None` and a non-string code (a proxy's `"code": 429`) would otherwise be a validation error instead of a reported failure. A failure with no code is classified by its class, as it already was for an adapter that sends none. Two edge cases follow from the rule: an EMPTY code on a yielded `Error` event is no code, so it is transient for strict aggregated failover (`code is None`) where the empty string used to be a set, non-transient code; and a code of a type that is neither a string nor an int on an exception that is RAISED (not a stream event) becomes the sentinel, a set and unclassified code, so the failure ENDS the session instead of being classified by its class (latent: no adapter raises one today). Pinned by `tests/llm/test_provider_error_code_is_sanitised.py`.
- **Exception classification is centralised.** Adapters call the matching `classify_*_exception` at the SDK boundary. The OpenAI/Anthropic classifiers map by SDK exception subclass; the Google classifier dispatches on the HTTP status carried by `google.genai.errors.APIError.code` (the SDK only distinguishes 4xx vs 5xx by subclass); the HuggingFace embedder uses an inline string-match classifier because sentence-transformers and huggingface_hub share no common base exception.

## 10. Testing patterns

Each adapter has a unit suite under `tests/llm/<provider>.py` or `tests/embedder/<provider>.py` driving a mocked SDK client (`AsyncMock` for the `openai` / `anthropic` / `google-genai` clients; `respx` for the OpenRouter transport; stubbed `SentenceTransformer` for the local adapters). The suites share a class layout: constructor validation, per-Part input mapping, tool/tool-choice/response-format translation, sampling and extended-kwargs handling, stop-reason mapping, full-stream translation, exception wrapping, concurrency, package re-export, and `count_tokens`.

`tests/llm/test_adapters_no_local_semaphore.py` is a source-level pin asserting every LLM, embedder, and cross-encoder adapter routes concurrency through the shared `RateLimiter` with no local `asyncio.Semaphore`. The shared classifiers have dedicated tests (`tests/test_openai_errors.py`, `tests/test_anthropic_errors.py`, `tests/test_google_errors.py`); the shared OpenAI helpers have direct coverage (`tests/llm/test_openai_compat.py`, `tests/llm/test_openai_common.py`). Per-provider token counters are tested under `tests/llm/_tokenizer/`.

Integration smokes live under `tests/integration/test_<provider>_smoke.py`, each gated behind the relevant API-key env var (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `HUGGINGFACE_SMOKE=1`) or a TCP probe (LM Studio, a reachable Ollama at `localhost:11434`). They stream a single prompt and assert at least one `TextDelta` plus a terminal `Done` with a stop reason in `{stop, max_tokens}`. The project pins coverage at `fail_under = 90` in `pyproject.toml`. API keys and bearer tokens in tests are read from env vars and the test skips when unset, never inlined.

## 11. Historical decisions

- **Adapter concurrency moved from a per-adapter `asyncio.Semaphore` to a shared `RateLimiter` keyed `llm:{provider.id}`.** Why: a local semaphore only caps in-flight requests inside one process, so two primer-api / primer-worker processes against the same provider would double the upstream load. Spec: docs/superpowers/specs/2026-04-26-provider-adapters-shared-arch-design.md.
- **The root exception class is `PrimerError` and the module is `except_.py`.** Why: `except` is a Python keyword that cannot name an importable module, and the project namespace settled on `primer` rather than `matrix`. Spec: docs/superpowers/specs/2026-04-26-provider-adapters-shared-arch-design.md.
- **Per-SDK exception classifiers were hoisted into `primer/common/<sdk>_errors.py` instead of being inlined per adapter.** Why: adapters that wrap the same SDK need identical mapping rules, so centralising them removed drift risk and made the rules testable in one place. Spec: docs/superpowers/specs/2026-04-26-provider-adapters-shared-arch-design.md.
- **`OpenChatLLM` and `OpenRouterLLM` were added as separate adapters alongside the original four.** Why: OpenResponses talks only to the OpenAI Responses API, but the legacy `/v1/chat/completions` surface is what every OpenAI-compatible third party actually supports, and OpenRouter adds gateway attribution headers. Spec: docs/superpowers/specs/2026-05-30-openchat-llm-provider-design.md.
- **OpenRouter ships as its own `LLMProviderType` variant rather than an `openchat` flavor.** Why: an operator-facing "add an OpenRouter row" affordance must drive its own Pydantic shape, registry factory arm, discover-route arm, and UI picker; muxing it under `openchat` would smuggle flavor checks through every layer. Spec: docs/superpowers/specs/2026-06-04-openrouter-llm-provider-design.md.
- **`count_tokens` became a mandatory abstract method backed by per-provider tokenizers under `primer/llm/_tokenizer/`.** Why: the compaction mixin needs a fast best-effort token count before every turn, and measuring by sending is far too expensive for a hot-path check. Spec: docs/superpowers/specs/2026-05-30-auto-compaction-token-counting-design.md.
- **`aclose()` was added to both the `LLM` and `Embedder` ABCs as a lifecycle hook.** Why: `ProviderRegistry` caches adapter instances per provider id and must drop and rebuild them on row invalidation; without `aclose()` the cached adapter's httpx pool would leak on every edit. Spec: docs/superpowers/specs/2026-04-26-provider-adapters-shared-arch-design.md.
- **Anthropic `max_tokens` defaults to 4096 with an INFO log when the caller leaves `max_output_tokens` unset.** Why: the Anthropic API requires `max_tokens`, so the adapter picks a sensible value and logs once rather than refusing the call or choosing silently. Spec: docs/superpowers/specs/2026-04-26-anthropic-llm-design.md.
- **Anthropic `response_format` is emulated by forcing a single synthetic `structured_output` tool, and combining it with caller tools or `tool_choice` raises `ConfigError`.** Why: Anthropic has no native JSON mode, and the synthetic tool fully owns the tools / tool_choice slots so silently overriding caller intent would be surprising. Spec: docs/superpowers/specs/2026-04-26-anthropic-llm-design.md.
- **Mid-stream exceptions yield a terminal `Error(fatal=True)` while pre-stream exceptions re-raise.** Why: once events are flowing the consumer is already iterating and a terminal error lets it close cleanly; pre-stream failures cannot be observed by the consumer so re-raising surfaces them through the caller's `try`/`except`. Spec: docs/superpowers/specs/2026-04-26-anthropic-llm-design.md.
- **Gemini uses the stateless `generate_content_stream` and lifts system messages to `system_instruction`; Vertex AI is out of scope.** Why: the universal interface always sends full history so the stateless surface fits, and Vertex's GCP application-default-credentials auth model warrants its own provider type. Spec: docs/superpowers/specs/2026-04-26-gemini-llm-design.md.
- **The Google classifier dispatches on `APIError.code` rather than the `google-api-core` subclass hierarchy.** Why: `google-genai` only distinguishes 4xx vs 5xx by subclass, so the granular auth / rate-limit / bad-request distinctions Primer cares about live in the HTTP status code. Spec: docs/superpowers/specs/2026-04-26-gemini-llm-design.md.
- **Ollama silently drops caller `tool_choice` (DEBUG log only) and maps `response_format` natively to its `format=` parameter.** Why: Ollama's HTTP surface has no `tool_choice` parameter, and raising would force every orchestrator to special-case Ollama; Ollama enforces structured-output schemas server-side so emulation would duplicate work. Spec: docs/superpowers/specs/2026-04-26-ollama-llm-design.md.
- **`require_api_key` was narrowed to the OPENAI flavor only; OTHER no longer hard-fails at adapter construction.** Why: OTHER is the catch-all for any OpenAI-compatible server that is not explicitly modelled, and that population is dominated by unauthenticated self-hosted backends, so requiring a key there left no valid configuration for a keyless server on the Responses API (LMSTUDIO was the only keyless flavor, and selecting it also flips `drop_encrypted_reasoning`). The `no-key-required` sentinel in `_get_client` already existed to support exactly this case but was unreachable for OTHER. A genuinely-missing key now surfaces as an upstream 401 at call time, matching the schema-level contract.
- **Assistant turns replay as string content rather than a list of `output_text` parts.** Why: the list form matches only `ResponseOutputMessageParam`, which additionally requires `id` and `status`. Real OpenAI infers them, so the bug was invisible against the reference implementation; a strict server rejects the entire union with a 400 listing every arm. String content matches `EasyInputMessageParam` and validates on both, and the unit suites could not catch it because they drive mocked SDK clients that never validate the request body.
- **OpenAI-compatible servers are distinguished by a `flavor` discriminator on the config rather than new provider-enum variants per server.** Why: the wire protocol is identical across OpenAI, LM Studio, vLLM, and friends; only server expectations (LM Studio's empty-api_key tolerance) differ, and those live as adapter-internal `_FlavorPolicy` rows. Spec: docs/superpowers/specs/2026-04-26-openai-embedder-design.md.
- **`_get_client` substitutes a `no-key-required` sentinel when the OpenAI api_key is empty or `None`.** Why: `AsyncOpenAI` rejects `api_key=None` outright, so the sentinel keeps the SDK constructor happy for unauthenticated LM Studio / Ollama / vLLM endpoints. Spec: docs/superpowers/specs/2026-04-26-openresponses-llm-adapter-design.md.
- **OpenResponses hardcodes `store=False`, keeps system messages inline as `system` input items, and silently ignores the `stop` knob with a WARNING.** Why: the universal interface always sends full history so server-side retention has no benefit; `instructions` accepts only one string and would lose ordering across multiple system messages; OpenAI Responses has no `stop` parameter and the foundation contract ignores unsupported knobs. Spec: docs/superpowers/specs/2026-04-26-openresponses-llm-adapter-design.md.
- **The Chat Completions request/response shaping was factored out of `OpenChatLLM` into `primer/llm/_openai_compat.py` once `OpenRouterLLM` arrived.** Why: the two adapters would otherwise duplicate every translation (messages, tools, tool_choice, response_format, SSE chunk parsing); the sampling builder lifted earlier into `_openai_common.py` while the rest of the shaping stayed adapter-local until a second consumer existed. Spec: docs/superpowers/specs/2026-05-30-openchat-llm-provider-design.md.
- **OpenRouter hard-codes its base URL, requires `api_key`, and sets `extra="forbid"`.** Why: OpenRouter is a single always-authenticated hosted endpoint, and its only field overlapping with sibling configs is `api_key`, so `extra="forbid"` is the only union discriminator. Spec: docs/superpowers/specs/2026-06-04-openrouter-llm-provider-design.md.
- **The HuggingFace embedder L2-normalises every output vector and prepends model-family query/document prompt prefixes.** Why: every vector store Primer ships ranks by cosine similarity, which is only well-defined after L2 normalisation, and asymmetric-retrieval models (BGE, E5, nomic-embed-text) were trained to expect different prefixes on queries versus documents. Spec: docs/superpowers/specs/2026-04-26-huggingface-embedder-design.md.
- **The HuggingFace embedder id `huggingface` is reserved and auto-bootstrapped with an empty-string token.** Why: local embeddings should work out of the box for a new operator with no API key and no config, and an empty `SecretStr` becomes `token=None` on the `SentenceTransformer` call, which is correct for public Hub models. Spec: docs/superpowers/specs/2026-04-26-huggingface-embedder-design.md.
- **The Gemini embedder reuses `GoogleConfig` and `classify_google_exception` and honours Google-only knobs (`task_type`, `document_ocr`, `audio_track_extraction`) that the OpenAI embedder ignores.** Why: sharing the config and classifier keeps the Gemini LLM and embedder consistent, and the Gemini endpoint actually consumes those knobs while the OpenAI endpoint does not. Spec: docs/superpowers/specs/2026-04-26-gemini-embedder-design.md.
- **The OpenAI embedder forwards `dimensions` without validating it against the model name.** Why: older models (text-embedding-ada-002) reject `dimensions`, and forwarding plus letting the API surface a `BadRequestError` keeps the adapter model-agnostic instead of maintaining a per-model capability table. Spec: docs/superpowers/specs/2026-04-26-openai-embedder-design.md.
- **`classify_openai_exception` was lifted into a shared `primer/common/openai_errors.py` module.** Why: both `OpenResponsesLLM` and `OpenAIEmbedder` wrap the same `openai.AsyncOpenAI` client, so one shared mapping prevents drift across adapters that share an SDK. Spec: docs/superpowers/specs/2026-04-26-openai-embedder-design.md.
- **The `LLMProvider._coerce_config_to_provider` validator and `OpenRouterConfig`'s `extra="forbid"` solve the same union-disambiguation problem at two layers.** Why: `OpenResponsesConfig` and `OpenChatConfig` are nominally identical (`_HttpApiKeyConfig` plus overlapping flavor values) so the provider enum is the natural discriminator, and `OpenRouterConfig` has no distinguishing field beyond the shared `api_key`. Spec: docs/superpowers/specs/2026-05-30-openchat-llm-provider-design.md.
- **The cross-encoder reranker family landed after the shared-architecture spec and is held to the same no-local-semaphore invariant.** Why: rerankers are a third model family that reuses the adapter contract (validate, lazy load, shared `RateLimiter`, classify) and the source-level pin keeps new adapters on the post-spec design. Spec: docs/superpowers/specs/2026-05-27-backend-architecture-audit.md.
