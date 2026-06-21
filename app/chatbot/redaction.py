"""Secret redaction applied before text is sent to the cloud LLM.

The cloud LLM layer (cloud_llm.py) is the only part of Sleuth that makes
outbound HTTP calls. Before any string — the user's message, retrieved
bug/comment text, conversation history — is handed to Gemini or
OpenRouter, it passes through `redact()` here.

Regexes cannot catch every possible secret, but they scrub the common
shapes (API keys, bearer tokens, JWTs, passwords in "password: x" form,
private-key blocks, long hex/base64 blobs) so a stray credential in a bug
description is not shipped to a third party. Operators who cannot accept
any egress should leave SLEUTH_CLOUD_ENABLED off.
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
    # Bearer/Authorization tokens. Swallows an optional inner scheme keyword
    # (Bearer/Token) before the actual token, so the whole credential is
    # scrubbed for "Authorization: Bearer <token>". The separator group keeps
    # the `:`-gated form to avoid the adjacent `\s*` ReDoS overlap; the
    # alternation's two branches don't overlap.
    (re.compile(
        r"(?i)\b(authorization|bearer|token)(\s*[:=]\s*|\s+)"
        r"(?:(?:bearer|token)\s+)?\S+"
    ), None),
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
    # Stripe-style secret/restricted keys (underscore-delimited, distinct from
    # the OpenAI sk- hyphen shape above).
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), _REDACTED),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), _REDACTED),
    # GitHub fine-grained PATs (github_pat_…) — underscores mean the base64 rule
    # below never matches them, so they need their own pattern.
    (re.compile(r"\bgithub_pat_\w{20,}\b"), _REDACTED),
    # Other modern token prefixes: npm, HuggingFace, DigitalOcean.
    (re.compile(r"\b(?:npm_|hf_|dop_v1_|doo_v1_)[A-Za-z0-9]{20,}\b"), _REDACTED),
    # Slack tokens and Google OAuth refresh tokens.
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), _REDACTED),
    (re.compile(r"\b1//[A-Za-z0-9_-]{20,}\b"), _REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _REDACTED),
    # GitLab personal access tokens.
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), _REDACTED),
    # SendGrid API keys (SG.<id>.<secret>).
    (re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"), _REDACTED),
    # Twilio Account SID / API-Key SID (AC… / SK… + 32 hex). The bare-hex
    # pattern below already covers the AC form; this also catches the SK form,
    # which has no leading word boundary before its hex run.
    (re.compile(r"\b(?:AC|SK)[0-9a-fA-F]{32}\b"), _REDACTED),
    # Slack app-level tokens.
    (re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b"), _REDACTED),
    # Phone numbers (PII): leading-`+` international form, or US 3-3-4 with
    # separators. Kept deliberately specific (requires `+` or separators) so the
    # many bare numbers in bug text — ids, counts, versions — aren't scrubbed.
    (re.compile(r"\+\d[\d\s().-]{7,18}\d"), _REDACTED),
    (re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"), _REDACTED),
    # Email addresses — PII that should not be shipped to the third-party model.
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), _REDACTED),
    # Long hex (>=32) or base64 (>=40) blobs — likely hashes / raw secrets. The
    # hex pattern runs first; base64 won't re-match the [REDACTED] it leaves behind.
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), _REDACTED),
    (re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"), _REDACTED),
]


def redact(text: str) -> str:
    """Return `text` with recognized secrets replaced by [REDACTED].

    Never raises. Returning the input unchanged on failure would be unsafe,
    so any error fails closed: the whole text is replaced with [REDACTED].
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
