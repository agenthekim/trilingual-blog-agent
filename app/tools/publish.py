"""Stage 3 tools: export, publish as draft, and verify.

Publishing is the one place where the agent acts outside the conversation, so
it is gated twice. First, the article is scanned for credential-looking
strings and refused if any are found. Second, the tool stops and asks for
human confirmation before it sends anything; it only proceeds when the run is
resumed with an approval.

Posts are created as private (Qiita) or unpublished (DEV) drafts. Making a
draft public stays a manual step on the platform.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import markdown as markdown_lib
from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from app import platforms, state
from app.persistence import get_article_store
from app.redaction import find_secrets
from app.schemas import (
    DevtoDraftInput,
    PublicationRecord,
    PublishOutput,
    QiitaDraftInput,
    UrlCheck,
    tool_error,
    validation_error,
)
from app.settings import LANGUAGE_FOR_PLATFORM, LANGUAGE_NAMES
from app.tools.articles import write_workspace_file

_INLINE_STYLES = {
    "h1": "font-size:28px;font-weight:700;margin:32px 0 16px;",
    "h2": "font-size:24px;font-weight:700;margin:28px 0 14px;",
    "h3": "font-size:20px;font-weight:700;margin:24px 0 12px;",
    "p": "font-size:16px;line-height:1.8;margin:12px 0;",
    "pre": "background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;"
    "padding:14px;overflow-x:auto;font-size:14px;line-height:1.5;",
    "code": "font-family:Consolas,Menlo,monospace;",
    "blockquote": "border-left:4px solid #d0d7de;margin:12px 0;padding:4px 14px;color:#57606a;",
    "table": "border-collapse:collapse;margin:12px 0;",
    "th": "border:1px solid #d0d7de;padding:6px 12px;background:#f6f8fa;",
    "td": "border:1px solid #d0d7de;padding:6px 12px;",
    "li": "font-size:16px;line-height:1.8;",
}


def markdown_to_inline_styled_html(markdown: str) -> str:
    """Render Markdown to HTML whose formatting survives pasting into a rich editor.

    Rich editors such as Naver SmartEditor drop ``<style>`` blocks and class
    names, so every element carries its own ``style`` attribute.
    """
    html = markdown_lib.markdown(
        markdown, extensions=["fenced_code", "tables", "sane_lists"]
    )
    for tag, style in _INLINE_STYLES.items():
        html = re.sub(rf"<{tag}(\s[^>]*)?>", rf'<{tag}\1 style="{style}">', html)
    return html


def _publishing_preconditions(
    tool_context: ToolContext, platform: str
) -> tuple[str, str, str] | dict[str, Any]:
    """Return (project_id, language, markdown) or a ToolError dict."""
    project_id = tool_context.state.get(state.PROJECT_ID)
    if not project_id or not tool_context.state.get(state.SEED_FINALIZED):
        return tool_error(
            "SEED_NOT_FINALIZED",
            "There is no finalized seed article yet.",
            "Finish stage 1 first: get the user's confirmation and call "
            "finalize_seed_article.",
        )
    language = LANGUAGE_FOR_PLATFORM[platform]
    existing = (tool_context.state.get(state.PUBLICATIONS) or {}).get(platform)
    if existing and existing.get("state") == "existing_source":
        return tool_error(
            "ALREADY_PUBLISHED_SOURCE",
            f"This article was imported from {platform} ({existing.get('url')}); "
            "it is already published there.",
            "Do not publish to this platform. Continue with the other platforms.",
        )
    markdown = state.article_markdown(tool_context.state, language)
    if not markdown:
        return tool_error(
            "LOCALIZATION_MISSING",
            f"There is no {LANGUAGE_NAMES[language]} version of the article yet.",
            "Run localization first by transferring to localization_pipeline.",
        )
    findings = find_secrets(markdown)
    if findings:
        return tool_error(
            "SECRET_IN_ARTICLE",
            f"The {LANGUAGE_NAMES[language]} article contains credential-looking "
            f"text: {findings[:5]}.",
            "Do not publish. Show the user the finding types and line numbers and "
            "ask them to remove the values from the seed article.",
        )
    return project_id, language, markdown


def _remember_publication(
    tool_context: ToolContext, project_id: str, record: PublicationRecord
) -> None:
    publications = dict(tool_context.state.get(state.PUBLICATIONS) or {})
    publications[record.platform] = record.model_dump()
    tool_context.state[state.PUBLICATIONS] = publications
    get_article_store().record_publication(
        project_id, record.platform, record.model_dump()
    )


def _approval(
    tool_context: ToolContext, *, platform: str, title: str, chars: int, updating: bool
) -> dict[str, Any] | None:
    """Stop for human approval. Returns a result dict unless approval was given."""
    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        action = "update the existing draft on" if updating else "create a draft on"
        tool_context.request_confirmation(
            hint=f"Approve to {action} {platform}: '{title}' ({chars} characters). "
            "It will be saved as a non-public draft.",
            payload={"platform": platform, "title": title, "updating": updating},
        )
        return PublishOutput(
            status="pending_human_approval",
            message=f"Waiting for the user's approval before sending anything to {platform}.",
        ).model_dump()
    if not confirmation.confirmed:
        return PublishOutput(
            status="declined",
            message=f"The user declined publishing to {platform}. Nothing was sent.",
        ).model_dump()
    return None


def export_naver_blog_paste_ready_html(tool_context: ToolContext) -> dict[str, Any]:
    """Export the Korean article as HTML ready to paste into the Naver Blog editor.

    Naver Blog has no public write API, so this tool never posts anything. It
    writes an HTML file with inline styles that keep headings, code blocks and
    tables intact when pasted into the editor. The user posts it by hand and
    then reports the URL with record_manually_posted_url.

    Returns:
        On success: status, file (path of the HTML file), publication and
        message with the manual steps. On failure: status 'error' with
        error_code, message and recovery_instruction.
    """
    checked = _publishing_preconditions(tool_context, "naver_blog")
    if isinstance(checked, dict):
        return checked
    project_id, language, markdown = checked
    title = tool_context.state.get(state.SEED_TITLE) or ""
    html = markdown_to_inline_styled_html(markdown)
    file = write_workspace_file(project_id, "naver_blog.html", html)
    record = PublicationRecord(
        platform="naver_blog",
        language=language,  # type: ignore[arg-type]
        state="exported_for_manual_post",
        title=title,
    )
    _remember_publication(tool_context, project_id, record)
    return {
        "status": "success",
        "file": file,
        "publication": record.model_dump(),
        "message": "Not posted: Naver Blog must be posted by hand. Tell the user to "
        "open the HTML file in a browser, select all, copy, paste it into the Naver "
        "Blog editor, publish, and then give you the post URL.",
    }


async def publish_qiita_article_draft(
    title: str, tags: list[str], tool_context: ToolContext
) -> dict[str, Any]:
    """Save the Japanese article to Qiita as a private draft, after human approval.

    The first call does not send anything: it pauses and asks the user to
    approve. When the run is resumed with approval, the draft is created, or
    updated if this project already has a Qiita draft. The draft stays private;
    making it public is done by the user on Qiita.

    Args:
        title: Japanese title of the article, at most 255 characters.
        tags: One to five Qiita tags without spaces, for example ['AI', 'LLM'].

    Returns:
        status 'pending_human_approval' while waiting, 'declined' if refused,
        or 'success' with the publication record (article_id, url, state).
        On failure: status 'error' with error_code, message and
        recovery_instruction.
    """
    try:
        request = QiitaDraftInput(title=title, tags=tags)
    except ValidationError as exc:
        return validation_error(
            exc, "Fix the title or tags (1-5 tags, no spaces) and call again."
        )
    checked = _publishing_preconditions(tool_context, "qiita")
    if isinstance(checked, dict):
        return checked
    project_id, language, markdown = checked
    existing = (tool_context.state.get(state.PUBLICATIONS) or {}).get("qiita") or {}
    item_id = existing.get("article_id") if existing.get("state") == "draft" else None

    pending = _approval(
        tool_context,
        platform="qiita",
        title=request.title,
        chars=len(markdown),
        updating=bool(item_id),
    )
    if pending:
        return pending
    try:
        item = await platforms.qiita_save_item(
            title=request.title,
            body=markdown,
            tags=request.tags,
            private=True,
            item_id=item_id,
        )
    except platforms.PlatformError as exc:
        return tool_error(exc.code, exc.message, exc.recovery)
    record = PublicationRecord(
        platform="qiita",
        language=language,  # type: ignore[arg-type]
        article_id=item["id"],
        url=item["url"],
        state="draft",
        title=request.title,
    )
    _remember_publication(tool_context, project_id, record)
    return PublishOutput(
        status="success",
        publication=record,
        message="Saved on Qiita as a private draft. The user makes it public on Qiita.",
    ).model_dump()


async def publish_devto_article_draft(
    title: str, tags: list[str], tool_context: ToolContext
) -> dict[str, Any]:
    """Save the English article to DEV Community as an unpublished draft, after human approval.

    The first call does not send anything: it pauses and asks the user to
    approve. When the run is resumed with approval, the draft is created, or
    updated if this project already has a DEV draft. The draft stays
    unpublished; publishing is done by the user on dev.to.

    Args:
        title: English title of the article, at most 128 characters.
        tags: Up to four DEV tags, lowercase letters and digits only, for
            example ['ai', 'agents', 'python'].

    Returns:
        status 'pending_human_approval' while waiting, 'declined' if refused,
        or 'success' with the publication record (article_id, url, state).
        On failure: status 'error' with error_code, message and
        recovery_instruction.
    """
    try:
        request = DevtoDraftInput(title=title, tags=tags)
    except ValidationError as exc:
        return validation_error(
            exc,
            "Fix the tags (at most 4, lowercase letters and digits only) or the "
            "title and call again.",
        )
    checked = _publishing_preconditions(tool_context, "devto")
    if isinstance(checked, dict):
        return checked
    project_id, language, markdown = checked
    existing = (tool_context.state.get(state.PUBLICATIONS) or {}).get("devto") or {}
    article_id = (
        existing.get("article_id") if existing.get("state") == "draft" else None
    )

    pending = _approval(
        tool_context,
        platform="devto",
        title=request.title,
        chars=len(markdown),
        updating=bool(article_id),
    )
    if pending:
        return pending
    try:
        article = await platforms.devto_save_article(
            title=request.title,
            body=markdown,
            tags=request.tags,
            published=False,
            article_id=article_id,
        )
    except platforms.PlatformError as exc:
        return tool_error(exc.code, exc.message, exc.recovery)
    record = PublicationRecord(
        platform="devto",
        language=language,  # type: ignore[arg-type]
        article_id=str(article["id"]),
        url=article.get("url"),
        state="draft",
        title=request.title,
    )
    _remember_publication(tool_context, project_id, record)
    return PublishOutput(
        status="success",
        publication=record,
        message="Saved on DEV Community as an unpublished draft. The user publishes "
        "it on dev.to.",
    ).model_dump()


def record_manually_posted_url(
    platform: str, url: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Record the URL of a post the user published by hand or made public.

    Use this after the user posts the Naver Blog export, or after they make a
    Qiita or DEV draft public, so the URL can be verified and listed.

    Args:
        platform: 'naver_blog', 'qiita' or 'devto'.
        url: https URL of the published post on that platform.

    Returns:
        On success: status and the updated publication record. On failure:
        status 'error' with error_code, message and recovery_instruction.
    """
    project_id = tool_context.state.get(state.PROJECT_ID)
    if not project_id:
        return tool_error(
            "NO_PROJECT",
            "No article project in this session.",
            "Start with a seed article.",
        )
    if platform not in LANGUAGE_FOR_PLATFORM:
        return tool_error(
            "UNKNOWN_PLATFORM",
            f"'{platform}' is not a supported platform.",
            "Use one of: naver_blog, qiita, devto.",
        )
    from app.schemas import FetchArticleInput  # reuse the URL allowlist

    try:
        checked_url = FetchArticleInput(url=url).url
    except ValidationError as exc:
        return validation_error(exc, "Ask the user for the https URL of the post.")
    previous = (tool_context.state.get(state.PUBLICATIONS) or {}).get(platform) or {}
    record = PublicationRecord(
        platform=platform,  # type: ignore[arg-type]
        language=LANGUAGE_FOR_PLATFORM[platform],  # type: ignore[arg-type]
        article_id=previous.get("article_id"),
        url=checked_url,
        state="published",
        title=previous.get("title") or tool_context.state.get(state.SEED_TITLE),
    )
    _remember_publication(tool_context, project_id, record)
    return {"status": "success", "publication": record.model_dump()}


async def _check_publication(record: dict[str, Any]) -> UrlCheck:
    platform, url = record["platform"], record.get("url")
    if not url:
        return UrlCheck(
            url="",
            platform=platform,
            reachable=False,
            problem="No URL recorded yet (not posted).",
        )
    try:
        if platform == "qiita" and record.get("article_id"):
            item = await platforms.qiita_get_item(record["article_id"])
            return UrlCheck(
                url=url,
                platform=platform,
                reachable=True,
                http_status=200,
                title_found=item.get("title"),
            )
        if platform == "devto" and record.get("state") == "draft":
            article = await platforms.devto_find_own_article(record["article_id"])
            if article is None:
                return UrlCheck(
                    url=url,
                    platform=platform,
                    reachable=False,
                    problem="Draft not found among the account's articles.",
                )
            return UrlCheck(
                url=url,
                platform=platform,
                reachable=True,
                http_status=200,
                title_found=article.get("title"),
            )
        status, title = await platforms.fetch_status_and_title(
            url.replace("https://blog.naver.com/", "https://m.blog.naver.com/")
        )
        return UrlCheck(
            url=url,
            platform=platform,
            reachable=status == 200,
            http_status=status,
            title_found=title,
            problem=None if status == 200 else f"HTTP {status}",
        )
    except platforms.PlatformError as exc:
        return UrlCheck(
            url=url, platform=platform, reachable=False, problem=exc.message
        )
    except Exception as exc:  # network errors
        return UrlCheck(
            url=url, platform=platform, reachable=False, problem=type(exc).__name__
        )


async def verify_published_article_urls(tool_context: ToolContext) -> dict[str, Any]:
    """Check every recorded publication of this article, all platforms at once.

    Qiita and DEV drafts are checked through their APIs (drafts are not
    publicly visible); public posts are fetched over HTTP. Run this after
    publishing and after the user reports a manually posted URL.

    Returns:
        status, all_reachable, and one check per platform with url, reachable,
        http_status, title_found and problem. On failure: status 'error' with
        error_code, message and recovery_instruction.
    """
    publications = tool_context.state.get(state.PUBLICATIONS) or {}
    if not publications:
        return tool_error(
            "NOTHING_PUBLISHED",
            "No publication has been recorded for this article yet.",
            "Publish or export first, or record a manually posted URL.",
        )
    checks = await asyncio.gather(
        *(_check_publication(record) for record in publications.values())
    )
    return {
        "status": "success",
        "all_reachable": all(check.reachable for check in checks),
        "checks": [check.model_dump() for check in checks],
    }
