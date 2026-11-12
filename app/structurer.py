"""Automatic Markdown structuring for plain-text or unformatted articles.

When an article is imported from a platform without Markdown export (such as
Naver Blog), or handed in as unstructured plain text without headings, this
module transforms the text into clean GitHub Flavored Markdown (GFM) with
appropriate heading hierarchy, itemized lists, tables, and code blocks.
"""

from __future__ import annotations

import logging
import re

from google import genai

from app.models import FAST_MODEL

logger = logging.getLogger(__name__)

_HEADING_PATTERN = re.compile(r"^#{1,3}\s+\S+", re.MULTILINE)

_STRUCTURING_PROMPT = """You are an expert technical editor.
Convert the following unformatted technical article text into clean, professional GitHub Flavored Markdown (GFM).

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


async def auto_structure_markdown(text: str) -> str:
    """Structure plain text into formatted Markdown using the fast model.

    Falls back safely to the original text on any transient failure or when
    running in an environment without LLM credentials.
    """
    if not text or not text.strip():
        return text

    try:
        client = genai.Client()
        response = await client.aio.models.generate_content(
            model=FAST_MODEL,
            contents=[
                {
                    "role": "user",
                    "parts": [{"text": f"{_STRUCTURING_PROMPT}\n\n{text}"}],
                }
            ],
        )
        candidate = (response.text or "").strip()
        if candidate.startswith("```markdown") and candidate.endswith("```"):
            candidate = candidate[len("```markdown") : -3].strip()
        elif candidate.startswith("```") and candidate.endswith("```"):
            candidate = candidate[3:-3].strip()

        # Sanity check: preserve content length
        if len(candidate) >= len(text.strip()) * 0.5:
            return candidate
        logger.warning(
            "Structured markdown was suspiciously short (%d vs %d chars); keeping original text.",
            len(candidate),
            len(text),
        )
        return text
    except Exception as exc:
        logger.warning(
            "Auto-structuring markdown failed (%s); keeping original text.", exc
        )
        return text
