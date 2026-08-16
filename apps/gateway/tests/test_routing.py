"""Tests for task classification and route selection."""

from __future__ import annotations

import pytest

from distillserve_gateway.routing.classifier import (
    Classification,
    LayeredTaskClassifier,
    RuleTaskClassifier,
)
from distillserve_gateway.routing.router import Router, RouterConfig
from distillserve_schemas import RoutePolicy, RouteTarget

TEACHER = "groq/llama-3.3-70b-versatile"
STUDENT = "groq/llama-3.1-8b-instant"


def make_router(classifier: object | None = None) -> Router:
    config = RouterConfig(teacher_model=TEACHER, student_model=STUDENT)
    return Router(config, classifier)  # type: ignore[arg-type]


class StubClassifier:
    """Returns a fixed classification, for testing router policy in isolation."""

    def __init__(self, task: str, confidence: float) -> None:
        self._result = Classification(task=task, confidence=confidence, reason="stub", layer="stub")

    def classify(self, prompt: str) -> Classification:
        return self._result


# --- classifier ------------------------------------------------------------


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Summarize this support thread in two sentences.", "summarization"),
        ("Extract the invoice number and total as JSON.", "extraction"),
        ("Write a Python function that reverses a linked list.", "code_generation"),
        ("Write a SQL query for monthly active users.", "sql_generation"),
        ("Classify the sentiment of this review.", "classification"),
        ("Rewrite this paragraph to be clearer.", "rewrite"),
        ("Prove that the sum of two even numbers is even, step by step.", "reasoning"),
        ("Write a poem about the sea.", "creative_writing"),
    ],
)
def test_rules_label_common_prompt_shapes(prompt: str, expected: str) -> None:
    assert RuleTaskClassifier().classify(prompt).task == expected


def test_more_specific_rules_win_over_broader_ones() -> None:
    """A SQL prompt that also says 'summarizes' is SQL generation, not summarization."""
    result = RuleTaskClassifier().classify("Write a SQL query that summarizes sales by region.")
    assert result.task == "sql_generation"


def test_word_boundaries_prevent_substring_false_positives() -> None:
    """'consume' must not trigger the summarization rule via 'sum'."""
    result = RuleTaskClassifier().classify("How much power does this consume?")
    assert result.task != "summarization"


def test_unmatched_prompt_is_low_confidence_general() -> None:
    result = RuleTaskClassifier().classify("Bananas.")
    assert result.task == "general"
    assert result.confidence < 0.55


def test_empty_prompt_is_zero_confidence() -> None:
    assert RuleTaskClassifier().classify("   ").confidence == 0.0


def test_long_prompts_are_damped() -> None:
    """A long prompt containing a keyword is likelier to be a compound task."""
    short = RuleTaskClassifier().classify("Summarize this.")
    long = RuleTaskClassifier().classify("Summarize this. " + "context " * 2_000)
    assert long.confidence < short.confidence


def test_layered_classifier_skips_the_fallback_when_rules_are_confident() -> None:
    calls: list[str] = []

    class RecordingFallback:
        def classify(self, prompt: str) -> Classification:
            calls.append(prompt)
            return Classification(task="reasoning", confidence=0.99, reason="model", layer="model")

    layered = LayeredTaskClassifier(fallback=RecordingFallback())
    result = layered.classify("Summarize this support thread.")

    assert result.task == "summarization"
    assert calls == []


def test_layered_classifier_consults_the_fallback_when_rules_are_unsure() -> None:
    layered = LayeredTaskClassifier(fallback=StubClassifier("reasoning", 0.95))
    result = layered.classify("Bananas.")

    assert result.task == "reasoning"
    assert result.layer == "stub"


def test_layered_classifier_keeps_the_more_confident_layer() -> None:
    """The rules encode operator knowledge; a weaker model must not override them."""
    layered = LayeredTaskClassifier(fallback=StubClassifier("creative_writing", 0.1))
    result = layered.classify("Bananas.")

    assert result.task == "general"


def test_layered_classifier_works_without_a_fallback() -> None:
    """Without the `ml` extra there is no fallback; the rules result must stand."""
    result = LayeredTaskClassifier().classify("Bananas.")
    assert result.task == "general"


# --- router ----------------------------------------------------------------


def test_auto_routes_a_distilled_task_to_the_student() -> None:
    decision = make_router().route(prompt="Summarize this support thread in two sentences.")

    assert decision.target is RouteTarget.STUDENT
    assert decision.model == STUDENT
    assert "distilled" in decision.reason


def test_auto_escalates_a_task_outside_the_students_scope() -> None:
    decision = make_router().route(prompt="Write a poem about the sea.")

    assert decision.target is RouteTarget.TEACHER
    assert "outside the student's distilled scope" in decision.reason


def test_auto_escalates_on_low_confidence() -> None:
    """A distilled task the classifier is unsure about still goes to the teacher."""
    router = make_router(StubClassifier("summarization", 0.2))
    decision = router.route(prompt="anything")

    assert decision.target is RouteTarget.TEACHER
    assert "below the" in decision.reason


def test_explicit_model_pins_the_route() -> None:
    decision = make_router().route(prompt="Summarize this.", explicit_model=TEACHER)

    assert decision.target is RouteTarget.TEACHER
    assert decision.model == TEACHER
    assert "pinned model" in decision.reason


def test_unknown_explicit_model_is_treated_as_teacher_tier() -> None:
    """An unrecognised id must not inherit the student's quality assumptions."""
    decision = make_router().route(prompt="Summarize this.", explicit_model="openai/gpt-4o")

    assert decision.target is RouteTarget.TEACHER
    assert decision.model == "openai/gpt-4o"


@pytest.mark.parametrize(
    ("policy", "expected_target", "expected_model"),
    [
        (RoutePolicy.TEACHER, RouteTarget.TEACHER, TEACHER),
        (RoutePolicy.STUDENT, RouteTarget.STUDENT, STUDENT),
    ],
)
def test_policy_pins_the_tier(
    policy: RoutePolicy, expected_target: RouteTarget, expected_model: str
) -> None:
    """Compare mode depends on being able to pin a side without naming a model."""
    decision = make_router().route(prompt="Write a poem about the sea.", policy=policy)

    assert decision.target is expected_target
    assert decision.model == expected_model


def test_declared_task_skips_classification_but_not_the_capability_check() -> None:
    decision = make_router().route(prompt="anything", declared_task="creative_writing")

    assert decision.confidence == 1.0
    assert decision.target is RouteTarget.TEACHER


def test_every_decision_explains_itself() -> None:
    """A route that cannot explain itself is undebuggable in production."""
    for prompt in ("Summarize this.", "Write a poem.", "Bananas."):
        decision = make_router().route(prompt=prompt)
        assert decision.reason
        assert decision.task
