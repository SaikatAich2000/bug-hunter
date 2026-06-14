"""Unit tests for the v3.0 Sleuth smartness improvements.

All pure / in-process — no DB, no network, no LLM:
  - bare (unquoted) free-text search after a topic cue word
  - the enum-word guard that stops a filter phrase being treated as text
  - quoted-search precedence
  - the classifier-ranked "did you mean" suggestion (best-effort, never raises)
  - the unknown-intent fallback Response shape
"""
from __future__ import annotations

import os

# Keep Sleuth's cloud layer off and give the app a throwaway DB URL so importing
# app modules never touches a real database (these tests don't query it).
os.environ.setdefault("SLEUTH_CLOUD_ENABLED", "0")
os.environ.setdefault("DATABASE_URL", "sqlite:///./_sleuth_smart_test.db")
os.environ.setdefault("SESSION_SECRET", "x" * 32)

from app.chatbot import nlu  # noqa: E402
from app.chatbot.executor import _did_you_mean, _handle_unknown  # noqa: E402


# ---------------------------------------------------------------------------
# Bare free-text search
# ---------------------------------------------------------------------------
def test_bare_text_search_after_cue_words():
    assert nlu._extract_text_search("find bugs about login crash") == "login crash"
    assert nlu._extract_text_search("issues regarding checkout") == "checkout"
    assert nlu._extract_text_search("anything mentioning timeout") == "timeout"
    assert nlu._extract_text_search("tickets concerning the API") == "the API"
    assert nlu._extract_text_search("bugs related to payments") == "payments"


def test_quoted_search_takes_precedence():
    # An explicit quote wins over the bare-cue path.
    assert nlu._extract_text_search('bugs about "exact phrase"') == "exact phrase"


def test_no_cue_means_no_text_search():
    assert nlu._extract_text_search("list open bugs") is None
    assert nlu._extract_text_search("how many critical bugs in PROD") is None


def test_enum_only_phrase_is_not_text_search():
    # "high priority" / "in progress" are filters, not search topics — the enum
    # extractors should own them, so the bare-text path must decline.
    assert nlu._extract_text_search("bugs about high priority") is None
    assert nlu._extract_text_search("anything regarding in progress") is None


def test_trailing_filter_clause_is_stripped():
    # The topic is captured, the "in project X" tail is dropped.
    assert nlu._extract_text_search("bugs about login in project Apollo") == "login"


# ---------------------------------------------------------------------------
# "Did you mean" — best-effort, deterministic, never raises
# ---------------------------------------------------------------------------
def test_did_you_mean_is_safe_on_gibberish():
    # Pure noise has no signal — returns None, never raises.
    assert _did_you_mean("qwerty zxcvbn asdfgh") is None
    assert _did_you_mean("") is None


def test_did_you_mean_returns_known_phrase_or_none():
    # Whatever it returns must be one of the canonical suggestions (or None);
    # it must never invent a string or throw.
    suggestions = set(__import__(
        "app.chatbot.executor", fromlist=["_INTENT_SUGGESTION"],
    )._INTENT_SUGGESTION.values())
    for msg in ("show me the bugs", "count things", "who are the people",
                "export everything", "what changed"):
        out = _did_you_mean(msg)
        assert out is None or out in suggestions


# ---------------------------------------------------------------------------
# Unknown-intent fallback
# ---------------------------------------------------------------------------
def test_handle_unknown_response_shape():
    resp = _handle_unknown("flibber the wobbular")
    assert resp.intent == "unknown"
    assert resp.fallback_eligible is True
    assert resp.blocks and resp.blocks[0].kind == "text"
    assert "help" in resp.blocks[0].payload["text"].lower()
