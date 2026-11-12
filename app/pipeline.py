"""Run the workflow for the web page and the command-line script.

``ArticlePipeline`` is one article in one agent session. What needs judgement
(localizing, planning a talk, a change asked for in words) it asks of the same
agent a chat user talks to. What needs none (storing the source document,
finalizing, exporting, creating a deck, posting, the manifest) it does by
calling the same tool functions directly, as an action a person started: no
model sits between a person's click and what it does, and the rules in
``app.policy`` apply on both routes because they live in the tools.

After each step the pipeline checks that the step produced what was asked and
says so when it did not, instead of trusting that a reply meant success.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.adk.errors import StaleSessionError
from google.adk.events import Event, EventActions
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
)
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.genai import types

from app import deck_sync, google_slides, platforms, policy, state
from app.markdown_checks import check_fidelity
from app.observability import log_event
from app.persistence import get_article_store
from app.policy import API_PLATFORMS, PLATFORM_NAMES
from app.redaction import find_secrets
from app.settings import (
    ALLOWED_ARTICLE_HOSTS,
    LANGUAGE_NAMES,
    LANGUAGES,
    MAX_ARTICLE_CHARS,
    PLATFORM_FOR_LANGUAGE,
    workspace_dir,
)
from app.structurer import auto_structure_markdown, needs_markdown_structuring
from app.tools.articles import (
    fetch_published_article_markdown,
    finalize_seed_article,
    has_title_line,
    save_seed_article_revision,
    title_of_draft,
)
from app.tools.publish import (
    export_naver_blog_paste_ready_html,
    finalize_localized_post,
    publish_devto_article_draft,
    publish_qiita_article_draft,
    record_manually_posted_url,
    verify_published_article_urls,
)
from app.tools.slides import (
    compile_artifact_manifest_markdown,
    create_google_slides_deck_with_speaker_notes,
    export_google_slides_pdf,
    parse_slide_plan_markdown,
    render_slide_plan_markdown,
    slide_pdf_file,
    slide_plan_file,
    slide_timing_report,
)

__all__ = ["API_PLATFORMS", "PLATFORM_NAMES", "ArticlePipeline"]

_EDITABLE_FILE = re.compile(r"^(post|slides)\.(ko|en|ja)\.md$")
MAX_TITLE_CHARS = 120
MIN_DRAFT_CHARS = 50  # what the seed tool accepts as a whole article
USER_ID = "pipeline"
CONFIRMATION_CALL = "adk_request_confirmation"
_POSTING_TOOL = {
    "qiita": publish_qiita_article_draft,
    "devto": publish_devto_article_draft,
}

# Agents whose output is a whole article or a JSON document. The gate and
# report agents summarise them, so only their size is logged.
_BULK_WRITERS = (
    "localizer_",
    "refiner_",
    "title_",
    "slide_plan_",
    "localization_critic",
)
_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_SAFE_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A message this short that contains a supported URL is a request about a
# published post; anything else is treated as the draft itself.
_MAX_URL_REQUEST_CHARS = 600


# State keys whose value is a dictionary keyed by language or platform. A write
# by code changes the entries it changed and leaves the others as the record
# has them now, so two actions on different languages keep both changes (RUN-2).
MERGED_KEYS = frozenset(
    {
        state.LOCALIZED_TITLES,
        state.FIDELITY_REPORT,
        state.FINALIZED_POSTS,
        state.FINALIZATIONS_ASKED,
        state.PUBLICATIONS,
        state.SLIDE_PLANS,
        state.SLIDE_TIMING,
        state.GOOGLE_SLIDES_DECKS,
        state.DECK_TEXT_BASELINE,
    }
)


def merge_delta(
    base: dict[str, Any], fresh: dict[str, Any], delta: dict[str, Any]
) -> dict[str, Any]:
    """What to write so that ``delta`` changes only what its author changed.

    ``base`` is the state the author read before it worked, ``fresh`` the
    state of the record now, ``delta`` the keys the author wrote. For the
    dictionaries in ``MERGED_KEYS`` an entry is taken from ``delta`` when it
    differs from ``base``, removed when the author removed it, and otherwise
    left as ``fresh`` has it. Every other key is written as given.
    """
    merged: dict[str, Any] = {}
    for key, value in delta.items():
        if key in MERGED_KEYS and isinstance(value, dict):
            before = base.get(key) or {}
            now = dict(fresh.get(key) or {})
            now.update({k: v for k, v in value.items() if before.get(k) != v})
            for gone in before.keys() - value.keys():
                now.pop(gone, None)
            merged[key] = now
        else:
            merged[key] = value
    return merged


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url).split("/", 1)[0].lower().removeprefix("www.")


def classify_source(text: str) -> tuple[str, str]:
    """What a person handed in, and the text to work with.

    'url': a bare address of a post on a supported blog site, read by code.
    'gdoc': a bare address of a Google Docs document, exported as Markdown.
    'request': a short message that contains a supported blog address but says
    more; it goes to the agent. 'elsewhere': a bare address that cannot be read
    (another site, or not https). 'draft': everything else, the article
    itself, stored exactly as given.
    """
    stripped = text.strip()
    urls = [u for u in _URL.findall(stripped) if _host(u) in ALLOWED_ARTICLE_HOSTS]
    if re.fullmatch(r"https?://\S+", stripped):
        if stripped.startswith("https://") and platforms.is_google_doc_url(stripped):
            return "gdoc", stripped
        if stripped.startswith("https://") and platforms.is_medium_url(stripped):
            return "medium", stripped
        readable = bool(urls) and stripped.startswith("https://")
        return ("url" if readable else "elsewhere"), stripped
    if urls and len(stripped) <= _MAX_URL_REQUEST_CHARS:
        return "request", stripped
    return "draft", text


def project_file(project_id: str, name: str) -> Path | None:
    """Path of a generated file, or None if the name is unsafe or the file is missing."""
    if not (
        _SAFE_FILE_NAME.match(project_id or "") and _SAFE_FILE_NAME.match(name or "")
    ):
        return None
    folder = (workspace_dir() / project_id).resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        return None
    return path


def overview(session_state: dict[str, Any]) -> dict[str, Any]:
    """Describe what exists for the article in this session, as plain data."""
    project_id = session_state.get(state.PROJECT_ID) or ""
    source = session_state.get(state.SOURCE_LANGUAGE)
    plans = session_state.get(state.SLIDE_PLANS) or {}
    decks = session_state.get(state.GOOGLE_SLIDES_DECKS) or {}
    timing = session_state.get(state.SLIDE_TIMING) or {}

    def existing(name: str) -> str | None:
        return name if project_file(project_id, name) else None

    rows = []
    for language in LANGUAGES:
        platform = PLATFORM_FOR_LANGUAGE[language]
        record = policy.publication(session_state, platform)
        has_text = bool(state.article_markdown(session_state, language))
        files = [existing(f"post.{language}.md")]
        if platform == "naver_blog":
            files.append(existing("naver_blog.html"))
        step = policy.next_step(session_state, language)
        rows.append(
            {
                "language": language,
                "language_name": LANGUAGE_NAMES[language],
                "title": state.article_title(session_state, language)
                if has_text
                else "",
                "platform": platform,
                "platform_name": PLATFORM_NAMES[platform],
                "status": record.get("state") or "not_posted",
                "finalized": policy.is_finalized(session_state, language),
                # The one thing to do next for this post; see policy.next_step.
                "next": step,
                "url": record.get("url"),
                "files": [f for f in files if f],
                # Any post with text in the workspace (including the source
                # document) can be edited locally; an existing source keeps
                # its 'original' step so it is never re-posted to its blog.
                "can_edit": has_text
                and step in ("original", "review", "post", "paste"),
                "slides_file": existing(slide_plan_file(language))
                if language in plans
                else None,
                "slides_pdf": existing(slide_pdf_file(language)),
                "slides_on_time": (timing.get(language) or {}).get("passed"),
                "deck": decks.get(language),
            }
        )
    slides = [row["slides_file"] for row in rows if row["slides_file"]]
    editable = [
        f"post.{row['language']}.md"
        for row in rows
        if row["can_edit"] and f"post.{row['language']}.md" in row["files"]
    ] + slides
    source_url = next(
        (r["url"] for r in rows if r["status"] == "existing_source" and r.get("url")),
        "",
    )
    return {
        "project_id": project_id,
        "title": session_state.get(state.SEED_TITLE) or "",
        "source_language": source,
        "source_text": source_url or (session_state.get(state.SEED_MARKDOWN) or ""),
        "rows": rows,
        "slides": slides,
        "decks": dict(decks),
        "editable": editable,
        "manifest": existing("manifest.md"),
    }


def progress(
    session_state: dict[str, Any], talk_languages: list[str], wants_decks: bool
) -> list[dict[str, Any]]:
    """The steps of a run and whether each is done, for what was asked for.

    Computed from the session and the files alone, so a page shows the same
    before and after a reload.
    """
    project_id = session_state.get(state.PROJECT_ID) or ""
    targets = session_state.get(state.TARGET_LANGUAGES) or []
    seeded = bool(session_state.get(state.SEED_MARKDOWN))
    transformed = bool(targets) and all(
        session_state.get(state.localized_key(language)) for language in targets
    )
    naver = policy.publication(session_state, "naver_blog").get("state")
    posts_ready = transformed and bool(
        naver == "existing_source" or project_file(project_id, "naver_blog.html")
    )
    steps = [
        {"name": "Source document", "done": seeded},
        {"name": "Transform", "done": transformed},
        {"name": "Prepare posts", "done": posts_ready},
    ]
    if talk_languages:
        plans = session_state.get(state.SLIDE_PLANS) or {}
        decks = session_state.get(state.GOOGLE_SLIDES_DECKS) or {}
        steps.append(
            {
                "name": "Prepare slides",
                "done": all(
                    language in plans and (not wants_decks or language in decks)
                    for language in talk_languages
                ),
            }
        )
    steps.append(
        {"name": "Summarize", "done": bool(project_file(project_id, "manifest.md"))}
    )
    return steps


def post_progress(
    session_state: dict[str, Any], talk_languages: list[str]
) -> list[dict[str, Any]]:
    """The five review-and-publishing steps after Summarize, computed from state."""
    project_id = session_state.get(state.PROJECT_ID) or ""
    summarized = bool(project_file(project_id, "manifest.md"))
    publishable = [
        lang
        for lang in LANGUAGES
        if policy.publication(session_state, PLATFORM_FOR_LANGUAGE[lang]).get("state")
        != "existing_source"
    ]
    has_posts = bool(publishable) and all(
        bool(state.article_markdown(session_state, lang)) for lang in publishable
    )
    reviewed = summarized and any(
        policy.is_finalized(session_state, lang)
        or policy.publication(session_state, PLATFORM_FOR_LANGUAGE[lang]).get("state")
        in ("draft", "published")
        for lang in publishable
    )
    all_finalized = has_posts and all(
        policy.is_finalized(session_state, lang)
        or policy.publication(session_state, PLATFORM_FOR_LANGUAGE[lang]).get("state")
        == "published"
        for lang in publishable
    )
    api_langs = [
        lang for lang in publishable if PLATFORM_FOR_LANGUAGE[lang] in API_PLATFORMS
    ]
    drafted = (
        all_finalized
        and bool(api_langs)
        and all(
            policy.publication(session_state, PLATFORM_FOR_LANGUAGE[lang]).get("state")
            in ("draft", "published")
            for lang in api_langs
        )
    )
    api_published = (
        all_finalized
        and bool(api_langs)
        and all(
            policy.publication(session_state, PLATFORM_FOR_LANGUAGE[lang]).get("state")
            == "published"
            for lang in api_langs
        )
    )
    all_complete = (
        has_posts
        and summarized
        and all(
            policy.publication(session_state, PLATFORM_FOR_LANGUAGE[lang]).get("state")
            in ("existing_source", "published")
            for lang in LANGUAGES
        )
    )
    return [
        {"name": "Review & edit", "done": reviewed},
        {"name": "Finalize posts", "done": all_finalized},
        {"name": "Approve & draft", "done": drafted},
        {"name": "Publish posts", "done": api_published},
        {"name": "Verify & complete", "done": all_complete},
    ]


@dataclass(frozen=True)
class Approval:
    """One request the agent is waiting for a person to answer."""

    id: str
    tool: str  # the tool that asked
    hint: str
    payload: dict[str, Any]
    call: types.FunctionCall

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "tool": self.tool, "hint": self.hint}


def _approval_from(call: types.FunctionCall) -> Approval:
    args = dict(call.args or {})
    original = args.get("originalFunctionCall") or {}
    confirmation = args.get("toolConfirmation") or {}
    payload = confirmation.get("payload")
    return Approval(
        id=call.id or uuid.uuid4().hex,
        tool=str(original.get("name") or ""),
        hint=str(confirmation.get("hint") or "Approve the pending action?"),
        payload=payload if isinstance(payload, dict) else {},
        call=call,
    )


class _StateEdits:
    """Session state as a tool sees it, remembering what the tool wrote."""

    def __init__(self, base: dict[str, Any]) -> None:
        self._base = base
        self.delta: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.delta:
            return self.delta[key]
        return self._base.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.delta[key] if key in self.delta else self._base[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.delta[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self.delta or key in self._base


@dataclass
class PersonContext:
    """What a tool is given when a person, not the model, started it.

    It carries the session state and, for a tool that asks for approval, the
    approval the person gave with the action itself: pressing "Approve and
    post" is the approval, so the tool is not paused to ask again.
    """

    state: _StateEdits
    tool_confirmation: ToolConfirmation | None = None

    def request_confirmation(self, **_: Any) -> None:
        raise RuntimeError("A person's action must carry its own approval.")


def _failure(result: dict[str, Any]) -> str | None:
    """Why a tool did not do what was asked, or None when it did."""
    if result.get("status") == "success" or "passed" in result:
        return None
    return str(result.get("message") or f"it ended as '{result.get('status')}'")


class ArticlePipeline:
    """One article, one agent session, driven step by step."""

    def __init__(
        self,
        log: Callable[[str], None] = print,
        *,
        session_service: BaseSessionService | None = None,
        session_id: str | None = None,
        agent_app: Any = None,
    ) -> None:
        if agent_app is None:
            from app.agent import app as agent_app
        if session_service is None:
            # The process-wide service: a SQLite file locally, so a run can be
            # opened again after the server restarts.
            from app.app_utils.services import get_session_service

            session_service = get_session_service()
        self._app = agent_app
        self._sessions = session_service
        self._runner = Runner(
            app=agent_app,
            session_service=self._sessions,
            memory_service=InMemoryMemoryService(),
            auto_create_session=True,
        )
        self.session_id = session_id or uuid.uuid4().hex
        self.log = log
        self._pending: list[Approval] = []
        self.last_reply = ""
        # The record has one writer at a time (RUN-2): an agent turn holds
        # this for the whole turn, a write by code for one read and one write.
        self._write_lock = asyncio.Lock()

    async def _session(self, *, events: bool = True) -> Any | None:
        config = None if events else GetSessionConfig(num_recent_events=0)
        return await self._sessions.get_session(
            app_name=self._app.name,
            user_id=USER_ID,
            session_id=self.session_id,
            config=config,
        )

    async def session_state(self) -> dict[str, Any]:
        """The state alone; the events, which hold whole articles, are not loaded.

        Reading never creates the session: a page that asks for the status
        while the run takes its first step must not get in its way.
        """
        found = await self._session(events=False)
        return dict(found.state) if found else {}

    async def overview(self) -> dict[str, Any]:
        return overview(await self.session_state())

    @property
    def approvals(self) -> list[dict[str, Any]]:
        """What the agent is waiting for a person to approve, if anything."""
        return [approval.public() for approval in self._pending]

    async def _send(self, content: types.Content) -> None:
        """One agent turn. It is the writer of the record until it ends."""
        async with self._write_lock:
            await self._turn(content)

    async def _turn(self, content: types.Content) -> None:
        self._pending = []
        self.last_reply = ""
        async for event in self._runner.run_async(
            user_id=USER_ID, session_id=self.session_id, new_message=content
        ):
            if not (event.content and event.content.parts):
                continue
            for part in event.content.parts:
                if part.function_call:
                    name = part.function_call.name
                    if name == CONFIRMATION_CALL:
                        self._pending.append(_approval_from(part.function_call))
                    elif name != "transfer_to_agent":
                        self.log(f"[{event.author}] tool: {name}")
                elif part.text and not part.thought and event.is_final_response():
                    if event.author.startswith(_BULK_WRITERS):
                        self.log(f"[{event.author}] wrote {len(part.text)} characters")
                    else:
                        self.last_reply = part.text.strip()
                        self.log(f"[{event.author}]\n{self.last_reply}")

    async def say(self, text: str) -> None:
        """Send one user message and run the agent until it stops."""
        shown = text if len(text) <= 200 else text[:200] + " ..."
        self.log(f">>> {shown}")
        await self._send(
            types.Content(role="user", parts=[types.Part.from_text(text=text)])
        )

    async def answer_approval(
        self, approval_id: str, confirmed: bool, payload: dict[str, Any] | None = None
    ) -> int:
        """Pass a person's answer on to the one request it was given for.

        An answer is never applied to another request that happens to be
        waiting. The agent takes all answers of a turn at once, so every other
        pending request is declined; the number of those is returned so the
        caller can say so. Raises ValueError when no such request is waiting.
        """
        chosen = next((a for a in self._pending if a.id == approval_id), None)
        if chosen is None:
            raise ValueError("That approval request is no longer waiting.")
        others = [a for a in self._pending if a.id != approval_id]
        self.log(
            f">>> approval of {chosen.tool or 'a request'}: "
            f"{'yes' if confirmed else 'no'}"
            + (f" ({payload})" if confirmed and payload else "")
            + (f"; {len(others)} other request(s) declined" if others else "")
        )
        log_event(
            "approval answered by a person",
            event_phase="HUMAN_APPROVAL",
            tool=chosen.tool,
            confirmed=confirmed,
            declined_others=len(others),
        )

        def answer(approval: Approval, yes: bool) -> types.Part:
            response: dict[str, Any] = {"confirmed": yes}
            if yes and payload:
                response["payload"] = payload
            return types.Part(
                function_response=types.FunctionResponse(
                    id=approval.call.id, name=approval.call.name, response=response
                )
            )

        parts = [answer(chosen, confirmed), *(answer(a, False) for a in others)]
        await self._send(types.Content(role="user", parts=parts))
        return len(others)

    async def act(
        self, tool: Callable[..., Any], *args: Any, approval: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Run a tool function as an action a person started; no model is involved.

        ``approval`` is the payload of the approval the person gave with the
        action (an empty dict for a plain yes); leave it None for tools that
        ask for none. What the tool wrote to the state is recorded in the
        session as one event: it waits for an agent turn that is writing, and
        changes only the entries the tool changed (RUN-2).
        """
        current = await self.session_state()
        base = copy.deepcopy(current)  # what the tool was shown, kept apart
        context = PersonContext(
            _StateEdits(current),
            None
            if approval is None
            else ToolConfirmation(confirmed=True, payload=approval),
        )
        name = tool.__name__
        log_event("tool call for a person's action", event_phase="INTENT", tool=name)
        result = tool(*args, tool_context=context, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        if context.state.delta:
            await self._update_state(context.state.delta, base)
        failure = _failure(result)
        log_event(
            "tool call for a person's action",
            event_phase="OUTCOME",
            tool=name,
            status=result.get("status"),
            error_code=result.get("error_code"),
        )
        self.log(f">>> {name}: " + (f"not done. {failure}" if failure else "done"))
        return result

    # --- the run ------------------------------------------------------------

    async def _store_source(self, source_text: str, notes: list[str]) -> str | None:
        """Stage 1. Returns a sentence when no usable seed came out of it."""
        kind, text = classify_source(source_text)
        if kind == "url":
            result = await self.act(fetch_published_article_markdown, text)
            if failure := _failure(result):
                return f"The post could not be read. {failure}"
            if not result.get("seed_finalized"):
                # Naver has no Markdown export; automatically structure plain text into Markdown.
                current = await self.session_state()
                raw_markdown = current.get(state.SEED_MARKDOWN) or ""
                structured = await auto_structure_markdown(raw_markdown)
                if structured and structured != raw_markdown:
                    await self.act(
                        save_seed_article_revision,
                        title=title_of_draft(structured)
                        or current.get(state.SEED_TITLE)
                        or "",
                        markdown=structured,
                        source_language=current.get(state.SOURCE_LANGUAGE) or "ko",
                        code_mode="inline_markdown" if "```" in structured else "none",
                        code_repo_url="",
                        finalized=True,
                    )
                    notes.append(
                        "The text was rebuilt from the Naver page and automatically structured into "
                        "Markdown with headings, lists, and tables. Check the Korean file "
                        "before using the results."
                    )
                else:
                    await self.act(finalize_seed_article)
                    notes.append(
                        "The text was rebuilt from the Naver page, so headings, lists, "
                        "images and links may be missing. Check the Korean file "
                        "before using the results."
                    )
            return None
        if kind == "request":
            await self.say(text)
            current = await self.session_state()
            if not current.get(state.SEED_MARKDOWN):
                return (
                    "No article was stored. See the agent's reply in the activity log."
                )
            if not current.get(state.SEED_FINALIZED):
                await self.act(finalize_seed_article)
            return None
        if kind == "elsewhere":
            return (
                "That address cannot be read: posts are read from Qiita, DEV "
                "Community and Naver Blog, and drafts from Google Docs "
                "(https://docs.google.com/document/d/...), over https. Hand in "
                "a supported URL, a .md or .txt file, or the Markdown text itself."
            )
        if kind == "gdoc":
            try:
                doc_id = platforms.parse_google_doc_id(text)
                text = await platforms.google_doc_get_markdown(doc_id)
            except platforms.PlatformError as exc:
                return f"The Google Docs document could not be read. {exc.message}"
        if kind == "medium":
            try:
                text = await platforms.medium_get_markdown(text)
            except platforms.PlatformError as exc:
                return f"The Medium post could not be read. {exc.message}"
        # Said in plain words here, before a validator says it in its own.
        if len(text.strip()) < MIN_DRAFT_CHARS:
            return (
                "The draft could not be stored. It is shorter than "
                f"{MIN_DRAFT_CHARS} characters; hand in the whole article."
            )
        if len(text) > MAX_ARTICLE_CHARS:
            return (
                f"The draft could not be stored. It has {len(text):,} characters; "
                f"the limit for one article is {MAX_ARTICLE_CHARS:,}."
            )
        # A draft is stored as it was handed in. If it lacks headings, auto-structure it into Markdown.
        draft_text = text
        if needs_markdown_structuring(draft_text):
            structured = await auto_structure_markdown(draft_text)
            if structured and structured != draft_text:
                draft_text = structured
                notes.append(
                    "Automatically structured plain text into Markdown with headings, "
                    "lists, and tables. Review the file before using the results."
                )
        result = await self.act(
            save_seed_article_revision,
            title=title_of_draft(draft_text),
            markdown=draft_text,
            source_language=platforms.guess_language(draft_text),
            code_mode="inline_markdown" if "```" in draft_text else "none",
            code_repo_url="",
            finalized=True,
        )
        if failure := _failure(result):
            return f"The draft could not be stored. {failure}"
        if not has_title_line(draft_text):
            notes.append(
                "The draft has no '# Title' line, so this was taken as the "
                f"title: '{title_of_draft(draft_text)}'. If that is not the title, "
                "add a '# Title' line at the top and run again."
            )
        return None

    async def run_stages(
        self,
        source_text: str,
        talk_minutes: int,
        talk_languages: list[str] | None = None,
        audience: str = "software engineers",
        create_deck: bool = False,
    ) -> str | None:
        """Both parts of a run, one after the other: the posts, then the slides.

        The page runs the two parts in two lanes (``app.runs``); the script
        and the tests call them in turn through this. Returns None when
        everything asked for was done, else sentences saying what was not or
        what the person should look at. Nothing is posted to Qiita or DEV
        Community here. ``create_deck`` is the person's approval, given with
        the run options, to create one Google Slides file per selected
        language in their own Drive.
        """
        ready, notice = await self.run_posts(source_text)
        if not ready:
            return notice
        wanted = list(dict.fromkeys(talk_languages or []))
        if talk_minutes <= 0 or not wanted:
            return notice
        slides = await self.run_slides(wanted, talk_minutes, audience, create_deck)
        return " ".join(filter(None, [notice, slides])) or None

    async def run_posts(self, source_text: str) -> tuple[bool, str | None]:
        """The posts part of a run: source document, localization, Naver file, manifest.

        Returns whether the posts are there to be reviewed, and a sentence
        when something did not happen or the person should look at something.
        When the first is false nothing else of the run may be started.
        """
        notes: list[str] = []
        if stopped := await self._store_source(source_text, notes):
            return False, stopped

        await self.say("Localize it into the other two languages.")
        current = await self.session_state()
        targets = current.get(state.TARGET_LANGUAGES) or []
        unwritten = [
            LANGUAGE_NAMES[language]
            for language in targets
            if not current.get(state.localized_key(language))
        ]
        if unwritten:
            return False, (
                f"No {' or '.join(unwritten)} text was written, so nothing else "
                "was done. See the agent's reply in the activity log."
            )
        report = current.get(state.FIDELITY_REPORT) or {}
        failed = [
            LANGUAGE_NAMES[language]
            for language in targets
            if not (report.get(language) or {}).get("passed")
        ]
        if failed:
            return False, (
                f"The structure check failed for {' and '.join(failed)}, so nothing "
                "was exported and no slides were made. Review the files; the "
                "activity log lists what differs."
            )

        if policy.publication(current, "naver_blog").get("state") != "existing_source":
            if failure := _failure(await self.act(export_naver_blog_paste_ready_html)):
                notes.append(f"The Naver Blog file was not written. {failure}")

        # The manifest is written as soon as the posts are there, so they can
        # be reviewed while the slides are still being made.
        await self.act(compile_artifact_manifest_markdown)
        return True, " ".join(notes) or None

    async def run_slides(
        self,
        talk_languages: list[str],
        talk_minutes: int,
        audience: str = "software engineers",
        create_deck: bool = False,
    ) -> str | None:
        """The slides part of a run: a plan and, if approved, a deck per language.

        Returns None when every language got what was asked for, else
        sentences saying what did not happen.
        """
        notes: list[str] = []
        for language in dict.fromkeys(talk_languages):
            notes += await self._prepare_talk(
                language, talk_minutes, audience, create_deck
            )
        await self.act(compile_artifact_manifest_markdown)
        return " ".join(notes) or None

    async def _prepare_talk(
        self, language: str, minutes: int, audience: str, create_deck: bool
    ) -> list[str]:
        """Plan one language's talk and, if approved, create its deck."""
        name = LANGUAGE_NAMES[language]
        if language not in LANGUAGES:
            return [f"'{language}' is not a language slides can be made in."]
        await self.say(
            f"Prepare a {minutes} minute talk in language '{language}'. "
            f"Audience: {audience}."
        )
        current = await self.session_state()
        brief = current.get(state.PRESENTATION_BRIEF) or {}
        plan = (current.get(state.SLIDE_PLANS) or {}).get(language)
        if not plan or brief.get("language") != language:
            return [
                f"No {name} slide plan was made. See the agent's reply in the "
                "activity log."
            ]
        notes = []
        timing = (current.get(state.SLIDE_TIMING) or {}).get(language) or {}
        if not timing.get("passed"):
            notes.append(
                f"The {name} slide plan missed the one-slide-per-minute target "
                "after two attempts; its speaker notes say how long each slide runs."
            )
        if create_deck:
            self.log(f">>> {name} deck: approved in advance with the run options")
            if failure := await self.create_deck(language, summarize=False):
                notes.append(failure)
        return notes

    # --- actions a person starts on the page ---------------------------------

    async def summarize(self) -> None:
        await self.act(compile_artifact_manifest_markdown)

    async def post(self, platform: str, tags: list[str], public: bool) -> str | None:
        """Send a finalized post, as the person who approved it chose.

        Returns None when it was sent, else why it was not.
        """
        log_event(
            "publication approved by a person",
            event_phase="HUMAN_APPROVAL",
            platform=platform,
            visibility="public" if public else "draft",
        )
        result = await self.act(
            _POSTING_TOOL[platform],
            tags,
            approval={"visibility": "public" if public else "draft"},
        )
        if failure := _failure(result):
            return f"Nothing was posted on {PLATFORM_NAMES[platform]}. {failure}"
        await self.summarize()
        return None

    async def create_deck(self, language: str, summarize: bool = True) -> str | None:
        """Create the deck of one language, approved by a person. None when created."""
        log_event(
            "slides deck approved by a person",
            event_phase="HUMAN_APPROVAL",
            language=language,
        )
        result = await self.act(
            create_google_slides_deck_with_speaker_notes, language, approval={}
        )
        if failure := _failure(result):
            return (
                f"The {LANGUAGE_NAMES.get(language, language)} Google Slides deck "
                f"was not created. {failure}"
            )
        if summarize:
            await self.summarize()
        return None

    async def prepare_slides_and_deck(
        self,
        language: str,
        minutes: int = 15,
        audience: str = "software engineers",
        create_deck: bool = True,
        replan: bool = False,
    ) -> str | None:
        """Create or recreate the slides and Google Slides deck for one language."""
        if replan:
            notes = await self._prepare_talk(language, minutes, audience, create_deck)
            await self.summarize()
            return " ".join(notes) or None
        return await self.create_deck(language, summarize=True)

    async def export_pdf(self, language: str) -> str | None:
        """Export one language's Google Slides deck as PDF (SLD-12)."""
        result = await self.act(export_google_slides_pdf, language)
        if failure := _failure(result):
            return (
                f"The {LANGUAGE_NAMES.get(language, language)} PDF was not "
                f"exported. {failure}"
            )
        self.log(
            f">>> exported {slide_pdf_file(language)} from the "
            f"{LANGUAGE_NAMES.get(language, language)} Google Slides deck"
        )
        await self.summarize()
        return None

    async def record_posted_url(self, platform: str, url: str) -> str | None:
        """Record where a person posted by hand, then check that it answers."""
        result = await self.act(record_manually_posted_url, platform, url)
        if failure := _failure(result):
            return f"The address was not recorded. {failure}"
        checks = (await self.act(verify_published_article_urls)).get("checks") or []
        await self.summarize()
        problem = next(
            (
                check.get("problem") or "no answer"
                for check in checks
                if check.get("platform") == platform and not check.get("reachable")
            ),
            None,
        )
        if problem:
            return (
                "The address was recorded, but it did not answer when checked "
                f"({problem}). Open it to make sure it is right."
            )
        return None

    async def finalize_post(
        self, language: str, acknowledge_findings: bool = False
    ) -> dict[str, Any]:
        """Record that a person has reviewed a post and calls it final.

        Text with credential-looking values is finalized only when the person
        was shown the findings and said they are not real: the first request
        comes back with ``needs_acknowledgement`` and the findings, and
        nothing is recorded. Other refusals raise ValueError.
        """
        result = await self.act(
            finalize_localized_post,
            language,
            approval={"acknowledge_findings": acknowledge_findings},
        )
        current = await self.session_state()
        if result.get("error_code") == "SECRET_IN_ARTICLE":
            return {
                "language": language,
                "finalized": False,
                "needs_acknowledgement": True,
                "findings": policy.findings(current, language),
            }
        if failure := _failure(result):
            raise ValueError(failure)
        self.log(f">>> {LANGUAGE_NAMES[language]} post finalized by a person")
        if (
            PLATFORM_FOR_LANGUAGE[language] == "naver_blog"
            and policy.publication(current, "naver_blog").get("state")
            != "existing_source"
        ):
            # The paste file is written from the text as finalized.
            await self.act(export_naver_blog_paste_ready_html)
        if ((current.get(state.GOOGLE_SLIDES_DECKS) or {}).get(language) or {}).get(
            "presentation_id"
        ):
            pdf_res = await self.act(export_google_slides_pdf, language)
            if not _failure(pdf_res):
                self.log(
                    f">>> exported {slide_pdf_file(language)} on finalizing the "
                    f"{LANGUAGE_NAMES[language]} post"
                )
                await self.summarize()
        check = (current.get(state.FIDELITY_REPORT) or {}).get(language)
        return {"language": language, "finalized": True, "check": check}

    async def _update_state(
        self, delta: dict[str, Any], base: dict[str, Any] | None = None
    ) -> None:
        """Record a change made outside any agent turn (RUN-2).

        ``base`` is the state the change was worked out from. Every caller in
        the application passes it; without it ``delta`` is written as given,
        whole dictionaries included.
        The write waits for an agent turn, reads the record as it is then,
        and writes once. Something that writes without the lock (ADK's own
        event compaction) can still get in between the read and the write;
        the record is then read again and the write made once more.
        """
        for attempt in (1, 2):
            async with self._write_lock:
                session = await self._session()
                if session is None:
                    session = await self._sessions.create_session(
                        app_name=self._app.name,
                        user_id=USER_ID,
                        session_id=self.session_id,
                    )
                merged = (
                    delta
                    if base is None
                    else merge_delta(base, dict(session.state), delta)
                )
                try:
                    await self._sessions.append_event(
                        session,
                        Event(
                            author="human_editor",
                            invocation_id=f"edit-{uuid.uuid4().hex[:8]}",
                            actions=EventActions(state_delta=merged),
                        ),
                    )
                    return
                except StaleSessionError:
                    if attempt == 2:
                        raise

    async def _save_slide_plan(
        self, path: Path, text: str, language: str
    ) -> dict[str, Any]:
        """Save an edited slide plan and read it back as the plan it describes.

        A file that reads as a plan is stored in its regular form, so the
        speaking time shown for each slide matches the edited script, and it
        replaces the agent's plan when that is the plan of this language. A
        file that does not is saved as typed and the problem is reported: the
        deck cannot be updated from it until it is fixed.
        """
        try:
            plan = parse_slide_plan_markdown(text, language)
        except ValueError as exc:
            path.write_text(text, encoding="utf-8")
            self.log(f">>> edited {path.name} by hand; it no longer reads as a plan")
            return {
                "saved": path.name,
                "check": {"passed": False, "errors": [str(exc)]},
                "secrets": [],
            }
        path.write_text(render_slide_plan_markdown(plan), encoding="utf-8")
        current = await self.session_state()
        await self._update_state(self._plan_of_record(current, language, plan), current)
        self.log(f">>> edited {path.name} by hand ({len(plan.slides)} slides)")
        return {
            "saved": path.name,
            "check": {"passed": True, "errors": []},
            "secrets": [],
        }

    async def sync_deck_text(
        self, language: str, direction: str, apply: bool, change_id: str | None = None
    ) -> dict[str, Any]:
        """Compare a deck with its Markdown plan and, if asked, bring one in line.

        ``direction`` is 'push' (file to deck) or 'pull' (deck to file). With
        ``apply`` false nothing is written anywhere; the result says what an
        approval would change and names that set of changes (``change_id``).
        With ``apply`` true a person has approved the set named by
        ``change_id``; if the file or the deck changed since they were shown
        it, nothing is written and the result says so.
        Raises ValueError when there is no deck or no readable plan.
        """
        if direction not in ("push", "pull"):
            raise ValueError("Unknown direction.")
        current = await self.session_state()
        deck = (current.get(state.GOOGLE_SLIDES_DECKS) or {}).get(language)
        if not deck or not deck.get("presentation_id"):
            raise ValueError("There is no Google Slides deck for this language yet.")
        project_id = current.get(state.PROJECT_ID) or ""
        path = project_file(project_id, f"slides.{language}.md")
        if path is None:
            raise ValueError("The slide plan file of this language is missing.")
        plan = parse_slide_plan_markdown(path.read_text(encoding="utf-8"), language)

        baselines = dict(current.get(state.DECK_TEXT_BASELINE) or {})
        baseline = baselines.get(language)
        file_view = deck_sync.plan_text(plan)
        presentation = await google_slides.read_presentation(deck["presentation_id"])
        deck_view, holders = deck_sync.deck_text(presentation)
        carried = deck_sync.to_carry(
            deck_sync.differences(file_view, deck_view, baseline), direction
        )
        result = deck_sync.describe(direction, carried, file_view, deck_view, baseline)
        shown = hashlib.sha256(
            repr(sorted((d.key, d.file, d.deck) for d in carried)).encode()
        ).hexdigest()[:32]
        result.update(
            language=language,
            deck_url=deck.get("url"),
            applied=False,
            change_id=shown,
            changed_since_preview=False,
        )
        if not apply or not carried:
            return result
        if change_id != shown:
            result["changed_since_preview"] = True
            return result

        if direction == "push":
            await google_slides.update_presentation(
                deck["presentation_id"], deck_sync.update_requests(carried, holders)
            )
            deck_view.update({change.key: change.file for change in carried})
        else:
            plan = deck_sync.merged_plan(plan, carried)
            path.write_text(render_slide_plan_markdown(plan), encoding="utf-8")
            file_view = deck_sync.plan_text(plan)
        delta: dict[str, Any] = {
            state.DECK_TEXT_BASELINE: {
                **baselines,
                language: deck_sync.agreed(file_view, deck_view, baseline),
            }
        }
        if direction == "pull":
            delta.update(self._plan_of_record(current, language, plan))
        await self._update_state(delta, current)
        if direction == "push" and project_file(project_id, slide_pdf_file(language)):
            await self.act(export_google_slides_pdf, language)
        where = "deck from the file" if direction == "push" else "file from the deck"
        self.log(
            f">>> updated the {LANGUAGE_NAMES[language]} {where}: "
            f"{len(result['slides'])} slides, approved by a person"
        )
        result["applied"] = True
        return result

    @staticmethod
    def _plan_of_record(
        current: dict[str, Any], language: str, plan: Any
    ) -> dict[str, Any]:
        """State change that makes ``plan`` the plan of its language, with its timing.

        Worked out from ``current``, which the caller passes on as the base of
        the write so that only this language's entries are changed.
        """
        timing = dict(current.get(state.SLIDE_TIMING) or {})
        expected = (timing.get(language) or {}).get("expected_slide_count")
        timing[language] = slide_timing_report(plan, int(expected or len(plan.slides)))
        return {
            state.SLIDE_PLANS: {
                **(current.get(state.SLIDE_PLANS) or {}),
                language: plan.model_dump(),
            },
            state.SLIDE_TIMING: timing,
        }

    async def save_edit(
        self, name: str, markdown: str, title: str | None = None
    ) -> dict[str, Any]:
        """Save a person's edit of a generated file and make the agent use it.

        A localized article is stored in the session state, the article store
        and the file, so a later post is made from the edited text. Changing
        the text or the title undoes the post's finalization (the digest no
        longer matches). The structure check is run again and returned; it
        does not block the save, because the person editing has the final
        say. The seed itself is not editable here: changing it would
        invalidate both localizations.
        """
        current = await self.session_state()
        project_id = current.get(state.PROJECT_ID) or ""
        match = _EDITABLE_FILE.match(name)
        if not match or project_file(project_id, name) is None:
            raise ValueError("This file cannot be edited here.")
        if match.group(1) == "post" and name not in overview(current)["editable"]:
            if policy.next_step(current, match.group(2)) == "original":
                raise ValueError(
                    "This post was read from its blog platform and cannot be edited here."
                )
            # A file left from an earlier text, or a post that is already
            # public: saving it would bring back or change what is settled.
            raise ValueError("This post can no longer be edited here.")
        text = markdown.rstrip() + "\n"
        if not text.strip():
            raise ValueError("The text is empty.")
        kind, language = match.groups()
        path = workspace_dir() / project_id / name
        if kind == "slides":
            return await self._save_slide_plan(path, text, language)

        if language == current.get(state.SOURCE_LANGUAGE):
            check = {"passed": True, "errors": [], "warnings": [], "stats": {}}
            delta: dict[str, Any] = {state.SEED_MARKDOWN: text}
            if title and title.strip():
                clean_title = title.strip()[:MAX_TITLE_CHARS]
                delta[state.SEED_TITLE] = clean_title
                delta[state.LOCALIZED_TITLES] = {
                    **(current.get(state.LOCALIZED_TITLES) or {}),
                    language: clean_title,
                }
                get_article_store().save_project(project_id, {"title": clean_title})
        else:
            check = check_fidelity(current.get(state.SEED_MARKDOWN) or "", text)
            delta = {
                state.localized_key(language): text,
                state.FIDELITY_REPORT: {
                    **(current.get(state.FIDELITY_REPORT) or {}),
                    language: check,
                },
            }
            if title and title.strip():
                delta[state.LOCALIZED_TITLES] = {
                    **(current.get(state.LOCALIZED_TITLES) or {}),
                    language: title.strip()[:MAX_TITLE_CHARS],
                }
        await self._update_state(delta, current)
        get_article_store().add_version(project_id, language, "edited", text)
        path.write_text(text, encoding="utf-8")
        if (
            PLATFORM_FOR_LANGUAGE[language] == "naver_blog"
            and policy.publication(current, "naver_blog").get("state")
            != "existing_source"
        ):
            # The paste file follows the text, by the same rules as any export:
            # a text the export refuses (credential-looking values nobody
            # accepted) leaves no file behind to be pasted by mistake.
            if _failure(await self.act(export_naver_blog_paste_ready_html)):
                (workspace_dir() / project_id / "naver_blog.html").unlink(
                    missing_ok=True
                )
        self.log(f">>> edited {name} by hand ({len(text)} characters)")
        return {
            "saved": name,
            "check": check,
            "secrets": sorted(
                find_secrets(text), key=lambda item: (item["line"], item["type"])
            ),
        }
