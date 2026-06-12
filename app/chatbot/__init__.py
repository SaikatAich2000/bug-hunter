"""Sleuth — Bug Hunter's built-in AI assistant.

Sleuth is the in-app conversational assistant. Users can ask questions
in natural language ("show open bugs assigned to alice") AND request
actions ("close bug 5", "comment on #12: works for me"). Every write
goes through an explicit Yes/Cancel confirmation prompt and is recorded
in the same audit log the REST API uses.

Architecture — three layers, ordered by cost:

  message ──► nlu (rules)  ──► executor (read)  ──► blocks
                  │                       │
                  ├─► classifier (TF-IDF)  ├─► actions (write, audited)
                  │                       │
                  └─► llm (optional)       └─► memory (per-user context)

  - nlu.py        Layer 1: pure-Python regex parser. Microseconds.
                  Catches ~80% of typical queries deterministically.
  - classifier.py Layer 2: TF-IDF + cosine similarity over a hand-curated
                  corpus. ~1 ms. Catches paraphrases the rules miss.
  - llm.py        Layer 3: OPTIONAL local llama.cpp inference. Lazy-loaded
                  if a GGUF model is dropped into models/. NEVER calls an
                  external API. Refuses to load when the container is too
                  small to fit the model (see memory_budget()).
  - cloud_llm.py  Layer 4: OPTIONAL cloud LLM (Gemini primary, OpenRouter
                  fallback). OFF unless SLEUTH_CLOUD_ENABLED + a key are
                  set. Read-only by construction: data questions are routed
                  back through the SQL handlers (numbers never invented) and
                  any write intent is dropped. rag.py grounds it with bug /
                  comment / doc retrieval; redaction.py scrubs secrets first.
  - executor.py   Read intents (list/count/detail/stats/export) → SQL
                  SELECTs only. Never writes.
  - actions.py    Write intents (assign/close/comment/create/...) →
                  permission-checked, audited, atomic mutations.
  - memory.py     Per-user conversation context with TTL. Resolves
                  pronouns ("it", "that bug") and stages pending
                  confirmations.
  - excel.py      In-memory Excel rendering with openpyxl.
  - router.py     FastAPI router exposing /api/chat/ask and the
                  download endpoint for generated files.

Database safety guarantee: the core read/write path adds NO tables and
issues SELECTs for reads; writes are atomic and roll back fully on error
or permission denial. The optional cloud layer adds exactly two ADDITIVE
conversation tables (chat_conversations, chat_messages) — new tables only,
existing tables and data untouched. See tests/test_sleuth_safety.py.

Privacy: by DEFAULT Sleuth makes NO outbound HTTP calls — the Layer 3 LLM
runs locally via llama.cpp and there are no keys to configure. The Layer 4
cloud LLM is strictly opt-in (SLEUTH_CLOUD_ENABLED + a key); when enabled,
it is the ONLY component that sends data off-box, and everything it sends
is passed through redaction.py first. Leave it off for a fully local,
no-egress deployment.
"""

__all__ = [
    "nlu", "executor", "actions", "memory", "classifier", "llm",
    "cloud_llm", "rag", "redaction", "excel", "router",
]
