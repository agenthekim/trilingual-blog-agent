"""Runs as the page knows them, and the two lanes each run works in.

A run is one article in one agent session (``ArticlePipeline``) plus what is
not in the session: what was asked for, the activity log, and what each lane
is doing. A run has two lanes (RUN-1):

- ``main``: the source document, localization, the posts (save, finalize,
  post, register an address) and requests to the agent;
- ``slides``: slide plans, decks, PDF export and deck text sync.

One action runs at a time in a lane, and the two lanes run at the same time,
so posts can be reviewed and posted while slides are made. Every action a
person starts ends as done or as a notice that the page shows (RUN-3): nothing
is started here whose result nobody waits for.

This module knows nothing about HTTP. The web layer turns ``LaneBusy`` and
``UnknownRun`` into answers.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opentelemetry.trace import StatusCode

from app.observability import log_event, root_span
from app.pipeline import ArticlePipeline
from app.redaction import redact_credentials
from app.settings import LANGUAGES, workspace_dir

MAIN = "main"
SLIDES = "slides"
LANES = (MAIN, SLIDES)
MAX_LOG_LINES = 400
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")

# One sentence per lane: it says that the lane works and which lane it is.
BUSY = {
    MAIN: (
        "The main lane (posts and requests to the agent) is still working. "
        "Try again when it has finished."
    ),
    SLIDES: (
        "The slides lane (plans, decks and PDF) is still working. "
        "Try again when it has finished."
    ),
}
INTERRUPTED = {
    MAIN: (
        "The server stopped while this run was working. What was finished is "
        "below; start a new run for the rest."
    ),
    SLIDES: (
        "The server stopped while the slides of this run were being made. "
        "What was finished is below; ask for the missing slides again."
    ),
}

Work = Callable[[], Awaitable[str | None]]


class LaneBusy(Exception):
    """An action was asked of a lane that is working."""

    def __init__(self, lane: str) -> None:
        super().__init__(BUSY[lane])
        self.lane = lane


class UnknownRun(Exception):
    """No run of this id is held by this process or recorded on this server."""

    def __init__(self) -> None:
        super().__init__("Unknown run. Its record is not on this server.")


def jobs_dir() -> Path:
    folder = workspace_dir() / ".jobs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@dataclass
class Lane:
    """What one lane of a run is doing and how its last action ended."""

    busy: bool = False
    action: str | None = None  # what is running, by name
    error: str | None = None  # the last action failed: why
    notice: str | None = None  # the last action ended: what did not happen
    task: asyncio.Task | None = None  # held so the task is not garbage-collected

    def public(self) -> dict[str, Any]:
        return {
            "busy": self.busy,
            "action": self.action,
            "error": self.error,
            "notice": self.notice,
        }


def _lanes() -> dict[str, Lane]:
    return {name: Lane() for name in LANES}


@dataclass
class Job:
    """One run as the page knows it: the pipeline plus what is not in the session."""

    id: str
    pipeline: ArticlePipeline
    created: float = field(default_factory=time.time)
    # What was asked for, so progress is judged against it after a reload too.
    source_text: str = ""
    talk_languages: list[str] = field(default_factory=list)
    wants_decks: bool = False
    log: list[str] = field(default_factory=list)
    lanes: dict[str, Lane] = field(default_factory=_lanes)
    reply: str | None = None  # the agent's answer to "Ask the agent for a change"

    @property
    def busy(self) -> bool:
        """Whether anything at all is running for this run."""
        return any(lane.busy for lane in self.lanes.values())

    @property
    def error(self) -> str | None:
        return (
            " ".join(lane.error for lane in self.lanes.values() if lane.error) or None
        )

    @property
    def notice(self) -> str | None:
        return (
            " ".join(lane.notice for lane in self.lanes.values() if lane.notice) or None
        )

    def add_log(self, line: str) -> None:
        line = redact_credentials(line)
        self.log.append(line)
        del self.log[:-MAX_LOG_LINES]
        with (jobs_dir() / f"{self.id}.log").open("a", encoding="utf-8") as file:
            file.write(json.dumps(line, ensure_ascii=False) + "\n")

    def save(self) -> None:
        """Write the record that lets this run be opened after a restart."""
        record = {
            "id": self.id,
            "session_id": self.pipeline.session_id,
            "created": self.created,
            "source_text": self.source_text,
            "talk_languages": self.talk_languages,
            "wants_decks": self.wants_decks,
            "busy": self.busy,
            "lanes": {
                name: {"busy": lane.busy, "error": lane.error, "notice": lane.notice}
                for name, lane in self.lanes.items()
            },
            "reply": self.reply,
        }
        path = jobs_dir() / f"{self.id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)


JOBS: dict[str, Job] = {}


def new_job(talk_languages: list[str], wants_decks: bool, source_text: str = "") -> Job:
    job = Job(
        id=uuid.uuid4().hex,
        pipeline=ArticlePipeline(),
        source_text=source_text,
        talk_languages=talk_languages,
        wants_decks=wants_decks,
    )
    job.pipeline.log = job.add_log
    JOBS[job.id] = job
    job.save()
    return job


def _recorded_lanes(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The lanes of a record. One written before there were lanes had one
    busy flag, one error and one notice: they are the main lane's."""
    lanes = record.get("lanes")
    if isinstance(lanes, dict):
        return {name: dict(lanes.get(name) or {}) for name in LANES}
    return {
        MAIN: {
            "busy": record.get("busy"),
            "error": record.get("error"),
            "notice": record.get("notice"),
        },
        SLIDES: {},
    }


def reopen(job_id: str) -> Job | None:
    """Bring back a run this process does not hold, from its record on disk."""
    path = jobs_dir() / f"{job_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        job = Job(
            id=job_id,
            pipeline=ArticlePipeline(session_id=record["session_id"]),
            created=float(record.get("created") or 0),
            source_text=str(record.get("source_text") or ""),
            talk_languages=[
                code for code in record.get("talk_languages") or [] if code in LANGUAGES
            ],
            wants_decks=bool(record.get("wants_decks")),
            reply=record.get("reply"),
        )
        recorded = _recorded_lanes(record)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    job.pipeline.log = job.add_log
    log = jobs_dir() / f"{job_id}.log"
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines()[-MAX_LOG_LINES:]:
            try:
                job.log.append(str(json.loads(line)))
            except ValueError:
                continue
    interrupted = False
    for name, lane in job.lanes.items():
        was = recorded[name]
        lane.error, lane.notice = was.get("error"), was.get("notice")
        if was.get("busy"):
            # Whatever was running died with the old process, and an approval
            # the agent was waiting for is gone with it.
            lane.notice = " ".join(filter(None, [INTERRUPTED[name], lane.notice]))
            interrupted = True
    if interrupted:
        job.save()  # said once, not on every reopening
    JOBS[job_id] = job
    return job


def get_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None and _JOB_ID.match(job_id):
        job = reopen(job_id)
    if job is None:
        raise UnknownRun()
    return job


@contextmanager
def claim(job: Job, lane: str, what: str) -> Iterator[None]:
    """Hold a lane for an action whose result the caller itself waits for.

    One thing at a time per lane: claim it, or say that the lane is working.
    A request that is turned away does not clear a flag it did not set.
    The action runs inside a trace of its own (OPS-1).
    """
    state = job.lanes[lane]
    if state.busy:
        raise LaneBusy(lane)
    state.busy, state.action = True, what
    try:
        with root_span(f"run.{lane}.{what}", run_id=job.id, lane=lane):
            yield
    finally:
        state.busy, state.action = False, None


def start(job: Job, lane: str, work: Work, what: str) -> None:
    """Run ``work`` in the background as the one action of its lane.

    ``work`` returns None when it did what was asked, or a sentence for the
    page saying what did not happen. Whatever it does, the lane ends free
    and with one of three outcomes the page shows: done, a notice, or an
    error (RUN-3). The action is one trace: its root span is opened here, and
    what runs under it, and what it logs, belongs to that trace (OPS-1).
    """
    state = job.lanes[lane]
    if state.busy:
        raise LaneBusy(lane)
    state.busy, state.action, state.error, state.notice = True, what, None, None
    if lane == MAIN:
        job.reply = None
    job.save()

    async def runner() -> None:
        with root_span(f"run.{lane}.{what}", run_id=job.id, lane=lane) as span:
            try:
                state.notice = await work()
            except Exception as exc:  # shown to the user; details go to the log
                state.error = (
                    redact_credentials(str(exc))[:500] or "It failed unexpectedly."
                )
                span.set_status(StatusCode.ERROR, type(exc).__name__)
                log_event(
                    "web ui action failed",
                    event_phase="WEB_UI",
                    action=what,
                    lane=lane,
                    run_id=job.id,
                    error_type=type(exc).__name__,
                )
            finally:
                state.busy, state.action = False, None
                job.save()

    state.task = asyncio.create_task(runner())


def start_run(
    job: Job,
    source_text: str,
    talk_minutes: int,
    talk_languages: list[str],
    audience: str,
) -> None:
    """Start a run: the posts in the main lane, then the slides in theirs.

    The slides are started when the posts are there, and only then; from
    that moment the main lane is free for reviewing, finalizing and posting.
    """

    async def posts() -> str | None:
        await job.pipeline.mark_surface(talk_minutes, audience)
        ready, notice = await job.pipeline.run_posts(source_text)
        if ready and talk_minutes > 0 and talk_languages:
            try:
                start(
                    job,
                    SLIDES,
                    lambda: job.pipeline.run_slides(
                        talk_languages, talk_minutes, audience, job.wants_decks
                    ),
                    "run_slides",
                )
            except LaneBusy as busy:
                notice = " ".join(
                    filter(None, [notice, f"No slides were started. {busy}"])
                )
        return notice

    start(job, MAIN, posts, "run_posts")


def start_slides(
    job: Job,
    languages: list[str],
    minutes: int | dict[str, int],
    audience: str,
    what: str = "slides",
) -> None:
    """Start a slide request a person approved, in the slides lane (INV-6).

    Plans and decks for all the languages asked for are one action of the
    lane. Raises ``LaneBusy`` when the lane is working.
    """
    start(
        job,
        SLIDES,
        lambda: job.pipeline.make_slides(languages, minutes, audience, True),
        what,
    )


async def start_requested_slides(job: Job) -> str | None:
    """After an agent turn: start the slide request a person approved in it.

    The agent's request tool only records the approved request; running it is
    this code's job, in the slides lane. Returns None when there was nothing
    to start or it was started, else a sentence saying that it was not.
    """
    request = await job.pipeline.take_slide_request()
    if request is None:
        return None
    try:
        start_slides(
            job,
            list(request["languages"]),
            dict(request.get("minutes") or {}),
            str(request.get("audience") or ""),
            "slides_request",
        )
    except LaneBusy as busy:
        return (
            "The slides that were approved were not started. "
            f"{busy} Then ask for them again."
        )
    return None
