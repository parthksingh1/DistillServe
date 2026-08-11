"""Request guardrails: prompt-injection screening and PII redaction.

Both are pluggable behind protocols and both degrade rather than fail: a
guardrail that can take the gateway down is a guardrail operators turn off.
"""

from distillserve_gateway.guardrails.injection import (
    InjectionAction,
    InjectionDetector,
    InjectionScreen,
    InjectionVerdict,
    RuleInjectionDetector,
)
from distillserve_gateway.guardrails.pii import (
    PIIDetector,
    PIIMatch,
    PIIRedactor,
    PIIType,
    RedactionResult,
    RegexPIIDetector,
)

__all__ = [
    "InjectionAction",
    "InjectionDetector",
    "InjectionScreen",
    "InjectionVerdict",
    "PIIDetector",
    "PIIMatch",
    "PIIRedactor",
    "PIIType",
    "RedactionResult",
    "RegexPIIDetector",
    "RuleInjectionDetector",
]
