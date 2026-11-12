"""Pydantic schemas for tool inputs, tool outputs, and structured model outputs.

ADK function tools receive plain JSON arguments from the model. Each tool
builds the matching ``*Input`` model from those arguments first, so malformed
or out-of-policy values are rejected before any side effect happens. Tools
return ``*Output`` models (as dicts), and every failure is a ``ToolError`` that
tells the model how to recover instead of raising.

``TOOL_OUTPUT_MODELS`` at the end of this file names the output model of every
tool of the agent tree (OPS-5); a tool without one fails a unit test.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.settings import ALLOWED_ARTICLE_HOSTS, MAX_ARTICLE_CHARS, MAX_TITLE_CHARS

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


class ToolOutput(BaseModel):
    """What a tool returns when it did what was asked, or waits for a person.

    A key the model does not declare is refused, so a tool cannot start to
    return something its output model does not say.
    """

    model_config = ConfigDict(extra="forbid")


def tool_output(model: type[ToolOutput], **fields: Any) -> dict[str, Any]:
    """A tool's result as a dict, checked against its declared output model.

    Args:
        model: The output model of the tool.
        fields: The keys of the result, ``status`` among them.

    Returns:
        Exactly the keys that were given, after validation: an optional key
        that was left out stays out, so a result that waits for approval has
        no empty ``deck`` or ``request`` in it.

    Raises:
        pydantic.ValidationError: The result does not fit the model. That is
        a bug in the tool; under an agent it reaches the model as the guided
        error ``TOOL_FAILED`` (``app.plugins.ToolFailurePlugin``).
    """
    return model(**fields).model_dump(exclude_unset=True)


ApprovalStatus = Literal["success", "pending_human_approval", "declined"]


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


class LanguageInput(BaseModel):
    """The one argument of every tool that works on one language's post or deck."""

    language: LanguageCode = Field(description="'ko', 'en' or 'ja'.")


def checked_language(language: str) -> LanguageCode | dict[str, Any]:
    """The language when it is one of the three, else the error a tool returns."""
    try:
        return LanguageInput(language=language).language  # type: ignore[arg-type]
    except ValidationError as exc:
        return validation_error(
            exc, "Call again with language set to 'ko', 'en' or 'ja'."
        )


# ---------------------------------------------------------------------------
# Stage 1: seed article
# ---------------------------------------------------------------------------
class FetchArticleInput(BaseModel):
    url: str = Field(
        description="https URL of a post on Qiita, DEV Community or Naver Blog."
    )

    _check_url = field_validator("url")(_allowed_article_url)


class FetchArticleOutput(ToolOutput):
    """Result of ``fetch_published_article_markdown``."""

    status: Literal["success"] = "success"
    platform: PlatformName = Field(description="The platform the post was read from.")
    url: str = Field(description="The address the post was read from.")
    title: str = Field(description="The title of the post on the platform.")
    language: LanguageCode = Field(
        description="The language the text is written in, as detected."
    )
    tags: list[str] = Field(description="The tags of the post on the platform.")
    markdown_chars: int = Field(description="Length of the Markdown text.")
    extraction: Literal["api_markdown", "html_best_effort"] = Field(
        description="'api_markdown' is the author's original Markdown; "
        "'html_best_effort' was reconstructed from a web page and needs review."
    )
    note: str = Field(description="How the text was obtained and what to check.")
    project_id: str = Field(description="Id of the article project that was made.")
    seed_finalized: bool = Field(
        description="Whether the seed is final already (original Markdown) or "
        "a draft a person has to review (reconstructed text)."
    )
    target_languages: list[LanguageCode] = Field(
        description="The languages the seed will be localized into."
    )
    file: str = Field(description="Path of the Markdown file of the seed.")
    next_step: str = Field(description="What to offer the user next.")


class SaveSeedInput(BaseModel):
    title: str = Field(
        min_length=1, max_length=200, description="Title in the source language."
    )
    markdown: str = Field(
        min_length=50,
        max_length=MAX_ARTICLE_CHARS,
        description="The whole Markdown body of the article, not an excerpt.",
    )
    source_language: LanguageCode = Field(
        description="The language the article is written in."
    )
    code_mode: CodeMode = Field(
        default="none", description="How companion code is delivered."
    )
    code_repo_url: str | None = Field(
        default=None, description="https URL of the companion code, if any."
    )
    finalized: bool = Field(
        default=False, description="True only when the user said the seed is final."
    )

    @field_validator("code_repo_url")
    @classmethod
    def _https_repo(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not value.startswith("https://"):
            raise ValueError("code_repo_url must start with https://")
        return value


class SeedSavedOutput(ToolOutput):
    """Result of ``save_seed_article_revision`` and ``load_markdown_draft_file``."""

    status: Literal["success"] = "success"
    project_id: str = Field(description="Id of the article project.")
    revision: int = Field(description="Number of the revision that was stored.")
    source_language: LanguageCode = Field(description="Language of the seed.")
    target_languages: list[LanguageCode] = Field(
        description="The languages the seed will be localized into."
    )
    finalized: bool = Field(description="Whether the seed is final or a draft.")
    file: str = Field(description="Path of the Markdown file of the seed.")
    next_step: str = Field(description="What to offer the user next.")


class LoadDraftInput(BaseModel):
    relative_path: str = Field(
        min_length=1,
        max_length=300,
        description="Path of a .md file, relative to the workspace directory.",
    )
    source_language: LanguageCode = Field(
        description="The language the draft is written in."
    )

    @field_validator("relative_path")
    @classmethod
    def _markdown_inside_workspace(cls, value: str) -> str:
        if value.startswith(("/", "~")) or ".." in value.split("/"):
            raise ValueError("path must be relative to the workspace directory")
        if not value.lower().endswith((".md", ".markdown")):
            raise ValueError("only .md files can be loaded")
        return value


class SeedFinalizedOutput(ToolOutput):
    """Result of ``finalize_seed_article``."""

    status: Literal["success"]
    project_id: str = Field(description="Id of the article project.")
    source_language: LanguageCode | None = Field(description="Language of the seed.")
    target_languages: list[LanguageCode] = Field(
        description="The languages the seed can now be localized into."
    )
    next_step: str = Field(description="What to offer the user next.")


# ---------------------------------------------------------------------------
# Stage 2: localization review (structured model output)
# ---------------------------------------------------------------------------
class LocalizedTitles(BaseModel):
    """Structured output of the title localizer: one title per language.

    The limit is the tightest platform limit (DEV Community: 128 characters).
    """

    ko: str = Field(
        max_length=MAX_TITLE_CHARS, description="Korean title for Naver Blog."
    )
    en: str = Field(
        max_length=MAX_TITLE_CHARS, description="English title for DEV Community."
    )
    ja: str = Field(max_length=MAX_TITLE_CHARS, description="Japanese title for Qiita.")


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
# Changing a post that exists (PUB-11)
# ---------------------------------------------------------------------------
class PostTitleInput(BaseModel):
    """A new title for one language's post."""

    language: LanguageCode = Field(description="Which post gets the title.")
    title: str = Field(
        min_length=1, max_length=MAX_TITLE_CHARS, description="The new title, one line."
    )

    @field_validator("title")
    @classmethod
    def _one_line_of_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("the title is empty")
        if "\n" in value or "\r" in value:
            raise ValueError("a title is one line")
        return value


class PassageReplacementInput(BaseModel):
    """A passage quoted from one language's post, and what to put in its place."""

    language: LanguageCode = Field(description="Which post is changed.")
    passage: str = Field(
        min_length=1,
        max_length=MAX_ARTICLE_CHARS,
        description="The passage to replace, quoted exactly from the post.",
    )
    replacement: str = Field(
        max_length=MAX_ARTICLE_CHARS,
        description="The new passage; an empty string removes the quoted one.",
    )

    @field_validator("passage")
    @classmethod
    def _says_something(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("the quoted passage is empty")
        return value

    @model_validator(mode="after")
    def _changes_something(self) -> PassageReplacementInput:
        if self.passage == self.replacement:
            raise ValueError("replacement is the same as passage; nothing would change")
        return self


class PostReadOutput(ToolOutput):
    """Result of ``read_article_post``."""

    status: Literal["success"]
    language: LanguageCode = Field(description="The language of the post.")
    title: str = Field(description="The title of the post as it is now.")
    markdown: str = Field(description="The whole Markdown text as it is now.")


class StructureCheck(BaseModel):
    """What ``check_fidelity`` found when it compared a post with the seed."""

    passed: bool = Field(description="False when something structural was lost.")
    errors: list[str] = Field(
        default_factory=list, description="Losses that must be fixed."
    )
    warnings: list[str] = Field(
        default_factory=list, description="Differences that deserve a look."
    )
    stats: dict[str, Any] = Field(
        default_factory=dict, description="Counts of what was compared."
    )


class PostEditOutput(ToolOutput):
    """Result of ``set_article_post_title`` and ``replace_passage_in_article_post``."""

    status: Literal["success"]
    language: LanguageCode = Field(description="The language of the changed post.")
    title: str = Field(description="The title of the post after the change.")
    file: str = Field(description="Path of the Markdown file that was written.")
    markdown_chars: int = Field(description="Length of the text after the change.")
    check: StructureCheck = Field(
        description="The structure check of the changed post against the seed; "
        "always passed for the source-language post, which is the seed."
    )
    naver_file: Literal["written", "removed"] | None = Field(
        description="What happened to the Naver paste file of the Korean post: "
        "written again, removed because the export rules refuse the new text, "
        "or null for a post that has no such file."
    )


# ---------------------------------------------------------------------------
# Stage 3: publishing
# ---------------------------------------------------------------------------
_DEVTO_TAG = re.compile(r"^[a-z0-9]{1,30}$")


class QiitaDraftInput(BaseModel):
    title: str = Field(
        min_length=1, max_length=255, description="The finalized Japanese title."
    )
    tags: list[str] = Field(
        min_length=1, max_length=5, description="One to five tags without spaces."
    )

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
    title: str = Field(
        min_length=1, max_length=128, description="The finalized English title."
    )
    tags: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Up to four tags of lowercase letters and digits.",
    )

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
    """Where one language's post stands on its platform."""

    platform: PlatformName
    language: LanguageCode
    article_id: str | None = Field(
        default=None, description="The platform's id of the post, once it has one."
    )
    url: str | None = Field(
        default=None, description="The address of the post, once it has one."
    )
    state: Literal[
        "exported_for_manual_post", "draft", "published", "existing_source"
    ] = Field(
        description="'exported_for_manual_post': a paste file was written; "
        "'draft': on the platform, not public; 'published': public; "
        "'existing_source': the post the article was read from."
    )
    title: str | None = Field(
        default=None, description="The title that was sent or exported."
    )


class PublishOutput(ToolOutput):
    """Result of ``publish_qiita_article_draft`` and ``publish_devto_article_draft``."""

    status: ApprovalStatus = Field(
        description="'pending_human_approval': nothing was sent, a person is "
        "asked; 'declined': the person said no; 'success': it was sent."
    )
    publication: PublicationRecord | None = Field(
        default=None, description="The record of the post; only with 'success'."
    )
    message: str = Field(description="What happened, to pass on to the user.")


class PostFinalizedOutput(ToolOutput):
    """Result of ``finalize_localized_post``."""

    status: ApprovalStatus = Field(
        description="'pending_human_approval': nothing is recorded, a person "
        "is asked; 'declined': the person said no; 'success': the sign-off is "
        "recorded."
    )
    message: str | None = Field(
        default=None, description="What is waited for, or that it was declined."
    )
    language: LanguageCode | None = Field(
        default=None, description="The language of the post; only with 'success'."
    )
    title: str | None = Field(
        default=None, description="The title that was signed off."
    )
    finalized: bool | None = Field(
        default=None, description="True once the sign-off is recorded."
    )
    structure_check: StructureCheck | None = Field(
        default=None,
        description="The last structure check of the post against the seed, "
        "or null when none was made.",
    )


class NaverExportOutput(ToolOutput):
    """Result of ``export_naver_blog_paste_ready_html``."""

    status: Literal["success"]
    file: str = Field(description="Path of the HTML file to paste from.")
    title: str = Field(description="The title to type into the Naver editor.")
    publication: PublicationRecord = Field(
        description="The record of the Korean post on Naver Blog."
    )
    message: str = Field(description="The manual steps, to pass on to the user.")


class PostedUrlInput(BaseModel):
    """Where a person says a post is: the platform and the address together."""

    platform: PlatformName = Field(description="The platform the post is on.")
    url: str = Field(description="https URL of the post on that platform's site.")

    _check_url = field_validator("url")(_allowed_article_url)


class PostedUrlOutput(ToolOutput):
    """Result of ``record_manually_posted_url``."""

    status: Literal["success"]
    publication: PublicationRecord = Field(
        description="The record of the post, now 'published' with its address."
    )


class UrlCheck(BaseModel):
    """Whether one recorded publication can be reached."""

    url: str = Field(description="The recorded address; empty when there is none.")
    platform: PlatformName
    reachable: bool
    http_status: int | None = Field(
        default=None, description="The HTTP status, when a request was answered."
    )
    title_found: str | None = Field(
        default=None, description="The title the platform or the page shows."
    )
    problem: str | None = Field(
        default=None, description="Why it is not reachable, when it is not."
    )


class UrlVerificationOutput(ToolOutput):
    """Result of ``verify_published_article_urls``."""

    status: Literal["success"]
    all_reachable: bool = Field(description="True when every check is reachable.")
    checks: list[UrlCheck] = Field(description="One check per recorded publication.")


# ---------------------------------------------------------------------------
# Stage 4: slides
# ---------------------------------------------------------------------------
class PresentationBriefInput(BaseModel):
    language: LanguageCode = Field(description="Language of the slides and script.")
    audience: str = Field(min_length=3, max_length=200, description="Who will watch.")
    minutes: int = Field(ge=1, le=60, description="Talk length; one slide per minute.")


class PresentationBriefOutput(ToolOutput):
    """Result of ``set_presentation_brief``."""

    status: Literal["success"]
    brief: PresentationBriefInput = Field(
        description="The brief as stored, with the length and audience resolved."
    )
    next_step: str = Field(description="What to do next.")


class SlideRequestInput(BaseModel):
    """Slides for one or several languages, asked for as one request."""

    languages: list[LanguageCode] = Field(
        min_length=1, max_length=3, description="The languages to make slides for."
    )
    minutes: int = Field(
        ge=0, le=60, description="Talk length; 0 keeps the length each language has."
    )
    audience: str = Field(
        max_length=200, description="Who will watch; empty keeps the audience."
    )

    @field_validator("languages")
    @classmethod
    def _each_once(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("audience")
    @classmethod
    def _stripped(cls, value: str) -> str:
        return value.strip()


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


class AcceptedSlideRequest(BaseModel):
    """A slide request a person approved, as the page takes it over."""

    languages: list[LanguageCode]
    minutes: dict[LanguageCode, int] = Field(
        description="Talk length in minutes, for each language."
    )
    audience: str = Field(description="Who will watch.")


class SlideRequestOutput(ToolOutput):
    """Result of ``request_approval_to_create_slide_decks``."""

    status: ApprovalStatus = Field(
        description="'pending_human_approval': nothing is made, a person is "
        "asked; 'declined': the person said no; 'success': the page took the "
        "request over."
    )
    message: str = Field(description="What happened, to pass on to the user.")
    request: AcceptedSlideRequest | None = Field(
        default=None, description="What was approved; only with 'success'."
    )


class SlideTiming(BaseModel):
    """The estimate for one slide's speaker script."""

    number: int
    estimated_seconds: float = Field(
        description="How long the script takes to read aloud; an estimate."
    )
    verdict: Literal["ok", "too_short", "too_long"]


class SlideTimingOutput(ToolOutput):
    """Result of ``check_slide_script_timing`` (``slide_timing_report``)."""

    status: Literal["success"] = Field(
        description="The check ran; whether the plan keeps the rule is 'passed'."
    )
    passed: bool = Field(
        description="True when the count is as asked for, every script takes "
        "about a minute, the slides are numbered in order and both the "
        "opening and the closing have something to say."
    )
    slide_count: int = Field(description="Slides in the plan.")
    expected_slide_count: int = Field(description="Slides that were asked for.")
    assumed_rate: str = Field(description="The speaking rate the estimate uses.")
    slides: list[SlideTiming] = Field(description="One estimate per slide.")
    recovery_instruction: str | None = Field(
        default=None, description="What to change; only when 'passed' is false."
    )


class SlidePlanExportOutput(ToolOutput):
    """Result of ``export_slide_plan_markdown``."""

    status: Literal["success"]
    file: str = Field(description="Path of the Markdown file that was written.")
    slide_count: int = Field(description="Slides in the plan.")


class DeckRecord(BaseModel):
    """A Google Slides deck that was created, as the session keeps it.

    Read leniently: the deck exists in Drive by the time this is checked, so
    a count that is missing must not turn a deck that was made into an error.
    """

    model_config = ConfigDict(extra="allow")

    presentation_id: str = Field(description="Google's id of the presentation.")
    url: str = Field(description="The address of the deck.")
    title: str | None = None
    slide_count: int | None = Field(
        default=None, description="Slides, with the title and thank-you slides."
    )
    slides_with_notes: int | None = None
    images_inserted: int | None = None
    images_failed: int | None = Field(
        default=None, description="Images Google could not fetch; they were skipped."
    )
    tables_inserted: int | None = None
    language: LanguageCode
    created: str = Field(description="When it was created, ISO 8601, UTC.")
    previous: list[str] = Field(
        description="Addresses of the decks this one replaced, most recent first."
    )


class DeckCreatedOutput(ToolOutput):
    """Result of ``create_google_slides_deck_with_speaker_notes``."""

    status: ApprovalStatus = Field(
        description="'pending_human_approval': nothing is created, a person "
        "is asked; 'declined': the person said no; 'success': the deck exists."
    )
    message: str = Field(description="What happened, to pass on to the user.")
    deck: DeckRecord | None = Field(
        default=None, description="The deck; only with 'success'."
    )


class ManifestOutput(ToolOutput):
    """Result of ``compile_artifact_manifest_markdown``."""

    status: Literal["success"]
    file: str = Field(description="Path of the manifest file that was written.")
    manifest: str = Field(description="The Markdown text of the manifest.")


# The output model of every tool of the agent tree, by the tool's name (OPS-5).
# ``tests/unit/test_tool_contract.py`` reads the tools from ``app/agent.py``
# and fails when one of them is not named here.
TOOL_OUTPUT_MODELS: dict[str, type[ToolOutput]] = {
    "fetch_published_article_markdown": FetchArticleOutput,
    "load_markdown_draft_file": SeedSavedOutput,
    "save_seed_article_revision": SeedSavedOutput,
    "finalize_seed_article": SeedFinalizedOutput,
    "read_article_post": PostReadOutput,
    "set_article_post_title": PostEditOutput,
    "replace_passage_in_article_post": PostEditOutput,
    "finalize_localized_post": PostFinalizedOutput,
    "export_naver_blog_paste_ready_html": NaverExportOutput,
    "publish_qiita_article_draft": PublishOutput,
    "publish_devto_article_draft": PublishOutput,
    "record_manually_posted_url": PostedUrlOutput,
    "verify_published_article_urls": UrlVerificationOutput,
    "set_presentation_brief": PresentationBriefOutput,
    "request_approval_to_create_slide_decks": SlideRequestOutput,
    "check_slide_script_timing": SlideTimingOutput,
    "export_slide_plan_markdown": SlidePlanExportOutput,
    "create_google_slides_deck_with_speaker_notes": DeckCreatedOutput,
    "compile_artifact_manifest_markdown": ManifestOutput,
}
