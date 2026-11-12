"""Stage 4 tools: presentation brief, timing check, slide export, Google Slides deck, manifest."""

from __future__ import annotations

import re
from typing import Any

from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from app import google_slides, state
from app.deck_sync import plan_text
from app.markdown_checks import (
    SPEECH_UNITS_PER_MINUTE,
    estimate_speaking_seconds,
    extract_images,
    extract_mermaid_diagrams,
    extract_tables,
)
from app.persistence import get_article_store
from app.schemas import (
    LanguageCode,
    PresentationBriefInput,
    SlideDeckPlan,
    checked_language,
    tool_error,
    validation_error,
)
from app.settings import (
    LANGUAGE_NAMES,
    LANGUAGES,
    PLATFORM_FOR_LANGUAGE,
    render_mermaid_enabled,
    workspace_dir,
)
from app.tools.articles import write_workspace_file

MIN_SLIDE_SECONDS = 45
MAX_SLIDE_SECONDS = 75


def set_presentation_brief(
    language: LanguageCode,
    audience: str = "software engineers",
    minutes: int = 15,
    tool_context: ToolContext = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Record who the talk is for, in which language, and how long it runs.

    Call this before transferring to slide_planning_pipeline. The deck is planned at
    one slide per minute, so 'minutes' is also the number of slides.

    Args:
        language: Language of the slides and speaker script: 'ko', 'en' or 'ja'.
        audience: Who will watch, for example 'software engineers'. Defaults to
            the previous brief's audience or 'software engineers'.
        minutes: Length of the talk in minutes, 1 to 60. Defaults to the
            existing plan's slide count or 15.

    Returns:
        On success: status, the stored brief, and next_step. On failure: status
        'error' with error_code, message and recovery_instruction.
    """
    prev_brief = tool_context.state.get(state.PRESENTATION_BRIEF) or {}
    existing_plan = (tool_context.state.get(state.SLIDE_PLANS) or {}).get(
        language
    ) or {}
    resolved_audience = (
        audience.strip()
        if audience and audience.strip()
        else str(prev_brief.get("audience") or "software engineers")
    )
    resolved_minutes = (
        minutes
        if minutes and minutes > 0
        else int(
            len(existing_plan.get("slides") or []) or prev_brief.get("minutes") or 15
        )
    )
    try:
        brief = PresentationBriefInput(
            language=language, audience=resolved_audience, minutes=resolved_minutes
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
    # A new planning run starts clean: what an earlier run, perhaps for another
    # language, left in the working area must not reach this one.
    tool_context.state[state.SLIDE_DECK_PLAN] = None
    tool_context.state[state.SLIDE_DRAFT_TIMING] = None
    return {
        "status": "success",
        "brief": brief.model_dump(),
        "next_step": "Transfer to slide_planning_pipeline to draft the deck.",
    }


def plan_of_record(session_state: Any, language: str) -> SlideDeckPlan | None:
    """The slide plan of ``language``, or None when there is none that reads as one."""
    raw = (session_state.get(state.SLIDE_PLANS) or {}).get(language)
    if not raw:
        return None
    try:
        return SlideDeckPlan.model_validate(raw)
    except ValidationError:
        return None


def _load_plan(
    tool_context: ToolContext, language: str
) -> SlideDeckPlan | dict[str, Any]:
    checked = checked_language(language)
    if isinstance(checked, dict):
        return checked
    language = checked
    plan = plan_of_record(tool_context.state, language)
    if plan is None:
        have = sorted(tool_context.state.get(state.SLIDE_PLANS) or {})
        return tool_error(
            "NO_SLIDE_PLAN",
            f"There is no {LANGUAGE_NAMES[language]} slide plan yet"
            + (f" (plans exist for: {', '.join(have)})." if have else "."),
            "Call set_presentation_brief for this language, then transfer to "
            "slide_planning_pipeline.",
        )
    return plan


def numbering_problem(plan: SlideDeckPlan) -> str | None:
    """Slides are numbered 1..N, each once; anything else cannot become a deck."""
    numbers = [slide.number for slide in plan.slides]
    if numbers != list(range(1, len(numbers) + 1)):
        return (
            f"number the slides 1 to {len(numbers)} in order, each once (now {numbers})"
        )
    return None


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

    # The title slide and the thank-you slide each need something to say.
    unspoken = [
        name
        for name in ("opening_script", "closing_script")
        if not getattr(plan, name).strip()
    ]
    misnumbered = numbering_problem(plan)
    passed = (
        not too_short
        and not too_long
        and not unspoken
        and not misnumbered
        and len(plan.slides) == expected_slides
    )
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
        if unspoken:
            fixes.append(f"write {' and '.join(unspoken)}")
        if misnumbered:
            fixes.append(misnumbered)
        report["recovery_instruction"] = (
            "Ask slide_planning_pipeline to "
            + "; ".join(fixes)
            + f". Target about {per_minute} {unit} per slide."
        )
    return report


def _expected_slides(session_state: Any, plan: SlideDeckPlan) -> int:
    """How many slides were asked for when this language was planned."""
    timing = (session_state.get(state.SLIDE_TIMING) or {}).get(plan.language) or {}
    return int(timing.get("expected_slide_count") or len(plan.slides))


def check_slide_script_timing(
    language: LanguageCode, tool_context: ToolContext
) -> dict[str, Any]:
    """Check one language's slide plan against the one-slide-per-minute rule.

    Estimates how long each speaker script takes to read aloud from its length
    and the language's speaking rate, and compares the slide count with the
    requested talk length. Run this after the user edits or questions a plan.

    Args:
        language: Language of the plan to check: 'ko', 'en' or 'ja'.

    Returns:
        status, passed, slide_count, expected_slide_count, and per-slide
        estimated_seconds with a verdict. When passed is false,
        recovery_instruction lists the slides to lengthen or shorten.
        On failure: status 'error' with error_code, message and
        recovery_instruction.
    """
    plan = _load_plan(tool_context, language)
    if isinstance(plan, dict):
        return plan
    report = slide_timing_report(plan, _expected_slides(tool_context.state, plan))
    timing = dict(tool_context.state.get(state.SLIDE_TIMING) or {})
    timing[language] = report
    tool_context.state[state.SLIDE_TIMING] = timing
    return report


def render_slide_plan_markdown(plan: SlideDeckPlan) -> str:
    """Render a slide plan, with speaker scripts, as Markdown."""
    lines = [
        f"# {plan.deck_title}",
        "",
        f"- Audience: {plan.audience}",
        f"- Slides: {len(plan.slides)} (one per minute)",
        "",
        _OPENING_HEADING,
        "",
        plan.opening_script,
        "",
    ]
    for slide in plan.slides:
        seconds = estimate_speaking_seconds(slide.speaker_script, plan.language)
        lines += [f"## {slide.number}. {slide.title}", ""]
        lines += [f"- {bullet}" for bullet in slide.bullets]
        lines += [
            "",
            f"Visual: {slide.visual_note}",
            *([f"Image: {slide.image_url}"] if slide.image_url else []),
            "",
            f"Speaker script (about {seconds:.0f}s):",
            "",
            slide.speaker_script,
            "",
        ]
    lines += [_CLOSING_HEADING, "", plan.closing_script, ""]
    return "\n".join(lines)


_SLIDE_HEADING = re.compile(r"^## (\d+)\. (.+)$")
# What is said on the two slides that have no number.
_OPENING_HEADING = "Opening (title slide):"
_CLOSING_HEADING = "Closing (thank-you slide):"
_OPENING_LINE = re.compile(r"^Opening \(title slide\):\s*$", re.I)
_CLOSING_LINE = re.compile(r"^Closing \(thank-you slide\):\s*$", re.I)
_SCRIPT_HEADING = re.compile(r"^Speaker script\b.*:\s*$")


def parse_slide_plan_markdown(text: str, language: str) -> SlideDeckPlan:
    """Read a slide plan back from the Markdown ``render_slide_plan_markdown`` wrote.

    A person may have edited the file, so the layout is read leniently, but
    the parts a deck cannot be built without must be there. A file that does
    not have them raises ValueError saying what is missing and where.
    """
    lines = text.splitlines()
    deck_title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), "")
    if not deck_title:
        raise ValueError("The first heading ('# Deck title') is missing.")
    audience = next(
        (ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("- Audience:")),
        "",
    )
    starts = [i for i, ln in enumerate(lines) if _SLIDE_HEADING.match(ln)]
    if not starts:
        raise ValueError("No slide heading ('## 1. Title') was found.")
    opening_at = next(
        (i for i, ln in enumerate(lines[: starts[0]]) if _OPENING_LINE.match(ln)), None
    )
    opening = (
        "\n".join(lines[opening_at + 1 : starts[0]]).strip()
        if opening_at is not None
        else ""
    )
    # The closing words follow the last slide; they are not part of its script.
    closing_at = next(
        (
            i
            for i in range(len(lines) - 1, starts[-1], -1)
            if _CLOSING_LINE.match(lines[i])
        ),
        None,
    )
    closing = ""
    if closing_at is not None:
        closing = "\n".join(lines[closing_at + 1 :]).strip()
        lines = lines[:closing_at]
    slides = []
    for start, end in zip(starts, [*starts[1:], len(lines)], strict=True):
        number, title = _SLIDE_HEADING.match(lines[start]).groups()
        body = lines[start + 1 : end]
        script_at = next(
            (i for i, ln in enumerate(body) if _SCRIPT_HEADING.match(ln)), None
        )
        if script_at is None:
            raise ValueError(f"Slide {number} has no 'Speaker script (...):' line.")
        head = body[:script_at]
        slides.append(
            {
                "number": int(number),
                "title": title.strip(),
                "bullets": [ln[2:].strip() for ln in head if ln.startswith("- ")],
                "visual_note": next(
                    (ln[7:].strip() for ln in head if ln.startswith("Visual:")),
                    "text only",
                ),
                "image_url": next(
                    (ln[6:].strip() for ln in head if ln.startswith("Image:")), ""
                ),
                "speaker_script": "\n".join(body[script_at + 1 :]).strip(),
            }
        )
    numbers = [slide["number"] for slide in slides]
    if len(set(numbers)) != len(numbers):
        raise ValueError("Two slides have the same number.")
    try:
        return SlideDeckPlan.model_validate(
            {
                "language": language,
                "deck_title": deck_title,
                "audience": audience,
                "opening_script": opening,
                "slides": slides,
                "closing_script": closing,
            }
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ValueError(
            f"The slide plan is not valid ({where}: {first['msg']})."
        ) from exc


def export_slide_plan_markdown(
    language: LanguageCode, tool_context: ToolContext
) -> dict[str, Any]:
    """Write one language's slide plan, with its speaker script, to a Markdown file.

    The file lists each slide's title, bullets, visual note and the script to
    read while recording. It is the reviewable form of the deck and the source
    for building the actual slides.

    Args:
        language: Language of the plan to write: 'ko', 'en' or 'ja'.

    Returns:
        On success: status, file and slide_count. On failure: status 'error'
        with error_code, message and recovery_instruction.
    """
    plan = _load_plan(tool_context, language)
    if isinstance(plan, dict):
        return plan
    project_id = tool_context.state.get(state.PROJECT_ID) or "unsaved"
    file = write_workspace_file(
        project_id, slide_plan_file(language), render_slide_plan_markdown(plan)
    )
    return {"status": "success", "file": file, "slide_count": len(plan.slides)}


def slide_plan_file(language: str) -> str:
    return f"slides.{language}.md"


def slide_pdf_file(language: str) -> str:
    return f"slides.{language}.pdf"


def compile_artifact_manifest_markdown(tool_context: ToolContext) -> dict[str, Any]:
    """Write a Markdown list of everything produced for this article.

    Lists each language version with its file and publication URL, the
    companion code repository if any, and the slide plan and Google Slides
    deck of every language that has one. The result is meant to be pasted
    into a README.

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
    plans = tool_context.state.get(state.SLIDE_PLANS) or {}
    decks = tool_context.state.get(state.GOOGLE_SLIDES_DECKS) or {}
    for language in LANGUAGES:
        name = LANGUAGE_NAMES[language]
        if language in plans:
            lines += [
                f"- Slides and speaker script ({name}): {slide_plan_file(language)}"
            ]
        if (decks.get(language) or {}).get("url"):
            lines += [f"- Google Slides deck ({name}): {decks[language]['url']}"]
        pdf_name = slide_pdf_file(language)
        if (decks.get(language) or {}).get("pdf_file") or (
            workspace_dir() / project_id / pdf_name
        ).is_file():
            lines += [f"- Slides PDF ({name}): {pdf_name}"]
    manifest = "\n".join(lines) + "\n"
    file = write_workspace_file(project_id, "manifest.md", manifest)
    return {"status": "success", "file": file, "manifest": manifest}


def article_images(session_state: Any, language: str) -> dict[str, str]:
    """References a plan may use, mapped to image addresses that are in the article."""
    images = {
        url: url
        for code in LANGUAGES
        for _, url in extract_images(state.article_markdown(session_state, code) or "")
    }
    if render_mermaid_enabled():
        # Diagrams come from the article in the deck's language, so their
        # labels match the slides. One too large for an image address is skipped.
        article = state.article_markdown(session_state, language) or ""
        for number, (_, code) in enumerate(extract_mermaid_diagrams(article), start=1):
            if rendered := google_slides.mermaid_image_url(code):
                images[google_slides.mermaid_reference(number)] = rendered
    return images


def article_tables(session_state: Any, language: str) -> dict[str, list[list[str]]]:
    """References a plan may use, mapped to Markdown table rows from the article."""
    article = state.article_markdown(session_state, language) or ""
    return {
        google_slides.table_reference(number): rows
        for number, (_, rows) in enumerate(extract_tables(article), start=1)
    }


async def create_google_slides_deck_with_speaker_notes(
    language: LanguageCode, tool_context: ToolContext
) -> dict[str, Any]:
    """Create a Google Slides deck from one language's slide plan, after human approval.

    Makes a title slide, one slide per planned slide and a thank-you slide,
    places the article image chosen for a slide next to its bullets, and puts
    each slide's speaker script (and the note on what to show) into its
    speaker notes. The deck is a new file in the Drive of the Google account
    that granted access; no existing file is changed. The first call does not
    create anything: it pauses and asks the user to approve. Calling it again
    for the same language creates another new deck from the current plan; the
    earlier file stays in Drive.

    Args:
        language: Language of the plan to build the deck from: 'ko', 'en' or 'ja'.

    Returns:
        status 'pending_human_approval' while waiting, 'declined' if refused,
        or 'success' with deck (presentation_id, url, title, slide_count,
        slides_with_notes, images_inserted, images_failed). On failure: status
        'error' with error_code, message and recovery_instruction.
    """
    plan = _load_plan(tool_context, language)
    if isinstance(plan, dict):
        return plan
    if problem := numbering_problem(plan):
        return tool_error(
            "SLIDE_PLAN_NOT_USABLE",
            f"The {LANGUAGE_NAMES[language]} slide plan cannot become a deck: {problem}.",
            "Fix the slide numbers in the plan, or plan the slides again.",
        )
    decks = dict(tool_context.state.get(state.GOOGLE_SLIDES_DECKS) or {})
    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        # Name the account, so a deck never lands in a Drive nobody expected.
        account = google_slides.granted_account()
        where = f"the Google Drive of {account}" if account else "your Google Drive"
        again = (
            " A deck of this language exists already; it stays in Drive and "
            "this page will link to the new one."
            if language in decks
            else ""
        )
        tool_context.request_confirmation(
            hint=f"Approve to create a Google Slides deck '{plan.deck_title}' "
            f"({LANGUAGE_NAMES[language]}, {len(plan.slides)} slides with "
            f"speaker notes) in {where}.{again}",
            payload={
                "action": "create_deck",
                "language": language,
                "deck_title": plan.deck_title,
                "slides": len(plan.slides),
            },
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
        tables = article_tables(tool_context.state, language)
        deck = await google_slides.create_deck(
            plan,
            article_images(tool_context.state, language),
            *([tables] if tables else []),
        )
    except google_slides.SlidesError as exc:
        return tool_error(exc.code, exc.message, exc.recovery)
    deck["language"] = language
    decks[language] = deck
    tool_context.state[state.GOOGLE_SLIDES_DECKS] = decks
    # What the deck and the plan agree on, for telling later edits apart.
    synced = dict(tool_context.state.get(state.DECK_TEXT_BASELINE) or {})
    synced[language] = plan_text(plan)
    tool_context.state[state.DECK_TEXT_BASELINE] = synced
    return {
        "status": "success",
        "deck": deck,
        "message": "The deck was created. Give the user its URL to review.",
    }


async def export_google_slides_pdf(
    language: LanguageCode, tool_context: ToolContext
) -> dict[str, Any]:
    """Export one language's Google Slides deck as ``slides.<lang>.pdf`` (SLD-12).

    Args:
        language: Language of the deck to export: 'ko', 'en' or 'ja'.

    Returns:
        On success: status, language, file, pdf_file and bytes. On failure:
        status 'error' with error_code, message and recovery_instruction.
    """
    checked = checked_language(language)
    if isinstance(checked, dict):
        return checked
    language = checked
    project_id = tool_context.state.get(state.PROJECT_ID)
    if not project_id:
        return tool_error(
            "NO_PROJECT",
            "No article project in this session.",
            "Start with a seed article first.",
        )
    decks = dict(tool_context.state.get(state.GOOGLE_SLIDES_DECKS) or {})
    deck = dict(decks.get(language) or {})
    presentation_id = deck.get("presentation_id")
    if not presentation_id:
        return tool_error(
            "NO_SLIDES_DECK",
            f"There is no {LANGUAGE_NAMES[language]} Google Slides deck to export.",
            "Create the Google Slides deck for this language first.",
        )
    try:
        pdf_bytes = await google_slides.export_deck_pdf(presentation_id)
    except google_slides.SlidesError as exc:
        return tool_error(exc.code, exc.message, exc.recovery)
    pdf_name = slide_pdf_file(language)
    folder = workspace_dir() / project_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / pdf_name
    path.write_bytes(pdf_bytes)
    deck["pdf_file"] = pdf_name
    decks[language] = deck
    tool_context.state[state.GOOGLE_SLIDES_DECKS] = decks
    return {
        "status": "success",
        "language": language,
        "file": str(path),
        "pdf_file": pdf_name,
        "bytes": len(pdf_bytes),
    }
