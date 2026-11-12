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

from app.notices import Notice, join, notice, parts_of, restore
from app.observability import log_activity, log_event, root_span
from app.pipeline import ArticlePipeline, describe_source
from app.redaction import redact_credentials
from app.settings import LANGUAGES, workspace_dir
from app.tools.articles import title_of_draft

MAIN = "main"
SLIDES = "slides"
LANES = (MAIN, SLIDES)
MAX_LOG_LINES = 400
MAX_LISTED_RUNS = 30
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")

# One sentence per lane: it says that the lane works and which lane it is.
# Like every sentence for the page it carries its code (app/notices.py).
BUSY = {lane: notice(f"lane_busy_{lane}") for lane in LANES}
INTERRUPTED = {lane: notice(f"interrupted_{lane}") for lane in LANES}

Work = Callable[[], Awaitable[str | None]]


class LaneBusy(Exception):
    """An action was asked of a lane that is working."""

    def __init__(self, lane: str) -> None:
        super().__init__(BUSY[lane])
        self.lane = lane
        self.notice = BUSY[lane]


class UnknownRun(Exception):
    """No run of this id is held by this process or recorded on this server."""

    def __init__(self) -> None:
        self.notice = notice("unknown_run")
        super().__init__(self.notice)


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
            # The same sentences in parts, each with the code the page
            # looks up to show it in the interface language (WEB-13).
            "errors": parts_of(self.error),
            "notices": parts_of(self.notice),
        }


def _lane_record(lane: Lane) -> dict[str, Any]:
    """What the run record keeps of a lane. The parts of a notice that has
    codes are kept with it, so it is still translated after a restart."""
    kept: dict[str, Any] = {
        "busy": lane.busy,
        "error": lane.error,
        "notice": lane.notice,
    }
    if isinstance(lane.notice, Notice):
        kept["notice_parts"] = parts_of(lane.notice)
    return kept


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
    # The length and the audience of the talk, as the run options said them.
    # With the two above they are the options of the run, kept in its record
    # so that slides a server stop left unmade can be asked for again as they
    # were asked for (RUN-4).
    talk_minutes: int = 0
    audience: str = ""
    log: list[str] = field(default_factory=list)
    lanes: dict[str, Lane] = field(default_factory=_lanes)
    reply: str | None = None  # the agent's answer to "Ask the agent for a change"
    # What "Recent sources" shows of this run (WEB-14). It is in the record so
    # that the list is made from the records alone: the kind and the name
    # people know it by from what was handed in, the title and the length
    # when the source is stored.
    title: str = ""
    source_kind: str = ""
    source_label: str = ""
    source_chars: int | None = None
    hidden: bool = False  # taken off the list by a person; its files stay

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
        return join(*(lane.notice for lane in self.lanes.values()))

    @property
    def options(self) -> dict[str, Any]:
        """What this run was asked to make of slides: for which languages,
        how long, for whom, and whether with decks."""
        return {
            "minutes": self.talk_minutes,
            "audience": self.audience,
            "languages": list(self.talk_languages),
            "decks": self.wants_decks,
        }

    def add_log(self, line: str) -> None:
        line = redact_credentials(line)
        self.log.append(line)
        del self.log[:-MAX_LOG_LINES]
        with (jobs_dir() / f"{self.id}.log").open("a", encoding="utf-8") as file:
            file.write(json.dumps(line, ensure_ascii=False) + "\n")
        # The same line, with the run id, in the structured log (OPS-1).
        log_activity(line, self.id)

    def source_stored(self, title: str, chars: int) -> None:
        """The source document is stored: its title and length are known."""
        self.title, self.source_chars = title, chars
        self.save()

    def save(self) -> None:
        """Write the record that lets this run be opened after a restart."""
        record = {
            "id": self.id,
            "session_id": self.pipeline.session_id,
            "created": self.created,
            "source_text": self.source_text,
            "title": self.title,
            "source_kind": self.source_kind,
            "source_label": self.source_label,
            "source_chars": self.source_chars,
            "hidden": self.hidden,
            "options": self.options,
            "busy": self.busy,
            "lanes": {name: _lane_record(lane) for name, lane in self.lanes.items()},
            "reply": self.reply,
        }
        path = jobs_dir() / f"{self.id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)


JOBS: dict[str, Job] = {}


def new_job(
    talk_languages: list[str],
    wants_decks: bool,
    source_text: str = "",
    talk_minutes: int = 0,
    audience: str = "",
) -> Job:
    job = Job(
        id=uuid.uuid4().hex,
        pipeline=ArticlePipeline(),
        source_text=source_text,
        talk_languages=talk_languages,
        wants_decks=wants_decks,
        talk_minutes=talk_minutes,
        audience=audience,
    )
    job.source_kind, job.source_label, _ = describe_source(source_text)
    job.pipeline.log = job.add_log
    job.pipeline.source_stored = job.source_stored
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


def _recorded_options(record: dict[str, Any]) -> dict[str, Any]:
    """The options of a record. One written before they were kept together
    has the languages and the decks only: its length and audience are not
    known here (the session has them, see ``RUN_OPTIONS``)."""
    kept = record.get("options")
    if not isinstance(kept, dict):
        kept = {
            "languages": record.get("talk_languages"),
            "decks": record.get("wants_decks"),
        }
    minutes = kept.get("minutes")
    return {
        "minutes": minutes if _chars(minutes) is not None and 0 <= minutes <= 60 else 0,
        "audience": str(kept.get("audience") or "")[:200],
        "languages": [
            code
            for code in dict.fromkeys(kept.get("languages") or [])
            if code in LANGUAGES
        ],
        "decks": bool(kept.get("decks")),
    }


def reopen(job_id: str) -> Job | None:
    """Bring back a run this process does not hold, from its record on disk."""
    path = jobs_dir() / f"{job_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        options = _recorded_options(record)
        job = Job(
            id=job_id,
            pipeline=ArticlePipeline(session_id=record["session_id"]),
            created=float(record.get("created") or 0),
            source_text=str(record.get("source_text") or ""),
            talk_languages=options["languages"],
            wants_decks=options["decks"],
            talk_minutes=options["minutes"],
            audience=options["audience"],
            reply=record.get("reply"),
            # A record written before these fields were kept lacks them.
            title=str(record.get("title") or ""),
            source_kind=str(record.get("source_kind") or ""),
            source_label=str(record.get("source_label") or ""),
            source_chars=_chars(record.get("source_chars")),
            hidden=bool(record.get("hidden")),
        )
        recorded = _recorded_lanes(record)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    job.pipeline.log = job.add_log
    job.pipeline.source_stored = job.source_stored
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
        lane.error = was.get("error")
        lane.notice = restore(was.get("notice"), was.get("notice_parts"))
        if was.get("busy"):
            # Whatever was running died with the old process, and an approval
            # the agent was waiting for is gone with it.
            lane.notice = join(INTERRUPTED[name], lane.notice)
            interrupted = True
    if interrupted:
        job.save()  # said once, not on every reopening
    JOBS[job_id] = job
    return job


def _chars(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def recent() -> list[dict[str, Any]]:
    """The runs "Recent sources" lists, newest first, from the records alone.

    No run is opened and no session is read for this (WEB-14): an entry says
    what its record says. A record written before the title, the kind and the
    length were kept is listed with what it has; the kind, its name and the
    address are read from what was handed in, which every record keeps, and
    so are the title and the length of a draft. The source text itself is
    not part of the list.
    """
    entries: list[dict[str, Any]] = []
    for path in jobs_dir().glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            job_id = str(record.get("id") or "")
            source_text = str(record.get("source_text") or "")
            if not _JOB_ID.match(job_id) or path.stem != job_id:
                continue
            if record.get("hidden") or not source_text.strip():
                continue
            kind, label, address = describe_source(source_text)
            title, chars = record.get("title"), _chars(record.get("source_chars"))
            if kind == "draft":
                # A draft is its own source: what an older record lacks is
                # in the text the record keeps.
                title = title or title_of_draft(source_text, fallback="")
                chars = len(source_text) if chars is None else chars
            entries.append(
                {
                    "job_id": job_id,
                    "created": float(record.get("created") or 0),
                    "title": str(title or ""),
                    "kind": str(record.get("source_kind") or kind),
                    "label": str(record.get("source_label") or label),
                    "chars": chars,
                    "address": address,
                }
            )
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    entries.sort(key=lambda entry: entry["created"], reverse=True)
    return entries[:MAX_LISTED_RUNS]


def hide(job_id: str) -> None:
    """Take a run off the list of recent sources. Its record, its log, its
    session and its files stay, and its address still opens it."""
    job = get_job(job_id)
    job.hidden = True
    job.save()


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
        ready, said = await job.pipeline.run_posts(source_text)
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
                said = join(said, notice("slides_not_started"), busy.notice)
        return said

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
        return join(
            notice("approved_slides_not_started"),
            busy.notice,
            notice("ask_for_slides_again"),
        )
    return None
