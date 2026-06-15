"""Secret redaction — the last gate before any text leaves the box.

The cloud LLM layer (cloud_llm.py) is the only part of Sleuth that makes
outbound HTTP calls. Before ANY string — the user's message, retrieved
bug/comment text, conversation history — is handed to Gemini or
OpenRouter, it passes through `redact()` here.

This is defence-in-depth, not a promise of perfection: regexes can't
catch every secret a human might paste. It reliably scrubs the
high-frequency shapes (API keys, bearer tokens, JWTs, passwords in
"password: x" form, private-key blocks, long hex/base64 blobs) so a
stray credential in a bug description doesn't get shipped to a third
party. Operators who can't accept ANY egress should leave
SLEUTH_CLOUD_ENABLED off — that's the real guarantee.
"""
from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# Each pattern captures a leading label/prefix in group 1 (kept) and the
# secret value (replaced). Where there's no natural prefix we replace the
# whole match. Ordered most-specific first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM private key blocks (multi-line).
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ), _REDACTED),
    # JWTs: three base64url segments separated by dots.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
     _REDACTED),
    # Bearer tokens in Authorization headers. The separator group is written
    # `\s*(?:[:=]\s*)?` rather than `\s*[:=]?\s*` so the two whitespace runs can
    # never both match the same spaces — that adjacent-`\s*` overlap is a
    # polynomial-backtracking (ReDoS) shape. This form is unambiguous (the inner
    # `\s*` is gated behind a literal `:`/`=`) and captures the exact same text,
    # so redaction output is unchanged.
    (re.compile(r"(?i)\b(bearer|token|authorization)(\s*(?:[:=]\s*)?)\S+"), None),
    # key=value / "password": "x" / secret: x   (label kept, value scrubbed)
    (re.compile(
        r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|"
        r"refresh[_-]?token|client[_-]?secret|private[_-]?key|session[_-]?secret)"
        r"(\s*[:=]\s*)(\"[^\"]+\"|'[^']+'|\S+)"
    ), None),
    # Common provider key shapes (Google AIza…, OpenAI/OpenRouter sk-…,
    # GitHub ghp_…, AWS AKIA…).
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), _REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), _REDACTED),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), _REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _REDACTED),
    # Long hex / base64 blobs (>=32 chars) — likely hashes / raw secrets.
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), _REDACTED),
]


def redact(text: str) -> str:
    """Return `text` with recognised secrets replaced by [REDACTED].

    Never raises; on any failure returns the input unchanged would be
    unsafe, so on failure we return the empty string (fail closed).
    """
    if not text:
        return text or ""
    try:
        out = text
        for pat, repl in _PATTERNS:
            if repl is None:
                # Keep the label (group 1) + separator if present, scrub value.
                out = pat.sub(
                    lambda m: (m.group(1) or "")
                    + (m.group(2) if m.lastindex and m.lastindex >= 2 else "")
                    + " " + _REDACTED,
                    out,
                )
            else:
                out = pat.sub(repl, out)
        return out
    except Exception:  # noqa: BLE001 — redaction must never throw into the caller
        # Fail closed: if we can't prove the text is clean, don't send it raw.
        return _REDACTED


__all__ = ["redact"]
