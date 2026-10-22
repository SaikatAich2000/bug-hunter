<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Sleuth, the assistant

Ask questions and run actions in plain English.

> [← Back to the README](../README.md)

## Sleuth

Sleuth is the in-app assistant, a floating widget on every page (open it with
`Ctrl + /` or `⌘ + /`).

**Ask questions** (answered by exact SQL handlers):

- *show open bugs assigned to alice*
- *how many critical bugs are in PROD?*
- *export all bugs in apollo to excel* (returns a real `.xlsx`)

**Run actions** (always confirmed before any change, always audited):

- *close bug 5* · *reopen #12* · *assign bug 3 to alice* · *set bug 9 priority to high*
- *comment on #5: looks fixed in v2.1*
- *create a bug titled "Login broken" in project Apollo*

With the cloud layer on, Sleuth can also call tools for agile work: create a
work item under a parent, move it through the workflow, and add it to or remove
it from a sprint. Every write still needs your confirmation and uses your own
permissions.

### How it works

Sleuth tries the cheapest layer first:

1. **Rules** (`app/chatbot/nlu.py`) is a regex parser over verbs, filters, names and IDs.
2. **Statistical classifier** (`app/chatbot/classifier.py`) uses TF-IDF and cosine similarity, with no external models.
3. **Local LLM** (`app/chatbot/llm.py`) is an optional `llama.cpp` backend for a GGUF model in `models/`. It stays dormant without a model file.
4. **Cloud LLM** (`app/chatbot/cloud_llm.py`, `llm_tools_agent.py`) is optional and off by default: Groq (primary) or OpenRouter (fallback), with tool calling.

With the defaults (`SLEUTH_CLOUD_ENABLED=0`, no model file), Sleuth is fully
local and makes no outbound HTTP calls. The cloud layer is the only path that
sends text off the box, and all text first passes through a secret-redaction
filter (`app/chatbot/redaction.py`). Optional read-only add-ons for the cloud
layer are `SLEUTH_RETRIEVAL_ENABLED` (keyword grounding),
`SLEUTH_AGENT_ENABLED` (multi-step lookups), `SLEUTH_VERIFY_ANSWERS` (citation
check) and `SLEUTH_EVAL_ENABLED` (LLM-as-judge note). `/api/chat` is limited to
30 messages per minute per user.
