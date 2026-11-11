"""An approval answers the one request it was given for.

The agent is real here: an ADK ``Runner`` with the real posting tools and the
real pause-and-resume machinery. Only the model is replaced, by one that says
what the test scripted, and the platform calls by recorders.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.agents import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

from app import platforms, policy, state
from app.pipeline import ArticlePipeline
from app.tools import articles, publish
from tests.unit.conftest import SEED_KO


class ScriptedModel(BaseLlm):
    """Answers each request with the next scripted content, then with 'Done.'."""

    script: list[Any] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        content = self.script.pop(0) if self.script else _says("Done.")
        yield LlmResponse(content=content)


def _says(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part.from_text(text=text)])


def _calls(*calls: tuple[str, dict]) -> types.Content:
    return types.Content(
        role="model",
        parts=[
            types.Part(function_call=types.FunctionCall(name=name, args=args))
            for name, args in calls
        ],
    )


async def _pipeline(script: list[types.Content]) -> ArticlePipeline:
    agent = Agent(
        name="trilingual_blog_agent",
        model=ScriptedModel(model="scripted", script=script),
        instruction="Post what the user asks for.",
        tools=[
            publish.publish_qiita_article_draft,
            publish.publish_devto_article_draft,
            publish.finalize_localized_post,
            articles.save_seed_article_revision,
        ],
    )
    app = App(
        name="app",
        root_agent=agent,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    pipeline = ArticlePipeline(
        log=lambda _: None, session_service=InMemorySessionService(), agent_app=app
    )
    english = SEED_KO.replace("제목", "Title")
    japanese = SEED_KO.replace("제목", "タイトル")
    start = {
        state.PROJECT_ID: "proj-approvals",
        state.SEED_TITLE: "제목",
        state.SEED_MARKDOWN: SEED_KO,
        state.SEED_FINALIZED: True,
        state.SOURCE_LANGUAGE: "ko",
        state.TARGET_LANGUAGES: ["en", "ja"],
        state.localized_key("en"): english,
        state.localized_key("ja"): japanese,
        state.LOCALIZED_TITLES: {"en": "Title", "ja": "タイトル"},
    }
    start[state.FINALIZED_POSTS] = {
        language: policy.finalization(start, language) for language in ("en", "ja")
    }
    await pipeline._update_state(start)
    return pipeline


def _record(monkeypatch) -> dict[str, list[dict]]:
    sent: dict[str, list[dict]] = {"qiita": [], "devto": []}

    async def qiita(**kwargs):
        sent["qiita"].append(kwargs)
        return {"id": "abc", "url": "https://qiita.com/u/private/abc"}

    async def devto(**kwargs):
        sent["devto"].append(kwargs)
        return {"id": 42, "url": "https://dev.to/u/t-42"}

    monkeypatch.setattr(platforms, "qiita_save_item", qiita)
    monkeypatch.setattr(platforms, "devto_save_article", devto)
    return sent


@pytest.mark.asyncio
async def test_an_approval_answers_one_request_and_never_another(
    isolated_workspace, monkeypatch
) -> None:
    sent = _record(monkeypatch)
    pipeline = await _pipeline(
        [
            _calls(
                ("publish_qiita_article_draft", {"tags": ["AI"]}),
                ("publish_devto_article_draft", {"tags": ["ai"]}),
            )
        ]
    )
    await pipeline.say("Post the Japanese and the English article.")

    waiting = {approval["tool"]: approval for approval in pipeline.approvals}
    assert sorted(waiting) == [
        "publish_devto_article_draft",
        "publish_qiita_article_draft",
    ]
    assert "DEV Community" in waiting["publish_devto_article_draft"]["hint"]
    assert sent == {"qiita": [], "devto": []}  # nothing leaves before an answer

    # The person approves the DEV post, as public. The Qiita request, which
    # nobody answered, must not inherit that yes or that choice.
    declined = await pipeline.answer_approval(
        waiting["publish_devto_article_draft"]["id"], True, {"visibility": "public"}
    )
    assert declined == 1
    assert len(sent["devto"]) == 1 and sent["devto"][0]["published"] is True
    assert sent["qiita"] == []
    current = await pipeline.session_state()
    assert current[state.PUBLICATIONS]["devto"]["state"] == "published"
    assert "qiita" not in current[state.PUBLICATIONS]
    assert pipeline.approvals == []


@pytest.mark.asyncio
async def test_a_declined_request_sends_nothing_and_an_unknown_one_is_refused(
    isolated_workspace, monkeypatch
) -> None:
    sent = _record(monkeypatch)
    pipeline = await _pipeline(
        [_calls(("publish_qiita_article_draft", {"tags": ["AI"]}))]
    )
    await pipeline.say("Post the Japanese article.")
    (approval,) = pipeline.approvals
    with pytest.raises(ValueError, match="no longer waiting"):
        await pipeline.answer_approval("some-other-id", True)
    assert sent["qiita"] == []

    assert await pipeline.answer_approval(approval["id"], False) == 0
    assert sent["qiita"] == []
    assert "qiita" not in (
        (await pipeline.session_state()).get(state.PUBLICATIONS) or {}
    )


@pytest.mark.asyncio
async def test_the_models_own_arguments_cannot_make_a_post_public_or_retitle_it(
    isolated_workspace, monkeypatch
) -> None:
    """Arguments the tools do not have are not passed on; the approval decides."""
    sent = _record(monkeypatch)
    pipeline = await _pipeline(
        [_calls(("publish_devto_article_draft", {"tags": ["ai"]}))]
    )
    await pipeline.say("Post the English article.")
    (approval,) = pipeline.approvals
    await pipeline.answer_approval(approval["id"], True)  # a plain yes
    (call,) = sent["devto"]
    assert call["published"] is False and call["title"] == "Title"
    current = await pipeline.session_state()
    assert current[state.PUBLICATIONS]["devto"]["state"] == "draft"


@pytest.mark.asyncio
async def test_finalizing_in_a_chat_waits_for_the_person(isolated_workspace) -> None:
    pipeline = await _pipeline(
        [_calls(("finalize_localized_post", {"language": "en"}))]
    )
    await pipeline._update_state({state.FINALIZED_POSTS: {}})
    await pipeline.say("The English post is ready.")
    (approval,) = pipeline.approvals
    assert approval["tool"] == "finalize_localized_post"
    assert not policy.is_finalized(await pipeline.session_state(), "en")
    await pipeline.answer_approval(approval["id"], True)
    assert policy.is_finalized(await pipeline.session_state(), "en")


@pytest.mark.asyncio
async def test_a_yes_to_finalize_does_not_cover_a_text_swapped_while_it_was_asked(
    isolated_workspace, monkeypatch
) -> None:
    """Calls of one model message all run before the pause for the person.

    The model asks to finalize the Korean post and, in the same message,
    replaces its text under the same title. The person's yes was asked for the
    text as it was; it must not finalize the other one.
    """
    sent = _record(monkeypatch)
    swapped = SEED_KO + "\n바꿔치기한 문단입니다.\n"
    pipeline = await _pipeline(
        [
            _calls(
                ("finalize_localized_post", {"language": "ko"}),
                (
                    "save_seed_article_revision",
                    {
                        "title": "제목",
                        "markdown": swapped,
                        "source_language": "ko",
                        "code_mode": "none",
                        "code_repo_url": "",
                        "finalized": True,
                    },
                ),
            )
        ]
    )
    await pipeline._update_state({state.FINALIZED_POSTS: {}})
    await pipeline.say("The Korean post is ready.")
    current = await pipeline.session_state()
    assert current[state.SEED_MARKDOWN] == swapped  # the swap happened
    (approval,) = pipeline.approvals
    assert approval["tool"] == "finalize_localized_post"

    await pipeline.answer_approval(approval["id"], True)
    assert not policy.is_finalized(await pipeline.session_state(), "ko")
    assert sent == {"qiita": [], "devto": []}
