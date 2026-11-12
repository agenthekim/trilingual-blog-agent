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
from contextvars import ContextVar
from typing import Any

from app.settings import env_flag

# True while the failure of Cloud DLP is being written to the log. The log
# record passes ``redact_text`` like any other, and must not call the service
# that has just failed, or the failure would be reported without end.
_reporting_dlp_failure: ContextVar[bool] = ContextVar(
    "reporting_dlp_failure", default=False
)


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

_EMAIL_PATTERN = dict(_PII_PATTERNS)["EMAIL_ADDRESS"]

# A card number stands by itself. Digits attached to an ASCII letter or an
# underscore, directly or through one hyphen, are part of an identifier
# (``call_4539148803436467``, a UUID) and are left alone (SEC-4).
_CARD_PATTERN = re.compile(
    r"(?<![0-9A-Za-z_])(?<![0-9A-Za-z_]-)"
    r"(?:\d[ \-]?){13,19}"
    r"(?![0-9A-Za-z_])(?!-[0-9A-Za-z_])"
)

# Fields of a log record that hold an identifier. The log filter writes them
# as they are: records are joined by them (intent to outcome, record to trace).
IDENTIFIER_FIELDS = frozenset(
    {
        "function_call_id",
        "invocation_id",
        "call_id",
        "run_id",
        "job_id",
        "session_id",
        "trace_id",
        "span_id",
    }
)


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


def _redact_numbers(text: str) -> str:
    """Replace the personal data that is written in digits: resident and
    social security numbers, phone numbers, card numbers."""
    for label, pattern in _PII_PATTERNS:
        if label != "EMAIL_ADDRESS":
            text = pattern.sub(f"[REDACTED_{label}]", text)
    return _CARD_PATTERN.sub(_card_replacer, text)


def redact_text(text: str) -> str:
    """Replace credentials and personal data in ``text`` with typed placeholders."""
    if not text:
        return text
    redacted = redact_credentials(text)
    redacted = _EMAIL_PATTERN.sub("[REDACTED_EMAIL_ADDRESS]", redacted)
    redacted = _redact_numbers(redacted)
    if env_flag("BLOG_AGENT_ENABLE_DLP"):
        redacted = _redact_with_cloud_dlp(redacted)
    return redacted


# An address in an activity line: of a post, a deck, a repository.
_WEB_ADDRESS = re.compile(r"https?://[^\s<>\"'`)\]]+")


def redact_stored_line(line: str) -> str:
    """Redact one activity line before it is stored and shown on the page (SEC-4).

    The line gets the same patterns as a log record: credentials, e-mail
    addresses, resident and social security numbers, phone numbers and card
    numbers. Two things differ from ``redact_text``, both on purpose.

    Regular expressions only, never Cloud DLP. A run writes these lines one
    after another from the event loop that also answers the page; a network
    call for each line would hold the page up, and a line that waits for a
    service cannot be stored before it is shown. The structured record of
    the same line (``log_activity``) passes the log filter like every other
    record, with Cloud DLP when that is enabled.

    The digit patterns are not applied inside a web address. An activity
    line says where a post or a deck is, and a person has to be able to open
    that address: a long number in a path or a query (a post number, a
    timestamp) is part of the address, not a phone or card number. A
    credential or an e-mail address inside an address is still replaced.

    Args:
        line: The activity line as the run wrote it.

    Returns:
        The line with typed placeholders where the patterns matched.
    """
    if not line:
        return line
    line = redact_credentials(line)
    line = _EMAIL_PATTERN.sub("[REDACTED_EMAIL_ADDRESS]", line)
    parts, position = [], 0
    for address in _WEB_ADDRESS.finditer(line):
        parts.append(_redact_numbers(line[position : address.start()]))
        parts.append(address.group(0))
        position = address.end()
    parts.append(_redact_numbers(line[position:]))
    return "".join(parts)


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
    if not project or _reporting_dlp_failure.get():
        return text
    try:
        if _dlp_client is None:
            from google.cloud import dlp_v2

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
        # Imported here: app.observability imports this module.
        from app.observability import log_event

        reporting = _reporting_dlp_failure.set(True)
        try:
            log_event(
                "Cloud DLP redaction skipped; the regex result is kept",
                level=logging.WARNING,
                event_phase="REDACTION",
                error_type=type(exc).__name__,
            )
        finally:
            _reporting_dlp_failure.reset(reporting)
        return text
