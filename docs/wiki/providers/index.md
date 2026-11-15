---
title: "Providers"
summary: "Admin: register the LLM, embedding, cross-encoder, vector-store, storage and web providers that agents run on."
audience: admin
order: 110
---

These pages are for admins. A provider is a connection the platform runs on: model servers, embedders, rerankers, vector stores, file storage and web search. Register providers here before agents and collections can use them. Start with [LLM providers](providers/llm).

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`cross_encoder_providers__find`](reference/tools/cross_encoder_providers/find) | Find CrossEncoderProvider with predicate. | admin |
| [`cross_encoder_providers__get_access`](reference/tools/cross_encoder_providers/get_access) | Who CrossEncoderProvider is shared with. | user |
| [`cross_encoder_providers__get_cross_encoder_provider_models`](reference/tools/cross_encoder_providers/get_cross_encoder_provider_models) | Fetch live model list from the cross-encoder provider. | admin |
| [`cross_encoder_providers__invalidate_cross_encoder_provider`](reference/tools/cross_encoder_providers/invalidate_cross_encoder_provider) | Invalidate cached cross-encoder adapter. | admin |
| [`cross_encoder_providers__put_access`](reference/tools/cross_encoder_providers/put_access) | Change who CrossEncoderProvider is shared with (owners only). | user |
| [`embedding_providers__discover_embedding_models`](reference/tools/embedding_providers/discover_embedding_models) | Probe a draft embedding provider for its model list. | admin |
| [`embedding_providers__find`](reference/tools/embedding_providers/find) | Find EmbeddingProvider with predicate. | admin |
| [`embedding_providers__get_access`](reference/tools/embedding_providers/get_access) | Who EmbeddingProvider is shared with. | user |
| [`embedding_providers__get_embedding_provider_models`](reference/tools/embedding_providers/get_embedding_provider_models) | Fetch live model list from the embedding provider. | admin |
| [`embedding_providers__invalidate_embedding_provider`](reference/tools/embedding_providers/invalidate_embedding_provider) | Invalidate cached embedder adapter. | admin |
| [`embedding_providers__put_access`](reference/tools/embedding_providers/put_access) | Change who EmbeddingProvider is shared with (owners only). | user |
| [`llm_providers__discover_llm_models`](reference/tools/llm_providers/discover_llm_models) | Probe a draft LLM provider for its model list. | admin |
| [`llm_providers__discover_saved_llm_models`](reference/tools/llm_providers/discover_saved_llm_models) | Live-probe a saved LLM provider for its upstream model list. | admin |
| [`llm_providers__find`](reference/tools/llm_providers/find) | Find LLMProvider with predicate. | admin |
| [`llm_providers__get_access`](reference/tools/llm_providers/get_access) | Who LLMProvider is shared with. | user |
| [`llm_providers__get_llm_provider_models`](reference/tools/llm_providers/get_llm_provider_models) | List the model names registered on this provider. | admin |
| [`llm_providers__invalidate_llm_provider`](reference/tools/llm_providers/invalidate_llm_provider) | Invalidate cached LLM adapter. | admin |
| [`llm_providers__put_access`](reference/tools/llm_providers/put_access) | Change who LLMProvider is shared with (owners only). | user |
| [`model_profiles__find`](reference/tools/model_profiles/find) | Find ModelProfile with predicate. | admin |
| [`model_profiles__get_access`](reference/tools/model_profiles/get_access) | Who ModelProfile is shared with. | user |
| [`model_profiles__put_access`](reference/tools/model_profiles/put_access) | Change who ModelProfile is shared with (owners only). | user |
| [`providers__draft_test_cross_encoder_provider`](reference/tools/providers/draft_test_cross_encoder_provider) | Test a draft cross-encoder provider before it is saved: check each named Hugging Face model on the Hub with the token. Returns {ok, models, checks} or {ok=false, error}. | admin |
| [`providers__draft_test_embedding_provider`](reference/tools/providers/draft_test_embedding_provider) | Test a draft embedding provider before it is saved: list the models an openai or gemini server offers, or check each named Hugging Face model on the Hub. Returns {ok, models} or {ok=false, error}. | admin |
| [`providers__list_cross_encoder_provider_types`](reference/tools/providers/list_cross_encoder_provider_types) | Provider-type metadata for the cross-encoder provider form. | admin |
| [`providers__list_embedding_provider_types`](reference/tools/providers/list_embedding_provider_types) | Provider-type metadata for the embedding provider form. | admin |
| [`providers__list_llm_provider_types`](reference/tools/providers/list_llm_provider_types) | Provider-type metadata for the console form: the field shape an operator fills in per LLM provider type. | admin |
| [`semantic_search_providers__find`](reference/tools/semantic_search_providers/find) | Find SemanticSearchProvider with predicate. | admin |
| [`semantic_search_providers__list`](reference/tools/semantic_search_providers/list) | List SemanticSearchProvider. | admin |
| [`semantic_search_providers__list_semantic_search_provider_types`](reference/tools/semantic_search_providers/list_semantic_search_provider_types) | Provider-type metadata for the SSP register form. | admin |
| [`semantic_search_providers__test_semantic_search_provider_draft`](reference/tools/semantic_search_providers/test_semantic_search_provider_draft) | Test a draft SSP config's reachability. No schema/filesystem mutation. Returns {ok: true} or {ok: false, error}. | admin |
| [`semantic_search_providers__test_semantic_search_provider_saved`](reference/tools/semantic_search_providers/test_semantic_search_provider_saved) | Test a saved SSP's reachability using its stored (unredacted) config. No schema/filesystem mutation. Returns {ok: true} or {ok: false, error}. | admin |
| [`ssp__get_access`](reference/tools/ssp/get_access) | Who SemanticSearchProvider is shared with. | user |
| [`ssp__put_access`](reference/tools/ssp/put_access) | Change who SemanticSearchProvider is shared with (owners only). | user |
| [`artifact_storage_providers__find`](reference/tools/artifact_storage_providers/find) | Find ArtifactStorageProvider with predicate. | admin |
| [`artifact_storage_providers__list_artifact_storage_types`](reference/tools/artifact_storage_providers/list_artifact_storage_types) | Provider-type metadata for the catalog's artifact-storage form. | user |
| [`artifact_storage_providers__put_active_artifact_storage_config`](reference/tools/artifact_storage_providers/put_active_artifact_storage_config) | Select the artifact store used for new writes. | admin |
| [`artifact_storage_providers__test_artifact_storage_draft`](reference/tools/artifact_storage_providers/test_artifact_storage_draft) | Test a draft artifact-storage provider with a live round trip. | admin |
| [`artifact_storage_providers__test_saved_artifact_storage_provider`](reference/tools/artifact_storage_providers/test_saved_artifact_storage_provider) | Test a saved artifact-storage provider with a live round trip. | admin |
