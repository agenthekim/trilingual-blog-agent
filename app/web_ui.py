"""A small web front end: paste a URL or a draft, get every artifact, approve posts.

Run it with::

    uv run python -m app.web_ui                 # http://127.0.0.1:8502
    uv run python -m app.web_ui --host 0.0.0.0  # reachable from other machines

The server has no login. Anyone who can reach it can run the agent and approve
posts with the configured platform credentials, so it binds to localhost unless
told otherwise. Jobs live in memory and are gone when the server stops; the
generated files stay in the workspace directory.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # the environment is then expected to be set already
    pass

import markdown as markdown_lib
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.observability import log_event
from app.pipeline import API_PLATFORMS, ArticlePipeline, project_file
from app.redaction import redact_credentials

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_LOG_LINES = 400
# Generated files are shown in the browser. They come from article text, so no
# script in them may run and nothing may be loaded except images.
_VIEW_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "img-src https: data:",
    "X-Content-Type-Options": "nosniff",
}
_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font:16px/1.7 system-ui,sans-serif;max-width:820px;margin:2rem auto;
padding:0 1rem;color:#1f2328}}pre{{background:#f6f8fa;padding:12px;overflow:auto;
border-radius:6px}}code{{font-family:ui-monospace,monospace;font-size:.9em}}
table{{border-collapse:collapse}}td,th{{border:1px solid #d0d7de;padding:6px 10px}}
img{{max-width:100%}}.bar{{font-size:.85em;color:#57606a;border-bottom:1px solid
#d0d7de;padding-bottom:.5rem;margin-bottom:1rem}}</style></head><body>
<div class="bar">{title} &middot; {edit}<a href="?raw=1">raw text</a></div>{body}</body></html>"""

web = FastAPI(title="Trilingual Blog Agent", docs_url=None, redoc_url=None)


@dataclass
class Job:
    id: str
    pipeline: ArticlePipeline
    log: list[str] = field(default_factory=list)
    busy: bool = False
    error: str | None = None
    notice: str | None = None
    task: asyncio.Task | None = None  # held so the task is not garbage-collected

    def add_log(self, line: str) -> None:
        self.log.append(redact_credentials(line))
        del self.log[:-MAX_LOG_LINES]


JOBS: dict[str, Job] = {}


class StartRequest(BaseModel):
    text: str = Field(min_length=10, max_length=80_000)
    talk_minutes: int = Field(default=5, ge=0, le=60)
    talk_language: Literal["ko", "en", "ja"] = "en"
    talk_languages: list[Literal["ko", "en", "ja"]] = Field(
        default_factory=lambda: ["en", "ko", "ja"]
    )
    audience: str = Field(default="software engineers", max_length=200)
    # Ticked by the person in the form: approval, in advance, to create
    # Google Slides files in their own Drive from the slide plans of this run.
    create_deck: bool = False


class DraftRequest(BaseModel):
    platform: Literal["qiita", "devto"]
    tags: list[str] = Field(min_length=1, max_length=5)
    # True only when a person pressed the approve button for this very post.
    approved: bool = False


class DeckRequest(BaseModel):
    # True only when a person pressed the approve button for this deck.
    approved: bool = False


class UrlRequest(BaseModel):
    platform: Literal["naver_blog", "qiita", "devto"]
    url: str = Field(pattern=r"^https://\S+$", max_length=500)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=80_000)


class EditRequest(BaseModel):
    markdown: str = Field(min_length=1, max_length=200_000)
    title: str | None = Field(default=None, max_length=200)


class ApprovalRequest(BaseModel):
    confirmed: bool


def _job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job. The server may have been restarted.")
    return job


def _start(job: Job, work: Any, what: str) -> None:
    """Run ``work`` in the background; one action at a time per job."""
    if job.busy:
        raise HTTPException(409, "The agent is still working on the previous request.")
    job.busy, job.error, job.notice = True, None, None

    async def runner() -> None:
        try:
            job.notice = await work()
        except Exception as exc:  # shown to the user; details go to the log
            job.error = redact_credentials(f"{type(exc).__name__}: {exc}")[:500]
            log_event("web ui action failed", event_phase="WEB_UI", action=what)
        finally:
            job.busy = False

    job.task = asyncio.create_task(runner())


# Two layouts of the same page talk to the same API. BLOG_AGENT_UI picks the one
# served at "/"; both stay reachable, so switching back is a link or one setting.
_LAYOUTS = {"v1": "index.html", "v2": "index_v2.html"}
_DEFAULT_LAYOUT = os.environ.get("BLOG_AGENT_UI", "v2")


def _page(layout: str) -> FileResponse:
    name = _LAYOUTS.get(layout, _LAYOUTS["v2"])
    return FileResponse(
        STATIC_DIR / name, media_type="text/html", headers={"Cache-Control": "no-store"}
    )


@web.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return _page(_DEFAULT_LAYOUT)


@web.get("/v1", response_class=HTMLResponse)
async def classic_layout() -> FileResponse:
    return _page("v1")


@web.get("/v2", response_class=HTMLResponse)
async def new_layout() -> FileResponse:
    return _page("v2")


@web.get("/edit", response_class=HTMLResponse)
async def file_editor() -> FileResponse:
    """The full-page editor. Which file it edits is in the address; the page
    loads the text and saves it through the same API as everything else, and
    shows it only as text in a text box, never as HTML."""
    return FileResponse(
        STATIC_DIR / "edit.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@web.post("/api/jobs")
async def start_job(request: StartRequest) -> dict[str, str]:
    job = Job(id=uuid.uuid4().hex, pipeline=ArticlePipeline())
    job.pipeline.log = job.add_log
    JOBS[job.id] = job
    _start(
        job,
        lambda: job.pipeline.run_stages(
            request.text,
            request.talk_minutes,
            request.talk_language,
            request.audience,
            request.create_deck,
            talk_languages=request.talk_languages,
        ),
        "run_stages",
    )
    return {"job_id": job.id}


@web.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    job = _job(job_id)
    return {
        "busy": job.busy,
        "error": job.error,
        "notice": job.notice,
        "log": job.log,
        "approvals": job.pipeline.approval_hints,
        "overview": await job.pipeline.overview(),
    }


@web.post("/api/jobs/{job_id}/draft")
async def post_draft(job_id: str, request: DraftRequest) -> dict[str, bool]:
    job = _job(job_id)
    if request.platform not in API_PLATFORMS:
        raise HTTPException(400, "Unsupported platform.")

    async def work() -> str | None:
        await job.pipeline.request_draft(request.platform, request.tags)
        if not job.pipeline.approval_hints:
            return "The agent did not ask for approval; see its reply in the log."
        if not request.approved:
            return None  # the page shows the approval request
        log_event(
            "publication approved in the web ui",
            event_phase="HUMAN_APPROVAL",
            platform=request.platform,
        )
        await job.pipeline.answer_approvals(True)
        await job.pipeline.say("Compile the artifact manifest.")
        return None

    _start(job, work, "post_draft")
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/deck")
async def create_deck(job_id: str, request: DeckRequest) -> dict[str, bool]:
    job = _job(job_id)

    async def work() -> str | None:
        await job.pipeline.request_slides_deck()
        if not job.pipeline.approval_hints:
            return "The agent did not ask for approval; see its reply in the log."
        if not request.approved:
            return None
        log_event("slides deck approved in the web ui", event_phase="HUMAN_APPROVAL")
        await job.pipeline.answer_approvals(True)
        if not (await job.pipeline.overview())["deck"]:
            return "No deck was created; see the agent's reply in the log."
        await job.pipeline.say("Compile the artifact manifest.")
        return None

    _start(job, work, "create_deck")
    return {"accepted": True}


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
    _start(job, lambda: job.pipeline.say(request.text), "message")
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/approval")
async def answer_approval(job_id: str, request: ApprovalRequest) -> dict[str, bool]:
    job = _job(job_id)
    log_event(
        "approval answered in the web ui",
        event_phase="HUMAN_APPROVAL",
        confirmed=request.confirmed,
    )
    _start(job, lambda: job.pipeline.answer_approvals(request.confirmed), "approval")
    return {"accepted": True}


@web.get("/api/jobs/{job_id}/files/{name}")
async def view_file(job_id: str, name: str, raw: int = 0) -> Any:
    job = _job(job_id)
    overview = await job.pipeline.overview()
    path = project_file(overview["project_id"], name)
    if path is None:
        raise HTTPException(404, "No such file for this article.")
    text = path.read_text(encoding="utf-8")
    if raw or path.suffix not in (".md", ".html"):
        return PlainTextResponse(text, headers=_VIEW_HEADERS)
    if path.suffix == ".html":
        return HTMLResponse(text, headers=_VIEW_HEADERS)
    body = markdown_lib.markdown(
        text, extensions=["fenced_code", "tables", "sane_lists"]
    )
    # A file a person may change gets a link to the full-page editor.
    edit = (
        f'<a href="/edit?job={job.id}&amp;file={html.escape(name)}">edit</a> &middot; '
        if name in overview["editable"]
        else ""
    )
    return HTMLResponse(
        _PAGE.format(title=html.escape(name), edit=edit, body=body),
        headers=_VIEW_HEADERS,
    )


@web.put("/api/jobs/{job_id}/files/{name}")
async def save_file(job_id: str, name: str, request: EditRequest) -> dict[str, Any]:
    job = _job(job_id)
    if job.busy:
        raise HTTPException(409, "Wait until the agent has finished, then save again.")
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
    uvicorn.run(web, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
