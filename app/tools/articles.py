"""Stage 1 tools: bring an article in and fix it as the seed for localization."""

from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import urlparse

from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from app import platforms, policy, sources, state
from app.markdown_checks import check_fidelity
from app.persistence import get_article_store
from app.schemas import (
    CodeMode,
    FetchArticleInput,
    FetchArticleOutput,
    LanguageCode,
    LoadDraftInput,
    PassageReplacementInput,
    PostEditOutput,
    PostReadOutput,
    PostTitleInput,
    PublicationRecord,
    SaveSeedInput,
    SeedFinalizedOutput,
    SeedSavedOutput,
    checked_language,
    tool_error,
    tool_output,
    validation_error,
)
from app.settings import (
    ALLOWED_ARTICLE_HOSTS,
    LANGUAGE_NAMES,
    LANGUAGES,
    MAX_ARTICLE_CHARS,
    MAX_TITLE_CHARS,
    PLATFORM_FOR_LANGUAGE,
    workspace_dir,
)


def _new_project_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "article"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def write_workspace_file(project_id: str, name: str, content: str) -> str:
    """Write a generated file under ``<workspace>/<project_id>/`` and return its path."""
    folder = workspace_dir() / project_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(content, encoding="utf-8")
    return str(path)


_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.S)
_FRONT_MATTER_TITLE = re.compile(r"^title:[ \t]*(.+)$", re.M)


def _prose_lines(markdown: str) -> list[str]:
    """Lines of a draft outside front matter and fenced code blocks.

    A ``# comment`` in a shell or Python block looks like a heading; it is
    not one, and it must never become the title of an article.
    """
    body = _FRONT_MATTER.sub("", markdown, count=1)
    lines, fence = [], None
    for line in body.splitlines():
        marker = _FENCE.match(line)
        if marker and fence is None:
            fence = marker.group(1)
        elif marker and marker.group(1) == fence:
            fence = None
        elif fence is None:
            lines.append(line)
    return lines


def _heading_title(markdown: str) -> str:
    for line in _prose_lines(markdown):
        heading = re.match(r"^#\s+(\S.*)$", line)
        if heading:
            return heading.group(1).strip()
    return ""


def has_title_line(markdown: str) -> bool:
    """Whether a Markdown draft names its own title with a level-1 heading."""
    return bool(_heading_title(markdown))


def title_of_draft(markdown: str, fallback: str = "Untitled") -> str:
    """Title of a draft: its level-1 heading, else its front-matter title, else its first line."""
    title = _heading_title(markdown)
    if not title:
        front = _FRONT_MATTER.match(markdown)
        named = _FRONT_MATTER_TITLE.search(front.group(1)) if front else None
        title = named.group(1).strip().strip("\"'") if named else ""
    if not title:
        first = next((ln.strip() for ln in _prose_lines(markdown) if ln.strip()), "")
        title = first.lstrip("#").strip()
    return title[:200] or fallback


def _discard_what_the_old_text_produced(
    tool_context: ToolContext, *, new_article: bool
) -> None:
    """Drop what was made from a seed that is being replaced.

    A revised seed makes its localizations, their sign-offs and the slide
    plans stale; publication records and decks are still this article's and
    stay. Another article keeps nothing, so that no draft id, deck or title
    of the previous article is ever used for the new one.
    """
    keep = () if new_article else (state.PUBLICATIONS, state.GOOGLE_SLIDES_DECKS)
    for key in state.DERIVED_FROM_SEED:
        if key not in keep and tool_context.state.get(key) is not None:
            tool_context.state[key] = None
    for language in LANGUAGES:
        if tool_context.state.get(state.localized_key(language)) is not None:
            tool_context.state[state.localized_key(language)] = None
    if new_article:
        return  # the new article gets a folder of its own
    # The files made from the old text go too, so nothing stale is shown,
    # pasted or saved back. The seed's own file is rewritten by the caller.
    folder = workspace_dir() / str(tool_context.state.get(state.PROJECT_ID) or "")
    stale = ["naver_blog.html", "manifest.md"]
    stale += [f"{kind}.{code}.md" for kind in ("post", "slides") for code in LANGUAGES]
    for name in stale:
        if tool_context.state.get(state.PROJECT_ID):
            (folder / name).unlink(missing_ok=True)


def _register_seed(
    tool_context: ToolContext,
    *,
    title: str,
    markdown: str,
    source_language: str,
    finalized: bool,
    entry_mode: str,
    code_mode: str,
    code_repo_url: str | None,
    new_article: bool = False,
) -> SeedSavedOutput:
    """Persist a seed revision and publish it to session state."""
    previous = tool_context.state.get(state.SEED_MARKDOWN)
    if previous is not None and (new_article or previous != markdown):
        _discard_what_the_old_text_produced(tool_context, new_article=new_article)
    project_id = (
        None if new_article else tool_context.state.get(state.PROJECT_ID)
    ) or _new_project_id(title)
    targets = [lang for lang in LANGUAGES if lang != source_language]
    store = get_article_store()
    revision = store.add_version(project_id, source_language, "seed", markdown)
    store.save_project(
        project_id,
        {
            "title": title,
            "source_language": source_language,
            "target_languages": targets,
            "entry_mode": entry_mode,
            "seed_finalized": finalized,
            "code_mode": code_mode,
            "code_repo_url": code_repo_url,
        },
    )
    file = write_workspace_file(project_id, f"post.{source_language}.md", markdown)

    tool_context.state[state.PROJECT_ID] = project_id
    tool_context.state[state.SEED_TITLE] = title
    tool_context.state[state.SEED_MARKDOWN] = markdown
    tool_context.state[state.SOURCE_LANGUAGE] = source_language
    tool_context.state[state.TARGET_LANGUAGES] = targets
    tool_context.state[state.SEED_FINALIZED] = finalized
    tool_context.state[state.ENTRY_MODE] = entry_mode
    tool_context.state[state.CODE_MODE] = code_mode
    tool_context.state[state.CODE_REPO_URL] = code_repo_url or ""

    next_step = (
        "The seed is final. Offer to localize it into "
        + " and ".join(targets)
        + " by transferring to localization_pipeline."
        if finalized
        else "The seed is a draft. Help the user revise it, then call "
        "finalize_seed_article when they confirm it is final."
    )
    return SeedSavedOutput(
        project_id=project_id,
        revision=revision,
        source_language=source_language,  # type: ignore[arg-type]
        target_languages=targets,  # type: ignore[arg-type]
        finalized=finalized,
        file=file,
        next_step=next_step,
    )


async def fetch_published_article_markdown(
    url: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Fetch an already-published blog post and register it as the seed article.

    Use this when the user gives the URL of a post that already exists on Qiita,
    DEV Community or Naver Blog and wants it expanded into the other languages.
    The post is recorded as the existing publication for its platform, so that
    platform is never published to again.

    Qiita and DEV Community return the author's original Markdown, which is
    registered as a finalized seed. Naver Blog has no Markdown export; its text
    is reconstructed from the web page, registered as a draft, and must be
    reviewed by the user before it is finalized.

    Args:
        url: https URL of the post. Only qiita.com, dev.to and blog.naver.com
            are accepted.

    Returns:
        On success: status, platform, title, language, tags, markdown_chars,
        extraction ('api_markdown' or 'html_best_effort'), project_id and
        next_step. On failure: status 'error' with error_code, message and
        recovery_instruction.
    """
    try:
        request = FetchArticleInput(url=url)
    except ValidationError as exc:
        return validation_error(
            exc,
            f"Ask the user for an https URL on {sources.blog_hosts_sentence()}. "
            "Do not try other hosts.",
        )

    host = (urlparse(request.url).hostname or "").removeprefix("www.")
    platform = ALLOWED_ARTICLE_HOSTS[host]
    try:
        if platform == "qiita":
            item = await platforms.qiita_get_item(
                platforms.parse_qiita_item_id(request.url)
            )
            title, markdown = item["title"], item["body"]
            tags = [tag["name"] for tag in item.get("tags", [])]
            article_id, extraction = item["id"], "api_markdown"
        elif platform == "devto":
            article = await platforms.devto_get_article(
                *platforms.parse_devto_path(request.url)
            )
            title, markdown = article["title"], article["body_markdown"]
            tags = list(article.get("tags") or [])
            article_id, extraction = str(article["id"]), "api_markdown"
        else:
            blog_id, log_no = platforms.parse_naver_post(request.url)
            title, markdown = await platforms.naver_get_post(blog_id, log_no)
            tags, article_id, extraction = [], log_no, "html_best_effort"
    except platforms.PlatformError as exc:
        return tool_error(exc.code, exc.message, exc.recovery)

    if len(markdown) > MAX_ARTICLE_CHARS:
        return tool_error(
            "ARTICLE_TOO_LONG",
            f"The article has {len(markdown)} characters; the limit is {MAX_ARTICLE_CHARS}.",
            "Tell the user the article is too long to process in one piece and ask "
            "whether to split it.",
        )

    language = platforms.guess_language(markdown)
    saved = _register_seed(
        tool_context,
        title=title,
        markdown=markdown,
        source_language=language,
        finalized=extraction == "api_markdown",
        entry_mode="published_url",
        code_mode="inline_markdown" if "```" in markdown else "none",
        code_repo_url=None,
        new_article=True,
    )
    record = PublicationRecord(
        platform=platform,  # type: ignore[arg-type]
        language=language,  # type: ignore[arg-type]
        article_id=article_id,
        url=request.url,
        state="existing_source",
        title=title,
    ).model_dump()
    publications = dict(tool_context.state.get(state.PUBLICATIONS) or {})
    publications[platform] = record
    tool_context.state[state.PUBLICATIONS] = publications
    get_article_store().record_publication(saved.project_id, platform, record)

    note = (
        "Original Markdown retrieved through the platform API."
        if extraction == "api_markdown"
        else "Text was reconstructed from the web page: headings, lists, images and "
        "links may be missing. Ask the user to review the saved file before finalizing."
    )
    if PLATFORM_FOR_LANGUAGE[language] != platform:
        note += (
            f" Note: the article is written in '{language}', which is normally "
            f"posted on {PLATFORM_FOR_LANGUAGE[language]}, not {platform}."
        )
    return FetchArticleOutput(
        platform=platform,  # type: ignore[arg-type]
        url=request.url,
        title=title,
        language=language,  # type: ignore[arg-type]
        tags=tags,
        markdown_chars=len(markdown),
        extraction=extraction,  # type: ignore[arg-type]
        note=note,
        project_id=saved.project_id,
        seed_finalized=saved.finalized,
        target_languages=saved.target_languages,
        file=saved.file,
        next_step=saved.next_step,
    ).model_dump()


def load_markdown_draft_file(
    relative_path: str, source_language: LanguageCode, tool_context: ToolContext
) -> dict[str, Any]:
    """Load a local Markdown draft from the workspace directory as a seed draft.

    Use this when the user names a .md file they have been writing. The file is
    registered as a draft (not final) so it can be revised before localization.

    Args:
        relative_path: Path of the .md file relative to the workspace directory,
            for example 'drafts/adk-memory.md'. Absolute paths and '..' are refused.
        source_language: Language the draft is written in: 'ko', 'en' or 'ja'.

    Returns:
        On success: status, project_id, revision, target_languages, finalized
        (false), file and next_step. On failure: status 'error' with error_code,
        message and recovery_instruction.
    """
    try:
        request = LoadDraftInput(
            relative_path=relative_path,
            source_language=source_language,
        )
    except ValidationError as exc:
        return validation_error(
            exc,
            "Ask the user for a .md path relative to the workspace directory; "
            "source_language must be 'ko', 'en' or 'ja'.",
        )
    root = workspace_dir()
    path = (root / request.relative_path).resolve()
    if root not in path.parents or not path.is_file():
        return tool_error(
            "DRAFT_NOT_FOUND",
            f"No file '{request.relative_path}' inside the workspace directory.",
            "Ask the user to check the file name, or to paste the draft text instead.",
        )
    markdown = path.read_text(encoding="utf-8")
    return save_seed_article_revision(
        title=title_of_draft(markdown, fallback=path.stem),
        markdown=markdown,
        source_language=request.source_language,
        code_mode="inline_markdown" if "```" in markdown else "none",
        code_repo_url="",
        finalized=False,
        tool_context=tool_context,
    )


def save_seed_article_revision(
    title: str,
    markdown: str,
    source_language: LanguageCode,
    code_mode: CodeMode,
    code_repo_url: str,
    finalized: bool,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Save a new revision of the seed article (the single source for all languages).

    Call this each time the draft changes: when the user pastes a draft, and
    after every revision you make. Every call stores a new numbered revision,
    so earlier text is never lost.

    Args:
        title: Article title in the source language.
        markdown: Complete Markdown body of the article, not a diff or excerpt.
        source_language: 'ko', 'en' or 'ja'.
        code_mode: How companion code is delivered: 'none', 'inline_markdown',
            'python_script', 'jupyter_notebook' or 'jupyter_book'. Use 'none'
            for articles without code.
        code_repo_url: https URL of the repository holding companion code, or
            an empty string when there is none.
        finalized: true only when the user has explicitly said the seed is final.

    Returns:
        On success: status, project_id, revision, source_language,
        target_languages, finalized, file and next_step. On failure: status
        'error' with error_code, message and recovery_instruction.
    """
    try:
        request = SaveSeedInput(
            title=title,
            markdown=markdown,
            source_language=source_language,
            code_mode=code_mode,
            code_repo_url=code_repo_url or None,
            finalized=finalized,
        )
    except ValidationError as exc:
        return validation_error(
            exc,
            "Fix the named argument and call again. markdown must be the full "
            "article (at least 50 characters); source_language must be ko, en or ja.",
        )
    entry_mode = tool_context.state.get(state.ENTRY_MODE) or "draft"
    saved = _register_seed(
        tool_context,
        title=request.title,
        markdown=request.markdown,
        source_language=request.source_language,
        finalized=request.finalized,
        entry_mode=entry_mode,
        code_mode=request.code_mode,
        code_repo_url=request.code_repo_url,
    )
    return saved.model_dump()


def finalize_seed_article(tool_context: ToolContext) -> dict[str, Any]:
    """Mark the current seed article as final, without changing its text.

    Call this only after the user explicitly confirms the seed needs no more
    changes. Localization and publishing require a finalized seed.

    Returns:
        On success: status, project_id, source_language, target_languages and
        next_step. On failure: status 'error' with error_code, message and
        recovery_instruction.
    """
    project_id = tool_context.state.get(state.PROJECT_ID)
    if not project_id or not tool_context.state.get(state.SEED_MARKDOWN):
        return tool_error(
            "NO_SEED_ARTICLE",
            "There is no seed article in this session yet.",
            "Ask the user for a draft (pasted text or a workspace .md file) or the "
            "URL of a published post.",
        )
    tool_context.state[state.SEED_FINALIZED] = True
    get_article_store().save_project(project_id, {"seed_finalized": True})
    targets = tool_context.state.get(state.TARGET_LANGUAGES) or []
    return tool_output(
        SeedFinalizedOutput,
        status="success",
        project_id=project_id,
        source_language=tool_context.state.get(state.SOURCE_LANGUAGE),
        target_languages=targets,
        next_step="Offer to localize into "
        + " and ".join(targets)
        + " by transferring to localization_pipeline.",
    )


def read_article_post(
    language: LanguageCode, tool_context: ToolContext
) -> dict[str, Any]:
    """Read the current title and Markdown text of one language's blog post.

    Use this before answering a question about a post or changing it, so that
    what you say or change is based on the text as it is now, including what
    the user edited on the page.

    Args:
        language: Which post to read: 'ko', 'en' or 'ja'.

    Returns:
        On success: status, language, title and markdown (the whole text). On
        failure: status 'error' with error_code, message and
        recovery_instruction.
    """
    checked = checked_language(language)
    if isinstance(checked, dict):
        return checked
    language = checked
    text = state.article_markdown(tool_context.state, language)
    if not text:
        return tool_error(
            "NO_POST_FOR_LANGUAGE",
            f"There is no {language} post in this session yet.",
            "Localize the seed article first.",
        )
    return tool_output(
        PostReadOutput,
        status="success",
        language=language,
        title=state.article_title(tool_context.state, language),
        markdown=text,
    )


_HEADING_LINE = re.compile(r"^(#\s+)(\S.*?)\s*$")


def _with_heading_renamed(markdown: str, old_title: str, new_title: str) -> str:
    """The text with its first heading line renamed, when that line is the old title.

    The first level-1 heading outside front matter and code blocks is the
    title line (see ``title_of_draft``). A heading that says something else
    than the old title was written on purpose and is left as it is.
    """
    front = _FRONT_MATTER.match(markdown)
    start = front.end() if front else 0
    lines = markdown[start:].splitlines(keepends=True)
    fence = None
    for index, line in enumerate(lines):
        marker = _FENCE.match(line)
        if marker and fence is None:
            fence = marker.group(1)
        elif marker and marker.group(1) == fence:
            fence = None
        elif fence is None and (heading := _HEADING_LINE.match(line.rstrip("\r\n"))):
            if heading.group(2) == old_title:
                ending = line[len(line.rstrip("\r\n")) :]
                lines[index] = heading.group(1) + new_title + ending
            break
    return markdown[:start] + "".join(lines)


def _post_that_may_change(
    tool_context: ToolContext, language: str
) -> tuple[str, str, str] | dict[str, Any]:
    """(project_id, title, text) of a post that may be changed, or a ToolError.

    A post that is public is settled: changing the copy here would make it
    differ from what readers see. Whether it is public is decided by the
    publishing rules in ``app/policy.py``.
    """
    project_id = tool_context.state.get(state.PROJECT_ID) or ""
    text = state.article_markdown(tool_context.state, language) or ""
    if not project_id or not text:
        return tool_error(
            "NO_POST_FOR_LANGUAGE",
            f"There is no {language} post in this session yet.",
            "Localize the seed article first.",
        )
    if policy.next_step(tool_context.state, language) == "public":
        platform = policy.PLATFORM_NAMES[PLATFORM_FOR_LANGUAGE[language]]
        return tool_error(
            "POST_IS_PUBLIC",
            f"The {LANGUAGE_NAMES[language]} post is already public on {platform}; "
            "it can no longer be changed here.",
            "Do not try again. Tell the user to edit the public post on the site.",
        )
    return project_id, state.article_title(tool_context.state, language), text


def apply_post_edit(
    language: str, title: str, markdown: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Change the title, the text, or both, of one language's post (PUB-11).

    Every change to a post that exists goes through here, whoever makes it:
    the editor page and the script (``ArticlePipeline.save_edit``) and the
    agent's tools (``set_article_post_title``,
    ``replace_passage_in_article_post``). In this one place a public post is
    refused, the structure of a localization is checked again, a version is
    kept, the file is written, and for the Korean post the Naver file is
    written again or removed. A change undoes the post's finalization by
    itself: the digest a person signed no longer matches (``app/policy.py``).

    An empty ``title`` keeps the title, an empty ``markdown`` keeps the text.
    When only the title is given and the first heading line of the text is
    the old title, that line gets the new title; a text that was handed in
    is stored as it was handed in.

    Editing the source-language post changes the seed text and keeps the
    localizations and slide plans (SRC-6); they are then shown as older than
    the source (``state.older_than_source``).

    Returns a tool result: status 'success' with language, title, file,
    markdown_chars, check (the structure check) and naver_file ('written',
    'removed' or None), or a ``ToolError``.
    """
    found = _post_that_may_change(tool_context, language)
    if isinstance(found, dict):
        return found
    project_id, old_title, old_text = found

    new_title = title.strip()[:MAX_TITLE_CHARS] if title and title.strip() else ""
    new_title = new_title or old_title
    if markdown and markdown.strip():
        new_text = markdown.rstrip() + "\n"
    elif new_title != old_title:
        new_text = _with_heading_renamed(old_text, old_title, new_title)
    else:
        new_text = old_text

    if new_title != old_title:
        titles = dict(tool_context.state.get(state.LOCALIZED_TITLES) or {})
        titles[language] = new_title
        tool_context.state[state.LOCALIZED_TITLES] = titles
    if language == tool_context.state.get(state.SOURCE_LANGUAGE):
        check = {"passed": True, "errors": [], "warnings": [], "stats": {}}
        tool_context.state[state.SEED_MARKDOWN] = new_text
        if new_title != old_title:
            tool_context.state[state.SEED_TITLE] = new_title
            get_article_store().save_project(project_id, {"title": new_title})
    else:
        check = check_fidelity(
            tool_context.state.get(state.SEED_MARKDOWN) or "", new_text
        )
        tool_context.state[state.localized_key(language)] = new_text
        reports = dict(tool_context.state.get(state.FIDELITY_REPORT) or {})
        reports[language] = check
        tool_context.state[state.FIDELITY_REPORT] = reports

    get_article_store().add_version(project_id, language, "edited", new_text)
    file = write_workspace_file(project_id, f"post.{language}.md", new_text)
    return tool_output(
        PostEditOutput,
        status="success",
        language=language,
        title=new_title,
        file=file,
        markdown_chars=len(new_text),
        check=check,
        naver_file=_naver_file_follows(tool_context, project_id, language),
    )


def _naver_file_follows(
    tool_context: ToolContext, project_id: str, language: str
) -> str | None:
    """Write the Naver paste file again after a change to the Korean post (PUB-7).

    By the same rules as any export: a text the export refuses (for example
    credential-looking values nobody accepted) leaves no file behind to be
    pasted by mistake. Returns 'written', 'removed', or None when the post
    has no Naver file (another language, or Naver is where it was read from).
    """
    # Imported here: app.tools.publish builds on this module.
    from app.tools.publish import export_naver_blog_paste_ready_html

    if (
        PLATFORM_FOR_LANGUAGE[language] != "naver_blog"
        or policy.publication(tool_context.state, "naver_blog").get("state")
        == "existing_source"
    ):
        return None
    exported = export_naver_blog_paste_ready_html(tool_context)
    if exported.get("status") == "success":
        return "written"
    (workspace_dir() / project_id / "naver_blog.html").unlink(missing_ok=True)
    return "removed"


def set_article_post_title(
    language: LanguageCode, title: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Set a new title for one language's blog post, leaving its text as it is.

    Use this when the user asks for another title of a post that exists. When
    the first heading line of the text is the old title, that line gets the
    new title too; nothing else in the text changes. A version is kept, and
    the change undoes the user's sign-off of the post. A post that is already
    public is not changed.

    Args:
        language: Which post to retitle: 'ko', 'en' or 'ja'.
        title: The new title, one line, at most 120 characters.

    Returns:
        On success: status, language, title, file, markdown_chars, check (the
        structure check of the post) and naver_file ('written' or 'removed'
        for the Korean post, else null). On failure: status 'error' with
        error_code, message and recovery_instruction.
    """
    try:
        request = PostTitleInput(language=language, title=title)
    except ValidationError as exc:
        return validation_error(
            exc,
            "Call again with language set to 'ko', 'en' or 'ja' and a title of "
            f"one line, 1 to {MAX_TITLE_CHARS} characters.",
        )
    return apply_post_edit(request.language, request.title, "", tool_context)


def replace_passage_in_article_post(
    language: LanguageCode, passage: str, replacement: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Replace one quoted passage of one language's blog post with another passage.

    Use this when the user asks for a change to the text of a post that
    exists. Call read_article_post first and quote the passage exactly as it
    is written there, character for character. The passage is replaced only
    when it occurs exactly once in the post; the rest of the post stays as it
    is. Call once for each passage that changes; never retype a whole post. A
    version is kept, and the change undoes the user's sign-off of the post. A
    post that is already public is not changed.

    Args:
        language: Which post to change: 'ko', 'en' or 'ja'.
        passage: The passage to replace, quoted exactly from the post and
            long enough to occur only once in it.
        replacement: What to put in its place. An empty string '' removes the
            passage.

    Returns:
        On success: status, language, title, file, markdown_chars, check (the
        structure check of the post) and naver_file ('written' or 'removed'
        for the Korean post, else null). On failure: status 'error' with
        error_code, message and recovery_instruction; when the passage was
        not found once, the message says how many times it occurs.
    """
    try:
        request = PassageReplacementInput(
            language=language, passage=passage, replacement=replacement
        )
    except ValidationError as exc:
        return validation_error(
            exc,
            "Call again with language set to 'ko', 'en' or 'ja', passage quoted "
            "exactly from read_article_post, and a replacement that differs from it.",
        )
    found = _post_that_may_change(tool_context, request.language)
    if isinstance(found, dict):
        return found
    _, _, text = found
    name = LANGUAGE_NAMES[request.language]
    count = text.count(request.passage)
    if count != 1:
        times = "does not occur" if count == 0 else f"occurs {count} times"
        return tool_error(
            "PASSAGE_NOT_FOUND" if count == 0 else "PASSAGE_NOT_UNIQUE",
            f"The quoted passage {times} in the {name} post (occurrences: {count}); "
            "it has to occur exactly once. Nothing was changed.",
            "Call read_article_post and quote a longer passage, exactly as it is "
            "written there, so that it occurs once in the post. Then call again."
            if count
            else "Call read_article_post and quote the passage exactly as it is "
            "written there, character for character, long enough to occur once "
            "in the post. Then call again.",
        )
    changed = text.replace(request.passage, request.replacement, 1)
    if not changed.strip() or len(changed) > MAX_ARTICLE_CHARS:
        return tool_error(
            "POST_SIZE_OUT_OF_RANGE",
            f"After the replacement the {name} post would have {len(changed.strip())} "
            f"characters; a post has 1 to {MAX_ARTICLE_CHARS}.",
            "Use a replacement that leaves a post of that size, or tell the user "
            "why the change cannot be made.",
        )
    return apply_post_edit(request.language, "", changed, tool_context)
