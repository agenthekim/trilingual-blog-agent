"""Tests for the pipeline helpers and the web UI's file endpoint: no model is called."""

from __future__ import annotations

import pytest

from app import state
from app.pipeline import overview, project_file, seed_message
from app.settings import workspace_dir
from app.tools.articles import write_workspace_file


def test_bare_url_becomes_a_seed_request() -> None:
    message = seed_message("  https://qiita.com/someone/items/0123456789abcdef0123 ")
    assert message.startswith("Use this already published post")
    assert message.endswith("https://qiita.com/someone/items/0123456789abcdef0123")


def test_short_request_with_a_supported_url_is_passed_through() -> None:
    text = "Expand this into Korean and English: https://dev.to/someone/my-post-1a2b"
    assert seed_message(text) == text


def test_other_text_is_treated_as_a_draft() -> None:
    draft = "# Title\n\nSee https://example.com for details.\n" + "Body. " * 50
    message = seed_message(draft)
    assert message.startswith("Save this draft as the seed article")
    assert message.endswith(draft.strip())

    long_draft_with_supported_link = "# T\n\nhttps://qiita.com/a/items/b\n" + "x " * 400
    assert seed_message(long_draft_with_supported_link).startswith("Save this draft")


def test_project_file_stays_inside_the_project_folder(isolated_workspace) -> None:
    write_workspace_file("proj-1", "post.ko.md", "본문")
    (workspace_dir() / "secret.txt").write_text("x", encoding="utf-8")
    assert project_file("proj-1", "post.ko.md") is not None
    assert project_file("proj-1", "missing.md") is None
    assert project_file("proj-1", "../secret.txt") is None
    assert project_file("..", "secret.txt") is None
    assert project_file("proj-1", "") is None
    assert project_file("", "post.ko.md") is None


def test_overview_lists_titles_files_and_next_steps(isolated_workspace) -> None:
    write_workspace_file("proj-2", "post.ko.md", "본문")
    write_workspace_file("proj-2", "naver_blog.html", "<p>본문</p>")
    write_workspace_file("proj-2", "slides.en.md", "# deck")
    session_state = {
        state.PROJECT_ID: "proj-2",
        state.SEED_TITLE: "元のタイトル",
        state.SEED_MARKDOWN: "本文",
        state.SOURCE_LANGUAGE: "ja",
        state.localized_key("ko"): "본문",
        state.LOCALIZED_TITLES: {"ko": "한국어 제목", "en": "English title", "ja": "x"},
        state.PUBLICATIONS: {
            "qiita": {"state": "existing_source", "url": "https://qiita.com/a/items/b"},
            "naver_blog": {"state": "exported_for_manual_post", "url": None},
        },
    }
    result = overview(session_state)
    rows = {row["language"]: row for row in result["rows"]}

    assert rows["ko"]["title"] == "한국어 제목"
    assert rows["ko"]["files"] == ["post.ko.md", "naver_blog.html"]
    assert rows["ko"]["status"] == "exported_for_manual_post"
    assert rows["ko"]["can_post_draft"] is False  # Naver has no write API
    assert rows["ko"]["can_record_url"] is True

    assert rows["ja"]["title"] == "元のタイトル"  # the source keeps its own title
    assert rows["ja"]["status"] == "existing_source"
    assert rows["ja"]["can_post_draft"] is False  # never post back to the source
    assert rows["ja"]["url"] == "https://qiita.com/a/items/b"

    assert rows["en"]["title"] == ""  # no English text yet
    assert rows["en"]["can_post_draft"] is False

    assert result["slides"] == ["slides.en.md"]
    assert result["manifest"] is None


def test_overview_of_an_empty_session() -> None:
    result = overview({})
    assert result["project_id"] == ""
    assert all(not row["title"] and not row["files"] for row in result["rows"])


async def _pipeline_with_article():
    from app.agent import app
    from app.pipeline import USER_ID, ArticlePipeline

    seed = "# 見出し\n\n本文です。[リンク](https://example.com)\n"
    korean = "# 제목\n\n본문입니다. [링크](https://example.com)\n"
    write_workspace_file("proj-3", "post.ko.md", korean)
    write_workspace_file("proj-3", "post.ja.md", seed)
    write_workspace_file("proj-3", "naver_blog.html", "<p>old</p>")
    pipeline = ArticlePipeline(log=lambda _: None)
    await pipeline._sessions.create_session(
        app_name=app.name,
        user_id=USER_ID,
        session_id=pipeline.session_id,
        state={
            state.PROJECT_ID: "proj-3",
            state.SEED_TITLE: "見出し",
            state.SEED_MARKDOWN: seed,
            state.SOURCE_LANGUAGE: "ja",
            state.localized_key("ko"): korean,
            state.LOCALIZED_TITLES: {"ko": "제목", "en": "Title", "ja": "見出し"},
        },
    )
    return pipeline


@pytest.mark.asyncio
async def test_edit_replaces_what_the_agent_will_publish(isolated_workspace) -> None:
    from app.persistence import get_article_store

    pipeline = await _pipeline_with_article()
    edited = "# 제목\n\n손으로 고친 본문입니다. [링크](https://example.com)"
    result = await pipeline.save_edit("post.ko.md", edited, title="  새 제목 ")

    assert result["check"]["passed"] is True
    current = await pipeline.session_state()
    assert current[state.localized_key("ko")] == edited + "\n"
    assert current[state.LOCALIZED_TITLES]["ko"] == "새 제목"
    assert current[state.LOCALIZED_TITLES]["en"] == "Title"  # untouched
    stored = get_article_store().latest_version("proj-3", "ko")
    assert stored["markdown"] == edited + "\n" and stored["kind"] == "edited"
    folder = workspace_dir() / "proj-3"
    assert (folder / "post.ko.md").read_text(encoding="utf-8") == edited + "\n"
    assert "손으로 고친" in (folder / "naver_blog.html").read_text(encoding="utf-8")
    assert (await pipeline.overview())["editable"] == ["post.ko.md"]


@pytest.mark.asyncio
async def test_edit_reports_a_broken_structure_but_still_saves(
    isolated_workspace,
) -> None:
    pipeline = await _pipeline_with_article()
    result = await pipeline.save_edit("post.ko.md", "# 제목\n\n링크를 지운 본문입니다.")
    assert result["check"]["passed"] is False
    assert "link" in result["check"]["errors"][0]
    current = await pipeline.session_state()
    assert current[state.FIDELITY_REPORT]["ko"]["passed"] is False


@pytest.mark.asyncio
async def test_source_and_unknown_files_cannot_be_edited(isolated_workspace) -> None:
    pipeline = await _pipeline_with_article()
    for name in ("post.ja.md", "manifest.md", "../post.ko.md", "post.en.md"):
        with pytest.raises(ValueError):
            await pipeline.save_edit(name, "text")
    with pytest.raises(ValueError):
        await pipeline.save_edit("post.ko.md", "   ")


@pytest.mark.asyncio
async def test_finalizing_holds_only_for_the_text_that_was_reviewed(
    isolated_workspace,
) -> None:
    pipeline = await _pipeline_with_article()
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    assert rows["ko"]["finalized"] is False

    done = await pipeline.finalize_post("ko")
    assert done["finalized"] is True
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    assert rows["ko"]["finalized"] is True and rows["ja"]["finalized"] is False

    # Any change to the text, here an edit by hand, makes it unfinished again.
    await pipeline.save_edit(
        "post.ko.md", "# 제목\n\n고친 본문. [링크](https://example.com)"
    )
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    assert rows["ko"]["finalized"] is False

    for language in ("ja", "en"):  # the source, and a language with no text
        with pytest.raises(ValueError, match="no localized post"):
            await pipeline.finalize_post(language)


@pytest.mark.asyncio
async def test_text_with_a_credential_cannot_be_finalized(isolated_workspace) -> None:
    pipeline = await _pipeline_with_article()
    key = "AIza" + "B" * 35
    await pipeline.save_edit(
        "post.ko.md", f"# 제목\n\n키는 {key} 입니다. [링크](https://example.com)"
    )
    with pytest.raises(ValueError, match="credential-looking"):
        await pipeline.finalize_post("ko")
