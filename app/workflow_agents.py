"""Deterministic workflow agents: gates, checks and reports that need no model call.

ADK's ``ParallelAgent``, ``SequentialAgent`` and ``LoopAgent`` have a fixed list
of sub-agents. These small ``BaseAgent`` subclasses add the decisions the
pipeline needs around them: which languages to run, whether the review passed,
and what to report. Keeping these decisions in code makes them testable and
keeps the model out of control flow.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, Literal

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from pydantic import ValidationError

from app import state
from app.markdown_checks import check_fidelity
from app.observability import log_event
from app.persistence import get_article_store
from app.schemas import SlideDeckPlan
from app.settings import LANGUAGE_NAMES, PLATFORM_FOR_LANGUAGE
from app.tools.articles import write_workspace_file
from app.tools.slides import (
    plan_of_record,
    render_slide_plan_markdown,
    slide_plan_file,
    slide_timing_report,
)


def _text_event(
    agent: BaseAgent, ctx: InvocationContext, text: str, **actions: Any
) -> Event:
    return Event(
        author=agent.name,
        invocation_id=ctx.invocation_id,
        branch=ctx.branch,
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
        actions=EventActions(**actions),
    )


def _state_event(agent: BaseAgent, ctx: InvocationContext, **actions: Any) -> Event:
    return Event(
        author=agent.name,
        invocation_id=ctx.invocation_id,
        branch=ctx.branch,
        actions=EventActions(**actions),
    )


def language_needs_refinement(session_state: Any, language: str) -> bool:
    """True when the structure check or the critic found problems for ``language``."""
    report = (session_state.get(state.FIDELITY_REPORT) or {}).get(language) or {}
    if report and not report.get("passed", True):
        return True
    verdict = session_state.get(state.REVIEW_VERDICT) or {}
    return any(
        review.get("language") == language and review.get("grade") == "fail"
        for review in verdict.get("reviews", [])
    )


class LanguageGate(BaseAgent):
    """Runs its one sub-agent only when its language has work to do.

    ``ParallelAgent`` always starts every sub-agent, but the set of languages
    to write depends on the seed's language. Three gates (ko, en, ja) are
    placed in the parallel fan-out; the gate for the source language stays
    silent. In ``refine`` mode a gate runs only for a language that failed
    review.
    """

    language: str
    mode: Literal["localize", "refine"] = "localize"

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        session_state = ctx.session.state
        targets = session_state.get(state.TARGET_LANGUAGES) or []
        should_run = self.language in targets
        if should_run and self.mode == "refine":
            should_run = language_needs_refinement(session_state, self.language)
        if not should_run:
            return
        async for event in self.sub_agents[0].run_async(ctx):
            yield event


def _seed_is_ready(session_state: Any) -> bool:
    return bool(
        session_state.get(state.SEED_FINALIZED)
        and session_state.get(state.SEED_MARKDOWN)
    )


class SeedGate(BaseAgent):
    """Runs its one sub-agent only when a finalized seed exists.

    Without a finalized seed it explains why nothing ran, so the pipeline can
    never translate a half-finished draft.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        if not _seed_is_ready(ctx.session.state):
            yield _text_event(
                self,
                ctx,
                "Localization was not started: there is no finalized seed article. "
                "Finalize the seed first.",
            )
            return
        async for event in self.sub_agents[0].run_async(ctx):
            yield event


class FidelityCheckAgent(BaseAgent):
    """Runs the deterministic structure check on every target language."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        session_state = ctx.session.state
        seed = session_state.get(state.SEED_MARKDOWN) or ""
        report = {
            language: check_fidelity(
                seed, session_state.get(state.localized_key(language)) or ""
            )
            for language in session_state.get(state.TARGET_LANGUAGES) or []
        }
        log_event(
            "self-evaluation: structure check",
            event_phase="SELF_EVAL",
            passed={language: r["passed"] for language, r in report.items()},
        )
        yield _state_event(self, ctx, state_delta={state.FIDELITY_REPORT: report})


class ReviewGate(BaseAgent):
    """Ends the review loop when both the structure check and the critic pass."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        session_state = ctx.session.state
        targets = session_state.get(state.TARGET_LANGUAGES) or []
        passed = not any(
            language_needs_refinement(session_state, lang) for lang in targets
        )
        log_event(
            "self-evaluation: review gate", event_phase="SELF_EVAL", passed=passed
        )
        yield _state_event(self, ctx, escalate=passed)


class LocalizationReportAgent(BaseAgent):
    """Saves the localized versions and tells the user where things stand."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        session_state = ctx.session.state
        project_id = session_state.get(state.PROJECT_ID)
        seed = session_state.get(state.SEED_MARKDOWN) or ""
        store = get_article_store()
        verdict = session_state.get(state.REVIEW_VERDICT) or {}
        critic_issues = {
            review.get("language"): review.get("issues", [])
            for review in verdict.get("reviews", [])
            if review.get("grade") == "fail"
        }

        lines = ["Localization finished.", ""]
        final_report: dict[str, Any] = {}
        source = session_state.get(state.SOURCE_LANGUAGE)
        titles: dict[str, str] = (
            {source: state.article_title(session_state, source)} if source else {}
        )
        for language in session_state.get(state.TARGET_LANGUAGES) or []:
            text = session_state.get(state.localized_key(language)) or ""
            check = check_fidelity(seed, text)
            final_report[language] = check
            if text and project_id:
                store.add_version(project_id, language, "localized", text)
                file = write_workspace_file(project_id, f"post.{language}.md", text)
            else:
                file = "(not written)"
            status = (
                "structure check passed"
                if check["passed"]
                else "structure check FAILED"
            )
            title = state.article_title(session_state, language)
            titles[language] = title
            lines.append(
                f"- {LANGUAGE_NAMES[language]} for {PLATFORM_FOR_LANGUAGE[language]}: "
                f"'{title}', {len(text)} chars, {status}. File: {file}"
            )
            for error in check["errors"]:
                lines.append(f"    - unresolved: {error}")
            for warning in check["warnings"]:
                lines.append(f"    - note: {warning}")
            for issue in critic_issues.get(language, []):
                lines.append(
                    f"    - reviewer issue from the last review round (a fix was "
                    f"attempted after it and not re-reviewed): {issue}"
                )
        if project_id:
            store.save_project(project_id, {"titles": titles})
        lines += [
            "",
            "Please review the files and the titles. Tell me what to change, or "
            "say which platforms to publish to.",
        ]
        yield _text_event(
            self,
            ctx,
            "\n".join(lines),
            state_delta={state.FIDELITY_REPORT: final_report},
        )


def _brief(session_state: Any) -> dict[str, Any]:
    return session_state.get(state.PRESENTATION_BRIEF) or {}


class BriefGate(BaseAgent):
    """Runs slide planning only when it is known what to plan.

    Planning needs a brief (language, audience, length) and the article in
    that language. Without them the planner would fall back on defaults and
    produce a talk nobody asked for, so nothing runs and the reason is said.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        session_state = ctx.session.state
        language = _brief(session_state).get("language")
        if not language or not state.article_markdown(session_state, language):
            yield _text_event(
                self,
                ctx,
                "Slide planning was not started: there is no presentation brief "
                "for a language that has an article. Call set_presentation_brief "
                "first.",
            )
            return
        async for event in self.sub_agents[0].run_async(ctx):
            yield event


class SlideTimingGate(BaseAgent):
    """Checks the planner's draft and makes it the plan of record of its language.

    The language of a plan is the language that was asked for in the brief,
    whatever the model wrote in the draft. The draft and its timing report are
    stored per language, so planning one language never overwrites another.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        session_state = ctx.session.state
        brief = _brief(session_state)
        language = brief.get("language")
        delta: dict[str, Any] = {}
        try:
            draft = dict(session_state.get(state.SLIDE_DECK_PLAN) or {})
            draft["language"] = language
            plan = SlideDeckPlan.model_validate(draft)
        except ValidationError:
            report: dict[str, Any] = {
                "passed": False,
                "recovery_instruction": "The plan was not valid; produce a complete plan.",
            }
        else:
            report = slide_timing_report(
                plan, int(brief.get("minutes") or len(plan.slides))
            )
            delta[state.SLIDE_PLANS] = {
                **(session_state.get(state.SLIDE_PLANS) or {}),
                language: plan.model_dump(),
            }
            delta[state.SLIDE_TIMING] = {
                **(session_state.get(state.SLIDE_TIMING) or {}),
                language: report,
            }
        log_event(
            "self-evaluation: slide timing",
            event_phase="SELF_EVAL",
            language=language,
            passed=report.get("passed"),
        )
        yield _state_event(
            self,
            ctx,
            state_delta={**delta, state.SLIDE_DRAFT_TIMING: report},
            escalate=bool(report.get("passed")),
        )


class SlideReportAgent(BaseAgent):
    """Writes the slide plan to a file and summarises it for the user."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        session_state = ctx.session.state
        language = _brief(session_state).get("language") or ""
        # Only a plan this run produced is reported; an older plan of the same
        # language is still there, but it is not what was just asked for.
        produced = bool(session_state.get(state.SLIDE_DECK_PLAN))
        plan = plan_of_record(session_state, language) if produced else None
        if plan is None:
            yield _text_event(
                self, ctx, "The slide plan could not be produced. Please try again."
            )
            return
        project_id = session_state.get(state.PROJECT_ID) or "unsaved"
        file = write_workspace_file(
            project_id, slide_plan_file(language), render_slide_plan_markdown(plan)
        )
        timing = (session_state.get(state.SLIDE_TIMING) or {}).get(language) or {}
        lines = [
            f"Slide plan drafted: '{plan.deck_title}', {len(plan.slides)} slides "
            f"({LANGUAGE_NAMES[plan.language]}). File: {file}",
            "",
        ]
        estimates = {s["number"]: s for s in timing.get("slides", [])}
        for slide in plan.slides:
            estimate = estimates.get(slide.number, {})
            lines.append(
                f"{slide.number}. {slide.title} "
                f"(~{estimate.get('estimated_seconds', '?')}s, {estimate.get('verdict', '?')})"
            )
        if timing.get("passed"):
            lines += [
                "",
                "Timing check passed (estimated from script length, not measured).",
            ]
        else:
            lines += [
                "",
                "Timing check did not pass: "
                + str(timing.get("recovery_instruction", "")),
            ]
        lines += ["", "Tell me what to change, or ask for the artifact manifest."]
        yield _text_event(self, ctx, "\n".join(lines))
