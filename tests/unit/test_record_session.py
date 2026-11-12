"""RUN-2: the session of a run is its record and has one writer at a time.

The session store is the SQLite one a real run uses, because that is the store
that refuses a write made from a stale copy of the session. The agent is a real
ADK ``Runner``; only the model is replaced by one that says what was scripted.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.errors import StaleSessionError
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.adk.tools.tool_context import ToolContext

from app import state
from app.pipeline import ArticlePipeline, merge_delta
from tests.unit.test_approvals import ScriptedModel, _calls


def _record_pipeline(tmp_path, tools: list[Any], script: list[Any]) -> ArticlePipeline:
    agent = Agent(
        name="trilingual_blog_agent",
        model=ScriptedModel(model="scripted", script=script),
        instruction="Do what the user asks for.",
        tools=tools,
    )
    return ArticlePipeline(
        log=lambda _: None,
        session_service=SqliteSessionService(db_path=str(tmp_path / "sessions.db")),
        agent_app=App(name="app", root_agent=agent),
    )


def sign_off(language: str, tool_context: Any) -> dict[str, Any]:
    """A person's action: writes one language's entry of a shared dictionary."""
    marks = dict(tool_context.state.get(state.FINALIZED_POSTS) or {})
    marks[language] = {"digest": f"digest-{language}"}
    tool_context.state[state.FINALIZED_POSTS] = marks
    return {"status": "success"}


@pytest.mark.asyncio
async def test_a_write_by_code_waits_for_the_agent_turn_in_the_record_session(
    isolated_workspace, tmp_path
) -> None:
    in_the_turn, let_the_turn_go_on = asyncio.Event(), asyncio.Event()

    async def take_a_while(tool_context: ToolContext) -> dict[str, Any]:
        """Stands for a long agent step that writes to the record at its end."""
        in_the_turn.set()
        await let_the_turn_go_on.wait()
        tool_context.state[state.SEED_TITLE] = "written by the turn"
        return {"status": "success"}

    pipeline = _record_pipeline(
        tmp_path, [take_a_while], [_calls(("take_a_while", {}))]
    )
    await pipeline._update_state({state.PROJECT_ID: "proj-record"})

    turn = asyncio.create_task(pipeline.say("Take a while."))
    await in_the_turn.wait()
    # A person finalizes a post while the agent turn is still running.
    action = asyncio.create_task(pipeline.act(sign_off, "en"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not action.done()  # the write waits; it does not get in between
    assert state.FINALIZED_POSTS not in await pipeline.session_state()

    let_the_turn_go_on.set()
    await turn  # B-01: this used to end in StaleSessionError
    assert (await action)["status"] == "success"

    current = await pipeline.session_state()
    assert current[state.SEED_TITLE] == "written by the turn"
    assert current[state.FINALIZED_POSTS] == {"en": {"digest": "digest-en"}}
    assert pipeline.last_reply == "Done."


@pytest.mark.asyncio
async def test_two_actions_on_different_languages_keep_both_changes(
    isolated_workspace, tmp_path
) -> None:
    pipeline = _record_pipeline(tmp_path, [], [])
    await pipeline._update_state(
        {
            state.PROJECT_ID: "proj-record",
            state.FINALIZED_POSTS: {"ko": {"digest": "digest-ko"}},
        }
    )
    both_have_read = asyncio.Barrier(2)

    async def slow_sign_off(language: str, tool_context: Any) -> dict[str, Any]:
        marks = dict(tool_context.state.get(state.FINALIZED_POSTS) or {})
        await both_have_read.wait()  # neither has seen the other's change
        marks[language] = {"digest": f"digest-{language}"}
        tool_context.state[state.FINALIZED_POSTS] = marks
        return {"status": "success"}

    async def undo_sign_off(language: str, tool_context: Any) -> dict[str, Any]:
        marks = dict(tool_context.state.get(state.FINALIZED_POSTS) or {})
        marks.pop(language, None)
        tool_context.state[state.FINALIZED_POSTS] = marks
        return {"status": "success"}

    await asyncio.gather(
        pipeline.act(slow_sign_off, "en"), pipeline.act(slow_sign_off, "ja")
    )
    marks = (await pipeline.session_state())[state.FINALIZED_POSTS]
    assert sorted(marks) == ["en", "ja", "ko"]  # B-02: one of them used to vanish

    # Removing an entry removes that entry and nothing else.
    await pipeline.act(undo_sign_off, "ko")
    marks = (await pipeline.session_state())[state.FINALIZED_POSTS]
    assert sorted(marks) == ["en", "ja"]


def test_only_what_the_author_changed_is_written_over_the_record() -> None:
    base = {state.SLIDE_PLANS: {"ko": "plan 1", "en": "plan 1"}, state.SEED_TITLE: "a"}
    fresh = {state.SLIDE_PLANS: {"ko": "plan 2", "en": "plan 1", "ja": "plan 1"}}
    delta = {
        state.SLIDE_PLANS: {"ko": "plan 1", "en": "plan 3"},  # 'en' changed
        state.SEED_TITLE: "b",  # not a per-language dictionary: written as given
        state.PUBLICATIONS: None,  # cleared as a whole
    }
    assert merge_delta(base, fresh, delta) == {
        state.SLIDE_PLANS: {"ko": "plan 2", "en": "plan 3", "ja": "plan 1"},
        state.SEED_TITLE: "b",
        state.PUBLICATIONS: None,
    }


@pytest.mark.asyncio
async def test_a_write_that_finds_the_record_changed_reads_it_again_and_writes_once_more(
    isolated_workspace, tmp_path, monkeypatch
) -> None:
    pipeline = _record_pipeline(tmp_path, [], [])
    await pipeline._update_state({state.FINALIZED_POSTS: {"ko": {"digest": "k"}}})
    real_append = pipeline._sessions.append_event
    refusals = {"left": 1, "seen": 0}

    async def append_event(session, event):
        # Something outside the lock wrote between the read and the write.
        if refusals["left"]:
            refusals["left"] -= 1
            refusals["seen"] += 1
            raise StaleSessionError("the session changed in storage")
        return await real_append(session, event)

    monkeypatch.setattr(pipeline._sessions, "append_event", append_event)
    await pipeline.act(sign_off, "en")
    assert refusals["seen"] == 1
    marks = (await pipeline.session_state())[state.FINALIZED_POSTS]
    assert sorted(marks) == ["en", "ko"]

    # It is tried once more, not for ever: a second refusal is reported.
    refusals["left"] = 2
    with pytest.raises(StaleSessionError):
        await pipeline.act(sign_off, "ja")
