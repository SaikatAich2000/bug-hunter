# Local LLM models for Sleuth

This folder holds the optional local LLM — Sleuth's third layer. Drop a GGUF
model file here and install `llama-cpp-python`, and Sleuth uses it as a fallback
for queries the rule parser and the classifier can't handle.

Model files are large and are **not committed** — `models/*.gguf`, `*.bin`, and
`*.safetensors` are gitignored. Each deployment downloads its own model.

You can skip this entirely: the rule parser and classifier handle most queries,
and the LLM is only consulted when both are unsure.

## Choosing a model

The target box is small (roughly 1 CPU core, 2 GB RAM, no GPU), so the model
must fit in RAM alongside FastAPI and PostgreSQL. Pick a small model.

| Model | Size on disk | RAM | Speed (tok/s, 1 CPU) | Quality |
|---|---|---|---|---|
| Qwen 2.5 0.5B Instruct Q4_K_M | ~370 MB | ~450 MB | 8–15 | good for intent JSON |
| SmolLM 360M Instruct Q4_K_M | ~230 MB | ~290 MB | 12–20 | acceptable |
| TinyLlama 1.1B Chat Q4_K_M | ~640 MB | ~750 MB | 4–8 | better, slower |

Models larger than ~750 MB will not fit alongside PostgreSQL on a 2 GB box. Qwen
2.5 0.5B is the best balance for this use case.

## Install and download

```bash
pip install llama-cpp-python    # CPU build; no CUDA/Metal flags needed

cd models
# Qwen 2.5 0.5B Instruct (recommended):
wget https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf -O sleuth.gguf
```

The file must be named `sleuth.gguf`, or set `SLEUTH_LLM_MODEL_PATH` to point
elsewhere. Restart the app; the model loads lazily on the first query that falls
through to the LLM (a few seconds), after which inference takes about 5–15 s per
fallback query. Leave this directory empty to disable LLM fallback entirely.

## Tuning

| Variable | Default | Purpose |
|---|---|---|
| `SLEUTH_LLM_MODEL_PATH` | `models/sleuth.gguf` | Path to the GGUF file. |
| `SLEUTH_LLM_TIMEOUT_S` | `12` | Inference budget, in seconds. |
| `SLEUTH_LLM_IDLE_UNLOAD_S` | `600` | Unload the model after this many idle seconds. |
| `SLEUTH_LLM_MAX_TOKENS` | `120` | Cap on generated tokens per call. |
| `SLEUTH_LLM_CTX_LEN` | `1024` | Context window (lower uses less RAM). |
| `SLEUTH_LLM_THREADS` | `1` | CPU threads (set to your core count). |

## Privacy

The local LLM runs entirely on this server; no data leaves the box on this path.
A separate, off-by-default cloud layer (`SLEUTH_CLOUD_ENABLED=1`) can send
free-form questions to Gemini or OpenRouter — the only path that sends data out,
and only after secret redaction. See the README *Sleuth* section.
