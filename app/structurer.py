"""Automatic Markdown structuring for plain-text or unformatted articles (SRC-7).

When an article is imported from a platform without Markdown export (such as
Naver Blog), or handed in as unstructured plain text without headings, a model
gives the text GitHub Flavored Markdown structure: headings, lists, tables and
code blocks.

The model is asked to change nothing else, and code checks that it did not:
the structured copy is used only when, markup and whitespace aside, it has the
same letters and digits as the original (``same_letters``). Otherwise the
original is the seed. The caller keeps the original either way (INV-11) and
tells the person which of the two happened.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from app import llm
from app.observability import log_event

_HEADING_PATTERN = re.compile(r"^#{1,3}\s+\S+", re.MULTILINE)
# The line that opens (or closes) a code fence: what follows the fence on that
# line is the language tag, which is markup and not the author's text.
_FENCE_LINE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*$", re.MULTILINE)

_STRUCTURING_PROMPT = """You are an expert technical editor.
Convert the unformatted technical article text you are given into clean, professional GitHub Flavored Markdown (GFM).

Strict Rules:
1. Retain 100% of the original content and wording. Do not add, omit, soften, or alter technical facts, data, explanations, commands, or code.
2. Structure the document properly:
   - Use a Level 1 heading (# Title) at the top for the document title if appropriate.
   - Use Level 2 headings (## 1. ...) for major numbered sections.
   - Use Level 3 headings (### 1-1 ...) for subsections.
   - Convert itemized listings into Markdown bullet points (- **Item**: description).
   - Convert comparative rows and tabular data into clean Markdown tables (| Col 1 | Col 2 |\\n|---|---|).
   - Wrap terminal commands, configuration snippets, or code in fenced code blocks (```bash, ```text, etc.).
3. Output ONLY the resulting clean Markdown text. Do NOT wrap the entire response in outer markdown fences (```markdown ... ```), and do not include any preface, conversational remarks, or commentary.
4. Add no word of your own: no title, table header, label or caption that is not in the text. The result is checked letter by letter against the original and is discarded if a letter or digit was added, removed or changed.
5. Do not copy the markers around the text into your answer.
"""


def needs_markdown_structuring(text: str, extraction: str = "") -> bool:
    """Return True if the text lacks Markdown headings and would benefit from structuring."""
    if extraction == "html_best_effort":
        return True
    stripped = text.strip()
    if len(stripped) < 100:
        return False
    # If the text already has Markdown headings (#, ##, ###), no structuring is needed.
    if _HEADING_PATTERN.search(stripped):
        return False
    return True


def _letters(text: str) -> Counter[str]:
    """How often each letter and digit occurs, fence language tags left out."""
    plain = _FENCE_LINE.sub("", unicodedata.normalize("NFC", text))
    return Counter(char for char in plain if char.isalnum())


def same_letters(original: str, structured: str) -> bool:
    """Whether two texts have the same letters and digits, kinds and counts.

    Markup, punctuation and whitespace are left out of the comparison, and so
    is the language tag of a code fence (``bash`` in a line that opens a
    block), which structuring adds. A word added, dropped or respelled makes
    the two differ.
    """
    return _letters(original) == _letters(structured)


StructuringOutcome = Literal["structured", "letters_differ", "no_answer"]


@dataclass(frozen=True)
class Structuring:
    """What came of structuring: the text to use as the seed, and why."""

    markdown: str
    outcome: StructuringOutcome

    @property
    def used(self) -> bool:
        """Whether ``markdown`` is the structured copy and not the original."""
        return self.outcome == "structured"


def _without_outer_fence(answer: str) -> str:
    candidate = answer.strip()
    if candidate.startswith("```markdown") and candidate.endswith("```"):
        return candidate[len("```markdown") : -3].strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        inner = candidate[3:-3]
        # One block around everything, not a text that begins and ends with
        # blocks of its own.
        if "```" not in inner:
            return inner.strip()
    return candidate


def _keeps_the_text(reason: str, **fields: Any) -> None:
    """Say in the structured log why the text is kept as it was supplied."""
    log_event(
        "structuring: keeping the text as supplied",
        level=logging.WARNING,
        event_phase="STRUCTURING",
        reason=reason,
        **fields,
    )


async def auto_structure_markdown(text: str) -> Structuring:
    """Give plain text Markdown structure through the model gateway.

    Returns the structured copy only when ``same_letters`` confirms it; in
    every other case (the letters differ, the model gave no answer, the call
    failed) the text comes back exactly as it went in, with the reason.
    """
    if not text or not text.strip():
        return Structuring(text, "no_answer")
    try:
        answer = await llm.generate_text("structure_source", _STRUCTURING_PROMPT, text)
    except llm.ModelCallFailed:
        _keeps_the_text("the model call failed")
        return Structuring(text, "no_answer")
    candidate = _without_outer_fence(answer)
    if not candidate:
        _keeps_the_text("the model gave no text")
        return Structuring(text, "no_answer")
    if not same_letters(text, candidate):
        _keeps_the_text(
            "the structured copy does not have the letters of the original",
            structured_chars=len(candidate),
            original_chars=len(text),
        )
        return Structuring(text, "letters_differ")
    return Structuring(candidate, "structured")
