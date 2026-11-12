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
    SlideRequestInput,
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


DEFAULT_TALK_MINUTES = 15
DEFAULT_AUDIENCE = "software engineers"


def slide_length(session_state: Any, language: str, minutes: int) -> int:
    """How long the talk of ``language`` is when a request says ``minutes``.

    A length that was given is used. A request that gives none (0) keeps the
    length of that language's plan; without a plan it takes the length the
    run was started with, and failing that 15 minutes. Code decides this, not
    a model.
    """
    if minutes and minutes > 0:
        return minutes
    plan = (session_state.get(state.SLIDE_PLANS) or {}).get(language) or {}
    options = session_state.get(state.RUN_OPTIONS) or {}
    brief = session_state.get(state.PRESENTATION_BRIEF) or {}
    return int(
        len(plan.get("slides") or [])
        or options.get("minutes")
        or brief.get("minutes")
        or DEFAULT_TALK_MINUTES
    )


def slide_audience(session_state: Any, audience: str) -> str:
    """Who the talk is for when a request says ``audience``; empty keeps it."""
    if audience and audience.strip():
        return audience.strip()
    options = session_state.get(state.RUN_OPTIONS) or {}
    brief = session_state.get(state.PRESENTATION_BRIEF) or {}
    return str(options.get("audience") or brief.get("audience") or DEFAULT_AUDIENCE)


def set_presentation_brief(
    language: LanguageCode,
    audience: str,
    minutes: int,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Record who the talk is for, in which language, and how long it runs.

    Call this before transferring to slide_planning_pipeline. The deck is planned at
    one slide per minute, so 'minutes' is also the number of slides. On the
    web page this tool is not used: slides are asked for there with
    request_approval_to_create_slide_decks, and this tool says so.

    Args:
        language: Language of the slides and speaker script: 'ko', 'en' or 'ja'.
        audience: Who will watch, for example 'software engineers'. An empty
            string '' keeps the audience of the previous brief.
        minutes: Length of the talk in minutes, 1 to 60. 0 keeps the length
            this language's plan has now (15 when there is no plan).

    Returns:
        On success: status, the stored brief, and next_step. On failure: status
        'error' with error_code, message and recovery_instruction.
    """
    if tool_context.state.get(state.SURFACE) == state.SURFACE_WEB:
        return tool_error(
            "USE_THE_SLIDE_REQUEST_TOOL",
            "On the web page slides are not planned in this conversation.",
            "Call request_approval_to_create_slide_decks with every language "
            "the user asked for, the length and the audience. It asks the user "
            "to approve; the page then makes the slides and shows the progress.",
        )
    resolved_audience = slide_audience(tool_context.state, audience)
    resolved_minutes = slide_length(tool_context.state, language, minutes)
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


def slide_request_summary(
    session_state: Any, languages: list[str], minutes: int, audience: str
) -> dict[str, Any]:
    """What a slide request will make, for the person who is asked to approve it.

    The same summary is behind the dialog of the page's button and behind the
    confirmation the agent's request tool pauses for (INV-6): the languages,
    the length of each, the audience, the account whose Drive receives the
    decks, and what an approval replaces.
    """
    plans = session_state.get(state.SLIDE_PLANS) or {}
    decks = session_state.get(state.GOOGLE_SLIDES_DECKS) or {}
    return {
        "languages": list(languages),
        "minutes": {
            language: slide_length(session_state, language, minutes)
            for language in languages
        },
        "audience": slide_audience(session_state, audience),
        "account": google_slides.granted_account(),
        "replaces_plans": [language for language in languages if language in plans],
        "replaces_decks": [language for language in languages if language in decks],
    }


def slide_request_hint(summary: dict[str, Any]) -> str:
    """One paragraph a person reads before approving a slide request."""
    lengths = ", ".join(
        f"{LANGUAGE_NAMES[language]} ({minutes} min)"
        for language, minutes in summary["minutes"].items()
    )
    where = (
        f"the Google Drive of {summary['account']}"
        if summary["account"]
        else "your Google Drive"
    )
    hint = (
        f"Approve to make slides and a Google Slides deck for: {lengths}. "
        f"Audience: {summary['audience']}. One slide per minute, with the "
        f"script in the speaker notes. Each deck is a new file in {where}."
    )
    if summary["replaces_plans"]:
        names = ", ".join(LANGUAGE_NAMES[code] for code in summary["replaces_plans"])
        hint += (
            f" {names}: there is a plan already; what was changed in its "
            "speaker notes is replaced by the new plan."
        )
    if summary["replaces_decks"]:
        names = ", ".join(LANGUAGE_NAMES[code] for code in summary["replaces_decks"])
        hint += (
            f" {names}: there is a deck already; a new file is made and the "
            "earlier deck stays in Drive."
        )
    return hint


def request_approval_to_create_slide_decks(
    languages: list[LanguageCode],
    minutes: int,
    audience: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Ask the user to approve making slides and Google Slides decks, on the web page.

    Use this on the web page when the user asks, in words, for slides or a
    deck in one or several languages, or to make them again. Pass every
    language asked for in one call: slides for several languages are one
    request. The first call makes nothing: it pauses and asks the user to
    approve, naming the languages, the length, the audience and the Google
    account. When the user approves, the request is handed to the page, which
    plans the slides and creates the decks and shows the progress; you do not
    plan or create them yourself, and you cannot approve for the user.

    Args:
        languages: The languages to make slides for, each 'ko', 'en' or 'ja'.
        minutes: Length of the talk in minutes, 1 to 60, or 0 when the user
            did not say: each language then keeps the length it has.
        audience: Who will watch, or an empty string '' when the user did not
            say: the audience of the run is kept.

    Returns:
        status 'pending_human_approval' while waiting, 'declined' if refused,
        or 'success' with request (languages, minutes per language, audience)
        once the user approved and the page took it over. On failure: status
        'error' with error_code, message and recovery_instruction.
    """
    try:
        request = SlideRequestInput(
            languages=languages, minutes=minutes, audience=audience
        )
    except ValidationError as exc:
        return validation_error(
            exc,
            "languages must be a list of 'ko', 'en' or 'ja'; minutes 0 to 60 "
            "(0 when the user gave no length); audience a short text or ''.",
        )
    if tool_context.state.get(state.SURFACE) != state.SURFACE_WEB:
        return tool_error(
            "ONLY_ON_THE_WEB_PAGE",
            "This tool hands a slide request to the web page; this "
            "conversation is not on the web page.",
            "Plan one language at a time instead: call set_presentation_brief, "
            "transfer to slide_planning_pipeline, and call "
            "create_google_slides_deck_with_speaker_notes when a deck is wanted.",
        )
    missing = [
        LANGUAGE_NAMES[language]
        for language in request.languages
        if not state.article_markdown(tool_context.state, language)
    ]
    if missing:
        return tool_error(
            "ARTICLE_VERSION_MISSING",
            f"There is no {' or '.join(missing)} version of the article to "
            "build slides from.",
            "Localize the article first, or ask for languages that exist.",
        )
    summary = slide_request_summary(
        tool_context.state, request.languages, request.minutes, request.audience
    )
    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        tool_context.request_confirmation(
            hint=slide_request_hint(summary),
            payload={"action": "slides", **summary},
        )
        return {
            "status": "pending_human_approval",
            "message": "Waiting for the user's approval before any slides are made.",
        }
    if not confirmation.confirmed:
        return {
            "status": "declined",
            "message": "The user declined. No slides and no deck were made.",
        }
    accepted = {
        "languages": summary["languages"],
        "minutes": summary["minutes"],
        "audience": summary["audience"],
    }
    # The run's slides lane takes this when the turn has ended (RUN-1); a
    # request that was waiting and not yet taken is replaced, not added to.
    tool_context.state[state.SLIDE_REQUEST] = accepted
    return {
        "status": "success",
        "request": accepted,
        "message": "Approved. The page is making the slides now and shows the "
        "progress; tell the user that, and do not plan or create them here.",
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
