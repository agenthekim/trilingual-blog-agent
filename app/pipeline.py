"""Run the workflow stage by stage without a person typing each prompt.

``ArticlePipeline`` drives the same agent a chat user talks to: it sends the
stage prompts in order, keeps track of approvals the agent is waiting for, and
reports the outcome as data. The command-line script and the web UI both use
it, so the two cannot drift apart.

Publishing stays human-approved. The pipeline never answers an approval
request on its own; the caller passes on a decision a person made.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from google.adk.events import Event, EventActions
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from app import state
from app.agent import app
from app.markdown_checks import check_fidelity
from app.persistence import get_article_store
from app.redaction import find_secrets
from app.settings import (
    ALLOWED_ARTICLE_HOSTS,
    LANGUAGE_NAMES,
    LANGUAGES,
    PLATFORM_FOR_LANGUAGE,
    workspace_dir,
)

_EDITABLE_FILE = re.compile(r"^(post|slides)\.(ko|en|ja)\.md$")
MAX_TITLE_CHARS = 120
USER_ID = "pipeline"
CONFIRMATION_CALL = "adk_request_confirmation"
PLATFORM_NAMES = {
    "qiita": "Qiita",
    "devto": "DEV Community",
    "naver_blog": "Naver Blog",
}
API_PLATFORMS = ("qiita", "devto")

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


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url).split("/", 1)[0].lower().removeprefix("www.")


def seed_message(text: str) -> str:
    """Turn what the user typed into the first message for the agent."""
    text = text.strip()
    urls = [u for u in _URL.findall(text) if _host(u) in ALLOWED_ARTICLE_HOSTS]
    if urls and len(text) <= _MAX_URL_REQUEST_CHARS:
        if text == urls[0]:
            return f"Use this already published post as the seed article: {text}"
        return text
    return (
        "Save this draft as the seed article and finalize it; it needs no "
        "further editing. Work out its language and title from the text.\n\n" + text
    )


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
    publications = session_state.get(state.PUBLICATIONS) or {}

    def existing(name: str) -> str | None:
        return name if project_file(project_id, name) else None

    rows = []
    for language in LANGUAGES:
        platform = PLATFORM_FOR_LANGUAGE[language]
        publication = publications.get(platform) or {}
        has_text = bool(state.article_markdown(session_state, language))
        files = [existing(f"post.{language}.md")]
        if platform == "naver_blog":
            files.append(existing("naver_blog.html"))
        rows.append(
            {
                "language": language,
                "language_name": LANGUAGE_NAMES[language],
                "title": state.article_title(session_state, language)
                if has_text
                else "",
                "platform": platform,
                "platform_name": PLATFORM_NAMES[platform],
                "status": publication.get("state") or "not_posted",
                "url": publication.get("url"),
                "files": [f for f in files if f],
                "can_post_draft": has_text
                and platform in API_PLATFORMS
                and publication.get("state") != "existing_source",
                "can_record_url": has_text
                and publication.get("state") != "existing_source",
            }
        )
    slides = [
        name for language in LANGUAGES if (name := existing(f"slides.{language}.md"))
    ]
    source = session_state.get(state.SOURCE_LANGUAGE)
    editable = [
        f"post.{row['language']}.md"
        for row in rows
        if row["language"] != source and f"post.{row['language']}.md" in row["files"]
    ] + slides
    return {
        "project_id": project_id,
        "title": session_state.get(state.SEED_TITLE) or "",
        "source_language": session_state.get(state.SOURCE_LANGUAGE),
        "rows": rows,
        "slides": slides,
        "deck": session_state.get(state.GOOGLE_SLIDES_DECK),
        "can_create_deck": bool(session_state.get(state.SLIDE_DECK_PLAN)),
        "editable": editable,
        "manifest": existing("manifest.md"),
    }


def _find_hint(value: Any) -> str:
    if isinstance(value, dict):
        if isinstance(value.get("hint"), str):
            return value["hint"]
        for child in value.values():
            if hint := _find_hint(child):
                return hint
    return ""


class ArticlePipeline:
    """One article, one agent session, driven stage by stage."""

    def __init__(self, log: Callable[[str], None] = print) -> None:
        self._sessions = InMemorySessionService()
        self._runner = Runner(
            app=app,
            session_service=self._sessions,
            memory_service=InMemoryMemoryService(),
            auto_create_session=True,
        )
        self.session_id = uuid.uuid4().hex
        self.log = log
        self._pending: list[types.FunctionCall] = []

    async def session_state(self) -> dict[str, Any]:
        session = await self._sessions.get_session(
            app_name=app.name, user_id=USER_ID, session_id=self.session_id
        )
        return dict(session.state) if session else {}

    async def overview(self) -> dict[str, Any]:
        return overview(await self.session_state())

    @property
    def approval_hints(self) -> list[str]:
        """What the agent is waiting for a person to approve, if anything."""
        return [
            _find_hint(dict(call.args or {})) or "Approve the pending action?"
            for call in self._pending
        ]

    async def _send(self, content: types.Content) -> None:
        self._pending = []
        async for event in self._runner.run_async(
            user_id=USER_ID, session_id=self.session_id, new_message=content
        ):
            if not (event.content and event.content.parts):
                continue
            for part in event.content.parts:
                if part.function_call:
                    name = part.function_call.name
                    if name == CONFIRMATION_CALL:
                        self._pending.append(part.function_call)
                    elif name != "transfer_to_agent":
                        self.log(f"[{event.author}] tool: {name}")
                elif part.text and not part.thought and event.is_final_response():
                    if event.author.startswith(_BULK_WRITERS):
                        self.log(f"[{event.author}] wrote {len(part.text)} characters")
                    else:
                        self.log(f"[{event.author}]\n{part.text.strip()}")

    async def say(self, text: str) -> None:
        """Send one user message and run the agent until it stops."""
        shown = text if len(text) <= 200 else text[:200] + " ..."
        self.log(f">>> {shown}")
        await self._send(
            types.Content(role="user", parts=[types.Part.from_text(text=text)])
        )

    async def answer_approvals(self, confirmed: bool) -> None:
        """Pass a person's yes/no on to every approval the agent is waiting for."""
        if not self._pending:
            return
        self.log(f">>> approval: {'yes' if confirmed else 'no'}")
        parts = [
            types.Part(
                function_response=types.FunctionResponse(
                    id=call.id, name=call.name, response={"confirmed": confirmed}
                )
            )
            for call in self._pending
        ]
        await self._send(types.Content(role="user", parts=parts))

    async def run_stages(
        self, source_text: str, talk_minutes: int, talk_language: str, audience: str
    ) -> str | None:
        """Seed, localize, export, plan the talk, compile the manifest.

        Returns None on success or a sentence saying why it stopped early.
        Nothing is posted to Qiita or DEV Community here.
        """
        await self.say(seed_message(source_text))
        current = await self.session_state()
        if not current.get(state.SEED_MARKDOWN):
            return "No seed article was saved. See the agent's reply above."
        if not current.get(state.SEED_FINALIZED):
            await self.say("The seed is final. Finalize it.")

        await self.say("Localize it into the other two languages.")
        current = await self.session_state()
        failed = [
            language
            for language, report in (current.get(state.FIDELITY_REPORT) or {}).items()
            if not report.get("passed")
        ]
        if failed:
            return (
                f"The structure check failed for {failed}; nothing was exported. "
                "Review the files before going on."
            )

        naver = (current.get(state.PUBLICATIONS) or {}).get("naver_blog") or {}
        if naver.get("state") != "existing_source":
            await self.say("Export the Naver Blog HTML.")
        if talk_minutes > 0:
            await self.say(
                f"Prepare a {talk_minutes} minute talk in language "
                f"'{talk_language}'. Audience: {audience}."
            )
        await self.say("Compile the artifact manifest.")
        return None

    async def request_draft(self, platform: str, tags: list[str]) -> None:
        """Ask the agent to create or update a draft; it stops at the approval request."""
        language = LANGUAGE_NAMES[
            next(lang for lang, p in PLATFORM_FOR_LANGUAGE.items() if p == platform)
        ]
        await self.say(
            f"Create the {language} draft on {PLATFORM_NAMES[platform]} from the "
            f"current text, or update the draft if one exists. Tags: {', '.join(tags)}."
        )

    async def request_slides_deck(self) -> None:
        """Ask for a Google Slides deck; the agent stops at the approval request."""
        await self.say("Create the Google Slides deck from the slide plan.")

    async def record_posted_url(self, platform: str, url: str) -> None:
        await self.say(
            f"I posted the article on {PLATFORM_NAMES[platform]} by hand. Record this "
            f"URL and verify it: {url}"
        )
        await self.say("Compile the artifact manifest.")

    async def save_edit(
        self, name: str, markdown: str, title: str | None = None
    ) -> dict[str, Any]:
        """Save a person's edit of a generated file and make the agent use it.

        A localized article is stored in the session state, the article store
        and the file, so a later draft is created from the edited text. The
        structure check is run again and returned; it does not block the save,
        because the person editing has the final say. The seed itself is not
        editable here: changing it would invalidate both localizations.
        """
        current = await self.session_state()
        project_id = current.get(state.PROJECT_ID) or ""
        match = _EDITABLE_FILE.match(name)
        if not match or project_file(project_id, name) is None:
            raise ValueError("This file cannot be edited here.")
        text = markdown.rstrip() + "\n"
        if not text.strip():
            raise ValueError("The text is empty.")
        kind, language = match.groups()
        path = workspace_dir() / project_id / name
        if kind == "slides":
            path.write_text(text, encoding="utf-8")
            return {"saved": name, "check": None, "secrets": []}
        if language == current.get(state.SOURCE_LANGUAGE):
            raise ValueError(
                "This is the source article. Edit it where it was written and "
                "run the stages again."
            )

        check = check_fidelity(current.get(state.SEED_MARKDOWN) or "", text)
        delta: dict[str, Any] = {
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
        session = await self._sessions.get_session(
            app_name=app.name, user_id=USER_ID, session_id=self.session_id
        )
        await self._sessions.append_event(
            session,
            Event(
                author="human_editor",
                invocation_id=f"edit-{uuid.uuid4().hex[:8]}",
                actions=EventActions(state_delta=delta),
            ),
        )
        get_article_store().add_version(project_id, language, "edited", text)
        path.write_text(text, encoding="utf-8")
        if language == "ko" and project_file(project_id, "naver_blog.html"):
            from app.tools.publish import markdown_to_inline_styled_html

            (workspace_dir() / project_id / "naver_blog.html").write_text(
                markdown_to_inline_styled_html(text), encoding="utf-8"
            )
        self.log(f">>> edited {name} by hand ({len(text)} characters)")
        return {
            "saved": name,
            "check": check,
            "secrets": [str(finding) for finding in find_secrets(text)[:5]],
        }
