"""Deterministic verification of Sleuth's free-text cloud answers.

Data questions run real SQL, so the model never invents numbers. The remaining
risk is fabricated bug references in "answer" mode. These checks are cheap and
require no extra model call: extract the bug numbers an answer cites, partition
them into grounded (present in the retrieved records) and ungrounded, and
append a short caveat for ungrounded citations. The answer text is never
rewritten — a correct answer passes through unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# Matches "#42", "bug 42", "bug #42", "issue 42", "issue #42" (case-insensitive).
# The trailing (?!\d) prevents a long numeric run (e.g. a Unix-ms timestamp)
# from being truncated to its first digits and mistaken for a bug citation.
_CITE_RE = re.compile(r"(?:#|\bbug\s*#?|\bissue\s*#?)(\d{1,9})(?!\d)", re.IGNORECASE)

_CAVEAT = (
    "\n\n_Note: I could not ground {refs} in the records I retrieved — "
    "please verify {pron} directly._"
)

# Strong "I did X to the tracker" forms only — "create/update" are too common
# in normal prose and would produce too many false positives. The pattern has
# no overlapping quantifiers, so there is no catastrophic-backtracking risk.
_WRITE_CLAIM_RE = re.compile(
    r"\bI(?:'ve| have| just| already| then| also| went ahead and)?\s+"
    r"(?:closed|reopened|assigned|unassigned|deleted|removed|resolved|"
    r"commented|marked)\b",
    re.IGNORECASE,
)

_WRITE_CLAIM_CAVEAT = (
    "\n\n_Note: I can't change anything in the tracker myself. To make that "
    'change, type the command (for example "close #12") and confirm the prompt._'
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
    """Return the answer unchanged if all citations are grounded; otherwise
    append a short caveat naming the ungrounded references so the reader
    knows to verify them directly.
    """
    verdict = verify(text, grounded_ids)
    if verdict.ok:
        return text
    refs_sorted = sorted(verdict.ungrounded)
    refs = ", ".join(f"#{i}" for i in refs_sorted)
    pron = "it" if len(refs_sorted) == 1 else "them"
    return text + _CAVEAT.format(refs=refs, pron=pron)


def flag_write_claims(text: str) -> str:
    """Append a correction when an answer claims it performed a tracker write.

    Sleuth never writes from an answer (the executor's firewall blocks it), but
    a confused model can still say it closed or assigned something, misleading
    the user. Like annotate(), this only appends a clearly-marked note; the
    original text is never changed.
    """
    if _WRITE_CLAIM_RE.search(text or ""):
        return text + _WRITE_CLAIM_CAVEAT
    return text


__all__ = ["Verdict", "cited_bug_ids", "verify", "annotate", "flag_write_claims"]
