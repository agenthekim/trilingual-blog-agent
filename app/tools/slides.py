"""Stage 4 tools: presentation brief, timing check, slide export, Google Slides deck, manifest."""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from app import google_slides, state
from app.markdown_checks import SPEECH_UNITS_PER_MINUTE, estimate_speaking_seconds
from app.persistence import get_article_store
from app.schemas import (
    PresentationBriefInput,
    SlideDeckPlan,
    tool_error,
    validation_error,
)
from app.settings import LANGUAGE_NAMES, LANGUAGES, PLATFORM_FOR_LANGUAGE
from app.tools.articles import write_workspace_file

MIN_SLIDE_SECONDS = 45
MAX_SLIDE_SECONDS = 75


def set_presentation_brief(
    language: str, audience: str, minutes: int, tool_context: ToolContext
) -> dict[str, Any]:
    """Record who the talk is for, in which language, and how long it runs.

    Call this before transferring to slide_planning_pipeline. The deck is planned at
    one slide per minute, so 'minutes' is also the number of slides.

    Args:
        language: Language of the slides and speaker script: 'ko', 'en' or 'ja'.
        audience: Who will watch, for example 'backend engineers new to agents'.
        minutes: Length of the talk in minutes, 1 to 60.

    Returns:
        On success: status, the stored brief, and next_step. On failure: status
        'error' with error_code, message and recovery_instruction.
    """
    try:
        brief = PresentationBriefInput(
            language=language, audience=audience, minutes=minutes
        )  # type: ignore[arg-type]
    except ValidationError as exc:
        return validation_error(
            exc,
            "Ask the user for the language (ko/en/ja), audience and length in minutes.",
        )
    if not state.article_markdown(tool_context.state, brief.language):
        return tool_error(
            "ARTICLE_VERSION_MISSING",
            f"There is no {LANGUAGE_NAMES[brief.language]} version of the article to "
            "build slides from.",
            "Localize the article first, or choose a language that exists.",
        )
    tool_context.state[state.PRESENTATION_BRIEF] = brief.model_dump()
    return {
        "status": "success",
        "brief": brief.model_dump(),
        "next_step": "Transfer to slide_planning_pipeline to draft the deck.",
    }


def _load_plan(tool_context: ToolContext) -> SlideDeckPlan | dict[str, Any]:
    raw = tool_context.state.get(state.SLIDE_DECK_PLAN)
    if not raw:
        return tool_error(
            "NO_SLIDE_PLAN",
            "No slide plan has been drafted yet.",
            "Call set_presentation_brief, then transfer to slide_planning_pipeline.",
        )
    try:
        return SlideDeckPlan.model_validate(raw)
    except ValidationError as exc:
        return validation_error(
            exc, "Transfer to slide_planning_pipeline to regenerate the plan."
        )


def slide_timing_report(plan: SlideDeckPlan, expected_slides: int) -> dict[str, Any]:
    """Evaluate a slide plan against the one-slide-per-minute rule (pure function)."""
    unit, per_minute = SPEECH_UNITS_PER_MINUTE[plan.language]
    slides, too_short, too_long = [], [], []
    for slide in plan.slides:
        seconds = estimate_speaking_seconds(slide.speaker_script, plan.language)
        verdict = "ok"
        if seconds < MIN_SLIDE_SECONDS:
            verdict = "too_short"
            too_short.append(slide.number)
        elif seconds > MAX_SLIDE_SECONDS:
            verdict = "too_long"
            too_long.append(slide.number)
        slides.append(
            {"number": slide.number, "estimated_seconds": seconds, "verdict": verdict}
        )

    passed = not too_short and not too_long and len(plan.slides) == expected_slides
    report: dict[str, Any] = {
        "status": "success",
        "passed": passed,
        "slide_count": len(plan.slides),
        "expected_slide_count": expected_slides,
        "assumed_rate": f"{per_minute} {unit} per minute ({plan.language}); "
        "an estimate, not a measurement",
        "slides": slides,
    }
    if not passed:
        fixes = []
        if len(plan.slides) != expected_slides:
            fixes.append(
                f"use exactly {expected_slides} slides (now {len(plan.slides)})"
            )
        if too_short:
            fixes.append(f"lengthen the scripts of slides {too_short}")
        if too_long:
            fixes.append(f"shorten the scripts of slides {too_long}")
        report["recovery_instruction"] = (
            "Ask slide_planning_pipeline to "
            + "; ".join(fixes)
            + f". Target about {per_minute} {unit} per slide."
        )
    return report


def check_slide_script_timing(tool_context: ToolContext) -> dict[str, Any]:
    """Check the slide plan against the one-slide-per-minute rule.

    Estimates how long each speaker script takes to read aloud from its length
    and the language's speaking rate, and compares the slide count with the
    requested talk length. Run this after the user edits or questions a plan.

    Returns:
        status, passed, slide_count, expected_slide_count, and per-slide
        estimated_seconds with a verdict. When passed is false,
        recovery_instruction lists the slides to lengthen or shorten.
    """
    plan = _load_plan(tool_context)
    if isinstance(plan, dict):
        return plan
    brief = tool_context.state.get(state.PRESENTATION_BRIEF) or {}
    return slide_timing_report(plan, int(brief.get("minutes") or len(plan.slides)))


def render_slide_plan_markdown(plan: SlideDeckPlan) -> str:
    """Render a slide plan, with speaker scripts, as Markdown."""
    lines = [
        f"# {plan.deck_title}",
        "",
        f"- Audience: {plan.audience}",
        f"- Slides: {len(plan.slides)} (one per minute)",
        "",
    ]
    for slide in plan.slides:
        seconds = estimate_speaking_seconds(slide.speaker_script, plan.language)
        lines += [f"## {slide.number}. {slide.title}", ""]
        lines += [f"- {bullet}" for bullet in slide.bullets]
        lines += [
            "",
            f"Visual: {slide.visual_note}",
            "",
            f"Speaker script (about {seconds:.0f}s):",
            "",
            slide.speaker_script,
            "",
        ]
    return "\n".join(lines)


def export_slide_plan_markdown(tool_context: ToolContext) -> dict[str, Any]:
    """Write the slide plan, with its speaker script, to a Markdown file.

    The file lists each slide's title, bullets, visual note and the script to
    read while recording. It is the reviewable form of the deck and the source
    for building the actual slides.

    Returns:
        On success: status, file and slide_count. On failure: status 'error'
        with error_code, message and recovery_instruction.
    """
    plan = _load_plan(tool_context)
    if isinstance(plan, dict):
        return plan
    project_id = tool_context.state.get(state.PROJECT_ID) or "unsaved"
    file = write_workspace_file(
        project_id, f"slides.{plan.language}.md", render_slide_plan_markdown(plan)
    )
    get_article_store().save_project(project_id, {"slide_plan_file": file})
    return {"status": "success", "file": file, "slide_count": len(plan.slides)}


def compile_artifact_manifest_markdown(tool_context: ToolContext) -> dict[str, Any]:
    """Write a Markdown list of everything produced for this article.

    Lists each language version with its file and publication URL, the
    companion code repository if any, and the slide files. The result is meant
    to be pasted into a README.

    Returns:
        On success: status, file and the manifest text. On failure: status
        'error' with error_code, message and recovery_instruction.
    """
    project_id = tool_context.state.get(state.PROJECT_ID)
    if not project_id:
        return tool_error(
            "NO_PROJECT",
            "No article project in this session.",
            "Start with a seed article before compiling a manifest.",
        )
    store = get_article_store()
    project = store.get_project(project_id) or {}
    publications = {p["platform"]: p for p in store.list_publications(project_id)}
    title = (
        project.get("title") or tool_context.state.get(state.SEED_TITLE) or project_id
    )

    rows = [
        "| Language | Title | Platform | Status | URL | File |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for language in LANGUAGES:
        platform = PLATFORM_FOR_LANGUAGE[language]
        publication = publications.get(platform, {})
        has_text = bool(store.latest_version(project_id, language))
        language_title = (
            state.article_title(tool_context.state, language) if has_text else ""
        )
        rows.append(
            f"| {LANGUAGE_NAMES[language]} | {language_title.replace('|', '/')} | "
            f"{platform} | "
            f"{publication.get('state', 'not posted')} | {publication.get('url') or ''} | "
            f"{f'post.{language}.md' if has_text else ''} |"
        )
    lines = [f"## {title}", "", *rows, ""]
    if project.get("code_repo_url"):
        lines += [f"- Code: {project['code_repo_url']} ({project.get('code_mode')})"]
    if project.get("slide_plan_file"):
        lines += [
            "- Slides and speaker script: "
            + project["slide_plan_file"].rsplit("/", 1)[-1]
        ]
    deck = tool_context.state.get(state.GOOGLE_SLIDES_DECK) or {}
    if deck.get("url"):
        lines += [f"- Google Slides deck: {deck['url']}"]
    manifest = "\n".join(lines) + "\n"
    file = write_workspace_file(project_id, "manifest.md", manifest)
    return {"status": "success", "file": file, "manifest": manifest}


async def create_google_slides_deck_with_speaker_notes(
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Create a Google Slides deck from the slide plan, after human approval.

    Makes one title-and-bullets slide per planned slide and puts each slide's
    speaker script (and the note on what to show) into its speaker notes. The
    deck is a new file in the Drive of the Google account that granted access;
    no existing file is changed. The first call does not create anything: it
    pauses and asks the user to approve. Calling it again later creates another
    new deck from the current plan.

    Returns:
        status 'pending_human_approval' while waiting, 'declined' if refused,
        or 'success' with deck (presentation_id, url, title, slide_count,
        slides_with_notes). On failure: status 'error' with error_code, message
        and recovery_instruction.
    """
    plan = _load_plan(tool_context)
    if isinstance(plan, dict):
        return plan
    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        tool_context.request_confirmation(
            hint=f"Approve to create a Google Slides deck '{plan.deck_title}' "
            f"({len(plan.slides)} slides with speaker notes) in your Google Drive.",
            payload={"deck_title": plan.deck_title, "slides": len(plan.slides)},
        )
        return {
            "status": "pending_human_approval",
            "message": "Waiting for the user's approval before creating the deck.",
        }
    if not confirmation.confirmed:
        return {
            "status": "declined",
            "message": "The user declined. No Google Slides deck was created.",
        }
    try:
        deck = await google_slides.create_deck(plan)
    except google_slides.SlidesError as exc:
        return tool_error(exc.code, exc.message, exc.recovery)
    tool_context.state[state.GOOGLE_SLIDES_DECK] = deck
    project_id = tool_context.state.get(state.PROJECT_ID)
    if project_id:
        get_article_store().save_project(project_id, {"google_slides_url": deck["url"]})
    return {
        "status": "success",
        "deck": deck,
        "message": "The deck was created. Give the user its URL to review.",
    }
