"""Run the whole workflow for one article from the command line.

The script goes through the same steps as the web page and prints what was
done. Nothing is posted to Qiita or DEV Community unless ``--publish`` is
given, and even then each post is first finalized and then approved by a
yes/no answer in the terminal, where you also choose draft or public.
``app/web_ui.py`` offers the same flow in a browser.

Examples:
    uv run python scripts/run_pipeline.py --url https://qiita.com/<user>/items/<id>
    uv run python scripts/run_pipeline.py --draft post.md \\
        --talk-minutes 10 --talk-language en --talk-language ko \\
        --publish devto --tags ai,agents
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # the environment is then expected to be set already
    pass

from app.observability import root_span, setup_tracing  # noqa: E402
from app.pipeline import API_PLATFORMS, PLATFORM_NAMES, ArticlePipeline  # noqa: E402
from app.settings import LANGUAGE_FOR_PLATFORM  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--url", help="URL of a post on Qiita, DEV Community or Naver Blog."
    )
    source.add_argument("--draft", type=Path, help="Path of a Markdown draft.")
    parser.add_argument(
        "--talk-minutes",
        type=int,
        default=5,
        help="Talk length in minutes; 0 skips the slide plan. Default: 5.",
    )
    parser.add_argument(
        "--talk-language",
        action="append",
        choices=["ko", "en", "ja"],
        default=[],
        help="Language to plan the talk in (repeatable). Default: all three.",
    )
    parser.add_argument("--audience", default="software engineers")
    parser.add_argument(
        "--publish",
        action="append",
        choices=sorted(API_PLATFORMS),
        default=[],
        help="Post on this platform (repeatable). Asks you to finalize and to "
        "approve each post, and whether it is a draft or public.",
    )
    parser.add_argument(
        "--create-deck",
        action="store_true",
        help="Also create a Google Slides deck in your Drive for each talk "
        "language. Giving this flag is the approval for those files.",
    )
    parser.add_argument(
        "--tags", default="", help="Comma-separated tags for --publish, e.g. ai,agents."
    )
    args = parser.parse_args()
    if args.publish and not args.tags:
        parser.error("--publish needs --tags")
    return args


async def ask(question: str) -> bool:
    reply = await asyncio.to_thread(input, f"{question} [y/N] ")
    return reply.strip().lower() in ("y", "yes")


async def publish(pipeline: ArticlePipeline, platform: str, tags: list[str]) -> None:
    """Finalize, approve and send one post; every step is a person's answer."""
    language = LANGUAGE_FOR_PLATFORM[platform]
    name = PLATFORM_NAMES[platform]
    row = next(
        r for r in (await pipeline.overview())["rows"] if r["language"] == language
    )
    if row["next"] not in ("review", "post"):
        print(f"{name}: nothing to post ({row['next']}).")
        return
    if row["next"] == "review":
        if not await ask(
            f"FINALIZE: is post.{language}.md ('{row['title']}') reviewed and ready?"
        ):
            print(f"{name}: not finalized, so not posted.")
            return
        done = await pipeline.finalize_post(language)
        if done.get("needs_acknowledgement"):
            found = ", ".join(
                f"{f['type']} on line {f['line']}" for f in done["findings"]
            )
            if not await ask(
                f"The text has values that look like credentials ({found}). "
                "Are they examples, not real credentials?"
            ):
                print(f"{name}: not finalized, so not posted.")
                return
            await pipeline.finalize_post(language, acknowledge_findings=True)
    if not await ask(f"APPROVAL NEEDED: post '{row['title']}' on {name}?"):
        print(f"{name}: declined. Nothing was sent.")
        return
    public = await ask("Make it PUBLIC right away? (No keeps it a non-public draft.)")
    if problem := await pipeline.post(platform, tags, public):
        print(problem, file=sys.stderr)


async def main() -> int:
    args = parse_args()
    setup_tracing()
    pipeline = ArticlePipeline(log=lambda line: print(line + "\n"))
    source_text = args.url or args.draft.read_text(encoding="utf-8")
    run_id = pipeline.session_id  # the script has no run record; the session is the run

    # The two parts of a run, in turn: the posts, then the slides. Each is an
    # action with a trace of its own, named as the page names it (OPS-1).
    with root_span("run.main.run_posts", run_id=run_id, lane="main"):
        ready, notes = await pipeline.run_posts(source_text)
    if notes:
        print(notes, file=sys.stderr)
    if not ready:
        return 1  # the run stopped before the posts were there
    if args.talk_minutes > 0:
        with root_span("run.slides.run_slides", run_id=run_id, lane="slides"):
            notes = await pipeline.run_slides(
                args.talk_language or ["en", "ko", "ja"],
                args.talk_minutes,
                args.audience,
                args.create_deck,
            )
        if notes:
            print(notes, file=sys.stderr)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    for platform in args.publish:
        with root_span("run.main.publish", run_id=run_id, lane="main"):
            await publish(pipeline, platform, tags)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
