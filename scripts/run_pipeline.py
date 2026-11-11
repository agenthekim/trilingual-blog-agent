"""Run the whole workflow for one article without typing each prompt.

The script sends the same messages a person would type, one stage after
another, and prints what the agent did. Nothing is posted to Qiita or DEV
Community unless ``--publish`` is given, and even then every post waits for a
yes/no answer in the terminal. ``app/web_ui.py`` offers the same flow in a
browser.

Examples:
    uv run python scripts/run_pipeline.py --url https://qiita.com/<user>/items/<id>
    uv run python scripts/run_pipeline.py --draft post.md \\
        --talk-minutes 10 --talk-language en --publish devto --tags ai,agents
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

from app.pipeline import API_PLATFORMS, ArticlePipeline  # noqa: E402


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
        "--talk-language", choices=["ko", "en", "ja"], default="en", help="Default: en."
    )
    parser.add_argument("--audience", default="software engineers")
    parser.add_argument(
        "--publish",
        action="append",
        choices=sorted(API_PLATFORMS),
        default=[],
        help="Create a draft on this platform (repeatable). Asks before each post.",
    )
    parser.add_argument(
        "--create-deck",
        action="store_true",
        help="Also create a Google Slides deck in your Drive from the slide plan. "
        "Giving this flag is the approval for that one file.",
    )
    parser.add_argument(
        "--tags", default="", help="Comma-separated tags for --publish, e.g. ai,agents."
    )
    args = parser.parse_args()
    if args.publish and not args.tags:
        parser.error("--publish needs --tags")
    return args


async def main() -> int:
    args = parse_args()
    pipeline = ArticlePipeline(log=lambda line: print(line + "\n"))
    source_text = args.url or args.draft.read_text(encoding="utf-8")

    stopped = await pipeline.run_stages(
        source_text,
        args.talk_minutes,
        args.talk_language,
        args.audience,
        args.create_deck,
    )
    if stopped:
        print(stopped, file=sys.stderr)
        return 1

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    for platform in args.publish:
        await pipeline.request_draft(platform, tags)
        while pipeline.approval_hints:
            hint = " / ".join(pipeline.approval_hints)
            reply = await asyncio.to_thread(input, f"APPROVAL NEEDED: {hint} [y/N] ")
            await pipeline.answer_approvals(reply.strip().lower() in ("y", "yes"))
    if args.publish:
        await pipeline.say("Compile the artifact manifest.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
