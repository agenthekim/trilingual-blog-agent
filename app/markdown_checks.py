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


def _parse_fences(markdown: str) -> tuple[list[tuple[str, str]], str, bool]:
    """Return ([(fence language, body)], text outside code blocks, fences balanced)."""
    blocks: list[tuple[str, str]] = []
    prose: list[str] = []
    current: list[str] | None = None
    fence = language = ""
    for line in (markdown or "").splitlines():
        match = _FENCE.match(line.strip())
        if current is None:
            if match:
                fence = match.group("fence")
                info = match.group("info").strip().split()
                language = info[0].lower() if info else ""
                current = []
            else:
                prose.append(line)
        elif (
            match
            and match.group("fence").startswith(fence[0])
            and len(match.group("fence")) >= len(fence)
            and not match.group("info").strip()
        ):
            blocks.append((language, "\n".join(current)))
            current = None
        else:
            current.append(line)
    balanced = current is None
    if current is not None:
        blocks.append((language, "\n".join(current)))
    return blocks, "\n".join(prose), balanced


def split_code_blocks(markdown: str) -> tuple[list[str], str, bool]:
    """Return (code block bodies, text outside code blocks, fences balanced)."""
    blocks, prose, balanced = _parse_fences(markdown)
    return [body for _, body in blocks], prose, balanced


def extract_urls(markdown: str) -> set[str]:
    _, prose, _ = split_code_blocks(markdown)
    urls = set(_LINK.findall(prose)) | set(_BARE_URL.findall(prose))
    return {u.rstrip(".,;:") for u in urls}


_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*<?(https://[^\s)>]+)>?")
_HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc=[\"'](https://[^\"']+)[\"'][^>]*>", re.I)
_HTML_ALT = re.compile(r"\balt=[\"']([^\"']*)[\"']", re.I)


def extract_images(markdown: str) -> list[tuple[str, str]]:
    """Return (description, https URL) of each image outside code blocks, in order."""
    _, prose, _ = split_code_blocks(markdown)
    found: list[tuple[int, str, str]] = [
        (m.start(), m.group(1).strip(), m.group(2)) for m in _MD_IMAGE.finditer(prose)
    ]
    for match in _HTML_IMAGE.finditer(prose):
        alt = _HTML_ALT.search(match.group(0))
        found.append(
            (match.start(), alt.group(1).strip() if alt else "", match.group(1))
        )
    seen: set[str] = set()
    images = []
    for _, description, url in sorted(found):
        if url not in seen:
            seen.add(url)
            images.append((description, url))
    return images


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


# Fenced blocks that hold text for people rather than code for machines. Their
# natural-language parts are translated; everything else must stay as written.
DIAGRAM_FENCES = frozenset({"mermaid"})
PLAIN_TEXT_FENCES = frozenset({"", "text", "txt", "plaintext", "plain"})

# Hangul, kana and CJK ideographs: a line containing any of these is prose.
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_MERMAID_LABELS = (
    (re.compile(r'"[^"\n]*"'), '""'),  # "quoted label"
    (re.compile(r"\|[^|\n]*\|"), "||"),  # -->|edge label|
    (re.compile(r"[\[({][^\[\](){}\n]*[\])}]"), "[]"),  # [bare label]
    (re.compile(r"(?<=\S):\s.*$", re.MULTILINE), ":"),  # A->>B: message
)


def _mermaid_skeleton(code: str) -> list[str]:
    """Diagram structure with every label blanked: nodes, edges and keywords remain."""
    for pattern, blank in _MERMAID_LABELS:
        previous = None
        while previous != code:
            previous, code = code, pattern.sub(blank, code)
    return [" ".join(line.split()) for line in code.splitlines() if line.strip()]


def _plain_text_problem(source: str, localized: str) -> str | None:
    """Check a plain-text block: prose lines may be translated, the rest may not."""
    src = [line.rstrip() for line in source.splitlines() if line.strip()]
    loc = [line.rstrip() for line in localized.splitlines() if line.strip()]
    if len(src) != len(loc):
        return f"line count changed from {len(src)} to {len(loc)}"
    for src_line, loc_line in zip(src, loc, strict=True):
        if src_line != loc_line and not _CJK.search(src_line):
            return f"the line '{src_line.strip()[:60]}' must be kept exactly as written"
    return None


def _compare_block(
    index: int, language: str, source: str, localized: str
) -> tuple[str, str] | None:
    """Return ("error" | "warning", message) for one code block, or None if fine."""
    if source == localized:
        return None
    if language in DIAGRAM_FENCES:
        if _mermaid_skeleton(source) == _mermaid_skeleton(localized):
            return None
        return (
            "error",
            f"Code block {index} (mermaid): the diagram structure changed. Keep "
            "node ids, arrows and keywords exactly; translate only label text.",
        )
    if language in PLAIN_TEXT_FENCES:
        problem = _plain_text_problem(source, localized)
        if problem is None:
            return None
        return (
            "error",
            f"Code block {index} (plain text): {problem}. Translate only the "
            "natural-language lines and keep the line layout.",
        )
    if _non_comment_lines(source) == _non_comment_lines(localized):
        return "warning", f"Code block {index}: only comments differ."
    return (
        "error",
        f"Code block {index}: code was altered. Code must be copied verbatim; "
        "only comments may be translated.",
    )


def check_fidelity(source: str, localized: str) -> dict[str, Any]:
    """Compare a localized article with its source.

    Returns ``{"passed": bool, "errors": [...], "warnings": [...], "stats": {...}}``.
    Errors are structural losses that must be fixed; warnings deserve a look.

    Code blocks are compared by kind. Program code must be identical apart from
    comments. Mermaid diagrams must keep their structure while label text may
    be translated. Plain-text blocks may translate lines written in Korean or
    Japanese; lines without such text (commands, identifiers, arrows, and all
    lines of an English source) must stay identical.
    """
    errors: list[str] = []
    warnings: list[str] = []

    src_blocks, src_prose, _ = _parse_fences(source)
    loc_blocks, loc_prose, loc_balanced = _parse_fences(localized)

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
        for index, ((language, src), (loc_language, loc)) in enumerate(
            zip(src_blocks, loc_blocks, strict=True), start=1
        ):
            if language != loc_language:
                errors.append(
                    f"Code block {index}: fence language changed from "
                    f"'{language}' to '{loc_language}'."
                )
                continue
            finding = _compare_block(index, language, src, loc)
            if finding:
                (errors if finding[0] == "error" else warnings).append(finding[1])

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
