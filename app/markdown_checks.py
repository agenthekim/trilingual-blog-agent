"""Deterministic Markdown checks used to evaluate the agent's own output.

Translation models make a recognisable set of mistakes with technical
articles: they translate identifiers inside code blocks, drop or rewrite
links, merge or split sections, and occasionally stop early. All of these can
be detected without another model call by comparing structure, so they are
checked in plain Python before (and independently of) the LLM critic.
"""

from __future__ import annotations

import re
from typing import Any

_FENCE = re.compile(r"^(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)$")
_LINK = re.compile(r"\]\(\s*<?(https?://[^\s)>]+)>?")
_BARE_URL = re.compile(r"(?<![(\[<\"'=])\bhttps?://[^\s)<>\]\"']+")
_HEADING = re.compile(r"^(#{1,6})\s+\S")
_COMMENT = re.compile(r"^\s*(#|//|--|;|%|/\*|\*|<!--)")


def split_code_blocks(markdown: str) -> tuple[list[str], str, bool]:
    """Return (code block bodies, text outside code blocks, fences balanced)."""
    blocks: list[str] = []
    prose: list[str] = []
    current: list[str] | None = None
    fence = ""
    for line in (markdown or "").splitlines():
        match = _FENCE.match(line.strip())
        if current is None:
            if match:
                fence = match.group("fence")
                current = []
            else:
                prose.append(line)
        elif (
            match
            and match.group("fence").startswith(fence[0])
            and len(match.group("fence")) >= len(fence)
            and not match.group("info").strip()
        ):
            blocks.append("\n".join(current))
            current = None
        else:
            current.append(line)
    balanced = current is None
    if current is not None:
        blocks.append("\n".join(current))
    return blocks, "\n".join(prose), balanced


def extract_urls(markdown: str) -> set[str]:
    _, prose, _ = split_code_blocks(markdown)
    urls = set(_LINK.findall(prose)) | set(_BARE_URL.findall(prose))
    return {u.rstrip(".,;:") for u in urls}


def heading_levels(markdown: str) -> list[int]:
    _, prose, _ = split_code_blocks(markdown)
    return [
        len(m.group(1)) for line in prose.splitlines() if (m := _HEADING.match(line))
    ]


def _non_comment_lines(code: str) -> list[str]:
    return [
        line.rstrip()
        for line in code.splitlines()
        if line.strip() and not _COMMENT.match(line)
    ]


def check_fidelity(source: str, localized: str) -> dict[str, Any]:
    """Compare a localized article with its source.

    Returns ``{"passed": bool, "errors": [...], "warnings": [...], "stats": {...}}``.
    Errors are structural losses that must be fixed; warnings deserve a look.
    """
    errors: list[str] = []
    warnings: list[str] = []

    src_blocks, src_prose, _ = split_code_blocks(source)
    loc_blocks, loc_prose, loc_balanced = split_code_blocks(localized)

    if not (localized or "").strip():
        return {
            "passed": False,
            "errors": ["The localized article is empty."],
            "warnings": [],
            "stats": {},
        }

    if not loc_balanced:
        errors.append("A code fence is opened but never closed.")

    if len(src_blocks) != len(loc_blocks):
        errors.append(
            f"Code block count changed: source has {len(src_blocks)}, "
            f"localized has {len(loc_blocks)}."
        )
    else:
        for index, (src, loc) in enumerate(
            zip(src_blocks, loc_blocks, strict=True), start=1
        ):
            if src == loc:
                continue
            if _non_comment_lines(src) == _non_comment_lines(loc):
                warnings.append(f"Code block {index}: only comments differ.")
            else:
                errors.append(
                    f"Code block {index}: code was altered. Code must be copied "
                    "verbatim; only comments may be translated."
                )

    missing_urls = sorted(extract_urls(source) - extract_urls(localized))
    if missing_urls:
        shown = ", ".join(missing_urls[:5])
        errors.append(
            f"{len(missing_urls)} link(s) missing from the localized text: {shown}"
        )

    src_headings, loc_headings = heading_levels(source), heading_levels(localized)
    if src_headings != loc_headings:
        errors.append(
            f"Heading structure changed: source levels {src_headings}, "
            f"localized levels {loc_headings}."
        )

    if src_prose.strip():
        ratio = len(loc_prose) / max(len(src_prose), 1)
        if ratio < 0.3:
            errors.append(
                f"Localized prose is only {ratio:.0%} of the source length; "
                "the text looks truncated."
            )
        elif ratio > 3.0:
            warnings.append(f"Localized prose is {ratio:.1f}x the source length.")

    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "code_blocks": len(loc_blocks),
            "links": len(extract_urls(localized)),
            "headings": len(loc_headings),
            "chars": len(localized),
        },
    }


# Speaking-rate assumptions for the one-minute-per-slide rule. These are
# starting values to be tuned against a timed read-through, not measurements.
SPEECH_UNITS_PER_MINUTE: dict[str, tuple[str, int]] = {
    "ko": ("chars", 300),
    "ja": ("chars", 330),
    "en": ("words", 140),
}


def estimate_speaking_seconds(script: str, language: str) -> float:
    unit, per_minute = SPEECH_UNITS_PER_MINUTE[language]
    if unit == "words":
        amount = len(script.split())
    else:
        amount = len(re.sub(r"\s+", "", script))
    return round(amount / per_minute * 60, 1)
