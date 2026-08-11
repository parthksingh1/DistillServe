"""PII detection and redaction.

Two things make this useful rather than theatre:

**Redaction is reversible within a request.** The gateway replaces each match
with a stable placeholder (``<EMAIL_1>``), sends the redacted prompt upstream,
and rehydrates the model's output on the way back. So a model asked to "write a
reply to this customer" still produces a usable reply naming the right person —
the provider just never saw the name. Irreversible masking would break that,
which is why most redaction layers get switched off in production.

**Detectors are pluggable.** The regex layer here is fast, dependency-free and
covers the identifier-shaped PII that dominates production traffic. A Presidio
or transformer NER detector implements the same protocol and composes with it;
neither layer knows about the other.

Precision matters more than recall for the regex layer specifically: a false
positive silently corrupts a prompt, and a user cannot tell why the answer got
worse. So the patterns are anchored and validated (Luhn for cards, structural
checks for IBAN) rather than greedy.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class PIIType(StrEnum):
    """Categories of personal data the platform recognises."""

    EMAIL = "EMAIL"
    PHONE = "PHONE"
    CREDIT_CARD = "CREDIT_CARD"
    SSN = "SSN"
    IP_ADDRESS = "IP_ADDRESS"
    API_KEY = "API_KEY"
    IBAN = "IBAN"
    PERSON = "PERSON"


@dataclass(frozen=True, slots=True)
class PIIMatch:
    """One detected span."""

    type: PIIType
    start: int
    end: int
    value: str
    detector: str = "regex"


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """A redacted text plus the mapping needed to undo it."""

    text: str
    matches: tuple[PIIMatch, ...] = ()
    #: placeholder -> original value, used to rehydrate the model's output.
    placeholders: dict[str, str] = field(default_factory=dict)

    @property
    def redacted(self) -> bool:
        """Whether anything was replaced."""
        return bool(self.matches)

    def rehydrate(self, text: str) -> str:
        """Restore original values in ``text``.

        Longest placeholder first, so ``<EMAIL_1>`` cannot be partially matched
        inside ``<EMAIL_10>``.
        """
        for placeholder in sorted(self.placeholders, key=len, reverse=True):
            text = text.replace(placeholder, self.placeholders[placeholder])
        return text


@runtime_checkable
class PIIDetector(Protocol):
    """Anything that can find personal data in text."""

    @property
    def name(self) -> str:
        """Detector identifier, recorded on the trace."""
        ...

    def detect(self, text: str) -> Sequence[PIIMatch]:
        """Return the spans of personal data found in ``text``."""
        ...


def _luhn_valid(digits: str) -> bool:
    """Check a card number against the Luhn checksum.

    Without this, any 16-digit sequence — an order id, a timestamp pair — reads
    as a credit card, and the redactor starts corrupting ordinary prompts.
    """
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


@dataclass(frozen=True, slots=True)
class _Pattern:
    """A compiled detector pattern with an optional validator."""

    type: PIIType
    regex: re.Pattern[str]
    validate: str = ""


_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        PIIType.EMAIL,
        re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    ),
    _Pattern(
        # Provider keys are matched before generic tokens: leaking one of these
        # into a prompt log is the most expensive mistake on this list.
        PIIType.API_KEY,
        re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{30,})\b"),
    ),
    _Pattern(
        PIIType.CREDIT_CARD,
        re.compile(r"\b(?:\d[ -]?){13,19}\b"),
        validate="luhn",
    ),
    _Pattern(
        PIIType.SSN,
        # Excludes the ranges the SSA never issues, which removes most false
        # positives from ids that merely look like an SSN.
        re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    ),
    _Pattern(
        PIIType.IBAN,
        re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    ),
    _Pattern(
        PIIType.PHONE,
        re.compile(r"(?<!\d)(?:\+\d{1,3}[ -]?)?(?:\(\d{3}\)|\d{3})[ -]\d{3}[ -]\d{4}(?!\d)"),
    ),
    _Pattern(
        PIIType.IP_ADDRESS,
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    ),
)


class RegexPIIDetector:
    """Pattern-based detector for identifier-shaped personal data."""

    @property
    def name(self) -> str:
        """Detector identifier."""
        return "regex"

    def detect(self, text: str) -> Sequence[PIIMatch]:
        """Find PII spans, preferring the earliest and longest non-overlapping ones."""
        found: list[PIIMatch] = []
        for pattern in _PATTERNS:
            for match in pattern.regex.finditer(text):
                value = match.group(0)
                if pattern.validate == "luhn" and not _luhn_valid(re.sub(r"\D", "", value)):
                    continue
                found.append(
                    PIIMatch(
                        type=pattern.type,
                        start=match.start(),
                        end=match.end(),
                        value=value,
                        detector=self.name,
                    )
                )
        return _resolve_overlaps(found)


def _resolve_overlaps(matches: Iterable[PIIMatch]) -> list[PIIMatch]:
    """Drop overlapping spans, keeping the longest.

    A 16-digit card inside a longer numeric run would otherwise be redacted
    twice, producing nested placeholders that cannot be rehydrated.
    """
    ordered = sorted(matches, key=lambda m: (m.start, -(m.end - m.start)))
    kept: list[PIIMatch] = []
    for match in ordered:
        if kept and match.start < kept[-1].end:
            continue
        kept.append(match)
    return kept


class PIIRedactor:
    """Replaces detected PII with stable, reversible placeholders."""

    def __init__(self, detectors: Sequence[PIIDetector] | None = None) -> None:
        """Compose one or more detectors.

        Args:
            detectors: Detectors to run. Defaults to the regex detector alone;
                a Presidio or NER detector is appended here without any other
                code changing.
        """
        self._detectors: tuple[PIIDetector, ...] = tuple(detectors or (RegexPIIDetector(),))

    def redact(self, text: str) -> RedactionResult:
        """Replace personal data in ``text`` with placeholders.

        Repeated occurrences of the same value share one placeholder, so a
        prompt naming the same person twice still reads coherently to the model.
        """
        matches = _resolve_overlaps(
            [match for detector in self._detectors for match in detector.detect(text)]
        )
        if not matches:
            return RedactionResult(text=text)

        placeholders: dict[str, str] = {}
        assigned: dict[tuple[PIIType, str], str] = {}
        counters: dict[PIIType, int] = {}
        pieces: list[str] = []
        cursor = 0

        for match in matches:
            key = (match.type, match.value)
            if key not in assigned:
                counters[match.type] = counters.get(match.type, 0) + 1
                token = f"<{match.type.value}_{counters[match.type]}>"
                assigned[key] = token
                placeholders[token] = match.value
            pieces.append(text[cursor : match.start])
            pieces.append(assigned[key])
            cursor = match.end

        pieces.append(text[cursor:])
        return RedactionResult(
            text="".join(pieces), matches=tuple(matches), placeholders=placeholders
        )
