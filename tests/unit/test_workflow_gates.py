"""Tests for the deterministic gate agents: no model is called.

Each gate is run through a real ADK ``Runner`` with a stub sub-agent, so the
tests cover the same event/state path the pipeline uses.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app import state
from app.workflow_agents import (
    SLIDE_TIMING_REPORT,
    FidelityCheckAgent,
    LanguageGate,
    ReviewGate,
    SeedGate,
    SlideTimingGate,
    language_needs_refinement,
)

SEED = "# Title\n\nIntro with a [link](https://example.com).\n\n```python\nprint('hi')\n```\n"
GOOD_JA = "# タイトル\n\n[リンク](https://example.com)付きの導入。\n\n```python\nprint('hi')\n```\n"
BROKEN_JA = "# タイトル\n\n導入。\n"


class StubWriter(BaseAgent):
    """Stands in for an LLM writer; says a fixed word when it runs."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(
                role="model", parts=[types.Part.from_text(text="stub ran")]
            ),
        )


async def run_agent(agent: BaseAgent, initial_state: dict[str, Any]) -> list[Event]:
    sessions = InMemorySessionService()
    session = await sessions.create_session(
        app_name="test", user_id="u", state=initial_state
    )
    runner = Runner(agent=agent, app_name="test", session_service=sessions)
    message = types.Content(role="user", parts=[types.Part.from_text(text="go")])
    return [
        event
        async for event in runner.run_async(
            user_id="u", session_id=session.id, new_message=message
        )
    ]


def texts(events: list[Event]) -> list[str]:
    return [
        part.text
        for event in events
        if event.content and event.content.parts
        for part in event.content.parts
        if part.text
    ]


def failing_verdict(language: str) -> dict[str, Any]:
    return {
        "reviews": [
            {"language": language, "grade": "fail", "issues": ["awkward wording"]}
        ]
    }


@pytest.mark.parametrize(
    ("session_state", "expected"),
    [
        ({}, False),
        ({state.FIDELITY_REPORT: {"ja": {"passed": True}}}, False),
        ({state.FIDELITY_REPORT: {"ja": {"passed": False}}}, True),
        ({state.REVIEW_VERDICT: failing_verdict("ja")}, True),
        ({state.REVIEW_VERDICT: failing_verdict("en")}, False),
    ],
)
def test_language_needs_refinement(
    session_state: dict[str, Any], expected: bool
) -> None:
    assert language_needs_refinement(session_state, "ja") is expected


@pytest.mark.asyncio
async def test_language_gate_skips_the_source_language() -> None:
    gate = LanguageGate(
        name="gate_ko", language="ko", sub_agents=[StubWriter(name="w")]
    )
    events = await run_agent(gate, {state.TARGET_LANGUAGES: ["en", "ja"]})
    assert texts(events) == []


@pytest.mark.asyncio
async def test_language_gate_runs_a_target_language() -> None:
    gate = LanguageGate(
        name="gate_ja", language="ja", sub_agents=[StubWriter(name="w")]
    )
    events = await run_agent(gate, {state.TARGET_LANGUAGES: ["en", "ja"]})
    assert texts(events) == ["stub ran"]


@pytest.mark.asyncio
async def test_refine_gate_runs_only_for_a_language_that_failed_review() -> None:
    session_state = {
        state.TARGET_LANGUAGES: ["en", "ja"],
        state.REVIEW_VERDICT: failing_verdict("ja"),
    }
    ja = LanguageGate(
        name="refine_ja",
        language="ja",
        mode="refine",
        sub_agents=[StubWriter(name="w1")],
    )
    en = LanguageGate(
        name="refine_en",
        language="en",
        mode="refine",
        sub_agents=[StubWriter(name="w2")],
    )
    assert texts(await run_agent(ja, session_state)) == ["stub ran"]
    assert texts(await run_agent(en, session_state)) == []


@pytest.mark.asyncio
async def test_seed_gate_blocks_an_unfinalized_seed() -> None:
    gate = SeedGate(name="seed_gate", sub_agents=[StubWriter(name="w")])
    events = await run_agent(
        gate, {state.SEED_MARKDOWN: SEED, state.SEED_FINALIZED: False}
    )
    output = texts(events)
    assert "stub ran" not in output
    assert "no finalized seed" in output[0]


@pytest.mark.asyncio
async def test_seed_gate_lets_a_finalized_seed_through() -> None:
    gate = SeedGate(name="seed_gate", sub_agents=[StubWriter(name="w")])
    events = await run_agent(
        gate, {state.SEED_MARKDOWN: SEED, state.SEED_FINALIZED: True}
    )
    assert texts(events) == ["stub ran"]


@pytest.mark.asyncio
async def test_fidelity_check_agent_reports_per_language() -> None:
    events = await run_agent(
        FidelityCheckAgent(name="check"),
        {
            state.SEED_MARKDOWN: SEED,
            state.TARGET_LANGUAGES: ["en", "ja"],
            state.localized_key("en"): SEED,
            state.localized_key("ja"): BROKEN_JA,
        },
    )
    report = events[-1].actions.state_delta[state.FIDELITY_REPORT]
    assert report["en"]["passed"] is True
    assert report["ja"]["passed"] is False
    assert report["ja"]["errors"]


@pytest.mark.asyncio
async def test_review_gate_escalates_only_when_everything_passed() -> None:
    passing = {
        state.TARGET_LANGUAGES: ["ja"],
        state.FIDELITY_REPORT: {"ja": {"passed": True}},
        state.REVIEW_VERDICT: {"reviews": [{"language": "ja", "grade": "pass"}]},
    }
    events = await run_agent(ReviewGate(name="gate"), passing)
    assert events[-1].actions.escalate is True

    failing = {**passing, state.REVIEW_VERDICT: failing_verdict("ja")}
    events = await run_agent(ReviewGate(name="gate"), failing)
    assert not events[-1].actions.escalate


def slide_plan(script: str, slides: int) -> dict[str, Any]:
    return {
        "deck_title": "Deck",
        "language": "en",
        "audience": "developers",
        "opening_script": "Welcome. This talk is Deck.",
        "closing_script": "Thank you. Questions?",
        "slides": [
            {
                "number": n,
                "title": f"Slide {n}",
                "bullets": ["one point"],
                "visual_note": "text only",
                "speaker_script": script,
            }
            for n in range(1, slides + 1)
        ],
    }


@pytest.mark.asyncio
async def test_slide_timing_gate_rejects_scripts_that_are_too_short() -> None:
    events = await run_agent(
        SlideTimingGate(name="timing"),
        {
            state.PRESENTATION_BRIEF: {"minutes": 3},
            state.SLIDE_DECK_PLAN: slide_plan("Too short.", 3),
        },
    )
    last = events[-1]
    report = last.actions.state_delta[SLIDE_TIMING_REPORT]
    assert report["passed"] is False
    assert {slide["verdict"] for slide in report["slides"]} == {"too_short"}
    assert not last.actions.escalate


@pytest.mark.asyncio
async def test_slide_timing_gate_accepts_one_minute_scripts() -> None:
    one_minute = " ".join(["word"] * 140)
    events = await run_agent(
        SlideTimingGate(name="timing"),
        {
            state.PRESENTATION_BRIEF: {"minutes": 3},
            state.SLIDE_DECK_PLAN: slide_plan(one_minute, 3),
        },
    )
    last = events[-1]
    assert last.actions.state_delta[SLIDE_TIMING_REPORT]["passed"] is True
    assert last.actions.escalate is True


@pytest.mark.asyncio
async def test_slide_timing_gate_handles_a_missing_plan() -> None:
    events = await run_agent(SlideTimingGate(name="timing"), {})
    report = events[-1].actions.state_delta[SLIDE_TIMING_REPORT]
    assert report["passed"] is False
    assert "not valid" in report["recovery_instruction"]
    assert not events[-1].actions.escalate


def test_good_translation_keeps_structure() -> None:
    from app.markdown_checks import check_fidelity

    assert check_fidelity(SEED, GOOD_JA)["passed"] is True
