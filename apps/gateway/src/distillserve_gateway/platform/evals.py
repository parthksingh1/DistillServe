"""The eval harness: LLM-as-judge, pairwise and rubric, with drift monitoring.

Three decisions that separate a usable judge from a number generator:

**Position bias is controlled, not ignored.** Judges systematically prefer
whichever answer they see first — often by 5-10 points of win rate. Every
pairwise comparison is therefore run twice with the candidates swapped, and a
verdict only counts as a win if it survives both orders. Disagreement between
the two orders is recorded as a tie, which is the honest reading: the judge
could not tell them apart.

**The judge is pinned and monitored.** A judge model that silently changes
under you invalidates every historical comparison. A small calibration set with
known-correct verdicts is re-run alongside each evaluation, and the drift from
its expected agreement is reported on the report itself.

**Ties are counted as half a win.** The alternative — discarding them — inflates
the winner's rate and hides the case the eval most needs to surface, which is
"these are indistinguishable, so ship the cheap one".
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from distillserve_gateway.backends.base import GenerationRequest, InferenceBackend
from distillserve_otel import get_logger
from distillserve_schemas import ChatMessage, ChatRole, JudgeResult

log = get_logger(__name__)

JUDGE_SYSTEM_PROMPT = """\
You are an impartial evaluator comparing two AI assistant responses to the same \
request. Judge only on: correctness, completeness, instruction-following, and \
conciseness. Ignore response length unless it affects those. Ignore which \
response appears first.

Reply with exactly one JSON object and nothing else:
{"winner": "A" | "B" | "tie", "reason": "<one sentence>", "confidence": 0.0-1.0}
"""

RUBRIC_SYSTEM_PROMPT = """\
You are grading a single AI assistant response against a rubric. Score each \
dimension from 0.0 to 1.0.

Reply with exactly one JSON object and nothing else:
{"correctness": 0.0-1.0, "completeness": 0.0-1.0, "instruction_following": \
0.0-1.0, "conciseness": 0.0-1.0, "reason": "<one sentence>"}
"""


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One test case."""

    id: str
    slice: str
    prompt: str
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class PairwiseVerdict:
    """The outcome of one debiased pairwise comparison."""

    case_id: str
    slice: str
    winner: str
    """``student``, ``teacher`` or ``tie``."""

    reason: str
    confidence: float
    position_consistent: bool
    """False when swapping the order changed the verdict."""


@dataclass(frozen=True, slots=True)
class RubricScore:
    """Per-dimension rubric grades for one response."""

    case_id: str
    slice: str
    correctness: float
    completeness: float
    instruction_following: float
    conciseness: float
    reason: str

    @property
    def overall(self) -> float:
        """Mean of the four dimensions."""
        return (
            self.correctness + self.completeness + self.instruction_following + self.conciseness
        ) / 4


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a judge response.

    Judges wrap JSON in prose and fences no matter how firmly you ask them not
    to. Failing the whole evaluation over that would make the harness useless,
    so the object is extracted rather than parsed strictly.

    Raises:
        ValueError: when no JSON object can be found.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise ValueError(f"no JSON object in judge response: {text[:160]!r}")
    parsed: dict[str, Any] = json.loads(match.group(0))
    return parsed


class LLMJudge:
    """Grades responses with a pinned judge model."""

    def __init__(
        self, backend: InferenceBackend, judge_model: str, *, temperature: float = 0.0
    ) -> None:
        """Bind to a backend and a pinned judge model.

        Temperature is 0 by default. A judge is a measuring instrument, and a
        measuring instrument that returns a different answer each time it is
        applied is not one.
        """
        self._backend = backend
        self._model = judge_model
        self._temperature = temperature

    async def _ask(self, system: str, user: str) -> str:
        """Run one judge call and return its full text."""
        request = GenerationRequest(
            model=self._model,
            messages=[
                ChatMessage(role=ChatRole.SYSTEM, content=system),
                ChatMessage(role=ChatRole.USER, content=user),
            ],
            temperature=self._temperature,
            max_tokens=300,
        )
        pieces = [chunk.content async for chunk in self._backend.generate(request)]
        return "".join(pieces)

    async def pairwise(
        self, case: EvalCase, student_answer: str, teacher_answer: str
    ) -> PairwiseVerdict:
        """Compare two answers, controlling for position bias.

        The comparison is run in both orders. Agreement is a real verdict;
        disagreement means the judge is responding to position rather than
        quality, and is recorded as a tie.
        """
        forward = self._prompt(case.prompt, student_answer, teacher_answer)
        reverse = self._prompt(case.prompt, teacher_answer, student_answer)

        first_raw, second_raw = await asyncio.gather(
            self._ask(JUDGE_SYSTEM_PROMPT, forward),
            self._ask(JUDGE_SYSTEM_PROMPT, reverse),
        )

        try:
            first = _extract_json(first_raw)
            second = _extract_json(second_raw)
        except ValueError as exc:
            log.warning("evals.judge_unparseable", error=str(exc))
            return PairwiseVerdict(case.id, case.slice, "tie", str(exc), 0.0, False)

        # In the forward order A is the student; in the reverse order A is the
        # teacher. Normalising both onto "who won" is what makes them comparable.
        forward_winner = {"A": "student", "B": "teacher"}.get(str(first.get("winner")), "tie")
        reverse_winner = {"A": "teacher", "B": "student"}.get(str(second.get("winner")), "tie")

        consistent = forward_winner == reverse_winner
        winner = forward_winner if consistent else "tie"
        confidence = (
            (float(first.get("confidence", 0.5)) + float(second.get("confidence", 0.5))) / 2
            if consistent
            else 0.0
        )
        reason = (
            str(first.get("reason", ""))
            if consistent
            else f"position-inconsistent: {forward_winner} then {reverse_winner}"
        )
        return PairwiseVerdict(case.id, case.slice, winner, reason, confidence, consistent)

    async def rubric(self, case: EvalCase, answer: str) -> RubricScore:
        """Grade one answer against the rubric."""
        prompt = f"Request:\n{case.prompt}\n\nResponse:\n{answer}"
        raw = await self._ask(RUBRIC_SYSTEM_PROMPT, prompt)
        try:
            scores = _extract_json(raw)
        except ValueError as exc:
            log.warning("evals.rubric_unparseable", error=str(exc))
            return RubricScore(case.id, case.slice, 0.0, 0.0, 0.0, 0.0, str(exc))

        def dimension(name: str) -> float:
            return max(0.0, min(1.0, float(scores.get(name, 0.0))))

        return RubricScore(
            case_id=case.id,
            slice=case.slice,
            correctness=dimension("correctness"),
            completeness=dimension("completeness"),
            instruction_following=dimension("instruction_following"),
            conciseness=dimension("conciseness"),
            reason=str(scores.get("reason", "")),
        )

    @staticmethod
    def _prompt(request: str, answer_a: str, answer_b: str) -> str:
        """Render one pairwise comparison prompt."""
        return (
            f"Request:\n{request}\n\n"
            f"Response A:\n{answer_a}\n\n"
            f"Response B:\n{answer_b}\n\n"
            "Which response is better?"
        )


@dataclass(frozen=True, slots=True)
class CalibrationCase:
    """A comparison whose correct verdict is already known."""

    case: EvalCase
    better_answer: str
    worse_answer: str


class JudgeDriftMonitor:
    """Detects a judge model changing under you.

    A judge that drifts invalidates every historical comparison silently, which
    is the worst failure mode an eval system has: the numbers keep arriving and
    stop meaning what they used to.
    """

    def __init__(self, judge: LLMJudge, calibration: Sequence[CalibrationCase]) -> None:
        """Bind a judge to its calibration set."""
        self._judge = judge
        self._calibration = calibration

    async def measure(self, *, expected_agreement: float = 1.0) -> float:
        """Return drift: how far below expected the judge's agreement now is.

        Zero means the judge still agrees with the calibration set as often as
        it did when pinned. Positive values mean it has drifted.
        """
        if not self._calibration:
            return 0.0

        verdicts = await asyncio.gather(
            *(
                self._judge.pairwise(item.case, item.better_answer, item.worse_answer)
                for item in self._calibration
            )
        )
        # The better answer is always passed as the student, so "student wins"
        # is the correct verdict for every calibration case.
        agreed = sum(1 for v in verdicts if v.winner == "student")
        return max(0.0, expected_agreement - agreed / len(self._calibration))


def aggregate_by_slice(verdicts: Sequence[PairwiseVerdict]) -> list[JudgeResult]:
    """Fold pairwise verdicts into per-slice win/loss/tie counts."""
    buckets: dict[str, dict[str, int]] = {}
    for verdict in verdicts:
        bucket = buckets.setdefault(verdict.slice, {"student": 0, "teacher": 0, "tie": 0})
        bucket[verdict.winner] += 1

    return [
        JudgeResult(
            slice=name,
            student_wins=counts["student"],
            teacher_wins=counts["teacher"],
            ties=counts["tie"],
        )
        for name, counts in sorted(buckets.items())
    ]


class EvalHarness:
    """Runs a full comparison between two models and grades it."""

    def __init__(self, backend: InferenceBackend, judge: LLMJudge) -> None:
        """Bind to the generation backend and the judge."""
        self._backend = backend
        self._judge = judge

    async def _answer(self, model: str, prompt: str) -> str:
        """Generate one answer."""
        request = GenerationRequest(
            model=model,
            messages=[ChatMessage(role=ChatRole.USER, content=prompt)],
            max_tokens=512,
        )
        return "".join([chunk.content async for chunk in self._backend.generate(request)])

    async def compare(
        self, cases: Sequence[EvalCase], *, student_model: str, teacher_model: str
    ) -> list[PairwiseVerdict]:
        """Run every case against both models and judge the pairs.

        Cases run concurrently but each case's two generations run together, so
        a slow teacher does not serialise the whole run.
        """

        async def one(case: EvalCase) -> PairwiseVerdict:
            student, teacher = await asyncio.gather(
                self._answer(student_model, case.prompt),
                self._answer(teacher_model, case.prompt),
            )
            return await self._judge.pairwise(case, student, teacher)

        return list(await asyncio.gather(*(one(case) for case in cases)))

    @staticmethod
    def summarise(verdicts: Sequence[PairwiseVerdict]) -> dict[str, float]:
        """Headline numbers for a comparison run."""
        if not verdicts:
            return {"win_rate": 0.0, "position_consistency": 0.0, "cases": 0}

        results = aggregate_by_slice(verdicts)
        wins = sum(r.student_wins for r in results)
        ties = sum(r.ties for r in results)
        total = len(verdicts)
        return {
            "win_rate": (wins + 0.5 * ties) / total,
            "position_consistency": sum(1 for v in verdicts if v.position_consistent) / total,
            "cases": float(total),
        }


def utc_now() -> dt.datetime:
    """Current UTC time, isolated so tests can freeze it."""
    return dt.datetime.now(dt.UTC)
