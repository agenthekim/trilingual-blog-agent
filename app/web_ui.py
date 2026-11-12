"""The web front end: hand in a URL or a draft, get every artifact, approve posts.

Run it with::

    uv run python -m app.web_ui                 # http://127.0.0.1:8502
    uv run python -m app.web_ui --host 0.0.0.0  # reachable from other machines

The server has no login. Anyone who can reach it can run the agent and approve
posts with the configured platform credentials, so it binds to localhost unless
told otherwise.

A run survives a restart of the server. Its session is in the session store
(a SQLite file locally) and a small record of the run, with its activity log,
is in ``<workspace>/.jobs``; opening the run's address again shows it as it
was. A run that was working when the server stopped is shown as interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # the environment is then expected to be set already
    pass

import httpx
import markdown as markdown_lib
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field, ValidationError, field_validator

from app import policy, secrets
from app.google_slides import SlidesError, granted_account, mermaid_preview_url
from app.observability import log_event
from app.pipeline import (
    ArticlePipeline,
    overview,
    post_progress,
    progress,
    project_file,
)
from app.redaction import redact_credentials
from app.schemas import DevtoDraftInput, QiitaDraftInput
from app.settings import (
    LANGUAGE_NAMES,
    LANGUAGES,
    PLATFORM_FOR_LANGUAGE,
    render_mermaid_enabled,
    workspace_dir,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_LOG_LINES = 400
# Generated files are shown in the browser. They come from article text, so no
# script in them may run and nothing may be loaded except images.
_VIEW_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "img-src 'self' https: data:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}
_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font:16px/1.7 system-ui,sans-serif;max-width:820px;margin:2rem auto;
padding:0 1rem;color:#1f2328}}pre{{background:#f6f8fa;padding:12px;overflow:auto;
border-radius:6px}}code{{font-family:ui-monospace,monospace;font-size:.9em}}
table{{border-collapse:collapse}}td,th{{border:1px solid #d0d7de;padding:6px 10px}}
img{{max-width:100%}}.diagram{{margin:1rem 0}}.diagram img{{display:block;margin:0 auto;
max-height:80vh}}.diagram summary{{font-size:.85em;color:#57606a;cursor:pointer}}.bar{{font-size:.85em;color:#57606a;border-bottom:1px solid
#d0d7de;padding-bottom:.5rem;margin-bottom:1rem}}</style></head><body>
<div class="bar">{title} &middot; {edit}<a href="?raw=1">raw text</a></div>{body}</body></html>"""

web = FastAPI(title="Trilingual Blog Agent", docs_url=None, redoc_url=None)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
# Names this server answers to. With no login, a page in the author's browser
# that points its own name at this machine would otherwise be served like the
# author. None means any name: set by main() when the operator binds the
# server to another address on purpose.
ALLOWED_HOSTS: frozenset[str] | None = LOOPBACK_HOSTS


@web.middleware("http")
async def only_its_own_name(request: Request, call_next: Any) -> Response:
    if ALLOWED_HOSTS is not None:
        host = request.headers.get("host", "").lower()
        name = host if host.endswith("]") else host.rsplit(":", 1)[0]
        if name not in ALLOWED_HOSTS:
            return PlainTextResponse(
                "This server answers only to its own address.", status_code=400
            )
    return await call_next(request)


BUSY = "The agent is still working. Try again when it has finished."
INTERRUPTED = (
    "The server stopped while this run was working. What was finished is "
    "below; start a new run for the rest."
)
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
Language = Literal["ko", "en", "ja"]


def _jobs_dir() -> Path:
    folder = workspace_dir() / ".jobs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@dataclass
class Job:
    """One run as the page knows it: the pipeline plus what is not in the session."""

    id: str
    pipeline: ArticlePipeline
    created: float = field(default_factory=time.time)
    # What was asked for, so progress is judged against it after a reload too.
    talk_languages: list[str] = field(default_factory=list)
    wants_decks: bool = False
    log: list[str] = field(default_factory=list)
    busy: bool = False
    error: str | None = None
    notice: str | None = None
    reply: str | None = None  # the agent's answer to "Ask the agent for a change"
    task: asyncio.Task | None = None  # held so the task is not garbage-collected

    def add_log(self, line: str) -> None:
        line = redact_credentials(line)
        self.log.append(line)
        del self.log[:-MAX_LOG_LINES]
        with (_jobs_dir() / f"{self.id}.log").open("a", encoding="utf-8") as file:
            file.write(json.dumps(line, ensure_ascii=False) + "\n")

    def save(self) -> None:
        """Write the record that lets this run be opened after a restart."""
        record = {
            "id": self.id,
            "session_id": self.pipeline.session_id,
            "created": self.created,
            "talk_languages": self.talk_languages,
            "wants_decks": self.wants_decks,
            "busy": self.busy,
            "error": self.error,
            "notice": self.notice,
            "reply": self.reply,
        }
        path = _jobs_dir() / f"{self.id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)


JOBS: dict[str, Job] = {}


def _new_job(talk_languages: list[str], wants_decks: bool) -> Job:
    job = Job(
        id=uuid.uuid4().hex,
        pipeline=ArticlePipeline(),
        talk_languages=talk_languages,
        wants_decks=wants_decks,
    )
    job.pipeline.log = job.add_log
    JOBS[job.id] = job
    job.save()
    return job


def _reopen(job_id: str) -> Job | None:
    """Bring back a run this process does not hold, from its record on disk."""
    path = _jobs_dir() / f"{job_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        job = Job(
            id=job_id,
            pipeline=ArticlePipeline(session_id=record["session_id"]),
            created=float(record.get("created") or 0),
            talk_languages=[
                code for code in record.get("talk_languages") or [] if code in LANGUAGES
            ],
            wants_decks=bool(record.get("wants_decks")),
            error=record.get("error"),
            notice=record.get("notice"),
            reply=record.get("reply"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    job.pipeline.log = job.add_log
    log = _jobs_dir() / f"{job_id}.log"
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines()[-MAX_LOG_LINES:]:
            try:
                job.log.append(str(json.loads(line)))
            except ValueError:
                continue
    if record.get("busy"):
        # Whatever was running died with the old process, and an approval the
        # agent was waiting for is gone with it.
        job.notice = " ".join(filter(None, [INTERRUPTED, job.notice]))
        job.save()
    JOBS[job_id] = job
    return job


def _job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None and _JOB_ID.match(job_id):
        job = _reopen(job_id)
    if job is None:
        raise HTTPException(404, "Unknown run. Its record is not on this server.")
    return job


@contextmanager
def _exclusive(job: Job) -> Iterator[None]:
    """One thing at a time per run: claim it, or say that the agent is working."""
    if job.busy:
        raise HTTPException(409, BUSY)
    job.busy = True
    try:
        yield
    finally:
        job.busy = False


def _start(job: Job, work: Any, what: str) -> None:
    """Run ``work`` in the background; one action at a time per job.

    ``work`` returns None when it did what was asked, or a sentence for the
    page saying what did not happen.
    """
    if job.busy:
        raise HTTPException(409, BUSY)
    job.busy, job.error, job.notice, job.reply = True, None, None, None
    job.save()

    async def runner() -> None:
        try:
            job.notice = await work()
        except Exception as exc:  # shown to the user; details go to the log
            job.error = redact_credentials(str(exc))[:500] or "It failed unexpectedly."
            log_event(
                "web ui action failed",
                event_phase="WEB_UI",
                action=what,
                error_type=type(exc).__name__,
            )
        finally:
            job.busy = False
            job.save()

    job.task = asyncio.create_task(runner())


class StartRequest(BaseModel):
    text: str = Field(min_length=10, max_length=80_000)
    talk_minutes: int = Field(default=5, ge=0, le=60)
    talk_languages: list[Language] = Field(default_factory=lambda: list(LANGUAGES))
    audience: str = Field(default="software engineers", max_length=200)
    # Set by the form when a length and a language were chosen: the person's
    # approval, in advance, to create one Google Slides file per language in
    # their own Drive from the slide plans of this run.
    create_deck: bool = False

    @field_validator("talk_languages")
    @classmethod
    def _each_once(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class DraftRequest(BaseModel):
    platform: Literal["qiita", "devto"]
    tags: list[str] = Field(max_length=5)
    # True only when a person pressed the approve button for this very post.
    approved: bool = False
    # True only when that person chose "public" in the approval dialog;
    # otherwise the post is a non-public draft.
    public: bool = False


class DeckRequest(BaseModel):
    language: Language
    # True only when a person pressed the approve button for this deck.
    approved: bool = False


class DeckPdfRequest(BaseModel):
    language: Language


class UrlRequest(BaseModel):
    platform: Literal["naver_blog", "qiita", "devto"]
    url: str = Field(pattern=r"^https://\S+$", max_length=500)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=80_000)


class EditRequest(BaseModel):
    markdown: str = Field(min_length=1, max_length=200_000)
    title: str | None = Field(default=None, max_length=120)


class FinalizeRequest(BaseModel):
    # Sent when a person, having reviewed the post, presses Finalize.
    language: Language
    # True only when that person was shown the credential-looking findings
    # and said they are not real credentials.
    acknowledge_findings: bool = False


class DeckSyncRequest(BaseModel):
    language: Language
    # push: the Markdown plan's text into the deck; pull: the deck's into the file.
    direction: Literal["push", "pull"]
    # False asks what would change. True only when a person, having seen
    # that, pressed the approve button.
    approved: bool = False
    # Names the set of changes the person was shown; sent back with the approval.
    change_id: str | None = Field(default=None, max_length=64)


class ApprovalRequest(BaseModel):
    # Which pending request the person answered; an answer goes to that one only.
    id: str = Field(min_length=1, max_length=200)
    confirmed: bool


_MERMAID_BLOCK = re.compile(r"^```mermaid[ \t]*\n(.*?)^```[ \t]*$", re.M | re.S)
_DIAGRAM_ID = re.compile(r"^[0-9a-f]{32}$")
# The drawing service refuses requests that arrive together, so one at a time.
_DIAGRAM_LOCK = asyncio.Lock()


def _diagram_id(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:32]


def _diagrams_as_images(markdown_text: str, job_id: str) -> str:
    """Show each Mermaid block of a previewed file as a drawing, source below it.

    The page may run no script, so the drawing is an image. This server gets
    it from mermaid.ink, the same service the slide images come from, keeps
    it, and serves it itself (see ``view_diagram``), and only when
    BLOG_AGENT_RENDER_MERMAID allows sending diagrams there. A diagram that
    cannot be drawn leaves its source, which is always there.
    """
    if not render_mermaid_enabled():
        return markdown_text

    def figure(match: re.Match[str]) -> str:
        code = match.group(1)
        return (
            '<div class="diagram"><img loading="lazy" alt="Diagram (see its source '
            f'below)" src="/api/jobs/{job_id}/diagrams/{_diagram_id(code)}">'
            "<details><summary>Diagram source</summary><pre><code>"
            f"{html.escape(code)}</code></pre></details></div>"
        )

    return _MERMAID_BLOCK.sub(figure, markdown_text)


async def _draw_diagram(code: str) -> bytes | None:
    """The diagram as SVG from mermaid.ink, or None when it cannot be drawn.

    A first drawing takes the service a few seconds, and it answers 503 when
    asked too fast; both are waited out here instead of in the browser.
    """
    async with _DIAGRAM_LOCK, httpx.AsyncClient(timeout=30) as client:
        for attempt in range(1, 5):
            try:
                response = await client.get(mermaid_preview_url(code))
            except httpx.HTTPError:
                return None
            if response.status_code == 200 and "svg" in response.headers.get(
                "content-type", ""
            ):
                return response.content
            if response.status_code not in (429, 503):
                return None
            await asyncio.sleep(1.5 * attempt)
    return None


def _static(name: str) -> FileResponse:
    return FileResponse(
        STATIC_DIR / name, media_type="text/html", headers={"Cache-Control": "no-store"}
    )


@web.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return _static("index.html")


@web.get("/edit", response_class=HTMLResponse)
async def file_editor() -> FileResponse:
    """The full-page editor. Which file it edits is in the address; the page
    loads the text and saves it through the same API as everything else, and
    shows it only as text in a text box, never as HTML."""
    return _static("edit.html")


_CONFIG_SECONDS = 60
_config_cache: tuple[float, dict[str, Any]] | None = None


def _configured(secret_name: str) -> bool:
    try:
        return bool(secrets.get_secret(secret_name))
    except secrets.MissingSecretError:
        return False


@web.get("/api/config")
async def config() -> dict[str, Any]:
    """What the Settings dialog shows: the fixed mapping and what is set up.

    Only whether each credential is configured is reported, never its name or
    value, and the address of the Google account that decks go to, so that a
    person approving a deck knows whose Drive it lands in. Looking these up
    can mean a call to Secret Manager, so the answer is kept for a minute.
    """
    global _config_cache
    if _config_cache and time.monotonic() - _config_cache[0] < _CONFIG_SECONDS:
        return _config_cache[1]
    names = {
        "qiita": secrets.QIITA_ACCESS_TOKEN,
        "devto": secrets.DEVTO_API_KEY,
        "google_slides": secrets.GOOGLE_SLIDES_OAUTH,
    }
    ready = dict(
        zip(
            names,
            await asyncio.gather(
                *(asyncio.to_thread(_configured, name) for name in names.values())
            ),
            strict=True,
        )
    )
    answer = {
        "platforms": [
            {
                "language": LANGUAGE_NAMES[language],
                "platform": policy.PLATFORM_NAMES[PLATFORM_FOR_LANGUAGE[language]],
                "how": "api"
                if PLATFORM_FOR_LANGUAGE[language] in policy.API_PLATFORMS
                else "paste",
                # Naver needs no credential: the person pastes the export.
                "configured": ready.get(PLATFORM_FOR_LANGUAGE[language], True),
            }
            for language in LANGUAGES
        ],
        "google_slides": ready["google_slides"],
        # Whose Drive receives the decks: shown before a deck is approved.
        "slides_account": await asyncio.to_thread(granted_account)
        if ready["google_slides"]
        else None,
        "diagrams": render_mermaid_enabled(),
    }
    _config_cache = (time.monotonic(), answer)
    return answer


@web.post("/api/jobs")
async def start_job(request: StartRequest) -> dict[str, str]:
    wants_slides = request.talk_minutes > 0 and bool(request.talk_languages)
    languages = list(request.talk_languages) if wants_slides else []
    job = _new_job(languages, wants_slides and request.create_deck)
    _start(
        job,
        lambda: job.pipeline.run_stages(
            request.text,
            request.talk_minutes,
            languages,
            request.audience,
            job.wants_decks,
        ),
        "run_stages",
    )
    return {"job_id": job.id}


@web.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    job = _job(job_id)
    current = await job.pipeline.session_state()
    return {
        "busy": job.busy,
        "error": job.error,
        "notice": job.notice,
        "reply": job.reply,
        "log": job.log,
        "approvals": job.pipeline.approvals,
        "overview": overview(current),
        "progress": progress(current, job.talk_languages, job.wants_decks),
        "post_progress": post_progress(current, job.talk_languages),
    }


_TAG_RULES = {
    "qiita": (QiitaDraftInput, "Qiita takes one to five tags without spaces."),
    "devto": (
        DevtoDraftInput,
        "DEV Community takes up to four tags, lowercase letters and digits only.",
    ),
}


@web.post("/api/jobs/{job_id}/draft")
async def post_draft(job_id: str, request: DraftRequest) -> dict[str, bool]:
    """Send a finalized post. The request is the approval of the person who pressed
    the button for this platform, with the draft-or-public choice they made."""
    job = _job(job_id)
    if not request.approved:
        raise HTTPException(400, "A post is sent only with a person's approval.")
    model, rule = _TAG_RULES[request.platform]
    try:
        model(title="title", tags=request.tags)
    except ValidationError as exc:
        raise HTTPException(400, rule) from exc
    if job.busy:
        raise HTTPException(409, BUSY)
    # Said at once rather than after a round trip: finalize first, and so on.
    if refusal := policy.posting_refusal(
        await job.pipeline.session_state(), request.platform
    ):
        raise HTTPException(409, refusal.message)
    _start(
        job,
        lambda: job.pipeline.post(request.platform, request.tags, request.public),
        "post",
    )
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/deck")
async def create_deck(job_id: str, request: DeckRequest) -> dict[str, bool]:
    """Create the Google Slides deck of one language, approved by a person."""
    job = _job(job_id)
    if not request.approved:
        raise HTTPException(400, "A deck is created only with a person's approval.")
    _start(job, lambda: job.pipeline.create_deck(request.language), "create_deck")
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/pdf")
async def export_deck_pdf(job_id: str, request: DeckPdfRequest) -> dict[str, bool]:
    """Export one language's Google Slides deck as slides.<lang>.pdf (SLD-12)."""
    job = _job(job_id)
    _start(job, lambda: job.pipeline.export_pdf(request.language), "export_pdf")
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/finalize")
async def finalize_post(job_id: str, request: FinalizeRequest) -> dict[str, Any]:
    """Mark a post as final. Posting needs it; editing the title or text undoes it."""
    job = _job(job_id)
    with _exclusive(job):
        try:
            return await job.pipeline.finalize_post(
                request.language, request.acknowledge_findings
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@web.post("/api/jobs/{job_id}/deck/sync")
async def sync_deck(job_id: str, request: DeckSyncRequest) -> dict[str, Any]:
    """Compare a deck with its slide plan file, or bring one in line with the other.

    Without approval this only reads: it answers how many slides a push or a
    pull would change. With approval it changes text in the existing deck
    (push) or in the file (pull); pictures and layout are not touched.
    """
    job = _job(job_id)

    async def run() -> dict[str, Any]:
        try:
            return await job.pipeline.sync_deck_text(
                request.language,
                request.direction,
                apply=request.approved,
                change_id=request.change_id,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except SlidesError as exc:
            raise HTTPException(502, exc.message) from exc

    if not request.approved:
        if job.busy:
            raise HTTPException(409, BUSY)
        return await run()
    if not request.change_id:
        raise HTTPException(400, "Ask what would change before approving it.")
    log_event(
        "deck text sync approved in the web ui",
        event_phase="HUMAN_APPROVAL",
        language=request.language,
        direction=request.direction,
    )
    with _exclusive(job):  # nothing else may run while the deck is written
        return await run()


@web.post("/api/jobs/{job_id}/url")
async def record_url(job_id: str, request: UrlRequest) -> dict[str, bool]:
    job = _job(job_id)
    _start(
        job,
        lambda: job.pipeline.record_posted_url(request.platform, request.url),
        "record_url",
    )
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/message")
async def send_message(job_id: str, request: MessageRequest) -> dict[str, bool]:
    job = _job(job_id)

    async def work() -> str | None:
        dropped = len(job.pipeline.approvals)
        await job.pipeline.say(request.text)
        job.reply = job.pipeline.last_reply[:2000] or (
            "The agent finished without a reply; see the activity log."
        )
        if dropped:
            return (
                f"{dropped} request(s) the agent was waiting on were left "
                "unanswered by this message and are gone. Ask again for those."
            )
        return None

    _start(job, work, "message")
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/approval")
async def answer_approval(job_id: str, request: ApprovalRequest) -> dict[str, bool]:
    """Answer one request the agent is waiting on, and only that one."""
    job = _job(job_id)
    if not any(approval["id"] == request.id for approval in job.pipeline.approvals):
        raise HTTPException(409, "That approval request is no longer waiting.")

    async def work() -> str | None:
        declined = await job.pipeline.answer_approval(request.id, request.confirmed)
        job.reply = job.pipeline.last_reply[:2000] or None
        if request.confirmed and not job.pipeline.approvals:
            await job.pipeline.summarize()  # something may have been posted
        if declined:
            return (
                f"{declined} other request(s) the agent was waiting on were "
                "declined: one answer goes to one request. Ask again for those."
            )
        return None

    _start(job, work, "approval")
    return {"accepted": True}


@web.get("/api/jobs/{job_id}/files/{name}")
async def view_file(job_id: str, name: str, raw: int = 0) -> Any:
    job = _job(job_id)
    summary = await job.pipeline.overview()
    path = project_file(summary["project_id"], name)
    if path is None:
        raise HTTPException(404, "No such file for this article.")
    if path.suffix == ".pdf":
        return FileResponse(path, media_type="application/pdf", headers=_VIEW_HEADERS)
    text = path.read_text(encoding="utf-8")
    if raw or path.suffix not in (".md", ".html"):
        return PlainTextResponse(text, headers=_VIEW_HEADERS)
    if path.suffix == ".html":
        return HTMLResponse(text, headers=_VIEW_HEADERS)
    body = markdown_lib.markdown(
        _diagrams_as_images(text, job.id),
        extensions=["fenced_code", "tables", "sane_lists"],
    )
    # A file a person may change gets a link to the full-page editor.
    edit = (
        f'<a href="/edit?job={job.id}&amp;file={html.escape(name)}">edit</a> &middot; '
        if name in summary["editable"]
        else ""
    )
    return HTMLResponse(
        _PAGE.format(title=html.escape(name), edit=edit, body=body),
        headers=_VIEW_HEADERS,
    )


@web.get("/api/jobs/{job_id}/diagrams/{diagram_id}")
async def view_diagram(job_id: str, diagram_id: str) -> Response:
    """A Mermaid diagram of this article as an SVG image, drawn once and kept.

    Only diagrams that are in the article's own files are drawn: the id is a
    digest of the diagram source, looked up there, never the source itself.
    """
    job = _job(job_id)
    if not render_mermaid_enabled() or not _DIAGRAM_ID.match(diagram_id):
        raise HTTPException(404, "No such diagram.")
    kept = workspace_dir() / ".diagrams" / f"{diagram_id}.svg"
    if not kept.is_file():
        project_id = (await job.pipeline.overview())["project_id"]
        folder = workspace_dir() / project_id if project_id else None
        code = next(
            (
                block
                for path in sorted(folder.glob("*.md") if folder else [])
                for block in _MERMAID_BLOCK.findall(path.read_text(encoding="utf-8"))
                if _diagram_id(block) == diagram_id
            ),
            None,
        )
        if code is None:
            raise HTTPException(404, "No such diagram.")
        drawing = await _draw_diagram(code)
        if drawing is None:
            raise HTTPException(502, "The diagram could not be drawn.")
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_bytes(drawing)
    return Response(
        kept.read_bytes(),
        media_type="image/svg+xml",
        headers={**_VIEW_HEADERS, "Cache-Control": "private, max-age=86400"},
    )


@web.put("/api/jobs/{job_id}/files/{name}")
async def save_file(job_id: str, name: str, request: EditRequest) -> dict[str, Any]:
    job = _job(job_id)
    with _exclusive(job):
        try:
            return await job.pipeline.save_edit(name, request.markdown, request.title)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Web front end for the blog agent.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8502)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        # Bound to another address on purpose: it is then reached under names
        # this server cannot know, and anyone who reaches it can use it.
        global ALLOWED_HOSTS
        ALLOWED_HOSTS = None
    uvicorn.run(web, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
