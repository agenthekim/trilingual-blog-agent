"""Tests for the pipeline helpers and the web UI's file endpoint: no model is called."""

from __future__ import annotations

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
