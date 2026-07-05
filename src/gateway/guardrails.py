"""Content safety guardrails engine.

Performs multi-layer checks on both inputs and outputs:
  - Prompt injection detection (regex + heuristic scoring)
  - Output validation (no fabricated data, no harmful content)
  - Destructive action gating (blocks dangerous tool calls)
  - Topic boundary enforcement (agent stays in SOC domain)
  - Confidence threshold gating
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    WARN = "warn"


@dataclass(frozen=True)
class GuardrailCheckResult:
    decision: str  # "allow" | "deny" | "warn"
    check_name: str
    reason: str
    score: float = 0.0  # 0..1 risk score


# ---------------------------------------------------------------------------
# Prompt injection patterns
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[tuple[re.Pattern[str], float, str]] = [
    (
        re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
        0.95,
        "Directive to ignore system prompt",
    ),
    (
        re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.IGNORECASE),
        0.85,
        "Persona override attempt",
    ),
    (
        re.compile(r"forget\s+(everything|all|your)", re.IGNORECASE),
        0.90,
        "Memory wipe attempt",
    ),
    (
        re.compile(r"system\s*:\s*", re.IGNORECASE),
        0.80,
        "System prompt injection via role prefix",
    ),
    (
        re.compile(r"\[INST\]|\[/INST\]|<\|im_start\|>|<\|im_end\|>", re.IGNORECASE),
        0.90,
        "Chat-template injection tokens",
    ),
    (
        re.compile(r"do\s+not\s+follow\s+(the\s+)?(rules|guidelines|instructions)", re.IGNORECASE),
        0.90,
        "Rule override attempt",
    ),
    (
        re.compile(r"pretend\s+(you\s+are|to\s+be)", re.IGNORECASE),
        0.70,
        "Pretend directive",
    ),
    (
        re.compile(r"repeat\s+(the\s+)?(system|secret|hidden)\s+(prompt|message|instructions)", re.IGNORECASE),
        0.95,
        "Prompt exfiltration attempt",
    ),
    (
        re.compile(r"output\s+(your|the)\s+(system|initial)\s+(prompt|instructions)", re.IGNORECASE),
        0.95,
        "System prompt extraction",
    ),
    (
        re.compile(r"base64\s+decode|eval\(|exec\(|__import__", re.IGNORECASE),
        0.90,
        "Code execution attempt",
    ),
]

# ---------------------------------------------------------------------------
# Off-topic patterns (SOC agent should not handle these)
# ---------------------------------------------------------------------------

_OFF_TOPIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(recipe|cooking|bake|ingredient)\b", re.IGNORECASE),
        "Cooking-related request outside SOC domain",
    ),
    (
        re.compile(r"\b(poem|story|fiction|novel|creative\s+writing)\b", re.IGNORECASE),
        "Creative writing outside SOC domain",
    ),
    (
        re.compile(r"\b(investment\s+advice|stock\s+tip|crypto\s+buy)\b", re.IGNORECASE),
        "Financial advice outside SOC domain",
    ),
    (
        re.compile(r"\b(medical\s+advice|diagnosis|prescription)\b", re.IGNORECASE),
        "Medical advice outside SOC domain",
    ),
]

# ---------------------------------------------------------------------------
# Destructive action patterns (require explicit approval)
# ---------------------------------------------------------------------------

_DESTRUCTIVE_ACTIONS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(isolate|quarantine|block)\s+(host|ip|endpoint|user)", re.IGNORECASE),
        "Network isolation / containment action",
    ),
    (
        re.compile(r"\b(disable|revoke|terminate)\s+(account|user|session|access)", re.IGNORECASE),
        "Account or access revocation",
    ),
    (
        re.compile(r"\b(delete|wipe|purge)\s+(logs?|data|evidence|files?)", re.IGNORECASE),
        "Data deletion action",
    ),
    (
        re.compile(r"\b(shutdown|reboot|restart)\s+(server|host|system|service)", re.IGNORECASE),
        "System shutdown action",
    ),
    (
        re.compile(r"\b(deploy|push|execute)\s+(rule|policy|firewall|block\s*list)", re.IGNORECASE),
        "Firewall / policy deployment",
    ),
]

# ---------------------------------------------------------------------------
# Harmful output patterns
# ---------------------------------------------------------------------------

_HARMFUL_OUTPUT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(here\s+is\s+how\s+to\s+hack|exploit\s+this\s+vulnerability\s+by)\b", re.IGNORECASE),
        "Offensive exploitation guidance",
    ),
    (
        re.compile(r"\b(password|credentials?)\s*[:=]\s*\S+", re.IGNORECASE),
        "Credential leakage in output",
    ),
    (
        re.compile(r"\b(sudo\s+rm\s+-rf|format\s+c:|drop\s+table)\b", re.IGNORECASE),
        "Destructive system command in output",
    ),
]


# ---------------------------------------------------------------------------
# Guardrails Engine
# ---------------------------------------------------------------------------


class GuardrailsEngine:
    """Stateless engine that evaluates text against guardrail checks."""

    def __init__(
        self,
        injection_threshold: float = 0.75,
        confidence_floor: float = 0.30,
    ) -> None:
        self._injection_threshold = injection_threshold
        self._confidence_floor = confidence_floor

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check_input(self, text: str) -> GuardrailCheckResult:
        """Run all input-side guardrail checks. Returns the first failing check,
        or an ALLOW result if everything passes."""
        checks: list[GuardrailCheckResult] = [
            self._check_prompt_injection(text),
            self._check_topic_boundary(text),
            self._check_destructive_action(text),
        ]
        # Return the highest-severity failure
        for result in checks:
            if result.decision == "deny":
                return result
        for result in checks:
            if result.decision == "warn":
                return result
        return GuardrailCheckResult(
            decision="allow", check_name="all_input_checks", reason="All checks passed"
        )

    async def check_output(self, text: str) -> GuardrailCheckResult:
        """Run all output-side guardrail checks."""
        checks: list[GuardrailCheckResult] = [
            self._check_harmful_output(text),
            self._check_fabricated_data(text),
        ]
        for result in checks:
            if result.decision == "deny":
                return result
        for result in checks:
            if result.decision == "warn":
                return result
        return GuardrailCheckResult(
            decision="allow", check_name="all_output_checks", reason="All checks passed"
        )

    async def check_tool_call(
        self, tool_name: str, parameters: dict
    ) -> GuardrailCheckResult:
        """Gate a tool call before execution."""
        combined = f"{tool_name} {str(parameters)}"
        return self._check_destructive_action(combined)

    async def check_confidence(
        self, confidence: float
    ) -> GuardrailCheckResult:
        """Gate a response based on confidence score."""
        if confidence < self._confidence_floor:
            return GuardrailCheckResult(
                decision="deny",
                check_name="confidence_threshold",
                reason=f"Confidence {confidence:.2f} below floor {self._confidence_floor:.2f}. "
                "Please provide more context or clarify your question.",
                score=1.0 - confidence,
            )
        if confidence < 0.50:
            return GuardrailCheckResult(
                decision="warn",
                check_name="confidence_threshold",
                reason=f"Low confidence {confidence:.2f}; response may need verification.",
                score=1.0 - confidence,
            )
        return GuardrailCheckResult(
            decision="allow",
            check_name="confidence_threshold",
            reason="Confidence acceptable",
            score=0.0,
        )

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_prompt_injection(self, text: str) -> GuardrailCheckResult:
        """Score text for prompt injection risk using pattern matching
        and heuristic signals."""
        max_score = 0.0
        matched_reason = ""

        for pattern, weight, reason in _INJECTION_PATTERNS:
            if pattern.search(text):
                if weight > max_score:
                    max_score = weight
                    matched_reason = reason

        # Heuristic: excessive use of special characters / delimiters
        delimiter_ratio = sum(
            1 for c in text if c in "{}[]<>|\\`"
        ) / max(len(text), 1)
        if delimiter_ratio > 0.15:
            heuristic_score = min(delimiter_ratio * 5, 1.0)
            if heuristic_score > max_score:
                max_score = heuristic_score
                matched_reason = "Excessive delimiter characters (possible injection)"

        # Heuristic: unusually long single-line input (potential encoded payload)
        lines = text.split("\n")
        if lines and len(lines[0]) > 2000:
            candidate = 0.60
            if candidate > max_score:
                max_score = candidate
                matched_reason = "Unusually long single-line input"

        if max_score >= self._injection_threshold:
            logger.warning(
                "prompt_injection_detected",
                score=max_score,
                reason=matched_reason,
            )
            return GuardrailCheckResult(
                decision="deny",
                check_name="prompt_injection",
                reason=matched_reason,
                score=max_score,
            )
        if max_score >= 0.50:
            return GuardrailCheckResult(
                decision="warn",
                check_name="prompt_injection",
                reason=matched_reason,
                score=max_score,
            )
        return GuardrailCheckResult(
            decision="allow",
            check_name="prompt_injection",
            reason="No injection detected",
            score=max_score,
        )

    def _check_topic_boundary(self, text: str) -> GuardrailCheckResult:
        """Ensure the request is within SOC / cybersecurity domain."""
        for pattern, reason in _OFF_TOPIC_PATTERNS:
            if pattern.search(text):
                return GuardrailCheckResult(
                    decision="warn",
                    check_name="topic_boundary",
                    reason=reason,
                    score=0.70,
                )
        return GuardrailCheckResult(
            decision="allow",
            check_name="topic_boundary",
            reason="Within domain",
        )

    def _check_destructive_action(self, text: str) -> GuardrailCheckResult:
        """Flag destructive actions that need human approval."""
        for pattern, reason in _DESTRUCTIVE_ACTIONS:
            if pattern.search(text):
                return GuardrailCheckResult(
                    decision="warn",
                    check_name="destructive_action",
                    reason=f"Requires approval: {reason}",
                    score=0.85,
                )
        return GuardrailCheckResult(
            decision="allow",
            check_name="destructive_action",
            reason="No destructive action detected",
        )

    def _check_harmful_output(self, text: str) -> GuardrailCheckResult:
        """Check output for harmful or dangerous content."""
        for pattern, reason in _HARMFUL_OUTPUT_PATTERNS:
            if pattern.search(text):
                return GuardrailCheckResult(
                    decision="deny",
                    check_name="harmful_output",
                    reason=reason,
                    score=0.90,
                )
        return GuardrailCheckResult(
            decision="allow",
            check_name="harmful_output",
            reason="Output is safe",
        )

    def _check_fabricated_data(self, text: str) -> GuardrailCheckResult:
        """Heuristic check for potentially fabricated IOC data.

        Looks for patterns that suggest invented IP addresses, hashes,
        or CVE numbers that are clearly synthetic.
        """
        # Check for obviously fake hash patterns (all zeros, sequential)
        fake_hash = re.compile(
            r"\b([0]{32}|[a-f0]{40}|[0]{64}|0123456789abcdef{2,})\b",
            re.IGNORECASE,
        )
        if fake_hash.search(text):
            return GuardrailCheckResult(
                decision="warn",
                check_name="fabricated_data",
                reason="Potentially fabricated hash value detected",
                score=0.60,
            )

        # Check for suspiciously formatted CVE references
        fake_cve = re.compile(r"CVE-\d{4}-99999\d+", re.IGNORECASE)
        if fake_cve.search(text):
            return GuardrailCheckResult(
                decision="warn",
                check_name="fabricated_data",
                reason="Suspiciously formatted CVE identifier",
                score=0.55,
            )

        return GuardrailCheckResult(
            decision="allow",
            check_name="fabricated_data",
            reason="No fabricated data detected",
        )
