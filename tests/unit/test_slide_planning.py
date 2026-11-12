"""SLD-5, SLD-14 and RUN-2: slide planning in a working session per language.

The agent is real here: the slide planning pipeline with its gates, run by an
ADK ``Runner`` on the SQLite session store a real run uses. Only the planner's
model is replaced, by one that answers with a plan for the language it was
asked for, when the test lets it.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types
from pydantic import Field, PrivateAttr

from app import policy, state
from app.agent import build_slide_planning_app
from app.pipeline import USER_ID, ArticlePipeline
from app.settings import workspace_dir
from app.tools.articles import write_workspace_file
from tests.unit.conftest import SEED_KO
from tests.unit.test_pipeline import _plan

PROJECT_ID = "proj-planning"
ENGLISH = SEED_KO.replace("제목", "Title")
JAPANESE = SEED_KO.replace("제목", "タイトル")


class PlannerModel(BaseLlm):
    """Plans the language named in its instruction; waits when told to."""

    fails_for: list[str] = Field(default_factory=list)
    seen: list[tuple[str, str]] = Field(default_factory=list)  # language, instruction
    most_at_once: int = 0
    _at_work: int = PrivateAttr(default=0)
    _all_started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _may_answer: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _wait_for: int = PrivateAttr(default=0)

    def hold_until_released(self, languages: int) -> None:
        """Answer nobody until ``release``; say when this many have asked."""
        self._wait_for = languages

    def release(self) -> None:
        self._may_answer.set()

    async def all_started(self) -> None:
        await asyncio.wait_for(self._all_started.wait(), timeout=5)

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        instruction = str(llm_request.config.system_instruction)
        language = re.search(
            r"Language of slides and script: \w+ \('(\w+)'\)", instruction
        )
        assert language, "the planner was not told a language"
        code = language.group(1)
        self.seen.append((code, instruction))
        self._at_work += 1
        self.most_at_once = max(self.most_at_once, self._at_work)
        try:
            if self._wait_for:
                if len(self.seen) >= self._wait_for:
                    self._all_started.set()
                await self._may_answer.wait()
            else:
                await asyncio.sleep(0)  # let the other languages start
            if code in self.fails_for:
                raise RuntimeError("429 RESOURCE_EXHAUSTED: quota used up")
            asked = re.search(r"exactly (\d+) slides", instruction)
            slides = int(asked.group(1)) if asked else 3
            plan = json.dumps(_plan(code, slides=slides), ensure_ascii=False)
        finally:
            self._at_work -= 1
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part.from_text(text=plan)])
        )


async def _pipeline(tmp_path, model: PlannerModel) -> ArticlePipeline:
    pipeline = ArticlePipeline(
        log=lambda _: None,
        session_service=SqliteSessionService(db_path=str(tmp_path / "sessions.db")),
        slide_app=build_slide_planning_app(model),
    )
    for language, text in (("ko", SEED_KO), ("en", ENGLISH), ("ja", JAPANESE)):
        write_workspace_file(PROJECT_ID, f"post.{language}.md", text)
    await pipeline._update_state(
        {
            state.PROJECT_ID: PROJECT_ID,
            state.SEED_TITLE: "제목",
            state.SEED_MARKDOWN: SEED_KO,
            state.SEED_FINALIZED: True,
            state.SOURCE_LANGUAGE: "ko",
            state.TARGET_LANGUAGES: ["en", "ja"],
            state.localized_key("en"): ENGLISH,
            state.localized_key("ja"): JAPANESE,
            state.LOCALIZED_TITLES: {"en": "Title", "ja": "タイトル"},
        }
    )
    return pipeline


async def _working_sessions(pipeline: ArticlePipeline) -> list[Any]:
    listed = await pipeline._sessions.list_sessions(
        app_name=pipeline._slide_app.name, user_id=USER_ID
    )
    return list(listed.sessions)


async def _record_events(pipeline: ArticlePipeline) -> int:
    return len((await pipeline._session()).events)


@pytest.mark.asyncio
async def test_slide_planning_runs_in_a_session_of_its_own_and_only_its_result_reaches_the_record(
    isolated_workspace, tmp_path
) -> None:
    model = PlannerModel(model="planner")
    pipeline = await _pipeline(tmp_path, model)
    # What an earlier planning run, for Japanese, left in the record: a brief,
    # a draft and a failed timing check.
    left_behind = {
        state.PRESENTATION_BRIEF: {"language": "ja", "audience": "x", "minutes": 9},
        state.SLIDE_DECK_PLAN: _plan("ja"),
        state.SLIDE_DRAFT_TIMING: {
            "passed": False,
            "recovery_instruction": "LENGTHEN-THE-JAPANESE-SLIDES",
        },
        state.SLIDE_PLANS: {"ja": _plan("ja")},
        state.SLIDE_TIMING: {"ja": {"passed": True, "expected_slide_count": 3}},
    }
    await pipeline._update_state(left_behind)
    events_before = await _record_events(pipeline)

    made, note = await pipeline.plan_slides("en", 3, "platform engineers")
    assert (made, note) == (True, None)

    # The planner worked on English, for the audience and length asked for,
    # and was shown nothing of the Japanese run.
    ((language, instruction),) = model.seen
    assert language == "en"
    assert "Audience: platform engineers." in instruction
    assert "exactly 3 slides" in instruction
    assert "LENGTHEN-THE-JAPANESE-SLIDES" not in instruction
    assert "previous draft failed" not in instruction.lower()

    record = await pipeline.session_state()
    # Only the result reached the record: the English plan and its timing,
    # in one write. The Japanese entries and the old working area are as
    # they were.
    assert await _record_events(pipeline) == events_before + 1
    assert sorted(record[state.SLIDE_PLANS]) == ["en", "ja"]
    assert record[state.SLIDE_PLANS]["en"]["deck_title"] == "Deck en"
    assert record[state.SLIDE_TIMING]["en"]["passed"] is True
    assert record[state.SLIDE_TIMING]["en"]["expected_slide_count"] == 3
    for key, value in left_behind.items():
        if key in state.PLANNING_WORKING_AREA:
            assert record[key] == value
    assert record[state.SLIDE_PLANS]["ja"] == left_behind[state.SLIDE_PLANS]["ja"]
    # The speaker notes are a file of this article, and the working session
    # is gone.
    assert (workspace_dir() / PROJECT_ID / "slides.en.md").is_file()
    assert await _working_sessions(pipeline) == []

    # A language without an article is refused before any session is made.
    await pipeline._update_state({state.localized_key("ja"): None})
    made, note = await pipeline.plan_slides("ja", 3, "platform engineers")
    assert made is False and note.startswith("No Japanese slide plan was made.")
    assert len(model.seen) == 1 and await _working_sessions(pipeline) == []


@pytest.mark.asyncio
async def test_a_person_edit_during_slide_planning_does_not_stop_the_planning(
    isolated_workspace, tmp_path
) -> None:
    model = PlannerModel(model="planner")
    model.hold_until_released(3)
    pipeline = await _pipeline(tmp_path, model)

    slides = asyncio.create_task(
        pipeline.make_slides(["en", "ko", "ja"], 3, "engineers", create_deck=False)
    )
    await model.all_started()
    # All three languages are being planned at the same time, each in a
    # working session of its own.
    assert model.most_at_once == 3 and len(await _working_sessions(pipeline)) == 3

    # Meanwhile a person edits the English post and finalizes the Japanese
    # one. Neither waits for the planning: both are done before it goes on.
    edited = ENGLISH.replace("끝입니다.", "끝입니다. Edited by hand.")
    saved = await asyncio.wait_for(pipeline.save_edit("post.en.md", edited), timeout=5)
    assert saved["saved"] == "post.en.md"
    done = await asyncio.wait_for(pipeline.finalize_post("ja"), timeout=5)
    assert done["finalized"] is True
    assert not slides.done()

    model.release()
    assert await slides is None  # the planning went on to its end: B-01

    record = await pipeline.session_state()
    assert sorted(record[state.SLIDE_PLANS]) == ["en", "ja", "ko"]
    assert sorted(record[state.SLIDE_TIMING]) == ["en", "ja", "ko"]
    assert "Edited by hand." in record[state.localized_key("en")]
    assert policy.is_finalized(record, "ja")
    assert await _working_sessions(pipeline) == []
    # The plans were made from the text as it was when planning started; the
    # edit is the person's and stays.
    assert (workspace_dir() / PROJECT_ID / "manifest.md").is_file()


@pytest.mark.asyncio
async def test_one_language_failing_does_not_stop_the_others(
    isolated_workspace, tmp_path, monkeypatch
) -> None:
    from app import google_slides

    created: list[str] = []

    async def fake_create(plan, allowed_images):
        created.append(plan.language)
        return {
            "presentation_id": f"deck-{plan.language}",
            "url": google_slides.deck_url(f"deck-{plan.language}"),
        }

    monkeypatch.setattr(google_slides, "create_deck", fake_create)
    model = PlannerModel(model="planner", fails_for=["ko"])
    pipeline = await _pipeline(tmp_path, model)

    notes = await pipeline.make_slides(["en", "ko", "ja"], 3, "engineers", True)

    # The failure is reported for Korean, in words, and only for Korean.
    assert notes.startswith("The Korean slides were not made.")
    assert "quota used up" in notes
    assert "English" not in notes and "Japanese" not in notes
    record = await pipeline.session_state()
    assert sorted(record[state.SLIDE_PLANS]) == ["en", "ja"]
    assert sorted(created) == ["en", "ja"]
    assert sorted(record[state.GOOGLE_SLIDES_DECKS]) == ["en", "ja"]
    assert await _working_sessions(pipeline) == []  # the failed one's too


@pytest.mark.asyncio
async def test_the_number_of_languages_planned_at_once_can_be_lowered(
    isolated_workspace, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BLOG_AGENT_SLIDE_PARALLELISM", "1")
    model = PlannerModel(model="planner")
    pipeline = await _pipeline(tmp_path, model)
    assert await pipeline.make_slides(["en", "ko", "ja"], 3, "engineers", False) is None
    assert model.most_at_once == 1
    assert [language for language, _ in model.seen] == ["en", "ko", "ja"]
    record = await pipeline.session_state()
    assert sorted(record[state.SLIDE_PLANS]) == ["en", "ja", "ko"]


@pytest.mark.asyncio
async def test_a_row_says_what_the_slides_lane_is_doing_with_its_language(
    isolated_workspace, tmp_path, monkeypatch
) -> None:
    """WEB-5: waiting, planning or creating the deck, in the row, while it lasts."""
    from app import google_slides
    from app.pipeline import SLIDE_PHASES, mark_slide_rows_in_progress, overview

    deck_started, deck_may_end = asyncio.Event(), asyncio.Event()

    async def fake_create(plan, allowed_images):
        if plan.language == "en":
            deck_started.set()
            await deck_may_end.wait()
        return {
            "presentation_id": f"deck-{plan.language}",
            "url": google_slides.deck_url(f"deck-{plan.language}"),
        }

    monkeypatch.setattr(google_slides, "create_deck", fake_create)
    monkeypatch.setenv("BLOG_AGENT_SLIDE_PARALLELISM", "2")
    model = PlannerModel(model="planner", fails_for=["ko"])
    model.hold_until_released(2)
    pipeline = await _pipeline(tmp_path, model)
    asked = ["en", "ko", "ja"]

    async def rows_now(busy: bool) -> dict[str, dict[str, Any]]:
        view = overview(await pipeline.session_state())
        languages = mark_slide_rows_in_progress(
            view, pipeline.slide_phases, asked, slides_busy=busy
        )
        # What comes back is the languages alone: no phase per language.
        assert languages == asked
        rows = {row["language"]: row for row in view["rows"]}
        assert all(
            row["slides_progress"] in (None, *SLIDE_PHASES) for row in rows.values()
        )
        return rows

    async def until(wanted: dict[str, str | None]) -> dict[str, dict[str, Any]]:
        for _ in range(500):
            rows = await rows_now(busy=True)
            said = {code: rows[code]["slides_progress"] for code in asked}
            if said == wanted:
                return rows
            await asyncio.sleep(0.01)
        raise AssertionError(f"the rows say {said}, not {wanted}")

    # Nothing was started: no row says anything, each offers its action.
    rows = await rows_now(busy=False)
    assert all(rows[code]["slides_progress"] is None for code in asked)
    assert all(rows[code]["slides_actions"] == ["create_slides"] for code in asked)

    slides = asyncio.create_task(pipeline.make_slides(asked, 3, "engineers", True))
    await model.all_started()
    # Two are planned at once; the third waits for its turn.
    rows = await until({"en": "planning", "ko": "planning", "ja": "waiting"})
    assert all(rows[code]["slides_actions"] == [] for code in asked)

    model.release()
    await asyncio.wait_for(deck_started.wait(), timeout=5)
    # English is at its deck, Korean failed, Japanese has its plan and waits
    # for the deck before it. A language that failed offers its action again.
    rows = await until({"en": "creating", "ko": None, "ja": "waiting"})
    assert rows["ko"]["slides_actions"] == ["create_slides"]
    assert rows["en"]["slides_actions"] == rows["ja"]["slides_actions"] == []

    deck_may_end.set()
    await slides
    rows = await rows_now(busy=False)
    assert all(rows[code]["slides_progress"] is None for code in asked)
    assert rows["en"]["slides_actions"] == ["open_deck", "notes"]
    assert rows["ko"]["slides_actions"] == ["create_slides"]

    # The page says it in the row and nowhere else: the slides step has no
    # mark per language (decision 54).
    from app import web_ui

    script = (web_ui.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 't("slidePhase." + row.slides_progress)' in script
    assert "SLIDE_MARK" not in script and "data.slides_progress" not in script
    styles = (web_ui.STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "slide-lang" not in styles
