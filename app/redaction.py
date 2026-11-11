"""Redaction of personal data and credentials before anything is logged or remembered.

Scope matters here. Redaction is applied to log records and to text handed to
long-term memory. It is deliberately *not* applied to the article being
written: a blog post may legitimately contain an example e-mail address or a
number that looks like a phone number, and silently rewriting the author's
text would be a bug. Articles get a separate pre-publish check
(``find_secrets``) that reports findings and leaves the decision to a human.

Two layers:

1. Deterministic regular expressions, always on, no network.
2. Cloud DLP ``deidentify_content``, enabled with ``BLOG_AGENT_ENABLE_DLP=true``,
   for the categories regular expressions handle poorly (names, addresses).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from typing import Any

from app.settings import env_flag

_logger = logging.getLogger(__name__)


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_replacer(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "[REDACTED_CREDIT_CARD]"
    return match.group(0)


# Credential patterns. Used both for log redaction and the pre-publish scan.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("GOOGLE_OAUTH_TOKEN", re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "BEARER_TOKEN",
        re.compile(r"(?i)\bbearer\s+(?=[0-9A-Za-z_\-\.]*\d)[0-9A-Za-z_\-\.]{20,}"),
    ),
    (
        "ASSIGNED_SECRET",
        re.compile(
            r"(?i)\b(api[-_ ]?key|access[-_ ]?token|secret|password)\b\s*[:=]\s*"
            r"[\"']?(?=[^\s\"']*\d)[^\s\"']{12,}"
        ),
    ),
]

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "EMAIL_ADDRESS",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    ),
    ("KR_RESIDENT_NUMBER", re.compile(r"\b\d{6}-[1-4]\d{6}\b")),
    ("US_SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    (
        "PHONE_NUMBER",
        re.compile(
            r"(?<!\d)(?:\+?\d{1,3}[-.\s])?\(?0?\d{2,3}\)?[-.\s]\d{3,4}[-.\s]\d{4}(?!\d)"
        ),
    ),
]

_CARD_PATTERN = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")


def find_secrets(text: str) -> list[dict[str, Any]]:
    """Report credential-looking strings without changing the text.

    Returns one finding per match with its type and line number. The matched
    value itself is not returned, so findings are safe to log.
    """
    findings: list[dict[str, Any]] = []
    for label, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text or ""):
            findings.append(
                {"type": label, "line": text.count("\n", 0, match.start()) + 1}
            )
    return findings


def redact_credentials(text: str) -> str:
    """Replace only credential-looking strings; personal data is left alone.

    Used on model output, where the text may be an article that legitimately
    contains an example e-mail address or phone number.
    """
    if not text:
        return text
    for label, pattern in _SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED_{label}]", text)
    return text


def redact_text(text: str) -> str:
    """Replace credentials and personal data in ``text`` with typed placeholders."""
    if not text:
        return text
    redacted = text
    for label, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(f"[REDACTED_{label}]", redacted)
    for label, pattern in _PII_PATTERNS:
        redacted = pattern.sub(f"[REDACTED_{label}]", redacted)
    redacted = _CARD_PATTERN.sub(_card_replacer, redacted)
    if env_flag("BLOG_AGENT_ENABLE_DLP"):
        redacted = _redact_with_cloud_dlp(redacted)
    return redacted


def redact_value(value: Any, *, max_chars: int = 500) -> Any:
    """Redact strings inside nested dicts and lists, truncating long strings.

    Truncation keeps whole articles out of the logs: a log line should say
    that a 12,000-character article was passed, not repeat it.
    """
    if isinstance(value, str):
        clean = redact_text(value)
        if len(clean) > max_chars:
            return f"{clean[:max_chars]}... [{len(clean)} chars total]"
        return clean
    if isinstance(value, Mapping):
        return {str(k): redact_value(v, max_chars=max_chars) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, max_chars=max_chars) for v in value]
    return value


_DLP_INFO_TYPES = (
    "PERSON_NAME",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "STREET_ADDRESS",
    "CREDIT_CARD_NUMBER",
    "KOREA_RRN",
    "US_SOCIAL_SECURITY_NUMBER",
)

_dlp_client: Any = None


def _redact_with_cloud_dlp(text: str) -> str:
    """De-identify ``text`` with Cloud DLP. Falls back to the input on any error.

    A redaction outage must not take the agent down, so failures are logged and
    the regex-redacted text is used as is.
    """
    global _dlp_client
    project = os.environ.get("DLP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        return text
    try:
        from google.cloud import dlp_v2

        if _dlp_client is None:
            _dlp_client = dlp_v2.DlpServiceClient()
        response = _dlp_client.deidentify_content(
            request={
                "parent": f"projects/{project}/locations/global",
                "inspect_config": {
                    "info_types": [{"name": name} for name in _DLP_INFO_TYPES]
                },
                "deidentify_config": {
                    "info_type_transformations": {
                        "transformations": [
                            {
                                "primitive_transformation": {
                                    "replace_with_info_type_config": {}
                                }
                            }
                        ]
                    }
                },
                "item": {"value": text},
            }
        )
        return response.item.value
    except Exception as exc:  # network, permission, or API-disabled errors
        _logger.warning("Cloud DLP redaction skipped: %s", type(exc).__name__)
        return text
