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
from pathlib import Path
from typing import Any, Literal

from app.env_file import load_env_file

# ``.env`` is read before the modules below read their settings from the
# environment, so the imports that follow cannot be at the top of the file.
# ruff: noqa: E402
load_env_file(Path(__file__).resolve().parent.parent / ".env")

import httpx
import markdown as markdown_lib
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from pydantic import BaseModel, Field, ValidationError, field_validator

from app import policy, runs, secrets, state
from app.google_slides import SlidesError, granted_account, mermaid_preview_url
from app.observability import log_event
from app.pipeline import (
    overview,
    post_progress,
    progress,
    project_file,
)
from app.schemas import DevtoDraftInput, QiitaDraftInput
from app.settings import (
    LANGUAGE_NAMES,
    LANGUAGES,
    MAX_TITLE_CHARS,
    PLATFORM_FOR_LANGUAGE,
    render_mermaid_enabled,
    workspace_dir,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
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


Language = Literal["ko", "en", "ja"]

# The runs and their lanes live in ``app.runs``; the names below are kept here
# because the routes and the tests use them.
Job = runs.Job
JOBS = runs.JOBS
BUSY = runs.BUSY
_job = runs.get_job
_new_job = runs.new_job
_start = runs.start
_claim = runs.claim


@web.exception_handler(runs.LaneBusy)
async def lane_is_busy(request: Request, exc: runs.LaneBusy) -> JSONResponse:
    """A lane that works refuses its own new actions, in one sentence (WEB-11)."""
    return JSONResponse({"detail": str(exc)}, status_code=409)


@web.exception_handler(runs.UnknownRun)
async def unknown_run(request: Request, exc: runs.UnknownRun) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=404)


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


class SlidesRequest(BaseModel):
    # Slides for several languages are one request (SLD-14).
    languages: list[Language] = Field(min_length=1, max_length=3)
    # 0 and "" keep the length and the audience each language has.
    minutes: int = Field(default=0, ge=0, le=60)
    audience: str = Field(default="", max_length=200)
    # True only when a person, shown the languages, the length, the audience
    # and the account, pressed the approve button of the dialog (INV-6).
    approved: bool = False

    @field_validator("languages")
    @classmethod
    def _each_once(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class DeckPdfRequest(BaseModel):
    language: Language


class UrlRequest(BaseModel):
    platform: Literal["naver_blog", "qiita", "devto"]
    url: str = Field(pattern=r"^https://\S+$", max_length=500)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=80_000)


class PromptLocalizeRequest(BaseModel):
    texts: list[str] = Field(default_factory=list, max_length=20)


class EditRequest(BaseModel):
    markdown: str = Field(min_length=1, max_length=200_000)
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)


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


@web.get("/api/jobs")
async def list_recent_jobs() -> dict[str, Any]:
    """Recent runs with their source text and title, newest first."""
    items: list[dict[str, Any]] = []
    for path in sorted(
        runs.jobs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:30]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            job_id = str(record.get("id") or "")
            source_text = str(record.get("source_text") or "").strip()
            if not re.fullmatch(r"[0-9a-f]{32}", job_id) or not source_text:
                continue
            job = _job(job_id)
            current = await job.pipeline.session_state()
            title = str(current.get(state.SEED_TITLE) or "").strip()
            items.append(
                {
                    "job_id": job_id,
                    "source_text": source_text,
                    "title": title,
                    "created": float(record.get("created") or 0),
                }
            )
        except Exception:
            continue
    return {"jobs": items}


@web.post("/api/prompts/localize")
async def localize_prompts(request: PromptLocalizeRequest) -> dict[str, Any]:
    """Translate user-entered quick prompts into English, Korean, and Japanese."""
    cleaned = [t.strip()[:200] for t in request.texts if t and t.strip()][:20]
    if not cleaned:
        return {"translations": {}}
    try:
        from google import genai
        from google.genai import types as genai_types

        from app.models import FAST_MODEL

        prompt = (
            "Translate each of the following user instructions for a blog/slides "
            "editing assistant into natural, concise English (en), Korean (ko), "
            "and Japanese (ja). Return a JSON object mapping each exact input "
            'string to {"en": "...", "ko": "...", "ja": "..."}.\n\n'
            f"Inputs:\n{json.dumps(cleaned, ensure_ascii=False)}"
        )

        def _call() -> dict[str, Any]:
            client = genai.Client()
            resp = client.models.generate_content(
                model=FAST_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            data = json.loads(resp.text or "{}")
            return data if isinstance(data, dict) else {}

        translations = await asyncio.to_thread(_call)
        return {"translations": translations}
    except Exception:
        return {"translations": {}}


@web.post("/api/jobs")
async def start_job(request: StartRequest) -> dict[str, str]:
    wants_slides = request.talk_minutes > 0 and bool(request.talk_languages)
    languages = list(request.talk_languages) if wants_slides else []
    job = _new_job(languages, wants_slides and request.create_deck, request.text)
    runs.start_run(job, request.text, request.talk_minutes, languages, request.audience)
    return {"job_id": job.id}


@web.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    job = _job(job_id)
    current = await job.pipeline.session_state()
    view = overview(current)
    if job.source_text:
        view["source_text"] = job.source_text
    return {
        # Whether anything runs at all; what may be started depends on the lane.
        "busy": job.busy,
        "lanes": {name: lane.public() for name, lane in job.lanes.items()},
        "error": job.error,
        "notice": job.notice,
        "reply": job.reply,
        "log": job.log,
        "approvals": job.pipeline.approvals,
        "overview": view,
        "progress": progress(current, job.talk_languages, job.wants_decks),
        "post_progress": post_progress(current, job.talk_languages),
    }


_TAG_RULES = {
    "qiita": (
        QiitaDraftInput,
        "Qiita takes one to five tags without spaces, separated by commas "
        '(Good: "GoogleCloud, Lustre, HPC, Storage, GCP" · '
        'Bad: "GoogleCloud Lustre HPC Storage GCP").',
    ),
    "devto": (
        DevtoDraftInput,
        "DEV Community takes up to four tags, lowercase letters and digits only, "
        'separated by commas (Good: "googlecloud, lustre, hpc, storage" · '
        'Bad: "GoogleCloud, google-cloud" or "gcp lustre hpc").',
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
    if job.lanes[runs.MAIN].busy:
        raise runs.LaneBusy(runs.MAIN)
    current = await job.pipeline.session_state()
    # Said at once rather than after a round trip: finalize first, and so on.
    if refusal := policy.posting_refusal(current, request.platform):
        raise HTTPException(409, refusal.message)
    _start(
        job,
        runs.MAIN,
        lambda: job.pipeline.post(request.platform, request.tags, request.public),
        "post",
    )
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/deck")
async def create_deck(job_id: str, request: DeckRequest) -> dict[str, bool]:
    """Create the Google Slides deck of one language from the plan it has,
    approved by a person. The plan is left as it is."""
    job = _job(job_id)
    if not request.approved:
        raise HTTPException(400, "A deck is created only with a person's approval.")
    _start(
        job,
        runs.SLIDES,
        lambda: job.pipeline.create_deck(request.language),
        "create_deck",
    )
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/slides")
async def request_slides(job_id: str, request: SlidesRequest) -> dict[str, bool]:
    """Plan the slides of the languages asked for and create their decks.

    The request is the approval of the person who pressed the button of the
    dialog that named the languages, the length, the audience and the
    account. Everything runs in the slides lane; the posts stay usable.
    """
    job = _job(job_id)
    if not request.approved:
        raise HTTPException(400, "Slides are made only with a person's approval.")
    log_event(
        "slide request approved in the web ui",
        event_phase="HUMAN_APPROVAL",
        languages=",".join(request.languages),
    )
    runs.start_slides(job, list(request.languages), request.minutes, request.audience)
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/pdf")
async def export_deck_pdf(job_id: str, request: DeckPdfRequest) -> dict[str, bool]:
    """Export one language's Google Slides deck as slides.<lang>.pdf (SLD-12)."""
    job = _job(job_id)
    _start(
        job,
        runs.SLIDES,
        lambda: job.pipeline.export_pdf(request.language),
        "export_pdf",
    )
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/finalize")
async def finalize_post(job_id: str, request: FinalizeRequest) -> dict[str, Any]:
    """Mark a post as final. Posting needs it; editing the title or text undoes it."""
    job = _job(job_id)
    with _claim(job, runs.MAIN, "finalize"):
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
        if job.lanes[runs.SLIDES].busy:
            raise runs.LaneBusy(runs.SLIDES)
        return await run()
    if not request.change_id:
        raise HTTPException(400, "Ask what would change before approving it.")
    log_event(
        "deck text sync approved in the web ui",
        event_phase="HUMAN_APPROVAL",
        language=request.language,
        direction=request.direction,
    )
    # Nothing else of the slides lane may run while the deck is written.
    with _claim(job, runs.SLIDES, "sync_deck"):
        return await run()


@web.post("/api/jobs/{job_id}/url")
async def record_url(job_id: str, request: UrlRequest) -> dict[str, bool]:
    job = _job(job_id)
    _start(
        job,
        runs.MAIN,
        lambda: job.pipeline.record_posted_url(request.platform, request.url),
        "record_url",
    )
    return {"accepted": True}


@web.post("/api/jobs/{job_id}/message")
async def send_message(job_id: str, request: MessageRequest) -> dict[str, bool]:
    job = _job(job_id)

    async def work() -> str | None:
        dropped = len(job.pipeline.approvals)
        await job.pipeline.mark_surface()
        await job.pipeline.say(request.text)
        job.reply = job.pipeline.last_reply[:2000] or (
            "The agent finished without a reply; see the activity log."
        )
        notes = []
        if dropped:
            notes.append(
                f"{dropped} request(s) the agent was waiting on were left "
                "unanswered by this message and are gone. Ask again for those."
            )
        # A slide request can only have been approved in an approval turn,
        # but one left from an earlier turn is not left lying either.
        if not_started := await runs.start_requested_slides(job):
            notes.append(not_started)
        return " ".join(notes) or None

    _start(job, runs.MAIN, work, "message")
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
        notes = []
        if declined:
            notes.append(
                f"{declined} other request(s) the agent was waiting on were "
                "declined: one answer goes to one request. Ask again for those."
            )
        # An approved slide request is run by code, in the slides lane.
        if not_started := await runs.start_requested_slides(job):
            notes.append(not_started)
        return " ".join(notes) or None

    _start(job, runs.MAIN, work, "approval")
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
    # A post is the main lane's; speaker notes (a slide plan) are the slides lane's.
    lane = runs.SLIDES if name.startswith("slides.") else runs.MAIN
    with _claim(job, lane, "save_file"):
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
