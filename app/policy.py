"""The rules that decide whether a post may be finalized and whether it may be sent.

One place answers both questions for every surface. The web page, the
command-line script and the agent's tools all end up in the functions here, so
a rule cannot hold on one path and be missing on another. The functions read
session state and return a reason for refusing, or None; they change nothing.

Finalizing is a person's sign-off of one post. What is recorded is a digest of
the title and the text together, so a later change to either, by a person or
by the agent, undoes the sign-off without anyone having to remember to.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, NamedTuple

from app import state
from app.redaction import find_secrets
from app.settings import LANGUAGE_FOR_PLATFORM, LANGUAGE_NAMES, PLATFORM_FOR_LANGUAGE

PLATFORM_NAMES = {
    "qiita": "Qiita",
    "devto": "DEV Community",
    "naver_blog": "Naver Blog",
}
# Platforms the system can write to. Naver Blog has no write API.
API_PLATFORMS = ("qiita", "devto")
MAX_FINDINGS_SHOWN = 5


class Refusal(NamedTuple):
    """Why something is not allowed, in the shape of a tool error."""

    code: str
    message: str
    recovery: str


def post_digest(title: str, text: str) -> str:
    """Digest of exactly what would be sent: the title and the text."""
    joined = f"{title}\x00{text}".encode()
    return hashlib.sha256(joined).hexdigest()


def publication(session_state: Mapping[str, Any], platform: str) -> dict[str, Any]:
    return (session_state.get(state.PUBLICATIONS) or {}).get(platform) or {}


def _mark(session_state: Mapping[str, Any], language: str) -> dict[str, Any]:
    mark = (session_state.get(state.FINALIZED_POSTS) or {}).get(language)
    return mark if isinstance(mark, dict) else {}


def is_finalized(session_state: Mapping[str, Any], language: str) -> bool:
    """Whether a person finalized exactly the title and text this language has now."""
    text = state.article_markdown(session_state, language)
    if not text:
        return False
    title = state.article_title(session_state, language)
    return _mark(session_state, language).get("digest") == post_digest(title, text)


def findings(session_state: Mapping[str, Any], language: str) -> list[dict[str, Any]]:
    """Credential-looking values in a post, as type and line; never the value."""
    text = state.article_markdown(session_state, language) or ""
    return find_secrets(text)[:MAX_FINDINGS_SHOWN]


def describe_findings(found: list[dict[str, Any]]) -> str:
    return ", ".join(f"{item['type']} on line {item['line']}" for item in found)


def finalize_refusal(
    session_state: Mapping[str, Any], language: str, acknowledge_findings: bool = False
) -> Refusal | None:
    """Why this post cannot be finalized now, or None when it can.

    The source language can be finalized too: an article that came in as a
    draft is posted on its own platform like the other two. Only the platform
    an article was read from is closed.
    """
    name = LANGUAGE_NAMES[language]
    platform = PLATFORM_FOR_LANGUAGE[language]
    if not state.article_markdown(session_state, language):
        return Refusal(
            "NO_TEXT",
            f"There is no {name} post to finalize yet.",
            "Localize the article first.",
        )
    record = publication(session_state, platform)
    if record.get("state") == "existing_source":
        return Refusal(
            "ALREADY_PUBLISHED_SOURCE",
            f"The {name} post is the original on {PLATFORM_NAMES[platform]}; "
            "there is nothing to finalize or post.",
            "Continue with the other languages.",
        )
    if record.get("state") == "published":
        return Refusal(
            "ALREADY_PUBLISHED",
            f"The {name} post is already public on {PLATFORM_NAMES[platform]}.",
            "Edit the public post on the site.",
        )
    found = findings(session_state, language)
    if found and not acknowledge_findings:
        return Refusal(
            "SECRET_IN_ARTICLE",
            f"The {name} text has values that look like credentials: "
            f"{describe_findings(found)}.",
            "Show the person the finding types and line numbers. Remove the "
            "values, or have the person confirm that they are not real credentials.",
        )
    return None


def finalization(
    session_state: Mapping[str, Any], language: str, acknowledge_findings: bool = False
) -> dict[str, Any]:
    """What to record under FINALIZED_POSTS for this language."""
    return {
        "digest": post_digest(
            state.article_title(session_state, language),
            state.article_markdown(session_state, language) or "",
        ),
        "acknowledged_findings": bool(
            acknowledge_findings and findings(session_state, language)
        ),
    }


def export_refusal(session_state: Mapping[str, Any], platform: str) -> Refusal | None:
    """Why a post cannot leave this machine at all, finalized or not.

    These are the conditions for writing the Naver paste file. Sending through
    an API needs them and more; see ``posting_refusal``.
    """
    language = LANGUAGE_FOR_PLATFORM[platform]
    name = LANGUAGE_NAMES[language]
    if not session_state.get(state.PROJECT_ID) or not session_state.get(
        state.SEED_FINALIZED
    ):
        return Refusal(
            "SEED_NOT_FINALIZED",
            "There is no finalized seed article yet.",
            "Finish stage 1 first: get the user's confirmation and call "
            "finalize_seed_article.",
        )
    record = publication(session_state, platform)
    if record.get("state") == "existing_source":
        return Refusal(
            "ALREADY_PUBLISHED_SOURCE",
            f"This article was read from {PLATFORM_NAMES[platform]} "
            f"({record.get('url')}); it is already published there.",
            "Do not publish to this platform. Continue with the other platforms.",
        )
    if not state.article_markdown(session_state, language):
        return Refusal(
            "LOCALIZATION_MISSING",
            f"There is no {name} version of the article yet.",
            "Run localization first by transferring to localization_pipeline.",
        )
    found = findings(session_state, language)
    acknowledged = is_finalized(session_state, language) and _mark(
        session_state, language
    ).get("acknowledged_findings")
    if found and not acknowledged:
        return Refusal(
            "SECRET_IN_ARTICLE",
            f"The {name} article has values that look like credentials: "
            f"{describe_findings(found)}.",
            "Do not publish. Show the user the finding types and line numbers. "
            "They remove the values, or finalize the post confirming that the "
            "values are not real credentials.",
        )
    return None


def posting_refusal(session_state: Mapping[str, Any], platform: str) -> Refusal | None:
    """Why a post cannot be sent to Qiita or DEV Community now, or None.

    Checked when the agent asks and again when the approval arrives, because
    the text can change in between.
    """
    if refusal := export_refusal(session_state, platform):
        return refusal
    language = LANGUAGE_FOR_PLATFORM[platform]
    name = LANGUAGE_NAMES[language]
    record = publication(session_state, platform)
    if record.get("state") == "published":
        return Refusal(
            "ALREADY_PUBLISHED",
            f"This article is already public on {PLATFORM_NAMES[platform]}: "
            f"{record.get('url')}.",
            "Do not post it again. Tell the user to edit the public post on the site.",
        )
    if not is_finalized(session_state, language):
        return Refusal(
            "NOT_FINALIZED",
            f"The {name} post has not been finalized by a person, or its title "
            "or text changed after it was.",
            "Ask the user to review the post and finalize it: with the Finalize "
            "button on the web page, or by confirming finalize_localized_post "
            f"for '{language}'. Then post.",
        )
    return None


def next_step(session_state: Mapping[str, Any], language: str) -> str:
    """The one thing left to do for a post, as a word the page understands.

    original, public, review (finalize it), post (through the API), paste
    (Naver, by hand), or none when there is no text.
    """
    platform = PLATFORM_FOR_LANGUAGE[language]
    if not state.article_markdown(session_state, language):
        return "none"
    record_state = publication(session_state, platform).get("state")
    if record_state == "existing_source":
        return "original"
    if record_state == "published":
        return "public"
    if not is_finalized(session_state, language):
        return "review"
    return "post" if platform in API_PLATFORMS else "paste"
