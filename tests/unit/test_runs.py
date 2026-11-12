"""RUN-1 and RUN-3: the two lanes of a run, and how every started action ends."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest

from app import runs, web_ui

ROOT = Path(__file__).resolve().parents[2]


class FakePipeline:
    """A pipeline whose two parts end when the test lets them."""

    session_id = "session-of-the-fake"

    def __init__(self, ready: bool = True, notice: str | None = None) -> None:
        self.ready, self.notice = ready, notice
        self.slides_may_end = asyncio.Event()
        self.calls: list[Any] = []

    async def run_posts(self, source_text: str) -> tuple[bool, str | None]:
        self.calls.append(("posts", source_text))
        return self.ready, self.notice

    async def run_slides(
        self, languages: list[str], minutes: int, audience: str, create_deck: bool
    ) -> str | None:
        self.calls.append(("slides", languages, minutes, audience, create_deck))
        await self.slides_may_end.wait()
        return None


def _job(pipeline: Any = None) -> runs.Job:
    return runs.Job(id="a" * 32, pipeline=pipeline or FakePipeline())


async def _until(condition) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("it did not happen")


@pytest.mark.asyncio
async def test_every_started_action_ends_as_done_or_as_a_notice(
    isolated_workspace,
) -> None:
    job = _job()

    async def done() -> None:
        return None

    async def not_done() -> str:
        return "Nothing was posted on DEV Community. The API key was rejected."

    async def breaks() -> None:
        raise RuntimeError("the connection was reset")

    outcomes = []
    for work in (done, not_done, breaks):
        runs.start(job, runs.MAIN, work, work.__name__)
        lane = job.lanes[runs.MAIN]
        assert lane.busy and lane.action == work.__name__
        await lane.task  # the lane holds the task: somebody waits for it
        assert not lane.busy and lane.action is None
        outcomes.append((lane.notice, lane.error))
    assert outcomes == [
        (None, None),  # done
        ("Nothing was posted on DEV Community. The API key was rejected.", None),
        (None, "the connection was reset"),  # an error is shown too
    ]
    # The outcome is in the record, so a reload or a restart shows the same.
    record = json.loads((runs.jobs_dir() / f"{job.id}.json").read_text("utf-8"))
    assert record["lanes"]["main"] == {
        "busy": False,
        "error": "the connection was reset",
        "notice": None,
    }

    # An action whose result the request itself waits for gives the lane back
    # whether it ends well or not.
    with pytest.raises(ValueError), runs.claim(job, runs.SLIDES, "sync_deck"):
        assert job.lanes[runs.SLIDES].busy
        raise ValueError("There is no Google Slides deck for this language yet.")
    assert not job.lanes[runs.SLIDES].busy

    # Nothing in the web layer or in the runs is started and left alone: the
    # one place that creates a task keeps it in its lane.
    web_source = inspect.getsource(web_ui)
    assert not re.search(r"create_task|ensure_future|run_in_executor", web_source)
    assert inspect.getsource(runs).count("create_task(") == 1
    assert "state.task = asyncio.create_task(runner())" in inspect.getsource(runs)


@pytest.mark.asyncio
async def test_a_run_makes_the_posts_in_the_main_lane_and_then_the_slides_in_theirs(
    isolated_workspace,
) -> None:
    pipeline = FakePipeline(notice="The draft has no '# Title' line.")
    job = _job(pipeline)
    job.wants_decks = True
    runs.start_run(job, "# 글\n\n본문", 5, ["en", "ja"], "engineers")
    main, slides = job.lanes[runs.MAIN], job.lanes[runs.SLIDES]
    assert main.busy and not slides.busy

    await main.task
    # The posts are there: the main lane is free while the slides are made.
    assert not main.busy and main.notice == "The draft has no '# Title' line."
    assert slides.busy and slides.action == "run_slides" and job.busy
    with pytest.raises(runs.LaneBusy, match="slides lane"):
        runs.start(job, runs.SLIDES, pipeline.run_posts, "anything")

    async def post() -> None:
        return None

    runs.start(job, runs.MAIN, post, "post")  # a post goes out meanwhile
    await main.task
    assert slides.busy and main.notice is None

    pipeline.slides_may_end.set()
    await slides.task
    assert not job.busy and job.notice is None and job.error is None
    assert pipeline.calls == [
        ("posts", "# 글\n\n본문"),
        ("slides", ["en", "ja"], 5, "engineers", True),
    ]


@pytest.mark.asyncio
async def test_no_slides_are_started_when_the_posts_did_not_come_out(
    isolated_workspace,
) -> None:
    pipeline = FakePipeline(ready=False, notice="No English text was written.")
    job = _job(pipeline)
    runs.start_run(job, "# 글\n\n본문", 5, ["en"], "engineers")
    await job.lanes[runs.MAIN].task
    assert not job.busy and job.notice == "No English text was written."
    assert pipeline.calls == [("posts", "# 글\n\n본문")]

    # Nor when no slides were asked for.
    pipeline = FakePipeline()
    job = _job(pipeline)
    runs.start_run(job, "# 글\n\n본문", 0, [], "engineers")
    await job.lanes[runs.MAIN].task
    assert not job.busy and [call[0] for call in pipeline.calls] == ["posts"]


def test_a_record_written_before_there_were_lanes_is_still_opened(
    isolated_workspace,
) -> None:
    job_id = "b" * 32
    old = {
        "id": job_id,
        "session_id": "s-1",
        "created": 1.0,
        "source_text": "# 글",
        "talk_languages": ["en", "xx"],
        "wants_decks": True,
        "busy": True,
        "error": None,
        "notice": "The Naver Blog file was not written.",
        "reply": "Done.",
    }
    (runs.jobs_dir() / f"{job_id}.json").write_text(json.dumps(old), "utf-8")
    job = runs.get_job(job_id)
    assert job.talk_languages == ["en"] and job.reply == "Done."
    assert not job.busy
    assert job.lanes[runs.MAIN].notice == (
        runs.INTERRUPTED[runs.MAIN] + " The Naver Blog file was not written."
    )
    assert job.lanes[runs.SLIDES].public() == {
        "busy": False,
        "action": None,
        "error": None,
        "notice": None,
    }
    runs.JOBS.clear()

    with pytest.raises(runs.UnknownRun):
        runs.get_job("c" * 32)
