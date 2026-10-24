"""The providers pages for the model families (pivot P4, task 13).

Four admin feature pages, one per model family: ``llm`` (the provider rows
agents talk through), ``model-profiles`` (the registry of what a provider
serves, single and aggregated), ``embedding`` (the embedders) and
``cross-encoder`` (the rerankers). The embedding and cross-encoder pages
list the probe tools as conditional: they are shown when live.
"""

from tests.docs._wiki_specs import PageSpec, scoped

SPECS = (
    PageSpec("providers/llm", "LLM providers",
             "Admin: register the model servers agents talk to and list the models a provider offers.",
             "feature", 1,
             audience="admin",
             tools=scoped("system", "list_llm_providers get_llm_provider create_llm_provider update_llm_provider delete_llm_provider find_llm_providers fetch_llm_provider_models"),
             related=("providers/model-profiles", "agents/create-and-edit")),
    PageSpec("providers/model-profiles", "Model profiles",
             "Admin: a model profile names a provider, a model and a context length. Agents bind to profiles, and a profile can pool others.",
             "feature", 2,
             audience="admin",
             tools=scoped("system", "list_model_profiles get_model_profile create_model_profile update_model_profile delete_model_profile find_model_profiles"),
             related=("providers/llm", "agents/create-and-edit")),
    PageSpec("providers/embedding", "Embedding providers",
             "Admin: register the embedders that turn text into vectors for semantic search.",
             "feature", 3,
             audience="admin",
             tools=scoped("system", "list_embedding_providers get_embedding_provider create_embedding_provider update_embedding_provider delete_embedding_provider find_embedding_providers fetch_embedding_provider_models"),
             tools_if_present=scoped("system", "check_huggingface_models test_embedding_provider"),
             related=("providers/semantic-search", "knowledge/collections")),
    PageSpec("providers/cross-encoder", "Cross-encoder providers",
             "Admin: register the rerankers that reorder search results.",
             "feature", 4,
             audience="admin",
             tools=scoped("system", "list_cross_encoder_providers get_cross_encoder_provider create_cross_encoder_provider update_cross_encoder_provider delete_cross_encoder_provider find_cross_encoder_providers fetch_cross_encoder_provider_models"),
             tools_if_present=scoped("system", "test_cross_encoder_provider"),
             related=("providers/embedding", "knowledge/search")),
)
