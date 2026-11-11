"""Tools: seed handling, publishing gates, persistence, export and manifest."""

from __future__ import annotations

import asyncio

import pytest

from app import platforms, state
from app.persistence import SqliteArticleStore, get_article_store
from app.tools import articles, publish, slides
from tests.unit.conftest import SEED_KO, FakeToolContext

FAKE_KEY = "AIza" + "B" * 35


def test_seed_revisions_are_numbered_and_persisted(
    tool_context: FakeToolContext,
) -> None:
    first = articles.save_seed_article_revision(
        "제목", SEED_KO, "ko", "none", "", False, tool_context
    )
    second = articles.save_seed_article_revision(
        "제목", SEED_KO + "\n추가 문단입니다.", "ko", "none", "", False, tool_context
    )
    assert (first["revision"], second["revision"]) == (1, 2)
    assert first["project_id"] == second["project_id"]
    assert first["target_languages"] == ["en", "ja"] and not second["finalized"]
    stored = get_article_store().latest_version(first["project_id"], "ko")
    assert stored["revision"] == 2 and "추가 문단" in stored["markdown"]


def test_invalid_seed_returns_recovery_instruction(
    tool_context: FakeToolContext,
) -> None:
    result = articles.save_seed_article_revision(
        "제목", "short", "fr", "none", "", False, tool_context
    )
    assert result["status"] == "error" and result["error_code"] == "INVALID_ARGUMENTS"
    assert "source_language" in result["message"] and result["recovery_instruction"]
    assert state.PROJECT_ID not in tool_context.state


def test_finalize_requires_a_seed(tool_context: FakeToolContext) -> None:
    assert (
        articles.finalize_seed_article(tool_context)["error_code"] == "NO_SEED_ARTICLE"
    )


def test_fetch_rejects_disallowed_host_without_network(
    tool_context, monkeypatch
) -> None:
    async def fail(*args, **kwargs):
        raise AssertionError("network must not be called")

    monkeypatch.setattr(platforms, "qiita_get_item", fail)
    result = asyncio.run(
        articles.fetch_published_article_markdown(
            "https://example.org/post", tool_context
        )
    )
    assert result["error_code"] == "INVALID_ARGUMENTS"


def test_fetched_qiita_post_becomes_final_seed_and_existing_source(
    tool_context, monkeypatch
) -> None:
    async def fake_item(item_id):
        return {
            "id": item_id,
            "title": "記事",
            "body": "これはテスト記事の本文です。" * 10,
            "tags": [{"name": "AI"}],
        }

    monkeypatch.setattr(platforms, "qiita_get_item", fake_item)
    url = "https://qiita.com/user/items/0123456789abcdef0123"
    result = asyncio.run(articles.fetch_published_article_markdown(url, tool_context))
    assert result["status"] == "success" and result["extraction"] == "api_markdown"
    assert result["language"] == "ja" and result["target_languages"] == ["ko", "en"]
    assert tool_context.state[state.SEED_FINALIZED] is True
    assert tool_context.state[state.PUBLICATIONS]["qiita"]["state"] == "existing_source"

    # The source platform must never be published to again.
    tool_context.state["localized_markdown_ko"] = "x"
    again = asyncio.run(
        publish.publish_qiita_article_draft("記事", ["AI"], tool_context)
    )
    assert again["error_code"] == "ALREADY_PUBLISHED_SOURCE"


def test_publish_needs_finalized_seed_and_localization(
    tool_context: FakeToolContext,
) -> None:
    result = asyncio.run(publish.publish_devto_article_draft("T", ["ai"], tool_context))
    assert result["error_code"] == "SEED_NOT_FINALIZED"
    articles.save_seed_article_revision(
        "제목", SEED_KO, "ko", "none", "", True, tool_context
    )
    result = asyncio.run(publish.publish_devto_article_draft("T", ["ai"], tool_context))
    assert result["error_code"] == "LOCALIZATION_MISSING"


def test_publish_pauses_for_human_approval_and_sends_nothing(
    finalized_project, monkeypatch
) -> None:
    async def fail(**kwargs):
        raise AssertionError("must not publish before approval")

    monkeypatch.setattr(platforms, "qiita_save_item", fail)
    result = asyncio.run(
        publish.publish_qiita_article_draft("記事", ["AI"], finalized_project)
    )
    assert result["status"] == "pending_human_approval"
    assert len(finalized_project.confirmation_requests) == 1
    assert "qiita" not in (finalized_project.state.get(state.PUBLICATIONS) or {})


def test_declined_approval_sends_nothing(finalized_project, monkeypatch) -> None:
    async def fail(**kwargs):
        raise AssertionError("must not publish when declined")

    monkeypatch.setattr(platforms, "devto_save_article", fail)
    finalized_project.decline()
    result = asyncio.run(
        publish.publish_devto_article_draft("T", ["ai"], finalized_project)
    )
    assert result["status"] == "declined"


def test_approved_publish_creates_draft_then_updates_it(
    finalized_project, monkeypatch
) -> None:
    calls = []

    async def fake_save(**kwargs):
        calls.append(kwargs)
        return {"id": 42, "url": "https://dev.to/user/t-temp-slug-42"}

    monkeypatch.setattr(platforms, "devto_save_article", fake_save)
    finalized_project.approve()
    first = asyncio.run(
        publish.publish_devto_article_draft("T", ["AI"], finalized_project)
    )
    assert first["status"] == "success" and first["publication"]["state"] == "draft"
    assert calls[0]["published"] is False and calls[0]["article_id"] is None
    assert calls[0]["tags"] == ["ai"]
    asyncio.run(publish.publish_devto_article_draft("T2", ["ai"], finalized_project))
    assert calls[1]["article_id"] == "42"  # second approval updates, never duplicates
    stored = get_article_store().list_publications(
        finalized_project.state[state.PROJECT_ID]
    )
    assert [p["platform"] for p in stored] == ["devto"]


def test_article_with_credential_is_refused_before_approval(finalized_project) -> None:
    finalized_project.state["localized_markdown_ja"] += f"\napi_key = '{FAKE_KEY}'\n"
    result = asyncio.run(
        publish.publish_qiita_article_draft("記事", ["AI"], finalized_project)
    )
    assert result["error_code"] == "SECRET_IN_ARTICLE"
    assert FAKE_KEY not in str(result)
    assert finalized_project.confirmation_requests == []


def test_platform_error_becomes_guided_tool_error(
    finalized_project, monkeypatch
) -> None:
    async def unauthorized(**kwargs):
        raise platforms.PlatformError("QIITA_UNAUTHORIZED", "rejected", "do not retry")

    monkeypatch.setattr(platforms, "qiita_save_item", unauthorized)
    finalized_project.approve()
    result = asyncio.run(
        publish.publish_qiita_article_draft("記事", ["AI"], finalized_project)
    )
    assert (result["status"], result["error_code"]) == ("error", "QIITA_UNAUTHORIZED")
    assert result["recovery_instruction"] == "do not retry"


def test_naver_export_writes_inline_styled_html_and_never_posts(
    finalized_project,
) -> None:
    result = publish.export_naver_blog_paste_ready_html(finalized_project)
    assert result["status"] == "success"
    assert result["publication"]["state"] == "exported_for_manual_post"
    html = open(result["file"], encoding="utf-8").read()
    assert (
        '<pre style="' in html
        and '<h2 style="' in html
        and "value = compute(1, 2)" in html
    )
    assert "<style" not in html and "class=" not in html.replace(
        'class="language-python"', ""
    )


def test_manual_url_is_recorded_only_for_allowed_hosts(finalized_project) -> None:
    bad = publish.record_manually_posted_url(
        "naver_blog", "https://example.org/x", finalized_project
    )
    assert bad["status"] == "error"
    good = publish.record_manually_posted_url(
        "naver_blog", "https://blog.naver.com/someone/223456789012", finalized_project
    )
    assert good["publication"]["state"] == "published"


def test_manifest_lists_every_language_and_publication(finalized_project) -> None:
    publish.record_manually_posted_url(
        "naver_blog", "https://blog.naver.com/someone/223456789012", finalized_project
    )
    result = slides.compile_artifact_manifest_markdown(finalized_project)
    manifest = result["manifest"]
    assert (
        "| naver_blog | published | https://blog.naver.com/someone/223456789012 |"
        in manifest
    )
    assert "| devto | not posted |" in manifest


def test_sqlite_store_survives_reopening(tmp_path) -> None:
    path = tmp_path / "store.sqlite3"
    store = SqliteArticleStore(path)
    store.save_project("p1", {"title": "A"})
    store.save_project("p1", {"seed_finalized": True})
    store.add_version("p1", "ko", "seed", "v1")
    store.record_publication("p1", "qiita", {"platform": "qiita", "state": "draft"})
    reopened = SqliteArticleStore(path)
    assert reopened.get_project("p1") == {"title": "A", "seed_finalized": True}
    assert reopened.latest_version("p1", "ko")["markdown"] == "v1"
    assert reopened.list_publications("p1")[0]["state"] == "draft"


def test_load_draft_reads_only_inside_workspace(
    tool_context, isolated_workspace
) -> None:
    drafts = isolated_workspace / "workspace" / "drafts"
    drafts.mkdir(parents=True)
    (drafts / "post.md").write_text(
        "# 초안 제목\n\n" + "본문입니다. " * 20, encoding="utf-8"
    )
    result = articles.load_markdown_draft_file("drafts/post.md", "ko", tool_context)
    assert result["status"] == "success" and not result["finalized"]
    assert tool_context.state[state.SEED_TITLE] == "초안 제목"
    missing = articles.load_markdown_draft_file("drafts/none.md", "ko", tool_context)
    assert missing["error_code"] == "DRAFT_NOT_FOUND"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://blog.naver.com/someone/223456789012", ("someone", "223456789012")),
        (
            "https://blog.naver.com/PostView.naver?blogId=someone&logNo=223456789012",
            ("someone", "223456789012"),
        ),
    ],
)
def test_naver_url_forms(url, expected) -> None:
    assert platforms.parse_naver_post(url) == expected


def test_naver_html_extraction_keeps_paragraphs_and_code() -> None:
    html = (
        '<meta property="og:title" content="글 제목"/>'
        '<p class="se-text-paragraph"><span>첫 문단</span></p>'
        '<div class="se-code-source"><div>print(1)<br>print(2)</div></div>'
    )
    title, markdown = platforms.naver_html_to_markdown(html)
    assert title == "글 제목"
    assert markdown == "첫 문단\n\n```\nprint(1)\nprint(2)\n```"


def test_language_guess() -> None:
    assert (
        platforms.guess_language(
            "이것은 한국어로 작성된 기술 블로그 글의 본문입니다. " * 3
        )
        == "ko"
    )
    assert (
        platforms.guess_language("これは日本語で書かれた技術ブログの記事です。" * 3)
        == "ja"
    )
    assert platforms.guess_language("This is an English technical article.") == "en"


def test_localized_title_is_used_for_target_languages_only(finalized_project) -> None:
    from app import state

    session_state = finalized_project.state
    assert state.article_title(session_state, "en") == "제목"  # fallback: none yet
    session_state[state.LOCALIZED_TITLES] = {
        "ko": "모델이 바꾼 제목",
        "en": "The Title",
        "ja": "  ",
    }
    assert state.article_title(session_state, "ko") == "제목"  # source stays as written
    assert state.article_title(session_state, "en") == "The Title"
    assert state.article_title(session_state, "ja") == "제목"  # blank falls back


def test_manifest_shows_each_language_title(finalized_project) -> None:
    from app import state
    from app.persistence import get_article_store

    finalized_project.state[state.LOCALIZED_TITLES] = {
        "ko": "제목",
        "en": "The Title",
        "ja": "タイトル",
    }
    store = get_article_store()
    project_id = finalized_project.state[state.PROJECT_ID]
    store.add_version(project_id, "en", "localized", "text")
    manifest = slides.compile_artifact_manifest_markdown(finalized_project)["manifest"]
    assert "| English | The Title | devto |" in manifest
    assert "| Korean | 제목 | naver_blog |" in manifest
    assert "| Japanese |  | qiita |" in manifest  # no Japanese text stored yet


def test_naver_export_reports_the_title_to_type(finalized_project) -> None:
    result = publish.export_naver_blog_paste_ready_html(finalized_project)
    assert result["title"] == "제목"
    assert result["publication"]["title"] == "제목"
