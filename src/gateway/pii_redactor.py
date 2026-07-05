"""PII detection and redaction engine.

Detects and redacts PII in both inputs and outputs using regex patterns.
Operates fully offline -- no external API calls.

Supported PII types:
  - Social Security Numbers (SSN)
  - Credit card numbers (Visa, MC, Amex, Discover)
  - Email addresses
  - Phone numbers (US, international)
  - IPv4 and IPv6 addresses
  - Names in structured contexts (e.g. "Name: John Doe")
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class RedactionMode(str, Enum):
    REDACT = "redact"  # Replace with [REDACTED_TYPE]
    MASK = "mask"  # Partial mask: ***-**-1234
    HASH = "hash"  # Replace with deterministic hash


@dataclass(frozen=True)
class RedactionRecord:
    """Immutable record of a single redaction event."""

    pii_type: str
    original_hash: str  # SHA-256 of original value (never store plaintext)
    replacement: str
    start: int
    end: int


@dataclass(frozen=True)
class PIIConfig:
    """Per-type redaction configuration."""

    mode: RedactionMode = RedactionMode.REDACT
    enabled: bool = True


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Each entry: (compiled regex, pii_type label, group index for the PII value)
_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # SSN: 123-45-6789 or 123 45 6789
    (
        re.compile(r"\b(\d{3}[-\s]?\d{2}[-\s]?\d{4})\b"),
        "SSN",
    ),
    # Credit card: Visa, MC, Amex, Discover (with optional separators)
    (
        re.compile(
            r"\b("
            r"4\d{3}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}"  # Visa
            r"|5[1-5]\d{2}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}"  # MC
            r"|3[47]\d{1}[-\s]?\d{6}[-\s]?\d{5}"  # Amex
            r"|6(?:011|5\d{2})[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}"  # Discover
            r")\b"
        ),
        "CREDIT_CARD",
    ),
    # Email
    (
        re.compile(
            r"\b([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\b"
        ),
        "EMAIL",
    ),
    # US phone: (555) 123-4567 or 555-123-4567 or +1-555-123-4567
    (
        re.compile(
            r"(?<!\d)(\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})(?!\d)"
        ),
        "PHONE",
    ),
    # IPv4
    (
        re.compile(
            r"\b((?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d))\b"
        ),
        "IP_ADDRESS",
    ),
    # IPv6 (simplified -- matches most common representations)
    (
        re.compile(
            r"\b((?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4})\b"
        ),
        "IP_ADDRESS",
    ),
    # Names in structured context: "Name: First Last", "User: First Last"
    (
        re.compile(
            r"(?:(?:Name|User|Patient|Employee|Analyst|Contact)\s*[:=]\s*)"
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})"
        ),
        "PERSON_NAME",
    ),
]

# Well-known private/reserved IPs that should NOT be redacted
_SAFE_IPS: frozenset[str] = frozenset(
    {
        "127.0.0.1",
        "0.0.0.0",
        "255.255.255.255",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
    }
)


# ---------------------------------------------------------------------------
# Redactor
# ---------------------------------------------------------------------------


class PIIRedactor:
    """Detect and redact PII from text.

    Usage::

        redactor = PIIRedactor()
        clean_text, records = redactor.redact("Call me at 555-123-4567")
        # clean_text == "Call me at [REDACTED_PHONE]"
    """

    def __init__(
        self,
        default_mode: RedactionMode = RedactionMode.REDACT,
        type_config: dict[str, PIIConfig] | None = None,
    ) -> None:
        self._default_mode = default_mode
        self._type_config: dict[str, PIIConfig] = type_config or {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def redact(self, text: str) -> tuple[str, list[RedactionRecord]]:
        """Detect and redact all PII from *text*.

        Returns the cleaned text and a list of redaction records for audit.
        """
        if not text:
            return text, []

        records: list[RedactionRecord] = []
        # Collect all matches first, then replace from end to preserve indices
        matches: list[tuple[int, int, str, str]] = []  # (start, end, pii_type, value)

        for pattern, pii_type in _PII_PATTERNS:
            cfg = self._type_config.get(pii_type, PIIConfig(mode=self._default_mode))
            if not cfg.enabled:
                continue
            for m in pattern.finditer(text):
                value = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
                # Skip safe IPs
                if pii_type == "IP_ADDRESS" and value in _SAFE_IPS:
                    continue
                matches.append((m.start(), m.end(), pii_type, value))

        # Deduplicate overlapping matches (keep the longest / first)
        matches = self._deduplicate_matches(matches)

        # Sort descending by start position so replacements don't shift indices
        matches.sort(key=lambda x: x[0], reverse=True)

        result = text
        for start, end, pii_type, value in matches:
            replacement = self._build_replacement(pii_type, value)
            original_hash = hashlib.sha256(value.encode()).hexdigest()
            records.append(
                RedactionRecord(
                    pii_type=pii_type,
                    original_hash=original_hash,
                    replacement=replacement,
                    start=start,
                    end=end,
                )
            )
            result = result[:start] + replacement + result[end:]

        # Re-sort records by position ascending for readable audit
        records.sort(key=lambda r: r.start)

        if records:
            logger.info(
                "pii_redacted",
                total_redactions=len(records),
                types=[r.pii_type for r in records],
            )

        return result, records

    def detect(self, text: str) -> list[tuple[str, int, int]]:
        """Detect PII without redacting. Returns list of (type, start, end)."""
        if not text:
            return []

        detections: list[tuple[str, int, int]] = []
        for pattern, pii_type in _PII_PATTERNS:
            cfg = self._type_config.get(pii_type, PIIConfig(mode=self._default_mode))
            if not cfg.enabled:
                continue
            for m in pattern.finditer(text):
                value = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
                if pii_type == "IP_ADDRESS" and value in _SAFE_IPS:
                    continue
                detections.append((pii_type, m.start(), m.end()))
        return detections

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_replacement(self, pii_type: str, value: str) -> str:
        """Build a replacement string based on the configured mode."""
        cfg = self._type_config.get(pii_type, PIIConfig(mode=self._default_mode))
        mode = cfg.mode

        if mode == RedactionMode.REDACT:
            return f"[REDACTED_{pii_type}]"

        if mode == RedactionMode.MASK:
            return self._mask_value(pii_type, value)

        if mode == RedactionMode.HASH:
            short_hash = hashlib.sha256(value.encode()).hexdigest()[:12]
            return f"[HASH_{pii_type}:{short_hash}]"

        return f"[REDACTED_{pii_type}]"

    @staticmethod
    def _mask_value(pii_type: str, value: str) -> str:
        """Partially mask a PII value, preserving last few characters."""
        stripped = re.sub(r"[\s\-()]", "", value)
        if pii_type == "SSN" and len(stripped) >= 4:
            return f"***-**-{stripped[-4:]}"
        if pii_type == "CREDIT_CARD" and len(stripped) >= 4:
            return f"****-****-****-{stripped[-4:]}"
        if pii_type == "PHONE" and len(stripped) >= 4:
            return f"***-***-{stripped[-4:]}"
        if pii_type == "EMAIL":
            parts = value.split("@")
            if len(parts) == 2:
                local = parts[0]
                masked_local = local[0] + "***" if len(local) > 1 else "***"
                return f"{masked_local}@{parts[1]}"
        # Default: mask all but last 4
        if len(stripped) > 4:
            return "*" * (len(stripped) - 4) + stripped[-4:]
        return "****"

    @staticmethod
    def _deduplicate_matches(
        matches: list[tuple[int, int, str, str]],
    ) -> list[tuple[int, int, str, str]]:
        """Remove overlapping matches, keeping the longest span."""
        if not matches:
            return []
        sorted_matches = sorted(matches, key=lambda x: (x[0], -(x[1] - x[0])))
        deduped: list[tuple[int, int, str, str]] = [sorted_matches[0]]
        for current in sorted_matches[1:]:
            prev = deduped[-1]
            # If current starts before previous ends, it overlaps
            if current[0] < prev[1]:
                continue
            deduped.append(current)
        return deduped
