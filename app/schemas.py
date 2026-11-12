"""Pydantic schemas for tool inputs, tool outputs, and structured model outputs.

ADK function tools receive plain JSON arguments from the model. Each tool
builds the matching ``*Input`` model from those arguments first, so malformed
or out-of-policy values are rejected before any side effect happens. Tools
return ``*Output`` models (as dicts), and every failure is a ``ToolError`` that
tells the model how to recover instead of raising.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.settings import ALLOWED_ARTICLE_HOSTS, MAX_ARTICLE_CHARS

LanguageCode = Literal["ko", "en", "ja"]
PlatformName = Literal["naver_blog", "devto", "qiita"]
CodeMode = Literal[
    "none", "inline_markdown", "python_script", "jupyter_notebook", "jupyter_book"
]


# ---------------------------------------------------------------------------
# Errors returned to the model
# ---------------------------------------------------------------------------
class ToolError(BaseModel):
    """What every tool returns when it cannot do what was asked."""

    status: Literal["error"] = "error"
    error_code: str = Field(description="Stable machine-readable code.")
    message: str = Field(description="What went wrong, in one or two sentences.")
    recovery_instruction: str = Field(
        description="What the model should do next: fix an argument, ask the user, or stop."
    )


def tool_error(
    error_code: str, message: str, recovery_instruction: str
) -> dict[str, Any]:
    return ToolError(
        error_code=error_code,
        message=message,
        recovery_instruction=recovery_instruction,
    ).model_dump()


def validation_error(exc: ValidationError, recovery_instruction: str) -> dict[str, Any]:
    """Turn a Pydantic error into a ``ToolError`` naming the offending fields."""
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}"
        for err in exc.errors()[:5]
    )
    return tool_error("INVALID_ARGUMENTS", problems, recovery_instruction)


# ---------------------------------------------------------------------------
# Shared validators
# ---------------------------------------------------------------------------
def _allowed_article_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https":
        raise ValueError("only https URLs are accepted")
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ALLOWED_ARTICLE_HOSTS:
        allowed = ", ".join(sorted(ALLOWED_ARTICLE_HOSTS))
        raise ValueError(f"host '{host}' is not allowed; allowed hosts: {allowed}")
    return value.strip()


# ---------------------------------------------------------------------------
# Stage 1: seed article
# ---------------------------------------------------------------------------
class FetchArticleInput(BaseModel):
    url: str = Field(
        description="https URL of a post on Qiita, DEV Community or Naver Blog."
    )

    _check_url = field_validator("url")(_allowed_article_url)


class FetchArticleOutput(BaseModel):
    status: Literal["success"] = "success"
    platform: PlatformName
    url: str
    title: str
    language: LanguageCode
    tags: list[str]
    markdown_chars: int
    extraction: Literal["api_markdown", "html_best_effort"] = Field(
        description="'api_markdown' is the author's original Markdown; "
        "'html_best_effort' was reconstructed from a web page and needs review."
    )
    note: str


class SaveSeedInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=50, max_length=MAX_ARTICLE_CHARS)
    source_language: LanguageCode
    code_mode: CodeMode = "none"
    code_repo_url: str | None = None
    finalized: bool = False

    @field_validator("code_repo_url")
    @classmethod
    def _https_repo(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not value.startswith("https://"):
            raise ValueError("code_repo_url must start with https://")
        return value


class SeedSavedOutput(BaseModel):
    status: Literal["success"] = "success"
    project_id: str
    revision: int
    source_language: LanguageCode
    target_languages: list[LanguageCode]
    finalized: bool
    file: str
    next_step: str


class LoadDraftInput(BaseModel):
    relative_path: str = Field(min_length=1, max_length=300)

    @field_validator("relative_path")
    @classmethod
    def _markdown_inside_workspace(cls, value: str) -> str:
        if value.startswith(("/", "~")) or ".." in value.split("/"):
            raise ValueError("path must be relative to the workspace directory")
        if not value.lower().endswith((".md", ".markdown")):
            raise ValueError("only .md files can be loaded")
        return value


# ---------------------------------------------------------------------------
# Stage 2: localization review (structured model output)
# ---------------------------------------------------------------------------
class LocalizedTitles(BaseModel):
    """Structured output of the title localizer: one title per language.

    The limit is the tightest platform limit (DEV Community: 128 characters).
    """

    ko: str = Field(max_length=120, description="Korean title for Naver Blog.")
    en: str = Field(max_length=120, description="English title for DEV Community.")
    ja: str = Field(max_length=120, description="Japanese title for Qiita.")


class LanguageReview(BaseModel):
    language: LanguageCode
    grade: Literal["pass", "fail"]
    issues: list[str] = Field(
        default_factory=list,
        description="Concrete, fixable problems. Empty when the grade is 'pass'.",
    )


class ReviewVerdict(BaseModel):
    """Output of the localization critic. Drives the review loop."""

    reviews: list[LanguageReview]
    overall: Literal["pass", "fail"] = Field(
        description="'pass' only if every reviewed language passes."
    )


# ---------------------------------------------------------------------------
# Stage 3: publishing
# ---------------------------------------------------------------------------
_DEVTO_TAG = re.compile(r"^[a-z0-9]{1,30}$")


class QiitaDraftInput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    tags: list[str] = Field(min_length=1, max_length=5)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        cleaned = [t.strip() for t in value if t and t.strip()]
        if not cleaned:
            raise ValueError("Qiita requires at least one tag")
        if any(" " in t for t in cleaned):
            raise ValueError("Qiita tags cannot contain spaces")
        return cleaned


class DevtoDraftInput(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    tags: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        cleaned = [t.strip().lower() for t in value if t and t.strip()]
        bad = [t for t in cleaned if not _DEVTO_TAG.match(t)]
        if bad:
            raise ValueError(
                f"DEV tags must be lowercase letters and digits only, no spaces or hyphens: {bad}"
            )
        return cleaned


class PublicationRecord(BaseModel):
    platform: PlatformName
    language: LanguageCode
    article_id: str | None = None
    url: str | None = None
    state: Literal["exported_for_manual_post", "draft", "published", "existing_source"]
    title: str | None = None


class PublishOutput(BaseModel):
    status: Literal["success", "pending_human_approval", "declined"]
    publication: PublicationRecord | None = None
    message: str


class UrlCheck(BaseModel):
    url: str
    platform: PlatformName
    reachable: bool
    http_status: int | None = None
    title_found: str | None = None
    problem: str | None = None


# ---------------------------------------------------------------------------
# Stage 4: slides
# ---------------------------------------------------------------------------
class PresentationBriefInput(BaseModel):
    language: LanguageCode
    audience: str = Field(min_length=3, max_length=200)
    minutes: int = Field(ge=1, le=60, description="Talk length; one slide per minute.")


class SlideSpec(BaseModel):
    number: int = Field(ge=1, le=60)
    title: str = Field(description="Slide title, at most about 10 words.")
    bullets: list[str] = Field(description="Two to five short on-slide bullet points.")
    visual_note: str = Field(
        description="What to show: a diagram, a code excerpt, a screenshot, or 'text only'."
    )
    speaker_script: str = Field(
        description="What the presenter says for this slide, written to be read "
        "aloud in about one minute."
    )
    image_url: str = Field(
        default="",
        description="One image, diagram or table from the article that illustrates "
        "this slide: its URL, its 'mermaid:N' reference, or its 'table:N' "
        "reference, copied exactly from the list of article visuals; empty "
        "when none fits.",
    )


class SlideDeckPlan(BaseModel):
    """Structured output of the slide planner."""

    language: LanguageCode
    deck_title: str
    audience: str
    opening_script: str = Field(
        default="",
        description="What the presenter says on the title slide, before slide 1: "
        "a greeting, the talk title said aloud, then one or two sentences on "
        "what the title means and what the audience will take away. About 20 "
        "seconds. No agenda here.",
    )
    slides: list[SlideSpec]
    closing_script: str = Field(
        default="",
        description="What the presenter says on the final thank-you slide: "
        "thanks to the audience and an invitation to ask questions. One or two "
        "sentences.",
    )
