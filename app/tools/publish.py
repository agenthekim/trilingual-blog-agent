"""Stage 3 tools: finalize, export, post, and verify.

Posting is the one place where the agent acts outside the conversation, so it
is gated three times. A person finalizes the post (title and text); the rules
in ``app.policy`` refuse anything that is not finalized, is the original, is
already public or carries credential-looking values nobody acknowledged; and
the tool stops and asks for human approval before it sends anything. The
rules are checked when the tool is first called and again when the run is
resumed with the approval, because the text can change in between.

Posts are created as private (Qiita) or unpublished (DEV) drafts unless the
person approving asks for a public post in that same approval. The model has
no argument for it, and none for the title either: the title sent is the one
that was finalized.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import markdown as markdown_lib
from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from app import platforms, policy, state
from app.persistence import get_article_store
from app.schemas import (
    DevtoDraftInput,
    LanguageCode,
    PlatformName,
    PostedUrlInput,
    PublicationRecord,
    PublishOutput,
    QiitaDraftInput,
    UrlCheck,
    checked_language,
    tool_error,
    validation_error,
)
from app.settings import (
    ALLOWED_ARTICLE_HOSTS,
    LANGUAGE_FOR_PLATFORM,
    LANGUAGE_NAMES,
)
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


# Naver's editor is the last line of defence, not the first: what the export
# hands a person to paste carries no script, no event handler and no link
# that runs code, whatever the article's own HTML contained.
# svg and math go with their content: both can carry links and animations
# whose targets are set by attributes no list of URL attributes covers.
_DROPPED_WITH_CONTENT = {
    "script",
    "style",
    "iframe",
    "object",
    "template",
    "svg",
    "math",
}
_DROPPED_TAGS = _DROPPED_WITH_CONTENT | {
    "embed",  # has no end tag, so there is no content to skip
    "form",
    "input",
    "button",
    "textarea",
    "select",
    "link",
    "meta",
    "base",
}
_URL_ATTRIBUTES = {"href", "src", "action", "formaction", "poster", "xlink:href"}
_VOID_TAGS = {"br", "hr", "img", "wbr", "col"}


def _safe_url(value: str) -> bool:
    compact = re.sub(r"[\x00-\x20]+", "", value).lower()
    scheme = re.match(r"^([a-z][a-z0-9+.-]*):", compact)
    return scheme is None or scheme.group(1) in ("http", "https", "mailto")


class _Sanitizer(HTMLParser):
    """Re-emits HTML without the tags and attributes that can run code."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED_WITH_CONTENT:
            self._skipping += 1
        if self._skipping or tag in _DROPPED_TAGS:
            return
        kept = []
        for name, value in attrs:
            if name.startswith("on") or name == "srcdoc":
                continue
            if name in _URL_ATTRIBUTES and not _safe_url(value or ""):
                continue
            kept.append(name if value is None else f'{name}="{html_lib.escape(value)}"')
        self.out.append(f"<{' '.join([tag, *kept])}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED_WITH_CONTENT:
            return  # self-closed: there is no content to skip
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_WITH_CONTENT:
            self._skipping = max(0, self._skipping - 1)
            return
        if self._skipping or tag in _DROPPED_TAGS or tag in _VOID_TAGS:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.out.append(html_lib.escape(data, quote=False))


def sanitize_html(markup: str) -> str:
    parser = _Sanitizer()
    parser.feed(markup)
    parser.close()
    return "".join(parser.out)


def markdown_to_inline_styled_html(markdown: str) -> str:
    """Render Markdown to HTML whose formatting survives pasting into a rich editor.

    Rich editors such as Naver SmartEditor drop ``<style>`` blocks and class
    names, so every element carries its own ``style`` attribute. Raw HTML in
    the article is kept only as far as it cannot run code.
    """
    html = sanitize_html(
        markdown_lib.markdown(
            markdown, extensions=["fenced_code", "tables", "sane_lists"]
        )
    )
    for tag, style in _INLINE_STYLES.items():
        html = re.sub(
            rf"<{tag}(\s[^>]*)?>",
            lambda match, tag=tag, style=style: _styled(tag, match.group(1), style),
            html,
        )
    return html


_STYLE_ATTRIBUTE = re.compile(r'\sstyle="([^"]*)"')


def _styled(tag: str, attributes: str | None, style: str) -> str:
    """The opening tag with ``style`` added; an existing style is kept after it.

    A table cell can already carry its alignment. Two style attributes would
    lose one of them, so they are joined into one.
    """
    attributes = attributes or ""
    existing = _STYLE_ATTRIBUTE.search(attributes)
    if existing:
        attributes = _STYLE_ATTRIBUTE.sub("", attributes, count=1)
        style = f"{style}{existing.group(1)}"
    return f'<{tag}{attributes} style="{style}">'


def _refused(refusal: policy.Refusal) -> dict[str, Any]:
    return tool_error(refusal.code, refusal.message, refusal.recovery)


def _post_to_send(
    tool_context: ToolContext, platform: str
) -> tuple[str, str, str, str] | dict[str, Any]:
    """(project_id, language, title, markdown) of a post that may be sent, or a ToolError.

    The title and the text come from the session, never from the model: they
    are what a person finalized.
    """
    rule = (
        policy.posting_refusal
        if platform in policy.API_PLATFORMS
        else policy.export_refusal
    )
    if refusal := rule(tool_context.state, platform):
        return _refused(refusal)
    language = LANGUAGE_FOR_PLATFORM[platform]
    return (
        tool_context.state.get(state.PROJECT_ID),
        language,
        state.article_title(tool_context.state, language),
        state.article_markdown(tool_context.state, language),
    )


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
    tool_context: ToolContext,
    *,
    platform: str,
    title: str,
    tags: list[str],
    chars: int,
    existing: dict[str, Any],
) -> dict[str, Any] | None:
    """Stop for human approval. Returns a result dict unless approval was given."""
    name = policy.PLATFORM_NAMES[platform]
    updating = existing.get("state") == "draft" and bool(existing.get("article_id"))
    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        action = f"create a draft on {name}"
        if updating:
            # Say whose draft is replaced: after a revision the title on the
            # platform can be another than the one that is about to be sent.
            was = existing.get("title")
            action = f"update the existing draft on {name}" + (
                f" (now titled '{was}')" if was and was != title else ""
            )
        tool_context.request_confirmation(
            hint=f"Approve to {action}: '{title}' ({chars} characters, "
            f"tags: {', '.join(tags) or 'none'}). It will be saved as a "
            "non-public draft, unless the approval itself asks for a public post.",
            payload={
                "action": "post",
                "platform": platform,
                "title": title,
                "tags": tags,
                "updating": updating,
                "replaces_title": existing.get("title") if updating else None,
            },
        )
        return PublishOutput(
            status="pending_human_approval",
            message=f"Waiting for the user's approval before sending anything to {name}.",
        ).model_dump()
    if not confirmation.confirmed:
        return PublishOutput(
            status="declined",
            message=f"The user declined publishing to {name}. Nothing was sent.",
        ).model_dump()
    return None


def _approved_as_public(tool_context: ToolContext) -> bool:
    """Whether the person who approved asked for a public post rather than a draft.

    The choice travels in the approval a person gives, never in the arguments
    the model passes, so the model cannot make anything public by itself. An
    approval that says nothing about it means a non-public draft.
    """
    payload = getattr(tool_context.tool_confirmation, "payload", None)
    return isinstance(payload, dict) and payload.get("visibility") == "public"


def finalize_localized_post(
    language: LanguageCode, tool_context: ToolContext
) -> dict[str, Any]:
    """Record the user's sign-off of one post (its title and text), after they confirm.

    Posting to Qiita or DEV Community needs it. Use it when the user says a
    post is ready, on a surface without the web page's Finalize button. The
    first call records nothing: it pauses and asks the user to confirm. The
    confirmation holds for the title and text as they were when the user was
    asked; if either changed meanwhile, nothing is recorded. Any later change
    undoes the sign-off. You cannot confirm for the user.

    Args:
        language: Which post: 'ko', 'en' or 'ja'.

    Returns:
        status 'pending_human_approval' while waiting, 'declined' if refused,
        or 'success' with language, title and finalized. On failure: status
        'error' with error_code, message and recovery_instruction.
    """
    checked = checked_language(language)
    if isinstance(checked, dict):
        return checked
    language = checked
    confirmation = tool_context.tool_confirmation
    payload = getattr(confirmation, "payload", None)
    acknowledged = isinstance(payload, dict) and bool(
        payload.get("acknowledge_findings")
    )
    found = policy.findings(tool_context.state, language)
    # Findings do not stop the request from being raised; whether they are
    # accepted is decided below, from what the person answered.
    if refusal := policy.finalize_refusal(
        tool_context.state, language, acknowledge_findings=True
    ):
        return _refused(refusal)
    title = state.article_title(tool_context.state, language)
    text = state.article_markdown(tool_context.state, language) or ""
    digest = policy.post_digest(title, text)
    name = LANGUAGE_NAMES[language]
    # None when a person started this from the page or the script: there the
    # click is the answer, and there was no earlier request to compare with.
    call_id = getattr(tool_context, "function_call_id", None)
    asked = dict(tool_context.state.get(state.FINALIZATIONS_ASKED) or {})
    if confirmation is None:
        warning = (
            " It has values that look like credentials "
            f"({policy.describe_findings(found)}). A yes here does not accept "
            "them: remove them, or finalize on the web page or with the script, "
            "where you can say that they are not real."
            if found
            else ""
        )
        if call_id:
            asked[call_id] = digest
            tool_context.state[state.FINALIZATIONS_ASKED] = asked
        tool_context.request_confirmation(
            hint=f"Finalize the {name} post '{title}' ({len(text)} characters) as "
            f"reviewed and ready to send?{warning}",
            payload={"action": "finalize", "language": language, "title": title},
        )
        return {
            "status": "pending_human_approval",
            "message": f"Waiting for the user to confirm that the {name} post is final.",
        }
    if call_id:
        expected = asked.pop(call_id, None)
        tool_context.state[state.FINALIZATIONS_ASKED] = asked
        if confirmation.confirmed and expected != digest:
            return tool_error(
                "POST_CHANGED",
                f"The {name} post changed after the user was asked to finalize it, "
                "so their answer does not cover what it is now. Nothing was recorded.",
                "Tell the user the post changed, ask them to review it again, and "
                "call finalize_localized_post again when they say it is ready.",
            )
    if not confirmation.confirmed:
        return {
            "status": "declined",
            "message": f"The user did not finalize the {name} post.",
        }
    # Findings are accepted only by a person who was shown them and said so
    # in the approval itself; a plain yes is not enough.
    if found and not acknowledged:
        return tool_error(
            "SECRET_IN_ARTICLE",
            f"The {name} text has values that look like credentials: "
            f"{policy.describe_findings(found)}. A yes in a chat cannot accept them.",
            "Do not ask again here. Tell the user to remove the values, or to "
            "finalize this post on the web page or with scripts/run_pipeline.py, "
            "where they are shown the findings and can say they are not real.",
        )
    marks = dict(tool_context.state.get(state.FINALIZED_POSTS) or {})
    marks[language] = policy.finalization(tool_context.state, language, acknowledged)
    tool_context.state[state.FINALIZED_POSTS] = marks
    return {
        "status": "success",
        "language": language,
        "title": title,
        "finalized": True,
        "structure_check": (tool_context.state.get(state.FIDELITY_REPORT) or {}).get(
            language
        ),
    }


def export_naver_blog_paste_ready_html(tool_context: ToolContext) -> dict[str, Any]:
    """Export the Korean article as HTML ready to paste into the Naver Blog editor.

    Naver Blog has no public write API, so this tool never posts anything. It
    writes an HTML file with inline styles that keep headings, code blocks and
    tables intact when pasted into the editor. The user posts it by hand and
    then reports the URL with record_manually_posted_url.

    Returns:
        On success: status, file (path of the HTML file), title, publication and
        message with the manual steps. On failure: status 'error' with
        error_code, message and recovery_instruction.
    """
    checked = _post_to_send(tool_context, "naver_blog")
    if isinstance(checked, dict):
        return checked
    project_id, language, title, markdown = checked
    file = write_workspace_file(
        project_id, "naver_blog.html", markdown_to_inline_styled_html(markdown)
    )
    previous = policy.publication(tool_context.state, "naver_blog")
    if previous.get("state") == "published":
        # The post is up; writing the file again must not forget its address.
        record = PublicationRecord.model_validate(previous)
    else:
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
        "title": title,
        "publication": record.model_dump(),
        "message": "Not posted: Naver Blog must be posted by hand. Tell the user to "
        "open the HTML file in a browser, select all, copy, paste it into the Naver "
        "Blog editor, type the title above into the editor's title field, publish, "
        "and then give you the post URL.",
    }


async def publish_qiita_article_draft(
    tags: list[str], tool_context: ToolContext
) -> dict[str, Any]:
    """Save the finalized Japanese article to Qiita as a private draft, after human approval.

    The post must have been finalized by the user. The title and text sent are
    the finalized ones; you cannot pass others. The first call does not send
    anything: it pauses and asks the user to approve. When the run is resumed
    with approval, the draft is created, or updated if this project already
    has a Qiita draft. The draft stays private and the user makes it public on
    Qiita, unless the user asked for a public post in the approval itself. You
    cannot choose that; never promise it.

    Args:
        tags: One to five Qiita tags without spaces, for example ['AI', 'LLM'].

    Returns:
        status 'pending_human_approval' while waiting, 'declined' if refused,
        or 'success' with the publication record (article_id, url, state).
        On failure: status 'error' with error_code, message and
        recovery_instruction.
    """
    checked = _post_to_send(tool_context, "qiita")
    if isinstance(checked, dict):
        return checked
    project_id, language, title, markdown = checked
    try:
        request = QiitaDraftInput(title=title, tags=tags)
    except ValidationError as exc:
        return validation_error(
            exc,
            "Fix the tags (1-5 tags, no spaces) and call again. If the title is "
            "the problem (255 characters at most), the user shortens it in the "
            "editor and finalizes the post again.",
        )
    existing = policy.publication(tool_context.state, "qiita")
    item_id = existing.get("article_id") if existing.get("state") == "draft" else None

    pending = _approval(
        tool_context,
        platform="qiita",
        title=request.title,
        tags=request.tags,
        chars=len(markdown),
        existing=existing,
    )
    if pending:
        return pending
    public = _approved_as_public(tool_context)
    try:
        item = await platforms.qiita_save_item(
            title=request.title,
            body=markdown,
            tags=request.tags,
            private=not public,
            item_id=item_id,
        )
    except platforms.PlatformError as exc:
        return tool_error(exc.code, exc.message, exc.recovery)
    record = PublicationRecord(
        platform="qiita",
        language=language,  # type: ignore[arg-type]
        article_id=item["id"],
        url=item["url"],
        state="published" if public else "draft",
        title=request.title,
    )
    _remember_publication(tool_context, project_id, record)
    return PublishOutput(
        status="success",
        publication=record,
        message="Posted on Qiita and public, as the user chose when approving."
        if public
        else "Saved on Qiita as a private draft. The user makes it public on Qiita.",
    ).model_dump()


async def publish_devto_article_draft(
    tags: list[str], tool_context: ToolContext
) -> dict[str, Any]:
    """Save the finalized English article to DEV Community as an unpublished draft, after human approval.

    The post must have been finalized by the user. The title and text sent are
    the finalized ones; you cannot pass others. The first call does not send
    anything: it pauses and asks the user to approve. When the run is resumed
    with approval, the draft is created, or updated if this project already
    has a DEV draft. The draft stays unpublished and the user publishes it on
    dev.to, unless the user asked for a public post in the approval itself.
    You cannot choose that; never promise it.

    Args:
        tags: Up to four DEV tags, lowercase letters and digits only, for
            example ['ai', 'agents', 'python'].

    Returns:
        status 'pending_human_approval' while waiting, 'declined' if refused,
        or 'success' with the publication record (article_id, url, state).
        On failure: status 'error' with error_code, message and
        recovery_instruction.
    """
    checked = _post_to_send(tool_context, "devto")
    if isinstance(checked, dict):
        return checked
    project_id, language, title, markdown = checked
    try:
        request = DevtoDraftInput(title=title, tags=tags)
    except ValidationError as exc:
        return validation_error(
            exc,
            "Fix the tags (at most 4, lowercase letters and digits only) and "
            "call again. If the title is the problem (128 characters at most), "
            "the user shortens it in the editor and finalizes the post again.",
        )
    existing = policy.publication(tool_context.state, "devto")
    article_id = (
        existing.get("article_id") if existing.get("state") == "draft" else None
    )

    pending = _approval(
        tool_context,
        platform="devto",
        title=request.title,
        tags=request.tags,
        chars=len(markdown),
        existing=existing,
    )
    if pending:
        return pending
    public = _approved_as_public(tool_context)
    try:
        article = await platforms.devto_save_article(
            title=request.title,
            body=markdown,
            tags=request.tags,
            published=public,
            article_id=article_id,
        )
    except platforms.PlatformError as exc:
        return tool_error(exc.code, exc.message, exc.recovery)
    record = PublicationRecord(
        platform="devto",
        language=language,  # type: ignore[arg-type]
        article_id=str(article["id"]),
        url=article.get("url"),
        state="published" if public else "draft",
        title=request.title,
    )
    _remember_publication(tool_context, project_id, record)
    return PublishOutput(
        status="success",
        publication=record,
        message="Posted on DEV Community and public, as the user chose when approving."
        if public
        else "Saved on DEV Community as an unpublished draft. The user publishes "
        "it on dev.to.",
    ).model_dump()


def record_manually_posted_url(
    platform: PlatformName, url: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Record the URL of a post the user published by hand or made public.

    Use this after the user posts the Naver Blog export, or after they make a
    Qiita or DEV draft public, so the URL can be verified and listed. The URL
    must be on that platform's own site.

    Args:
        platform: 'naver_blog', 'qiita' or 'devto'.
        url: https URL of the published post on that platform.

    Returns:
        On success: status and the updated publication record. On failure:
        status 'error' with error_code, message and recovery_instruction.
    """
    try:
        # The platform is one of the three; the URL is https on one of their sites.
        request = PostedUrlInput(platform=platform, url=url)
    except ValidationError as exc:
        return validation_error(
            exc,
            "platform must be 'naver_blog', 'qiita' or 'devto'; ask the user for "
            "the https URL of the post on that platform.",
        )
    platform, checked_url = request.platform, request.url
    project_id = tool_context.state.get(state.PROJECT_ID)
    if not project_id:
        return tool_error(
            "NO_PROJECT",
            "No article project in this session.",
            "Start with a seed article.",
        )
    name = policy.PLATFORM_NAMES[platform]
    host = (urlparse(checked_url).hostname or "").lower().removeprefix("www.")
    if ALLOWED_ARTICLE_HOSTS.get(host) != platform:
        return tool_error(
            "URL_ON_ANOTHER_SITE",
            f"That address is not on {name}.",
            f"Ask the user for the address of the post on {name}.",
        )
    previous = policy.publication(tool_context.state, platform)
    if previous.get("state") == "existing_source":
        return tool_error(
            "ALREADY_PUBLISHED_SOURCE",
            f"The article was read from {name}; its address is already recorded.",
            "Nothing to record. Continue with the other platforms.",
        )
    language = LANGUAGE_FOR_PLATFORM[platform]
    record = PublicationRecord(
        platform=platform,  # type: ignore[arg-type]
        language=language,  # type: ignore[arg-type]
        article_id=previous.get("article_id"),
        url=checked_url,
        state="published",
        title=previous.get("title")
        or state.article_title(tool_context.state, language),
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
