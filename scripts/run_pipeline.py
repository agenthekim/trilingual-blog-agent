"""Run the whole workflow for one article without typing each prompt.

The script sends the same messages a person would type, one stage after
another, and prints what the agent did. Nothing is posted to Qiita or DEV
Community unless ``--publish`` is given, and even then every post waits for a
yes/no answer in the terminal.

Examples:
    uv run python scripts/run_pipeline.py --url https://qiita.com/<user>/items/<id>
    uv run python scripts/run_pipeline.py --draft post.md --draft-language ko \\
        --talk-minutes 10 --talk-language en --publish devto --tags ai,agents
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # the environment is then expected to be set already
    pass

from google.adk.memory.in_memory_memory_service import (  # noqa: E402
    InMemoryMemoryService,
)
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions.in_memory_session_service import (  # noqa: E402
    InMemorySessionService,
)
from google.genai import types  # noqa: E402

from app import state  # noqa: E402
from app.agent import app  # noqa: E402

USER_ID = "pipeline"
CONFIRMATION_CALL = "adk_request_confirmation"
PLATFORM_LANGUAGE = {"qiita": "Japanese", "devto": "English"}
PLATFORM_NAME = {"qiita": "Qiita", "devto": "DEV Community"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--url", help="URL of a post on Qiita, DEV Community or Naver Blog."
    )
    source.add_argument("--draft", type=Path, help="Path of a Markdown draft.")
    parser.add_argument(
        "--draft-language",
        choices=["ko", "en", "ja"],
        help="Language of the draft (required with --draft).",
    )
    parser.add_argument(
        "--talk-minutes",
        type=int,
        default=5,
        help="Talk length in minutes; 0 skips the slide plan. Default: 5.",
    )
    parser.add_argument(
        "--talk-language", choices=["ko", "en", "ja"], default="ko", help="Default: ko."
    )
    parser.add_argument("--audience", default="software engineers")
    parser.add_argument(
        "--publish",
        action="append",
        choices=sorted(PLATFORM_NAME),
        default=[],
        help="Create a draft on this platform (repeatable). Asks before each post.",
    )
    parser.add_argument(
        "--tags", default="", help="Comma-separated tags for --publish, e.g. ai,agents."
    )
    args = parser.parse_args()
    if args.draft and not args.draft_language:
        parser.error("--draft needs --draft-language")
    if args.publish and not args.tags:
        parser.error("--publish needs --tags")
    return args


async def send(runner: Runner, session_id: str, content: types.Content) -> list:
    """Run one turn, print a summary of each event, return pending approvals."""
    pending = []
    async for event in runner.run_async(
        user_id=USER_ID, session_id=session_id, new_message=content
    ):
        if not (event.content and event.content.parts):
            continue
        for part in event.content.parts:
            if part.function_call:
                if part.function_call.name == CONFIRMATION_CALL:
                    pending.append(part.function_call)
                elif part.function_call.name != "transfer_to_agent":
                    print(f"  [{event.author}] tool: {part.function_call.name}")
            elif part.text and not part.thought and event.is_final_response():
                # Writers and planners emit whole articles or JSON; the gate
                # and report agents summarise them, so skip those here.
                if event.author.startswith(
                    (
                        "localizer_",
                        "refiner_",
                        "title_",
                        "slide_plan_",
                        "localization_critic",
                    )
                ):
                    print(f"  [{event.author}] wrote {len(part.text)} characters")
                else:
                    print(f"\n[{event.author}]\n{part.text.strip()}\n")
    return pending


def text_message(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


async def say(runner: Runner, session_id: str, text: str) -> None:
    shown = text if len(text) <= 200 else text[:200] + " ..."
    print(f"\n=== {shown}")
    pending = await send(runner, session_id, text_message(text))
    while pending:
        answers = []
        for call in pending:
            hint = ((call.args or {}).get("toolConfirmation") or {}).get("hint", "")
            reply = await asyncio.to_thread(input, f"APPROVAL NEEDED: {hint} [y/N] ")
            answers.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=call.id,
                        name=call.name,
                        response={"confirmed": reply.strip().lower() in ("y", "yes")},
                    )
                )
            )
        pending = await send(
            runner, session_id, types.Content(role="user", parts=answers)
        )


async def main() -> int:
    args = parse_args()
    sessions = InMemorySessionService()
    runner = Runner(
        app=app,
        session_service=sessions,
        memory_service=InMemoryMemoryService(),
        auto_create_session=True,
    )
    session_id = uuid.uuid4().hex

    async def session_state() -> dict:
        session = await sessions.get_session(
            app_name=app.name, user_id=USER_ID, session_id=session_id
        )
        return dict(session.state) if session else {}

    # Stage 1: seed
    if args.url:
        await say(
            runner,
            session_id,
            f"Use this already published post as the seed article: {args.url}",
        )
    else:
        draft = args.draft.read_text(encoding="utf-8")
        await say(
            runner,
            session_id,
            f"Save this draft as the seed article (language: {args.draft_language}) "
            f"and finalize it; it needs no further editing.\n\n{draft}",
        )
    current = await session_state()
    if not current.get(state.SEED_MARKDOWN):
        print("Stopped: no seed article was saved.", file=sys.stderr)
        return 1
    if not current.get(state.SEED_FINALIZED):
        await say(runner, session_id, "The seed is final. Finalize it.")

    # Stage 2: localization
    await say(runner, session_id, "Localize it into the other two languages.")
    current = await session_state()
    failed = [
        language
        for language, report in (current.get(state.FIDELITY_REPORT) or {}).items()
        if not report.get("passed")
    ]
    if failed:
        print(
            f"Stopped: the structure check failed for {failed}. "
            "Nothing was exported or published.",
            file=sys.stderr,
        )
        return 2

    # Stage 3: publishing
    source = current.get(state.SOURCE_LANGUAGE)
    if source != "ko":
        await say(runner, session_id, "Export the Naver Blog HTML.")
    for platform in args.publish:
        tags = ", ".join(t.strip() for t in args.tags.split(",") if t.strip())
        await say(
            runner,
            session_id,
            f"Create the {PLATFORM_LANGUAGE[platform]} draft on "
            f"{PLATFORM_NAME[platform]}. Tags: {tags}.",
        )

    # Stage 4: talk and manifest
    if args.talk_minutes > 0:
        await say(
            runner,
            session_id,
            f"Prepare a {args.talk_minutes} minute talk in language "
            f"'{args.talk_language}'. Audience: {args.audience}.",
        )
    await say(runner, session_id, "Compile the artifact manifest.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
