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
    again = asyncio.run(publish.publish_qiita_article_draft(["AI"], tool_context))
    assert again["error_code"] == "ALREADY_PUBLISHED_SOURCE"


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


def _deck_plan() -> dict:
    return {
        "language": "en",
        "deck_title": "Deck",
        "audience": "developers",
        "opening_script": "Welcome. This talk is Deck.",
        "closing_script": "Thank you. Questions?",
        "slides": [
            {
                "number": n,
                "title": f"Slide {n}",
                "bullets": ["first point", "second point"],
                "visual_note": "text only" if n == 1 else "architecture diagram",
                "speaker_script": f"Script for slide {n}.",
            }
            for n in (1, 2)
        ],
    }


def test_slide_requests_build_a_cover_and_one_slide_per_plan_entry() -> None:
    from app.google_slides import (
        image_request,
        notes_requests,
        slide_images,
        slide_requests,
    )
    from app.schemas import SlideDeckPlan

    raw = _deck_plan()
    raw["slides"][0]["image_url"] = "https://img.example.com/invented.png"
    raw["slides"][1]["image_url"] = "https://img.example.com/real.png"
    plan = SlideDeckPlan.model_validate(raw)

    # A URL the model made up is dropped; only article images are placed.
    images = slide_images(
        plan, {"https://img.example.com/real.png": "https://img.example.com/real.png"}
    )
    assert images == {2: "https://img.example.com/real.png"}

    requests = slide_requests(plan, "p", images)
    created = [r["createSlide"] for r in requests if "createSlide" in r]
    # A title slide, the planned slides, then a thank-you slide.
    assert [c["objectId"] for c in created] == [
        "slide_000",
        "slide_001",
        "slide_002",
        "slide_999",
    ]
    assert [c["insertionIndex"] for c in created] == [1, 2, 3, 4]
    texts = [r["insertText"] for r in requests if "insertText" in r]
    assert {"objectId": "slide_000_title", "text": "Deck"} in texts
    assert {"objectId": "slide_001_title", "text": "Slide 1"} in texts
    assert {"objectId": "slide_999_title", "text": "Thank you"} in texts
    assert {"objectId": "slide_002_body", "text": "first point\nsecond point"} in texts
    assert requests[-1] == {"deleteObject": {"objectId": "p"}}  # placeholder slide
    assert {"deleteObject": {"objectId": "slide_000_subtitle"}} in requests
    assert not any("updatePageProperties" in r for r in requests)  # plain cover

    widths = {
        r["createShape"]["objectId"]: r["createShape"]["elementProperties"]["size"][
            "width"
        ]["magnitude"]
        for r in requests
        if "createShape" in r and r["createShape"]["shapeType"] == "TEXT_BOX"
    }
    assert widths["slide_002_body"] < widths["slide_001_body"]  # room for the image

    picture = image_request(2, images[2])["createImage"]
    assert picture["url"] == "https://img.example.com/real.png"
    assert picture["elementProperties"]["pageObjectId"] == "slide_002"

    presentation = {
        "slides": [
            {
                "objectId": f"slide_00{n}",
                "slideProperties": {
                    "notesPage": {"notesProperties": {"speakerNotesObjectId": f"n{n}"}}
                },
            }
            for n in (0, 1, 2)
        ]
        + [
            {
                "objectId": "slide_999",
                "slideProperties": {
                    "notesPage": {"notesProperties": {"speakerNotesObjectId": "n9"}}
                },
            }
        ]
    }
    notes = [r["insertText"] for r in notes_requests(plan, presentation)]
    # The greeting is said on the title slide, the goodbye on the thank-you slide.
    assert notes[0] == {"objectId": "n0", "text": "Welcome. This talk is Deck."}
    assert notes[1] == {"objectId": "n1", "text": "Script for slide 1."}
    assert notes[2]["text"] == "Script for slide 2.\n\n[Visual] architecture diagram"
    assert notes[3] == {"objectId": "n9", "text": "Thank you. Questions?"}


def test_long_titles_get_a_smaller_font() -> None:
    from app.google_slides import body_points, title_points

    assert title_points("Short title") == 30
    assert (
        title_points("Moving Beyond Monolithic Agents to Multi-Agent Coordination")
        == 26
    )
    assert title_points("한국어 제목은 글자 폭이 두 배라서 더 빨리 줄어든다") < 30
    assert body_points(["a", "b", "c"], beside_image=False) == 20
    assert body_points(["a", "b", "c"], beside_image=True) == 18
    assert body_points(["a fairly long bullet point " * 3] * 5, beside_image=True) == 14


_BULLETS = [
    "Guardrails intercept policy violations",
    "Pause execution for human authorization",
    "Handle budget exceptions like business class",
    "Resume to Act or Finish upon approval",
]


def _overlap(a: tuple, b: tuple) -> bool:
    return (
        a[0] < b[0] + b[2]
        and b[0] < a[0] + a[2]
        and a[1] < b[1] + b[3]
        and b[1] < a[1] + a[3]
    )


def test_a_wide_image_goes_under_the_bullets_and_spans_the_page() -> None:
    from app.google_slides import slide_layout

    layout = slide_layout("Tool Manager: Routing", _BULLETS, True, aspect=4.1)
    assert layout.stacked and layout.title_right is None
    assert layout.image[2] > 8  # was 4.4 inches in the right half
    assert layout.image[1] >= layout.text[1] + layout.text[3]  # under the bullets
    assert layout.text[2] == 8.9


def test_a_tall_image_takes_the_page_height_and_the_title_makes_room() -> None:
    from app.google_slides import slide_layout

    layout = slide_layout("Unified End-to-End Architecture", _BULLETS, True, 0.72)
    left, top, _, height = layout.image
    assert not layout.stacked and height > 5  # was 3.6 inches under the title
    assert top < 0.51  # starts above the title line ...
    assert layout.title_right < left  # ... so the title must end before it
    assert layout.text[0] + layout.text[2] < left
    # If the title cannot be narrowed, the image stays under the title.
    assert layout.fallback_image[1] >= 1.4
    assert layout.fallback_image[0] >= left


def test_image_layouts_stay_on_the_page_and_never_shrink_the_image() -> None:
    from app.google_slides import slide_layout

    titles = [
        "Short",
        "How Guardrails and Human Approval Work Together",
        "가드레일과 사람의 승인이 함께 동작하는 방식을 단계별로 살펴보기",
    ]
    for title in titles:
        for aspect in (0.3, 0.45, 0.72, 1.0, 1.16, 1.38, 1.6, 2.2, 2.9, 4.1, 7.0):
            for bullets in (_BULLETS[:2], _BULLETS, _BULLETS + _BULLETS):
                layout = slide_layout(title, bullets, True, aspect)
                left, top, width, height = layout.image
                where = (title, aspect, len(bullets))
                assert abs(width / height - aspect) < 0.01 * aspect, where
                assert left >= 0.55 - 0.002 and left + width <= 9.45 + 0.002, where
                assert top >= 0.35 - 0.002 and top + height <= 5.4 + 0.002, where
                assert not _overlap(layout.text, layout.image), where
                assert layout.text[2] >= 3.3 - 0.001, where
                # The old placement fitted the image into 4.4 x 3.6 inches.
                assert width >= min(4.4, 3.6 * aspect) * 0.999, where


def test_an_image_of_unknown_shape_keeps_the_right_half() -> None:
    from app.google_slides import image_request, slide_layout

    layout = slide_layout("Some title", ["a", "b"], True, None)
    assert layout.text == (0.55, 1.65, 4.3, 3.6) and layout.title_right is None
    assert layout.image[0] == 5.05 and layout.image[2] == 4.4
    size = image_request(3, "https://img.example.com/a.png", layout.image)[
        "createImage"
    ]["elementProperties"]["size"]
    assert size["width"]["magnitude"] == round(4.4 * 914400)
    # Without an image nothing changes: full width, same place as before.
    assert slide_layout("Some title", ["a", "b"], False).text == (0.55, 1.65, 8.9, 3.6)


def test_title_is_narrowed_using_the_geometry_google_reports() -> None:
    from app.google_slides import slide_layout, title_width_requests

    layouts = {
        1: slide_layout("Unified End-to-End Architecture", _BULLETS, True, 0.72),
        2: slide_layout("Tool Manager: Routing", _BULLETS, True, 4.1),  # not narrowed
        3: slide_layout("Unified End-to-End Architecture", _BULLETS, True, 0.72),
    }
    title = {
        "objectId": "slide_001_title",
        "size": {
            "width": {"magnitude": 3000000, "unit": "EMU"},
            "height": {"magnitude": 3000000, "unit": "EMU"},
        },
        "transform": {
            "scaleX": 2.84,
            "scaleY": 0.19,
            "translateX": 311700,
            "translateY": 445025,
            "unit": "EMU",
        },
    }
    presentation = {
        "slides": [
            {"pageElements": [title]},
            {"pageElements": [{**title, "objectId": "slide_002_title"}]},
            # A title whose geometry is not reported is left alone.
            {"pageElements": [{"objectId": "slide_003_title"}]},
        ]
    }
    requests = title_width_requests(layouts, presentation)
    assert list(requests) == [1]
    update = requests[1]["updatePageElementTransform"]
    assert update["objectId"] == "slide_001_title" and update["applyMode"] == "ABSOLUTE"
    moved = update["transform"]
    right_edge = (moved["translateX"] + 3000000 * moved["scaleX"]) / 914400
    assert abs(right_edge - layouts[1].title_right) < 0.001
    assert (moved["scaleY"], moved["translateY"]) == (0.19, 445025)  # only the width


def test_png_size_reads_the_header() -> None:
    import struct

    from app.google_slides import png_size

    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    assert png_size(header + struct.pack(">II", 1200, 311)) == (1200, 311)
    assert png_size(b"<html>503 Service Temporarily Unavailable</html>") is None
    assert png_size(header) is None


@pytest.mark.asyncio
async def test_diagram_sizes_are_read_one_at_a_time_and_retried(monkeypatch) -> None:
    import struct

    import httpx

    from app import google_slides

    png = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 1200, 300)
    )
    calls: list[str] = []
    busy = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal busy
        busy += 1
        assert busy == 1  # the renderer refuses requests that arrive together
        await asyncio.sleep(0)
        busy -= 1
        calls.append(str(request.url))
        assert "authorization" not in request.headers
        if str(request.url).endswith("second") and calls.count(str(request.url)) == 1:
            return httpx.Response(503, text="<html>503</html>")
        return httpx.Response(200, content=png)

    pauses: list[float] = []
    real_sleep = asyncio.sleep

    async def no_wait(seconds: float) -> None:
        pauses.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(google_slides.asyncio, "sleep", no_wait)
    images = {
        4: google_slides.MERMAID_RENDER_URL + "first",
        7: "https://img.example.com/from-the-article.png",  # never fetched here
        9: google_slides.MERMAID_RENDER_URL + "second",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        aspects = await google_slides.image_aspects(client, images)
    assert aspects == {4: 4.0, 9: 4.0}
    assert [c.rsplit(":", 1)[-1] for c in calls] == ["first", "second", "second"]
    assert pauses.count(2) == 1  # one pause before the retry


def test_images_are_found_in_markdown_and_html_but_not_in_code() -> None:
    from app.markdown_checks import extract_images

    article = (
        "![Agent loop](https://img.example.com/a.png)\n\n"
        '<img width="600" alt="Runtime" src="https://img.example.com/b.jpeg">\n\n'
        "![again](https://img.example.com/a.png) ![local](./c.png)\n\n"
        "```md\n![in code](https://img.example.com/x.png)\n```\n"
    )
    assert extract_images(article) == [
        ("Agent loop", "https://img.example.com/a.png"),
        ("Runtime", "https://img.example.com/b.jpeg"),
    ]


def test_mermaid_diagrams_become_references_and_image_addresses(monkeypatch) -> None:
    import base64
    import json
    import zlib

    from app.google_slides import mermaid_image_url, slide_images
    from app.markdown_checks import extract_mermaid_diagrams
    from app.schemas import SlideDeckPlan

    article = (
        "# Title\n\n## Loop\n\n```mermaid\nflowchart TD\n  A --> B\n```\n\n"
        "```python\nprint(1)\n```\n\n## Parts\n\n```mermaid\ngraph LR\n  X --> Y\n```\n"
    )
    diagrams = extract_mermaid_diagrams(article)
    assert diagrams == [
        ("Loop", "flowchart TD\n  A --> B"),
        ("Parts", "graph LR\n  X --> Y"),
    ]

    url = mermaid_image_url(diagrams[0][1])
    assert url.startswith("https://mermaid.ink/img/pako:") and "type=png" in url
    packed = url.split("pako:")[1].split("?")[0]
    assert json.loads(zlib.decompress(base64.urlsafe_b64decode(packed)))["code"] == (
        "flowchart TD\n  A --> B"
    )
    assert (
        mermaid_image_url(
            "A --> B\n" + "".join(f"N{i} --> M{i * 7919}\n" for i in range(900))
        )
        is None
    )

    raw = _deck_plan()
    raw["slides"][0]["image_url"] = "mermaid:2"
    raw["slides"][1]["image_url"] = "mermaid:9"  # no such diagram
    plan = SlideDeckPlan.model_validate(raw)
    assert slide_images(plan, {"mermaid:2": "https://mermaid.ink/img/x"}) == {
        1: "https://mermaid.ink/img/x"
    }


def test_mermaid_rendering_is_off_unless_enabled(monkeypatch) -> None:
    from app.settings import render_mermaid_enabled

    monkeypatch.delenv("BLOG_AGENT_RENDER_MERMAID", raising=False)
    assert render_mermaid_enabled() is False
    monkeypatch.setenv("BLOG_AGENT_RENDER_MERMAID", "true")
    assert render_mermaid_enabled() is True


def _plan_for(language: str) -> dict:
    return {**_deck_plan(), "language": language, "deck_title": f"Deck {language}"}


@pytest.mark.asyncio
async def test_a_deck_needs_the_plan_of_its_language_and_human_approval(
    tool_context, monkeypatch
) -> None:
    from app import google_slides, state

    tool = slides.create_google_slides_deck_with_speaker_notes
    assert (await tool("en", tool_context))["error_code"] == "NO_SLIDE_PLAN"
    assert (await tool("fr", tool_context))["error_code"] == "UNKNOWN_LANGUAGE"

    calls = []

    async def fake_create(plan, allowed_images, *args):
        calls.append(plan.language)
        return {
            "presentation_id": f"id-{plan.language}",
            "url": google_slides.deck_url(f"id-{plan.language}"),
        }

    monkeypatch.setattr(google_slides, "create_deck", fake_create)
    tool_context.state[state.SLIDE_PLANS] = {
        "en": _plan_for("en"),
        "ko": _plan_for("ko"),
    }
    missing = await tool("ja", tool_context)
    assert missing["error_code"] == "NO_SLIDE_PLAN" and "en, ko" in missing["message"]

    monkeypatch.setattr(google_slides, "granted_account", lambda: "owner@example.com")
    assert (await tool("ko", tool_context))["status"] == "pending_human_approval"
    request = tool_context.confirmation_requests[0]
    assert "Korean, 2 slides" in request["hint"]
    assert "the Google Drive of owner@example.com" in request["hint"]
    assert request["payload"]["language"] == "ko"
    tool_context.decline()
    assert (await tool("ko", tool_context))["status"] == "declined"
    assert calls == [] and not tool_context.state.get(state.GOOGLE_SLIDES_DECKS)

    tool_context.approve()
    result = await tool("ko", tool_context)
    assert result["status"] == "success" and calls == ["ko"]
    await tool("en", tool_context)
    decks = tool_context.state[state.GOOGLE_SLIDES_DECKS]
    # One deck per language; creating the English one did not replace the Korean.
    assert decks["ko"]["url"].endswith("/d/id-ko/edit")
    assert decks["en"]["url"].endswith("/d/id-en/edit")
    assert sorted(tool_context.state[state.DECK_TEXT_BASELINE]) == ["en", "ko"]


@pytest.mark.asyncio
async def test_google_slides_without_a_grant_gives_guidance(
    tool_context, monkeypatch
) -> None:
    from app import secrets, state

    monkeypatch.delenv("GOOGLE_SLIDES_OAUTH", raising=False)
    monkeypatch.setattr(secrets, "_from_secret_manager", lambda name: None)
    secrets.get_secret.cache_clear()
    tool_context.state[state.SLIDE_PLANS] = {"en": _plan_for("en")}
    tool_context.approve()
    result = await slides.create_google_slides_deck_with_speaker_notes(
        "en", tool_context
    )
    assert result["error_code"] == "SLIDES_NOT_AUTHORIZED"
    assert "authorize_google_slides.py" in result["recovery_instruction"]


@pytest.mark.asyncio
async def test_a_failure_after_the_file_exists_names_the_file(monkeypatch) -> None:
    """A deck that was created and then failed must not be lost in Drive."""
    import httpx

    from app import google_slides
    from app.schemas import SlideDeckPlan

    async def token(client):
        return "token"

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/presentations":
            return httpx.Response(
                200, json={"presentationId": "made-123", "slides": []}
            )
        return httpx.Response(500, json={"error": {"message": "backend error"}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(google_slides, "_access_token", token)
    monkeypatch.setattr(
        google_slides.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(answer)),
    )
    with pytest.raises(google_slides.SlidesError) as failure:
        await google_slides.create_deck(SlideDeckPlan.model_validate(_deck_plan()), {})
    assert "adding the slides" in failure.value.message
    assert google_slides.deck_url("made-123") in failure.value.message


def test_timing_export_and_manifest_work_per_language(finalized_project) -> None:
    from app import state

    finalized_project.state[state.SLIDE_PLANS] = {
        "en": _plan_for("en"),
        "ja": _plan_for("ja"),
    }
    finalized_project.state[state.GOOGLE_SLIDES_DECKS] = {
        "ja": {"url": "https://docs.google.com/presentation/d/ja/edit"}
    }
    report = slides.check_slide_script_timing("en", finalized_project)
    assert report["slide_count"] == 2
    assert list(finalized_project.state[state.SLIDE_TIMING]) == ["en"]
    assert slides.check_slide_script_timing("ko", finalized_project)["error_code"] == (
        "NO_SLIDE_PLAN"
    )

    written = slides.export_slide_plan_markdown("ja", finalized_project)
    assert written["file"].endswith("slides.ja.md") and written["slide_count"] == 2

    manifest = slides.compile_artifact_manifest_markdown(finalized_project)["manifest"]
    assert "Slides and speaker script (English): slides.en.md" in manifest
    assert "Slides and speaker script (Japanese): slides.ja.md" in manifest
    assert "Google Slides deck (Japanese): https://docs.google.com/" in manifest
    assert "Google Slides deck (English)" not in manifest


def test_a_new_brief_clears_what_the_last_planning_run_left(finalized_project) -> None:
    from app import state

    finalized_project.state[state.SLIDE_DECK_PLAN] = _plan_for("en")
    finalized_project.state[state.SLIDE_DRAFT_TIMING] = {
        "passed": False,
        "recovery_instruction": "lengthen the scripts of slides [1]",
    }
    done = slides.set_presentation_brief("ja", "developers", 5, finalized_project)
    assert done["status"] == "success"
    assert finalized_project.state[state.SLIDE_DECK_PLAN] is None
    assert finalized_project.state[state.SLIDE_DRAFT_TIMING] is None

    from types import SimpleNamespace

    from app import prompts

    instruction = prompts.slide_planner_instruction(
        SimpleNamespace(state=finalized_project.state)
    )
    assert "previous draft failed" not in instruction.lower()
    assert "Language of slides and script: Japanese ('ja')" in instruction


def test_markdown_tables_are_extracted_and_placed_as_native_slide_tables(
    finalized_project,
) -> None:
    """SLD-11: article tables become table:N references and native slide tables."""
    from types import SimpleNamespace

    from app import google_slides, prompts, state
    from app.markdown_checks import extract_tables
    from app.schemas import SlideDeckPlan

    article = (
        "# Storage Comparison\n\n"
        "## Throughput by Tier\n\n"
        "| Tier | Bandwidth | Use Case |\n"
        "| :--- | :---: | :--- |\n"
        "| **Standard** | `125 MB/s` | [General](https://example.com) |\n"
        "| High | 1000 MB/s | AI training |\n\n"
        "```python\n"
        "# | Not | A | Table |\n"
        "# | --- | --- | --- |\n"
        "```\n"
    )
    found = extract_tables(article)
    assert len(found) == 1
    heading, rows = found[0]
    assert heading == "Throughput by Tier"
    assert rows == [
        ["Tier", "Bandwidth", "Use Case"],
        ["Standard", "125 MB/s", "General"],
        ["High", "1000 MB/s", "AI training"],
    ]

    finalized_project.state[state.localized_key("en")] = article
    slides.set_presentation_brief("en", "developers", 2, finalized_project)
    instruction = prompts.slide_planner_instruction(
        SimpleNamespace(state=finalized_project.state)
    )
    assert "table in the section 'Throughput by Tier'" in instruction
    assert "table:1" in instruction

    allowed = slides.article_tables(finalized_project.state, "en")
    assert list(allowed) == ["table:1"]

    raw_plan = _plan_for("en")
    raw_plan["slides"][0]["image_url"] = "table:1"
    raw_plan["slides"][1]["image_url"] = "table:1"  # duplicate use is dropped
    plan = SlideDeckPlan.model_validate(raw_plan)
    chosen = google_slides.slide_tables(plan, allowed)
    assert list(chosen) == [1]

    layouts = google_slides.slide_layouts(plan, {}, {}, chosen)
    assert layouts[1].stacked is True and layouts[1].image is not None
    reqs = google_slides.table_requests(1, chosen[1], layouts[1].image)
    assert reqs[0]["createTable"]["rows"] == 3
    assert reqs[0]["createTable"]["columns"] == 3
    assert reqs[0]["createTable"]["objectId"] == "slide_001_table"
    inserted = [r["insertText"]["text"] for r in reqs if "insertText" in r]
    assert "Standard" in inserted and "1000 MB/s" in inserted
