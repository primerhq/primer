"""Shared model-family → query/document prompt mapping.

Asymmetric-retrieval embedding models (BGE, E5, nomic-embed-text)
expect different instruction prefixes on the query side vs the
document side. Without the prefix the query lands in a slightly
different region of vector space and similarity scores collapse —
on bge-small-en-v1.5 a "web search" query against the indexed
``web-search: Perform a web search…`` tool description drops from
~0.7 to ~0.25 without the query prefix.

Both adapters that can serve these models (HuggingFace via
``sentence-transformers``, OpenAI-compatible via the LM Studio /
Ollama / vLLM proxy paths) consult this module so the same prompt
discipline is applied regardless of which adapter is on the wire.
"""

from __future__ import annotations

from collections.abc import Mapping


# Mapping of model-family substring → (query_prompt, document_prompt).
# Both can be None when the family doesn't recommend a prefix. Match
# is by case-insensitive substring on the SentenceTransformer / OpenAI
# model id. Operators who use a model not on this list (or who want to
# override the defaults) can pass ``config.raw["query_prompt"]`` /
# ``config.raw["document_prompt"]`` to bypass.
_MODEL_FAMILY_PROMPTS: list[tuple[str, str | None, str | None]] = [
    # BGE: asymmetric. Query gets the prompt; document does not.
    # Source: model card (BAAI/bge-small-en-v1.5, bge-large, bge-m3).
    ("bge", "Represent this sentence for searching relevant passages: ", None),
    # E5: symmetric prefixes. Both sides need a prefix to be in-distribution.
    # Source: intfloat/e5-* / multilingual-e5-* model cards.
    ("e5", "query: ", "passage: "),
    # nomic-embed-text: symmetric task-prefixed.
    # Source: nomic-ai/nomic-embed-text-v1* / v1.5 model card.
    ("nomic-embed-text", "search_query: ", "search_document: "),
]


def resolve_prompts_for_model(model_name: str) -> tuple[str | None, str | None]:
    """Return (query_prompt, document_prompt) for a model.

    Unknown families get (None, None) — encode raw text on both sides,
    matching the default SentenceTransformer behaviour.
    """
    lower = model_name.lower()
    for needle, qp, dp in _MODEL_FAMILY_PROMPTS:
        if needle in lower:
            return qp, dp
    return None, None


def select_prompt(
    *,
    task_type: str | None,
    model_name: str,
    raw: Mapping[str, object] | None = None,
) -> str | None:
    """Pick the prompt prefix for this call.

    Precedence: explicit ``raw["query_prompt"]`` / ``raw["document_prompt"]``
    override family defaults, so an operator can use a non-default
    prompt for a model we don't recognise. Without a ``task_type`` hint
    we treat the input as a document (the conservative choice — only
    the search code path opts into ``retrieval_query``).
    """
    raw_map: Mapping[str, object] = raw or {}
    if task_type == "retrieval_query":
        if "query_prompt" in raw_map:
            v = raw_map.get("query_prompt")
            return v or None  # type: ignore[return-value]
        return resolve_prompts_for_model(model_name)[0]
    # Default to document semantics for everything else (None task_type,
    # retrieval_document, semantic_similarity, classification, ...). Only
    # E5 / nomic actually prefix documents; BGE / MiniLM / GTE do not.
    if "document_prompt" in raw_map:
        v = raw_map.get("document_prompt")
        return v or None  # type: ignore[return-value]
    return resolve_prompts_for_model(model_name)[1]


__all__ = [
    "resolve_prompts_for_model",
    "select_prompt",
]
