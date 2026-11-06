---
title: "Embedding model options"
summary: "The option set for the internal-collections embedding model: three HuggingFace models spanning memory tiers, plus any HuggingFace model."
audience: admin
order: 4
related: [providers/embedding, runbooks/configure-internal-collections]
---
## What this is
The enable-internal-collections flow ([runbooks/configure-internal-collections](runbooks/configure-internal-collections)) presents these options when the user has not named a model. This page is the option SET - data, not code: re-dict it here and the flow presents what this table says.

Two facts the flow relies on:

- A clean install already carries a working local embedder: the reserved provider id `huggingface` with model `BAAI/bge-small-en-v1.5` (see [embedding providers](providers/embedding)). Choosing the mid row below needs no registration at all - the flow uses the seeded row.
- The user may instead name ANY HuggingFace model id. The flow checks it with [`system__check_huggingface_models`](reference/tools/system/check_huggingface_models), tests the draft with [`system__test_embedding_provider`](reference/tools/system/test_embedding_provider), and registers it with [`system__create_embedding_provider`](reference/tools/system/create_embedding_provider) (provider `huggingface`, the model id in `models`), exactly like any other embedding provider.

## The option set
Measured 2026-11-06 on a K2-class box (16-core AMD Ryzen 7 7840HS, 27 GB RAM, CPU-only inference; sentence-transformers 6.2; peak RSS over a load + 150 wiki-sized documents; the process baseline - the torch + sentence-transformers import alone - was 734 MB, so the delta column is the model's own cost).

| tier | model id | dim | peak RSS | model delta | 150 docs | notes |
|---|---|---|---|---|---|---|
| small | `sentence-transformers/all-MiniLM-L6-v2` | 384 | 893 MB | ~160 MB | 0.8 s | Fastest; the documented safe pick when the user says "just pick one" (OQ-2: no silent default - the operator still confirms the tier). English-first. |
| mid | `BAAI/bge-small-en-v1.5` | 384 | 929 MB | ~200 MB | 1.4 s | Stronger similarity quality than the small tier; the model a clean install already seeds under the reserved `huggingface` provider, so choosing it adds no registration and no download. |
| large | `BAAI/bge-m3` | 1024 | 3.9 GB | ~3.1 GB | 20 s (load 92 s) | Multilingual + hybrid retrieval; the strongest of the three; needs ~4 GB of free memory - the tier to skip on a memory-constrained box. |

Quality ordering (published similarity benchmarks, strongest to weakest): `bge-m3` > `bge-small-en-v1.5` > `all-MiniLM-L6-v2`. The tiers therefore spread on both memory and quality.

## The optional reranker
The config's reranker field is optional (the vector search runs without one). The measured reference reranker is `BAAI/bge-reranker-v2-m3`: same ~568M parameter class as `bge-m3`, measured peak RSS 2.3 GB on the probe box (load ~3 s from cache; scoring is fast - 8 candidate pairs in 0.35 s). Register it as a `cross_encoder_provider` the same way as an embedding provider, and name it in the config's `cross_encoder` field - it stays editable while the subsystem is active, so adding it later does not disturb the vectors.

## How to choose
- A memory-constrained box: the small tier. The mid tier costs ~40 MB more and buys quality; on a 16-32 GB class machine both are comfortable.
- The user names a model: check it on the Hub, test the draft, register it. The dimension is learned from the first chunk at index time - a mismatch against existing vectors is a `dimension-mismatch` at re-index, which is why the vector-space fields freeze once the subsystem is active.
- The memory numbers are the probe box's measurements; a different CPU, a different sentence-transformers version, or a longer document shifts them. Treat the tier as the unit of choice, not the megabyte.

## Related
- [embedding providers](providers/embedding): the provider rows these models register under.
- [runbooks/configure-internal-collections](runbooks/configure-internal-collections): the flow that presents this set.
