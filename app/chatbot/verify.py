"""Deterministic verification of Sleuth's free-text cloud answers.

The model may phrase an answer, but it must not invent facts. Data questions
already run real SQL (the model never produces a number), so the remaining
risk is fabricated bug references in "answer" mode. These checks are cheap and
deterministic — no extra model call:

  - extract the bug numbers an answer cites,
  - partition them into grounded (present in the retrieved records) vs
    ungrounded, and
  - append a short transparency caveat for ungrounded citations rather than
    silently trusting them.

The answer text is never rewritten, only annotated, so a correct answer is
left exactly as-is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# Matches "#42", "bug 42", "bug #42", "issue 42", "issue #42" (case-insensitive).
_CITE_RE = re.compile(r"(?:#|\bbug\s*#?|\bissue\s*#?)(\d{1,9})", re.IGNORECASE)

_CAVEAT = (
    "\n\n_Note: I could not ground {refs} in the records I retrieved — "
    "please verify {pron} directly._"
)


def cited_bug_ids(text: str) -> set[int]:
    """Bug numbers referenced in an answer, from '#42' / 'bug 42' forms."""
    return {int(m) for m in _CITE_RE.findall(text or "")}


@dataclass
class Verdict:
    """The result of checking an answer's citations against the grounding set."""
    grounded: set[int] = field(default_factory=set)
    ungrounded: set[int] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.ungrounded


def verify(text: str, grounded_ids: Iterable[int]) -> Verdict:
    """Partition an answer's bug citations into grounded vs ungrounded."""
    allowed = set(grounded_ids or ())
    cited = cited_bug_ids(text)
    return Verdict(grounded=cited & allowed, ungrounded=cited - allowed)


def annotate(text: str, grounded_ids: Iterable[int]) -> str:
    """Return the answer unchanged when every citation is grounded, otherwise
    append a short caveat naming the ungrounded references.

    The answer is never silently edited; the caveat only flags what could not
    be verified so the reader does not over-trust it.
    """
    verdict = verify(text, grounded_ids)
    if verdict.ok:
        return text
    refs_sorted = sorted(verdict.ungrounded)
    refs = ", ".join(f"#{i}" for i in refs_sorted)
    pron = "it" if len(refs_sorted) == 1 else "them"
    return text + _CAVEAT.format(refs=refs, pron=pron)


__all__ = ["Verdict", "cited_bug_ids", "verify", "annotate"]
