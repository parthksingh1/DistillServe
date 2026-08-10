"""Task classification for routing.

Routing decides whether a request is cheap enough to serve from the distilled
student. That decision hinges on *what kind of work* the prompt is, so the
classifier's job is to label the task, not to guess the answer's difficulty.

The design is two-layer on purpose:

1. **Rules** — keyword and shape heuristics. Microseconds, no model load, and
   they cover the overwhelming majority of production traffic, which is
   templated rather than free-form. They are also auditable: an operator can
   read why a request routed the way it did.
2. **Embedding fallback** — a small sentence-transformer nearest-centroid
   classifier for prompts the rules do not recognise. It lives behind
   :class:`TaskClassifier`, so it can be absent (the `ml` extra not installed)
   without changing any call site.

When neither layer is confident the router escalates to the teacher. Escalating
on uncertainty is the correct bias: routing a hard prompt to the student costs
quality, while routing an easy one to the teacher costs cents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: Confidence below which the router refuses to trust a label and escalates.
ESCALATION_THRESHOLD = 0.55

#: The task taxonomy. These labels are the join key between routing, the
#: adapter registry and the per-slice eval heatmap, so they are a contract:
#: renaming one invalidates historical eval reports.
TASK_LABELS: tuple[str, ...] = (
    "summarization",
    "extraction",
    "classification",
    "code_generation",
    "sql_generation",
    "rewrite",
    "question_answering",
    "reasoning",
    "creative_writing",
    "general",
)


@dataclass(frozen=True, slots=True)
class Classification:
    """A task label with the evidence behind it."""

    task: str
    confidence: float
    reason: str
    layer: str = "rules"


@runtime_checkable
class TaskClassifier(Protocol):
    """Anything that can label a prompt.

    A protocol rather than a base class so the embedding classifier, a hosted
    classifier and a test stub are all interchangeable without inheritance.
    """

    def classify(self, prompt: str) -> Classification:
        """Return the task label for ``prompt``."""
        ...


@dataclass(frozen=True, slots=True)
class _Rule:
    """One keyword rule: a compiled pattern, its label and its confidence."""

    task: str
    pattern: re.Pattern[str]
    confidence: float
    reason: str


def _rule(task: str, keywords: str, confidence: float, reason: str) -> _Rule:
    """Build a word-boundary-anchored, case-insensitive rule.

    Word boundaries matter more than they look: without them "sum" matches
    inside "summary", "consume" and "assume", and the summarization rule starts
    swallowing unrelated traffic.
    """
    return _Rule(
        task=task,
        pattern=re.compile(rf"\b(?:{keywords})\b", re.IGNORECASE),
        confidence=confidence,
        reason=reason,
    )


# Ordered by specificity: the first match wins, so narrow rules (SQL, code)
# precede broad ones (question answering). A prompt asking to "write a SQL
# query that summarizes sales" is SQL generation, not summarization.
_RULES: tuple[_Rule, ...] = (
    _rule(
        "sql_generation",
        r"sql|select \* from|postgres|sqlite|query the (?:table|database)",
        0.92,
        "prompt names SQL or a database dialect",
    ),
    _rule(
        "code_generation",
        r"function|refactor|python|typescript|javascript|unit test|stack trace|traceback|regex",
        0.88,
        "prompt names a programming language or code construct",
    ),
    _rule(
        "extraction",
        r"extract|parse|pull out|list all|as json|json schema|fields?",
        0.86,
        "prompt asks for structured fields to be pulled from text",
    ),
    _rule(
        "summarization",
        r"summar\w*|tl;?dr|condense|key points|in (?:one|two|three) sentences?",
        0.9,
        "prompt asks for a shorter form of provided text",
    ),
    _rule(
        "classification",
        r"classify|categor\w+|label|sentiment|is this (?:a|an)|which category",
        0.85,
        "prompt asks for a label from a closed set",
    ),
    _rule(
        "rewrite",
        r"rewrite|rephrase|paraphrase|translate|make (?:it|this) (?:shorter|clearer|formal)",
        0.84,
        "prompt asks for a transformation of provided text",
    ),
    _rule(
        "reasoning",
        r"prove|derive|step by step|reason through|explain why|trade-?offs?|analy[sz]e",
        0.72,
        "prompt asks for multi-step reasoning",
    ),
    _rule(
        "creative_writing",
        r"poem|story|screenplay|lyrics|write (?:a|an) (?:essay|blog|narrative)",
        0.8,
        "prompt asks for open-ended creative output",
    ),
    _rule(
        "question_answering",
        r"what|who|when|where|why|how|which",
        0.6,
        "prompt is interrogative with no more specific signal",
    ),
)


class RuleTaskClassifier:
    """Keyword-and-shape classifier. Fast, auditable, no model load."""

    def classify(self, prompt: str) -> Classification:
        """Label ``prompt`` using the first matching rule.

        Args:
            prompt: The user turn to classify.

        Returns:
            A :class:`Classification`; ``general`` with low confidence when no
            rule matches, which the router treats as an escalation signal.
        """
        text = prompt.strip()
        if not text:
            return Classification(
                task="general", confidence=0.0, reason="empty prompt", layer="rules"
            )

        for rule in _RULES:
            if rule.pattern.search(text):
                return Classification(
                    task=rule.task,
                    confidence=self._adjust(rule.confidence, text),
                    reason=rule.reason,
                    layer="rules",
                )

        return Classification(
            task="general",
            confidence=0.3,
            reason="no rule matched",
            layer="rules",
        )

    @staticmethod
    def _adjust(base: float, text: str) -> float:
        """Damp confidence for very long prompts.

        A 4,000-character prompt that happens to contain "summarize" is far
        more likely to be a compound task than a 40-character one. Damping
        pushes those toward the teacher rather than mislabelling them
        confidently.
        """
        if len(text) <= 2_000:
            return base
        penalty = min(0.25, (len(text) - 2_000) / 20_000)
        return max(0.0, base - penalty)


class LayeredTaskClassifier:
    """Rules first, embedding classifier second.

    The fallback is consulted only when the rules layer is unconfident, so the
    common path never pays for an embedding. When no fallback is configured
    (the `ml` extra is not installed) the rules result stands and the router
    escalates on low confidence — degraded, never broken.
    """

    def __init__(
        self,
        rules: TaskClassifier | None = None,
        fallback: TaskClassifier | None = None,
        *,
        threshold: float = ESCALATION_THRESHOLD,
    ) -> None:
        """Compose the two layers.

        Args:
            rules: Primary classifier. Defaults to :class:`RuleTaskClassifier`.
            fallback: Consulted when the primary is below ``threshold``.
            threshold: Confidence below which the fallback is consulted.
        """
        self._rules = rules or RuleTaskClassifier()
        self._fallback = fallback
        self._threshold = threshold

    def classify(self, prompt: str) -> Classification:
        """Return the best available label for ``prompt``."""
        primary = self._rules.classify(prompt)
        if primary.confidence >= self._threshold or self._fallback is None:
            return primary

        secondary = self._fallback.classify(prompt)
        # Keep whichever layer is more confident, rather than blindly
        # preferring the model: the rules encode operator knowledge the
        # embedding centroids do not have.
        return secondary if secondary.confidence > primary.confidence else primary
