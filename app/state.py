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
LOCALIZED_TITLES = "localized_titles"
# Per language, what a person signed off: a digest of the title and text and
# whether they acknowledged credential-looking findings (see app/policy.py).
FINALIZED_POSTS = "finalized_posts"
# Per pending request to finalize, the digest of what the person was asked
# about, so a yes cannot be carried over to a text that changed meanwhile.
FINALIZATIONS_ASKED = "finalizations_asked"

# Slide planning works on one language at a time. These three keys are the
# working area of the planning run in progress; nothing reads them afterwards.
PRESENTATION_BRIEF = "presentation_brief"  # language, audience, minutes
SLIDE_DECK_PLAN = "slide_deck_plan"  # the planner model's latest draft
SLIDE_DRAFT_TIMING = "slide_timing_report"  # timing check of that draft
# A planning run started by the pipeline gets a working session of its own
# (SLD-5); these keys are not copied into it and never come back from it.
PLANNING_WORKING_AREA = (PRESENTATION_BRIEF, SLIDE_DECK_PLAN, SLIDE_DRAFT_TIMING)

# The record, per language. Everything outside the planning loop reads these.
SLIDE_PLANS = "slide_plans"  # the plan of record
SLIDE_TIMING = "slide_timing"  # its timing report
GOOGLE_SLIDES_DECKS = "google_slides_decks"  # the deck created from it
# The text on which the deck and its Markdown plan last agreed.
DECK_TEXT_BASELINE = "google_slides_synced_text"

# Keys that describe one article. Storing a different article in the same
# session discards them, so nothing of the old article is posted or shown
# under the new one.
DERIVED_FROM_SEED = (
    FIDELITY_REPORT,
    REVIEW_VERDICT,
    PUBLICATIONS,
    LOCALIZED_TITLES,
    FINALIZED_POSTS,
    FINALIZATIONS_ASKED,
    PRESENTATION_BRIEF,
    SLIDE_DECK_PLAN,
    SLIDE_DRAFT_TIMING,
    SLIDE_PLANS,
    SLIDE_TIMING,
    GOOGLE_SLIDES_DECKS,
    DECK_TEXT_BASELINE,
)


def localized_key(language: str) -> str:
    return f"localized_markdown_{language}"


def article_markdown(state: Mapping[str, Any], language: str) -> str | None:
    """Markdown of the article in ``language``: the seed itself or its localization."""
    if state.get(SOURCE_LANGUAGE) == language:
        return state.get(SEED_MARKDOWN)
    return state.get(localized_key(language))


def article_title(state: Mapping[str, Any], language: str) -> str:
    """Title of the article in ``language``.

    The source language always uses the seed title as the author wrote it. A
    target language uses its localized title, falling back to the seed title
    when localization has not produced one yet.
    """
    seed_title = state.get(SEED_TITLE) or ""
    if state.get(SOURCE_LANGUAGE) == language:
        return seed_title
    return (
        (state.get(LOCALIZED_TITLES) or {}).get(language) or ""
    ).strip() or seed_title
