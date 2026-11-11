"""Names of the session-state keys shared by tools and agents, and small accessors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PROJECT_ID = "project_id"
SEED_TITLE = "seed_title"
SEED_MARKDOWN = "seed_markdown"
SEED_FINALIZED = "seed_finalized"
SOURCE_LANGUAGE = "source_language"
TARGET_LANGUAGES = "target_languages"
ENTRY_MODE = "entry_mode"
CODE_MODE = "code_mode"
CODE_REPO_URL = "code_repo_url"
FIDELITY_REPORT = "fidelity_report"
REVIEW_VERDICT = "review_verdict"
PUBLICATIONS = "publications"
PRESENTATION_BRIEF = "presentation_brief"
SLIDE_DECK_PLAN = "slide_deck_plan"


def localized_key(language: str) -> str:
    return f"localized_markdown_{language}"


def article_markdown(state: Mapping[str, Any], language: str) -> str | None:
    """Markdown of the article in ``language``: the seed itself or its localization."""
    if state.get(SOURCE_LANGUAGE) == language:
        return state.get(SEED_MARKDOWN)
    return state.get(localized_key(language))
