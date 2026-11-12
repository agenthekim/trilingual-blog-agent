"""INV-6 and WEB-9: slides asked for in words, or with the page's button.

The agent is real here: an ADK ``Runner`` with the real request tool and the
real pause-and-resume machinery, and the real slide planning pipeline in its
working sessions. Only the two models are replaced (the coordinator's by one
that says what the test scripted, the planner's by one that writes a plan),
and the Google Slides calls by recorders.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.adk.agents import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.sessions import InMemorySessionService

from app import google_slides, runs, state, web_ui
from app.agent import build_slide_planning_app
from app.pipeline import ArticlePipeline
from app.tools import slides
from app.tools.articles import write_workspace_file
from tests.unit.conftest import SEED_KO, FakeToolContext
from tests.unit.test_approvals import ScriptedModel, _calls
from tests.unit.test_pipeline import _plan
from tests.unit.test_slide_planning import ENGLISH, JAPANESE, PlannerModel
from tests.unit.test_web_ui import _row, _wait_for_lane

PROJECT_ID = "proj-slide-request"
JOB_ID = "e" * 32
ACCOUNT = "author@example.com"
REQUEST_TOOL = "request_approval_to_create_slide_decks"


class Page:
    """A run on the page: the client, the run and the stand-ins behind it."""

    def __init__(self, client: TestClient, job: runs.Job) -> None:
        self.client, self.job = client, job
        self.base = f"/api/jobs/{job.id}"
        self.coordinator: ScriptedModel
        self.planner: PlannerModel
        self.decks: list[str] = []

    def status(self) -> dict[str, Any]:
        return self.client.get(self.base).json()

    def say(self, text: str, *calls: tuple[str, dict]) -> dict[str, Any]:
        """Send a message the coordinator answers with these tool calls."""
        if calls:
            self.coordinator.script.append(_calls(*calls))
        sent = self.client.post(f"{self.base}/message", json={"text": text})
        assert sent.json() == {"accepted": True}
        return _wait_for_lane(self.client, self.job.id, "main")

    def answer(self, approval: dict[str, Any], confirmed: bool) -> dict[str, Any]:
        sent = self.client.post(
            f"{self.base}/approval", json={"id": approval["id"], "confirmed": confirmed}
        )
        assert sent.json() == {"accepted": True}
        return _wait_for_lane(self.client, self.job.id, "main")

    def record(self) -> dict[str, Any]:
        return self.client.portal.call(self.job.pipeline.session_state)


@pytest.fixture
def page(isolated_workspace, monkeypatch):
    coordinator = ScriptedModel(model="scripted")
    planner = PlannerModel(model="planner")
    agent = Agent(
        name="trilingual_blog_agent",
        model=coordinator,
        instruction="Do what the user asks for.",
        tools=[
            slides.request_approval_to_create_slide_decks,
            slides.set_presentation_brief,
        ],
    )
    pipeline = ArticlePipeline(
        log=lambda _: None,
        session_service=InMemorySessionService(),
        agent_app=App(
            name="app",
            root_agent=agent,
            resumability_config=ResumabilityConfig(is_resumable=True),
        ),
        slide_app=build_slide_planning_app(planner),
    )
    job = web_ui.Job(id=JOB_ID, pipeline=pipeline)
    pipeline.log = job.add_log
    web_ui.JOBS[job.id] = job
    for language, text in (("ko", SEED_KO), ("en", ENGLISH), ("ja", JAPANESE)):
        write_workspace_file(PROJECT_ID, f"post.{language}.md", text)
    asyncio.run(
        pipeline._update_state(
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
                # The run was started with a five-minute talk for students.
                state.RUN_OPTIONS: {"minutes": 5, "audience": "students"},
            }
        )
    )
    monkeypatch.setattr(google_slides, "granted_account", lambda: ACCOUNT)
    with TestClient(web_ui.web, base_url="http://127.0.0.1:8502") as client:
        held = Page(client, job)
        held.coordinator, held.planner = coordinator, planner

        async def fake_create(plan, allowed_images):
            held.decks.append(plan.language)
            return {
                "presentation_id": f"deck-{plan.language}",
                "url": google_slides.deck_url(f"deck-{plan.language}"),
            }

        monkeypatch.setattr(google_slides, "create_deck", fake_create)
        yield held
    web_ui.JOBS.clear()


def _asks_for(
    *languages: str, minutes: int = 0, audience: str = ""
) -> tuple[str, dict]:
    return (
        REQUEST_TOOL,
        {"languages": list(languages), "minutes": minutes, "audience": audience},
    )


def test_a_message_to_the_agent_creates_no_deck(page: Page) -> None:
    # The sentence that used to create a deck at once (B-03).
    status = page.say(
        "Re-create the English slides and Google Slides deck", _asks_for("en")
    )

    # Nothing was planned and nothing was created; no slide work was started.
    assert page.planner.seen == [] and page.decks == []
    assert status["lanes"]["slides"] == {
        "busy": False,
        "action": None,
        "error": None,
        "notice": None,
        "errors": [],
        "notices": [],
    }
    record = page.record()
    assert not record.get(state.SLIDE_PLANS) and not record.get(state.SLIDE_REQUEST)
    assert not record.get(state.GOOGLE_SLIDES_DECKS)
    # What the person sees instead is a request to approve.
    assert [approval["tool"] for approval in status["approvals"]] == [REQUEST_TOOL]

    # The web layer passes the sentence on and reads nothing into it: the one
    # place it touches the text is the call that sends it to the agent.
    route = inspect.getsource(web_ui.send_message)
    assert route.count("request.text") == 1
    assert "job.pipeline.say(request.text)" in route
    assert "lower()" not in route and " in request.text" not in route
    source = inspect.getsource(web_ui)
    for gone in ("_requested_slide_languages", "_LANG_MENTIONS", "hasattr(", "replan"):
        assert gone not in source


def test_a_slide_request_from_the_agent_waits_for_a_persons_approval(
    page: Page,
) -> None:
    # Japanese has a plan and a deck already; English has neither.
    page.client.portal.call(
        page.job.pipeline._update_state,
        {
            state.SLIDE_PLANS: {"ja": _plan("ja", slides=7)},
            state.GOOGLE_SLIDES_DECKS: {"ja": {"presentation_id": "old", "url": "u"}},
        },
    )
    status = page.say("Make English and Japanese slides", _asks_for("en", "ja"))

    # One request for both languages, and it says what an approval is for:
    # the languages, the length of each, the audience and the account.
    (approval,) = status["approvals"]
    hint = approval["hint"]
    assert "English (5 min)" in hint  # no plan: the length the run was started with
    assert "Japanese (7 min)" in hint  # a plan: the length it has
    assert "Audience: students." in hint
    assert f"the Google Drive of {ACCOUNT}" in hint
    # What an approval replaces is said for the language it applies to.
    assert "Japanese: there is a plan already" in hint
    assert "Japanese: there is a deck already" in hint
    assert "the earlier deck stays in Drive" in hint
    assert "English: there is" not in hint
    assert page.planner.seen == [] and page.decks == []

    # Declined: nothing is made, and nothing is left waiting to be made.
    status = page.answer(approval, False)
    assert status["approvals"] == [] and page.planner.seen == [] and page.decks == []
    assert not status["lanes"]["slides"]["busy"]
    assert not page.record().get(state.SLIDE_REQUEST)
    assert list(page.record()[state.SLIDE_PLANS]) == ["ja"]

    # A length and an audience that were said are the ones shown.
    status = page.say(
        "English slides, 12 minutes, for SREs",
        _asks_for("en", minutes=12, audience="SREs"),
    )
    hint = status["approvals"][0]["hint"]
    assert "English (12 min)" in hint and "Audience: SREs." in hint


def test_an_approved_slide_request_runs_in_the_slides_lane(page: Page) -> None:
    page.planner.hold_until_released(1)
    status = page.say("Make the English slides", _asks_for("en"))
    (approval,) = status["approvals"]

    status = page.answer(approval, True)
    # The agent's turn has ended and the main lane is free; code took the
    # approved request and is making the slides in the slides lane.
    assert status["lanes"]["main"]["busy"] is False
    assert status["lanes"]["slides"]["busy"] is True
    assert status["lanes"]["slides"]["action"] == "slides_request"
    assert status["approvals"] == []
    assert not page.record().get(state.SLIDE_REQUEST)  # taken once
    page.client.portal.call(page.planner.all_started)

    # Meanwhile a post can be edited: the other lane is not held.
    edited = ENGLISH.replace("끝입니다.", "끝입니다. Edited meanwhile.")
    saved = page.client.put(f"{page.base}/files/post.en.md", json={"markdown": edited})
    assert saved.status_code == 200

    page.client.portal.call(page.planner.release)
    status = _wait_for_lane(page.client, page.job.id, "slides")
    assert status["lanes"]["slides"]["notice"] is None and status["error"] is None
    # Planned for the length and the audience the person approved, and the
    # deck was created with that approval.
    ((language, instruction),) = page.planner.seen
    assert language == "en" and "exactly 5 slides" in instruction
    assert "Audience: students." in instruction
    assert page.decks == ["en"]
    assert _row(status, "en")["deck"]["url"].endswith("/d/deck-en/edit")
    assert _row(status, "en")["slides_file"] == "slides.en.md"
    assert "Edited meanwhile." in page.record()[state.localized_key("en")]

    # Approved while the slides lane is working: it is not started, and the
    # person is told so instead of being left waiting.
    page.job.lanes["slides"].busy = True
    status = page.say("Make the Japanese slides", _asks_for("ja"))
    status = page.answer(status["approvals"][0], True)
    assert "The slides that were approved were not started." in status["notice"]
    assert "slides lane" in status["notice"]
    assert len(page.planner.seen) == 1 and page.decks == ["en"]
    assert not page.record().get(state.SLIDE_REQUEST)


def test_slides_for_several_languages_are_one_request(page: Page) -> None:
    url = f"{page.base}/slides"
    body = {"languages": ["en", "ko", "en"], "approved": True}
    # The button's dialog is the approval; without it nothing starts.
    refused = page.client.post(url, json={**body, "approved": False})
    assert refused.status_code == 400 and "approval" in refused.json()["detail"]
    assert page.client.post(url, json={"approved": True}).status_code == 422
    assert not page.status()["lanes"]["slides"]["busy"]

    page.planner.hold_until_released(2)
    assert page.client.post(url, json=body).json() == {"accepted": True}
    page.client.portal.call(page.planner.all_started)
    status = page.status()
    # One action of the slides lane for both languages, planned together.
    assert (
        status["lanes"]["slides"]["busy"]
        and status["lanes"]["slides"]["action"] == "slides"
    )
    assert page.planner.most_at_once == 2
    again = page.client.post(url, json={"languages": ["ja"], "approved": True})
    assert again.status_code == 409 and again.json()["detail"] == web_ui.BUSY["slides"]

    page.client.portal.call(page.planner.release)
    status = _wait_for_lane(page.client, page.job.id, "slides")
    assert status["lanes"]["slides"]["notice"] is None and status["error"] is None
    assert sorted(language for language, _ in page.planner.seen) == ["en", "ko"]
    assert sorted(page.decks) == ["en", "ko"]
    assert sorted(page.record()[state.SLIDE_PLANS]) == ["en", "ko"]
    # What a later request without a length would use is on each row.
    assert _row(status, "en")["slides_minutes"] == 5
    assert status["overview"]["slides_audience"] == "students"

    # The agent's tool takes several languages in one call too: one approval.
    status = page.say("Japanese and Korean slides", _asks_for("ja", "ko"))
    (approval,) = status["approvals"]
    assert "Japanese (5 min), Korean (5 min)" in approval["hint"]


def test_on_the_page_the_brief_tool_points_to_the_request_tool() -> None:
    article = {
        state.PROJECT_ID: PROJECT_ID,
        state.SEED_MARKDOWN: SEED_KO,
        state.SOURCE_LANGUAGE: "ko",
        state.localized_key("en"): ENGLISH,
    }
    on_the_page = FakeToolContext({**article, state.SURFACE: state.SURFACE_WEB})
    refused = slides.set_presentation_brief("en", "engineers", 10, on_the_page)
    assert refused["status"] == "error"
    assert refused["error_code"] == "USE_THE_SLIDE_REQUEST_TOOL"
    assert REQUEST_TOOL in refused["recovery_instruction"]
    assert state.PRESENTATION_BRIEF not in on_the_page.state

    # In a chat or through the API both tools are as they were: the brief is
    # stored, and the request tool says it is the page's.
    in_a_chat = FakeToolContext(article)
    stored = slides.set_presentation_brief("en", "engineers", 10, in_a_chat)
    assert stored["status"] == "success" and stored["brief"]["minutes"] == 10
    # No length and no audience keep what there is: here, the defaults.
    kept = slides.set_presentation_brief("en", "", 0, in_a_chat)
    assert kept["brief"] == {"language": "en", "audience": "engineers", "minutes": 10}
    not_here = slides.request_approval_to_create_slide_decks(["en"], 0, "", in_a_chat)
    assert not_here["error_code"] == "ONLY_ON_THE_WEB_PAGE"
    assert "set_presentation_brief" in not_here["recovery_instruction"]
    assert in_a_chat.confirmation_requests == []

    # On the page the request tool refuses a language that has no article,
    # before it asks anybody anything.
    missing = slides.request_approval_to_create_slide_decks(
        ["en", "ja"], 0, "", on_the_page
    )
    assert missing["error_code"] == "ARTICLE_VERSION_MISSING"
    assert "Japanese" in missing["message"] and on_the_page.confirmation_requests == []
