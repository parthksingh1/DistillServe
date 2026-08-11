"""The routing decision: teacher, student, or a named LoRA adapter.

Routing is where the platform's economics live, so the policy is deliberately
explicit and conservative:

* An explicit ``model`` on the request pins the route. A caller who names a
  model gets that model — the router never second-guesses an explicit choice,
  because A/B harnesses and the Playground's Compare mode depend on it.
* ``route_policy`` of ``teacher`` or ``student`` pins the tier without pinning
  a specific model id.
* Under ``auto``, a task the student has been distilled for and the classifier
  is confident about routes to the student. Everything else escalates.

Escalation is the default for uncertainty. Sending a hard prompt to the student
costs quality — which is what a distillation platform exists to protect —
while sending an easy one to the teacher costs a fraction of a cent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from distillserve_gateway.routing.classifier import (
    ESCALATION_THRESHOLD,
    Classification,
    LayeredTaskClassifier,
    TaskClassifier,
)
from distillserve_schemas import RouteDecision, RoutePolicy, RouteTarget

#: Tasks the distilled student is trained for. A task outside this set goes to
#: the teacher regardless of classifier confidence: high confidence in a label
#: the student was never distilled on is confidence in the wrong thing.
#:
#: In phase 4 this set is derived from the adapter registry rather than
#: hard-coded, so distilling a new task automatically opens the student route.
STUDENT_CAPABLE_TASKS: frozenset[str] = frozenset(
    {
        "summarization",
        "extraction",
        "classification",
        "rewrite",
        "sql_generation",
    }
)


@dataclass(frozen=True, slots=True)
class RouterConfig:
    """Models and thresholds the router works with."""

    teacher_model: str
    student_model: str
    threshold: float = ESCALATION_THRESHOLD
    student_capable_tasks: frozenset[str] = field(default=STUDENT_CAPABLE_TASKS)


class Router:
    """Chooses a destination for each request and explains the choice."""

    def __init__(self, config: RouterConfig, classifier: TaskClassifier | None = None) -> None:
        """Bind the router to its models and classifier.

        Args:
            config: Model ids and thresholds.
            classifier: Task classifier. Defaults to the layered rules-first one.
        """
        self._config = config
        self._classifier = classifier or LayeredTaskClassifier(threshold=config.threshold)

    def route(
        self,
        *,
        prompt: str,
        policy: RoutePolicy = RoutePolicy.AUTO,
        explicit_model: str | None = None,
        declared_task: str | None = None,
    ) -> RouteDecision:
        """Decide where a request goes.

        Args:
            prompt: The user turn, used for classification.
            policy: Caller's routing policy.
            explicit_model: A concrete model id, which pins the route.
            declared_task: A task label supplied by the caller, which skips
                classification but not the capability check.

        Returns:
            A fully explained :class:`RouteDecision`.
        """
        classification = self._classify(prompt, declared_task)

        if explicit_model is not None:
            return self._pinned_by_model(explicit_model, classification)
        if policy is RoutePolicy.TEACHER:
            return self._teacher(classification, policy, "route_policy pinned the teacher")
        if policy is RoutePolicy.STUDENT:
            return self._student(classification, policy, "route_policy pinned the student")

        return self._auto(classification)

    # -- internals ----------------------------------------------------------

    def _classify(self, prompt: str, declared_task: str | None) -> Classification:
        """Classify, or accept the caller's declared task at full confidence."""
        if declared_task is not None:
            return Classification(
                task=declared_task,
                confidence=1.0,
                reason="task declared by caller",
                layer="declared",
            )
        return self._classifier.classify(prompt)

    def _auto(self, classification: Classification) -> RouteDecision:
        """Apply the automatic policy: student only when it is safe."""
        if classification.task not in self._config.student_capable_tasks:
            return self._teacher(
                classification,
                RoutePolicy.AUTO,
                f"task '{classification.task}' is outside the student's distilled scope",
            )
        if classification.confidence < self._config.threshold:
            return self._teacher(
                classification,
                RoutePolicy.AUTO,
                (
                    f"classifier confidence {classification.confidence:.2f} is below the "
                    f"{self._config.threshold:.2f} escalation threshold"
                ),
            )
        return self._student(
            classification,
            RoutePolicy.AUTO,
            (
                f"task '{classification.task}' is distilled and confidence "
                f"{classification.confidence:.2f} clears the threshold"
            ),
        )

    def _pinned_by_model(self, model: str, classification: Classification) -> RouteDecision:
        """Honour an explicit model id, labelling the tier it corresponds to."""
        if model == self._config.student_model:
            target = RouteTarget.STUDENT
        elif model == self._config.teacher_model:
            target = RouteTarget.TEACHER
        else:
            # An unrecognised id is treated as a teacher-tier model: it is not
            # our distilled student, so it must not inherit the student's
            # quality assumptions on the eval surfaces.
            target = RouteTarget.TEACHER

        return RouteDecision(
            target=target,
            model=model,
            task=classification.task,
            policy=RoutePolicy.AUTO,
            confidence=classification.confidence,
            reason=f"request pinned model '{model}'",
        )

    def _teacher(
        self, classification: Classification, policy: RoutePolicy, reason: str
    ) -> RouteDecision:
        """Build a teacher-targeted decision."""
        return RouteDecision(
            target=RouteTarget.TEACHER,
            model=self._config.teacher_model,
            task=classification.task,
            policy=policy,
            confidence=classification.confidence,
            reason=reason,
        )

    def _student(
        self, classification: Classification, policy: RoutePolicy, reason: str
    ) -> RouteDecision:
        """Build a student-targeted decision."""
        return RouteDecision(
            target=RouteTarget.STUDENT,
            model=self._config.student_model,
            task=classification.task,
            policy=policy,
            confidence=classification.confidence,
            reason=reason,
        )
