"""Sleuth — Bug Hunter's built-in assistant.

The in-app conversational assistant. Users can ask questions in natural
language ("show open bugs assigned to alice") and request actions
("close bug 5", "comment on #12: works for me"). Every write goes through
an explicit Yes/Cancel confirmation prompt and is recorded in the same
audit log the REST API uses.

Layers, ordered by cost:

  - nlu.py        Layer 1: pure-Python regex parser, handles most typical
                  queries deterministically.
  - classifier.py Layer 2: TF-IDF + cosine similarity over a hand-curated
                  corpus, for paraphrases the rules miss.
  - llm.py        Layer 3: optional local llama.cpp inference. Lazy-loaded
                  if a GGUF model is dropped into models/. Makes no external
                  API calls. Refuses to load when the container is too small
                  to fit the model (see memory_budget()).
  - cloud_llm.py  Layer 4: optional cloud LLM (Gemini primary, OpenRouter
                  fallback). Off unless SLEUTH_CLOUD_ENABLED and a key are
                  set. Read-only by construction: data questions are routed
                  back through the SQL handlers and any write intent is
                  dropped. rag.py grounds it with bug / comment / doc
                  retrieval; redaction.py scrubs secrets first.
                  Optional, flag-gated read-only helpers (off by default):
                  retrieval.py (keyword grounding), agent.py (bounded
                  multi-step reasoning over the same read-only tools and
                  write firewall), verify.py (citation checking) and
                  evals.py (LLM-as-judge that only annotates).
  - executor.py   Read intents (list/count/detail/stats/export) run SQL
                  SELECTs only.
  - actions.py    Write intents (assign/close/comment/create/...) are
                  permission-checked, audited, atomic mutations.
  - memory.py     Per-user conversation context with TTL. Resolves
                  pronouns ("it", "that bug") and stages pending
                  confirmations.
  - excel.py      In-memory Excel rendering with openpyxl.
  - router.py     FastAPI router exposing /api/chat/ask and the
                  download endpoint for generated files.

Database safety: the core read/write path adds no tables and issues
SELECTs for reads; writes are atomic and roll back fully on error or
permission denial. The optional cloud layer adds two additive conversation
tables (chat_conversations, chat_messages); existing tables and data are
untouched. See tests/test_sleuth_safety.py.

Privacy: by default Sleuth makes no outbound HTTP calls — the Layer 3 LLM
runs locally via llama.cpp and there are no keys to configure. The Layer 4
cloud LLM is opt-in (SLEUTH_CLOUD_ENABLED + a key); when enabled, it is the
only component that sends data off-box, and everything it sends passes
through redaction.py first. Leave it off for a fully local, no-egress
deployment.
"""

__all__ = [
    "nlu", "executor", "actions", "memory", "classifier", "llm",
    "cloud_llm", "rag", "redaction", "excel", "router",
]
