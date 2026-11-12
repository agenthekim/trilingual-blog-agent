"""Tests for the pipeline helpers and the web UI's file endpoint: no model is called."""

from __future__ import annotations

import httpx
import pytest

from app import state
from app.pipeline import classify_source, overview, project_file
from app.settings import workspace_dir
from app.structurer import same_letters
from app.tools.articles import write_workspace_file


def test_a_bare_address_is_read_by_code() -> None:
    kind, value = classify_source(
        "  https://qiita.com/someone/items/0123456789abcdef0123 "
    )
    assert (kind, value) == (
        "url",
        "https://qiita.com/someone/items/0123456789abcdef0123",
    )


def test_a_short_request_with_a_supported_address_goes_to_the_agent() -> None:
    text = "Expand this into Korean and English: https://dev.to/someone/my-post-1a2b"
    assert classify_source(text) == ("request", text)


def test_other_text_is_the_draft_itself_exactly_as_given() -> None:
    draft = "\n# Title\n\nSee https://example.com for details.\n" + "Body. " * 50 + "\n"
    assert classify_source(draft) == ("draft", draft)

    long_draft_with_supported_link = "# T\n\nhttps://qiita.com/a/items/b\n" + "x " * 400
    assert classify_source(long_draft_with_supported_link)[0] == "draft"


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
        state.SLIDE_PLANS: {"en": {"deck_title": "Deck"}},
        state.GOOGLE_SLIDES_DECKS: {
            "en": {"url": "https://docs.google.com/presentation/d/x"}
        },
    }
    result = overview(session_state)
    rows = {row["language"]: row for row in result["rows"]}

    assert rows["ko"]["title"] == "한국어 제목"
    assert rows["ko"]["files"] == ["post.ko.md", "naver_blog.html"]
    assert rows["ko"]["status"] == "exported_for_manual_post"
    assert rows["ko"]["next"] == "review"  # finalize it, then paste and register
    assert rows["ko"]["can_edit"] is True

    assert rows["ja"]["title"] == "元のタイトル"  # the source keeps its own title
    assert rows["ja"]["status"] == "existing_source"
    assert rows["ja"]["next"] == "original"  # never post back to the source
    assert rows["ja"]["can_edit"] is True
    assert rows["ja"]["url"] == "https://qiita.com/a/items/b"

    assert rows["en"]["title"] == ""  # no English text yet
    assert rows["en"]["next"] == "none"

    # A slide file is listed for a language only when that language has a plan.
    assert rows["en"]["slides_file"] == "slides.en.md"
    assert rows["ko"]["slides_file"] is None and rows["ko"]["deck"] is None
    assert rows["en"]["deck"] == {"url": "https://docs.google.com/presentation/d/x"}
    assert result["slides"] == ["slides.en.md"]
    assert result["editable"] == ["post.ko.md", "slides.en.md"]
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
            state.SEED_FINALIZED: True,
            state.TARGET_LANGUAGES: ["ko", "en"],
            # The article was read from Qiita: that platform is closed.
            state.PUBLICATIONS: {
                "qiita": {
                    "platform": "qiita",
                    "state": "existing_source",
                    "url": "https://qiita.com/a/items/b",
                }
            },
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
    assert (await pipeline.overview())["editable"] == ["post.ko.md", "post.ja.md"]


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
async def test_source_and_unknown_files_cannot_be_edited(
    isolated_workspace,
) -> None:
    pipeline = await _pipeline_with_article()
    saved = await pipeline.save_edit(
        "post.ja.md",
        "# 新しい見出し\n\n修正した本文です。[リンク](https://example.com)",
        title="新しい見出し",
    )
    assert saved["saved"] == "post.ja.md"
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    assert rows["ja"]["status"] == "existing_source"
    assert rows["ja"]["next"] == "original"
    assert rows["ja"]["title"] == "新しい見出し"

    for name in ("manifest.md", "../post.ko.md", "post.en.md"):
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

    # The original on its own platform, and a language with no text.
    with pytest.raises(ValueError, match="original on Qiita"):
        await pipeline.finalize_post("ja")
    with pytest.raises(ValueError, match="no English post"):
        await pipeline.finalize_post("en")


@pytest.mark.asyncio
async def test_changing_only_the_title_makes_a_post_unfinished_again(
    isolated_workspace,
) -> None:
    pipeline = await _pipeline_with_article()
    await pipeline.finalize_post("ko")
    text = (workspace_dir() / "proj-3" / "post.ko.md").read_text(encoding="utf-8")
    await pipeline.save_edit("post.ko.md", text, title="다른 제목")
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    assert rows["ko"]["finalized"] is False and rows["ko"]["title"] == "다른 제목"


@pytest.mark.asyncio
async def test_finalizing_korean_writes_the_naver_file_from_the_finalized_text(
    isolated_workspace,
) -> None:
    pipeline = await _pipeline_with_article()
    await pipeline.save_edit(
        "post.ko.md", "# 제목\n\n마지막으로 고친 본문. [링크](https://example.com)"
    )
    await pipeline.finalize_post("ko")
    html = (workspace_dir() / "proj-3" / "naver_blog.html").read_text(encoding="utf-8")
    assert "마지막으로 고친 본문" in html
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    assert rows["ko"]["next"] == "paste"
    assert rows["ko"]["status"] == "exported_for_manual_post"


@pytest.mark.asyncio
async def test_text_with_a_credential_is_finalized_only_after_acknowledgement(
    isolated_workspace,
) -> None:
    pipeline = await _pipeline_with_article()
    key = "AIza" + "B" * 35
    await pipeline.save_edit(
        "post.ko.md", f"# 제목\n\n키는 {key} 입니다. [링크](https://example.com)"
    )
    asked = await pipeline.finalize_post("ko")
    assert asked["finalized"] is False and asked["needs_acknowledgement"] is True
    assert asked["findings"] == [{"type": asked["findings"][0]["type"], "line": 3}]
    assert key not in str(asked)
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    assert rows["ko"]["finalized"] is False

    # The person saw the findings and says they are not real credentials.
    done = await pipeline.finalize_post("ko", acknowledge_findings=True)
    assert done["finalized"] is True
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    assert rows["ko"]["finalized"] is True


# --- the run, with the agent's two jobs (localize, plan a talk) stood in for ---

DRAFT_KO = (
    "\n# 에이전트 런타임 이해하기\n\n"
    "이 글은 에이전트 런타임의 구조를 설명합니다. [문서](https://example.com/docs)\n\n"
    "## 정리\n\n끝입니다.\n\n"
)


def _localized(marker: str) -> str:
    return (
        f"# {marker} title\n\n{marker} body. [docs](https://example.com/docs)\n\n"
        f"## {marker} summary\n\n{marker} end.\n"
    )


def _plan(language: str, slides: int = 3) -> dict:
    script = {"en": "word " * 140, "ko": "단어 " * 150, "ja": "言葉" * 180}[language]
    return {
        "language": language,
        "deck_title": f"Deck {language}",
        "audience": "developers",
        "opening_script": "Hello.",
        "closing_script": "Thank you.",
        "slides": [
            {
                "number": n,
                "title": f"Slide {n}",
                "bullets": ["one point"],
                "visual_note": "text only",
                "speaker_script": script,
            }
            for n in range(1, slides + 1)
        ],
    }


def _stand_in_agent(pipeline, *, write=("en", "ja"), check=True, plan_as=None):
    """Replace the two places a model works by code that writes what it would.

    ``pipeline.say`` (the coordinator's turn in the record session) and
    ``pipeline._run_planning`` (the slide planning pipeline in the working
    session of one language). ``said`` lists what was asked of either.
    """
    from google.adk.events import Event, EventActions

    from app.pipeline import USER_ID

    said: list[str] = []

    async def run_planning(session_id: str, language: str) -> None:
        app_name = pipeline._slide_app.name
        working = await pipeline._sessions.get_session(
            app_name=app_name, user_id=USER_ID, session_id=session_id
        )
        brief = working.state[state.PRESENTATION_BRIEF]
        assert brief["language"] == language
        said.append(
            f"Plan a {brief['minutes']} minute talk in language '{language}'. "
            f"Audience: {brief['audience']}."
        )
        written = plan_as or language
        await pipeline._sessions.append_event(
            working,
            Event(
                author="slide_timing_gate",
                invocation_id="planning-stand-in",
                actions=EventActions(
                    state_delta={
                        state.SLIDE_DECK_PLAN: _plan(written),
                        state.SLIDE_PLANS: {
                            **(working.state.get(state.SLIDE_PLANS) or {}),
                            written: _plan(written),
                        },
                        state.SLIDE_TIMING: {
                            **(working.state.get(state.SLIDE_TIMING) or {}),
                            written: {"passed": True, "expected_slide_count": 3},
                        },
                    }
                ),
            ),
        )

    pipeline._run_planning = run_planning

    async def say(text: str) -> None:
        said.append(text)
        current = await pipeline.session_state()
        if text.startswith("Localize"):
            targets = current[state.TARGET_LANGUAGES]
            delta = {state.localized_key(lang): _localized(lang) for lang in write}
            delta[state.FIDELITY_REPORT] = {
                lang: {"passed": check, "errors": [], "warnings": []}
                for lang in targets
            }
            delta[state.LOCALIZED_TITLES] = {lang: f"{lang} title" for lang in targets}
            await pipeline._update_state(delta)

    pipeline.say = say
    return said


def _new_pipeline():
    from app.pipeline import ArticlePipeline

    return ArticlePipeline(log=lambda _: None)


@pytest.mark.asyncio
async def test_a_pasted_draft_is_stored_exactly_as_given_and_no_model_retypes_it(
    isolated_workspace,
) -> None:
    pipeline = _new_pipeline()
    said = _stand_in_agent(pipeline)
    assert await pipeline.run_stages(DRAFT_KO, talk_minutes=0) is None

    current = await pipeline.session_state()
    assert current[state.SEED_MARKDOWN] == DRAFT_KO  # byte for byte
    assert current[state.SOURCE_LANGUAGE] == "ko"
    assert current[state.SEED_TITLE] == "에이전트 런타임 이해하기"
    assert current[state.SEED_FINALIZED] is True
    # The agent was asked for one thing only: the localization.
    assert said == ["Localize it into the other two languages."]
    folder = workspace_dir() / current[state.PROJECT_ID]
    assert (folder / "post.ko.md").read_text(encoding="utf-8") == DRAFT_KO
    assert (folder / "naver_blog.html").is_file() and (folder / "manifest.md").is_file()
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    # A draft's own language is posted like the others: Korean goes to Naver.
    assert [rows[code]["next"] for code in ("ko", "en", "ja")] == ["review"] * 3


@pytest.mark.asyncio
async def test_a_run_stops_and_says_which_language_was_not_written(
    isolated_workspace,
) -> None:
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline, write=("en",))
    stopped = await pipeline.run_stages(DRAFT_KO, talk_minutes=3, talk_languages=["en"])
    assert "No Japanese text was written" in stopped
    current = await pipeline.session_state()
    assert not current.get(state.SLIDE_PLANS)
    assert project_file(current[state.PROJECT_ID], "naver_blog.html") is None


@pytest.mark.asyncio
async def test_a_run_stops_when_the_structure_check_fails(isolated_workspace) -> None:
    pipeline = _new_pipeline()
    said = _stand_in_agent(pipeline, check=False)
    stopped = await pipeline.run_stages(DRAFT_KO, talk_minutes=3, talk_languages=["en"])
    assert "structure check failed for English and Japanese" in stopped
    assert len(said) == 1  # no talk was planned after the failure


@pytest.mark.asyncio
async def test_each_language_asked_for_gets_its_own_plan_and_its_own_deck(
    isolated_workspace, monkeypatch
) -> None:
    from app import google_slides

    created: list[str] = []

    async def fake_create(plan, allowed_images):
        if plan.language == "ja":
            raise google_slides.SlidesError(
                "SLIDES_API_ERROR", "Quota used up.", "wait"
            )
        created.append(plan.language)
        return {
            "presentation_id": f"deck-{plan.language}",
            "url": google_slides.deck_url(f"deck-{plan.language}"),
        }

    monkeypatch.setattr(google_slides, "create_deck", fake_create)
    pipeline = _new_pipeline()
    said = _stand_in_agent(pipeline)
    notes = await pipeline.run_stages(
        DRAFT_KO,
        3,
        ["en", "ko", "ja", "en"],
        "students and beginners",
        create_deck=True,
    )

    assert said[1:] == [
        f"Plan a 3 minute talk in language '{code}'. Audience: students and beginners."
        for code in ("en", "ko", "ja")  # each once, in the order asked
    ]
    assert created == ["en", "ko"]
    assert "Japanese Google Slides deck was not created. Quota used up." in notes
    current = await pipeline.session_state()
    decks = current[state.GOOGLE_SLIDES_DECKS]
    assert {code: deck["presentation_id"] for code, deck in decks.items()} == {
        "en": "deck-en",
        "ko": "deck-ko",
    }
    assert sorted(current[state.DECK_TEXT_BASELINE]) == ["en", "ko"]
    manifest = (workspace_dir() / current[state.PROJECT_ID] / "manifest.md").read_text(
        encoding="utf-8"
    )
    for name in ("slides.en.md", "slides.ko.md", "slides.ja.md"):
        assert name in manifest
    assert "Google Slides deck (English)" in manifest
    assert "Google Slides deck (Korean)" in manifest
    assert "Google Slides deck (Japanese)" not in manifest

    from app.pipeline import progress

    steps = {
        step["name"]: step["done"]
        for step in progress(current, ["en", "ko", "ja"], True)
    }
    assert steps == {
        "Source document": True,
        "Transform": True,
        "Prepare posts": True,
        "Prepare slides": False,  # the Japanese deck is missing
        "Summarize": True,
    }
    assert [s["name"] for s in progress(current, [], False)] == [
        "Source document",
        "Transform",
        "Prepare posts",
        "Summarize",
    ]


@pytest.mark.asyncio
async def test_no_deck_is_created_unless_the_person_asked_for_decks(
    isolated_workspace, monkeypatch
) -> None:
    from app import google_slides

    async def fail(plan, allowed_images):
        raise AssertionError("no deck was asked for")

    monkeypatch.setattr(google_slides, "create_deck", fail)
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline)
    assert await pipeline.run_stages(DRAFT_KO, 3, ["en"], create_deck=False) is None
    current = await pipeline.session_state()
    assert list(current[state.SLIDE_PLANS]) == ["en"]
    assert not current.get(state.GOOGLE_SLIDES_DECKS)


@pytest.mark.asyncio
async def test_a_plan_in_another_language_than_asked_for_is_reported(
    isolated_workspace,
) -> None:
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline, plan_as="en")
    notes = await pipeline.run_stages(DRAFT_KO, 3, ["ko"])
    assert "No Korean slide plan was made" in notes


@pytest.mark.asyncio
async def test_a_published_post_is_read_by_code_and_its_platform_is_closed(
    isolated_workspace, monkeypatch
) -> None:
    from app import platforms

    async def fake_item(item_id):
        return {"id": item_id, "title": "記事", "body": "本文です。" * 20, "tags": []}

    monkeypatch.setattr(platforms, "qiita_get_item", fake_item)
    pipeline = _new_pipeline()
    said = _stand_in_agent(pipeline, write=("ko", "en"))
    url = "https://qiita.com/user/items/0123456789abcdef0123"
    assert await pipeline.run_stages(url, talk_minutes=0) is None
    assert said == ["Localize it into the other two languages."]
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    assert rows["ja"]["next"] == "original" and rows["ja"]["url"] == url
    with pytest.raises(ValueError, match="original on Qiita"):
        await pipeline.finalize_post("ja")


def _model_behind_the_gateway(monkeypatch, answer) -> list[str]:
    """Stand in for the model the gateway calls; returns what it was sent."""
    from app import llm

    sent: list[str] = []

    async def call_model(model: str, instruction: str, contents: str) -> str:
        sent.append(contents)
        return answer(contents) if callable(answer) else answer

    monkeypatch.setattr(llm, "_call_model", call_model)
    return sent


def _seed_versions(project_id: str, language: str) -> list[str]:
    """Every version of the seed in the article store, oldest first."""
    from app.persistence import get_article_store

    rows = get_article_store()._db.execute(
        "SELECT markdown FROM versions WHERE project_id = ? AND language = ? "
        "ORDER BY revision",
        (project_id, language),
    )
    return [row[0] for row in rows]


PLAIN_DRAFT_KO = (
    "에이전트 런타임 이해하기\n\n"
    "1. 개요\n\n"
    "에이전트 런타임은 세션과 도구 호출을 관리합니다. 한 번의 실행은 평균 3초가 걸립니다.\n\n"
    "2. 설치\n\n"
    "pip install agent-runtime 명령으로 설치하고 버전 2 이상인지 확인합니다.\n"
)
PLAIN_DRAFT_KO_STRUCTURED = (
    "# 에이전트 런타임 이해하기\n\n"
    "## 1. 개요\n\n"
    "에이전트 런타임은 세션과 도구 호출을 관리합니다. 한 번의 실행은 평균 3초가 걸립니다.\n\n"
    "## 2. 설치\n\n"
    "`pip install agent-runtime` 명령으로 설치하고 버전 2 이상인지 확인합니다."
)


@pytest.mark.asyncio
async def test_the_text_as_supplied_is_kept_beside_the_seed(
    isolated_workspace, monkeypatch
) -> None:
    assert same_letters(PLAIN_DRAFT_KO, PLAIN_DRAFT_KO_STRUCTURED)

    # A draft without a heading whose structured copy has the same letters:
    # the copy is the seed, and the text as supplied is still there, as a
    # file and as the first version in the store.
    sent = _model_behind_the_gateway(monkeypatch, PLAIN_DRAFT_KO_STRUCTURED)
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline)
    notes = await pipeline.run_stages(PLAIN_DRAFT_KO, talk_minutes=0)
    current = await pipeline.session_state()
    folder = workspace_dir() / current[state.PROJECT_ID]
    assert len(sent) == 1
    assert current[state.SEED_MARKDOWN] == PLAIN_DRAFT_KO_STRUCTURED
    assert current[state.SEED_TITLE] == "에이전트 런타임 이해하기"
    assert current[state.SEED_FINALIZED] is True
    assert (folder / "source.original.md").read_bytes() == PLAIN_DRAFT_KO.encode()
    assert (folder / "post.ko.md").read_text(encoding="utf-8") == (
        PLAIN_DRAFT_KO_STRUCTURED
    )
    assert _seed_versions(current[state.PROJECT_ID], "ko") == [
        PLAIN_DRAFT_KO,
        PLAIN_DRAFT_KO_STRUCTURED,
    ]
    assert "gave the text Markdown structure" in notes
    assert "source.original.md" in notes

    # The model changed a number: its copy is not used, the seed is the text
    # as supplied, and the notice says so.
    _model_behind_the_gateway(
        monkeypatch, PLAIN_DRAFT_KO_STRUCTURED.replace("3초", "5초")
    )
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline)
    notes = await pipeline.run_stages(PLAIN_DRAFT_KO, talk_minutes=0)
    current = await pipeline.session_state()
    folder = workspace_dir() / current[state.PROJECT_ID]
    assert current[state.SEED_MARKDOWN] == PLAIN_DRAFT_KO  # byte for byte
    assert (folder / "source.original.md").read_bytes() == PLAIN_DRAFT_KO.encode()
    assert _seed_versions(current[state.PROJECT_ID], "ko") == [PLAIN_DRAFT_KO]
    assert "was not used because its letters and digits were not the same" in notes
    assert "The article is the text as supplied" in notes

    # A draft with headings goes to no model, and is kept beside the seed too.
    sent = _model_behind_the_gateway(monkeypatch, "never asked")
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline)
    assert await pipeline.run_stages(DRAFT_KO, talk_minutes=0) is None
    current = await pipeline.session_state()
    folder = workspace_dir() / current[state.PROJECT_ID]
    assert sent == []
    assert (folder / "source.original.md").read_bytes() == DRAFT_KO.encode()
    assert _seed_versions(current[state.PROJECT_ID], "ko") == [DRAFT_KO]


@pytest.mark.asyncio
async def test_a_post_read_from_the_naver_page_goes_on_with_a_notice(
    isolated_workspace, monkeypatch
) -> None:
    from app import platforms

    body = "네이버 페이지에서 읽어 온 본문입니다. " * 5

    async def fake_post(blog_id, log_no):
        return "네이버 글", body

    monkeypatch.setattr(platforms, "naver_get_post", fake_post)

    # The model's copy says something the page did not: it is not used.
    _model_behind_the_gateway(monkeypatch, "## 요약\n\n" + body)
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline)
    notes = await pipeline.run_stages(
        "https://blog.naver.com/someone/223456789012", talk_minutes=0
    )
    assert "rebuilt from the Naver page" in notes
    assert "headings, lists, images and links may be missing" in notes
    assert "was not used because its letters and digits were not the same" in notes
    current = await pipeline.session_state()
    as_read = current[state.SEED_MARKDOWN]
    assert "요약" not in as_read and body.strip() in as_read
    assert current[state.SEED_FINALIZED] is True
    assert (
        project_file(current[state.PROJECT_ID], "source.original.md").read_text(
            encoding="utf-8"
        )
        == as_read
    )
    assert _seed_versions(current[state.PROJECT_ID], "ko") == [as_read]
    # Naver is the original here, so no paste file is written for it.
    assert project_file(current[state.PROJECT_ID], "naver_blog.html") is None

    # A copy with the same letters is used; the text as read stays beside it.
    def with_a_heading(contents: str) -> str:
        text = contents.split("\n", 1)[1].rsplit("\n", 1)[0]  # between the markers
        first, rest = text.strip().split(". ", 1)
        return f"## {first}\n\n{rest}"

    _model_behind_the_gateway(monkeypatch, with_a_heading)
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline)
    notes = await pipeline.run_stages(
        "https://blog.naver.com/someone/223456789012", talk_minutes=0
    )
    assert "rebuilt from the Naver page" in notes
    assert "gave the text Markdown structure" in notes
    current = await pipeline.session_state()
    assert current[state.SEED_MARKDOWN].startswith("## ")
    assert current[state.SEED_FINALIZED] is True
    assert _seed_versions(current[state.PROJECT_ID], "ko") == [
        as_read,
        current[state.SEED_MARKDOWN],
    ]
    assert (
        project_file(current[state.PROJECT_ID], "source.original.md").read_text(
            encoding="utf-8"
        )
        == as_read
    )
    assert project_file(current[state.PROJECT_ID], "naver_blog.html") is None


@pytest.mark.asyncio
async def test_a_source_that_cannot_be_stored_stops_the_run_with_the_reason(
    isolated_workspace, monkeypatch
) -> None:
    from app import platforms

    pipeline = _new_pipeline()
    said = _stand_in_agent(pipeline)
    stopped = await pipeline.run_stages("# Too short", talk_minutes=0)
    assert stopped.startswith("The draft could not be stored.") and said == []

    async def not_found(item_id):
        raise platforms.PlatformError(
            "QIITA_NOT_FOUND", "No such post.", "check the URL"
        )

    monkeypatch.setattr(platforms, "qiita_get_item", not_found)
    stopped = await pipeline.run_stages(
        "https://qiita.com/user/items/0123456789abcdef0123", talk_minutes=0
    )
    assert stopped == "The post could not be read. No such post." and said == []


@pytest.mark.asyncio
async def test_a_draft_without_a_title_line_says_which_title_was_taken(
    isolated_workspace,
) -> None:
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline)
    draft = "## 들어가며\n\n" + "제목 줄이 없는 초안의 본문입니다. " * 5 + "\n"
    notes = await pipeline.run_stages(draft, talk_minutes=0)
    assert "has no '# Title' line" in notes and "'들어가며'" in notes
    current = await pipeline.session_state()
    assert current[state.SEED_TITLE] == "들어가며"
    assert current[state.SEED_MARKDOWN] == draft


def test_an_address_that_cannot_be_read_is_told_apart_from_a_draft() -> None:
    assert classify_source("https://medium.com/@someone/a-post-1a2b3c") == (
        "medium",
        "https://medium.com/@someone/a-post-1a2b3c",
    )
    for text in (
        "https://example.com/@someone/a-post-1a2b3c",
        "http://qiita.com/someone/items/0123456789abcdef0123",  # not https
    ):
        assert classify_source(text) == ("elsewhere", text)


@pytest.mark.asyncio
async def test_what_cannot_be_used_as_a_source_is_said_in_plain_words(
    isolated_workspace,
) -> None:
    pipeline = _new_pipeline()
    said = _stand_in_agent(pipeline)
    elsewhere = await pipeline.run_stages(
        "https://example.com/@a/post-1", talk_minutes=0
    )
    assert elsewhere.startswith("That address cannot be read")
    assert "Qiita, DEV Community and Naver Blog" in elsewhere
    short = await pipeline.run_stages("# 제목\n\n짧은 글", talk_minutes=0)
    assert short == (
        "The draft could not be stored. It is shorter than 50 characters; "
        "hand in the whole article."
    )
    for message in (elsewhere, short):
        assert "String should" not in message and "markdown:" not in message
    assert said == [] and await pipeline.session_state() == {}


@pytest.mark.asyncio
async def test_reading_the_status_never_creates_the_session(isolated_workspace) -> None:
    """A status request must not race the run's first step for the session."""
    from app.agent import app
    from app.pipeline import USER_ID

    pipeline = _new_pipeline()
    assert await pipeline.session_state() == {}
    assert (await pipeline.overview())["project_id"] == ""
    found = await pipeline._sessions.get_session(
        app_name=app.name, user_id=USER_ID, session_id=pipeline.session_id
    )
    assert found is None
    await pipeline._update_state({state.SEED_TITLE: "첫 단계"})  # the run's first step
    assert (await pipeline.session_state())[state.SEED_TITLE] == "첫 단계"


@pytest.mark.asyncio
async def test_a_file_left_from_an_earlier_text_cannot_be_saved_back(
    isolated_workspace,
) -> None:
    pipeline = await _pipeline_with_article()
    # English has a file on disk but no text in the session: a leftover.
    write_workspace_file("proj-3", "post.en.md", "# Old\n\nOld text.\n")
    with pytest.raises(ValueError, match="no longer be edited"):
        await pipeline.save_edit(
            "post.en.md", "# Old\n\nOld text. [링크](https://example.com)"
        )
    assert not (await pipeline.session_state()).get(state.localized_key("en"))


@pytest.mark.asyncio
async def test_an_edit_the_export_refuses_leaves_no_naver_file_to_paste(
    isolated_workspace,
) -> None:
    pipeline = await _pipeline_with_article()
    key = "AIza" + "B" * 35
    saved = await pipeline.save_edit(
        "post.ko.md", f"# 제목\n\n키는 {key} 입니다. [링크](https://example.com)"
    )
    assert saved["secrets"] == [{"type": saved["secrets"][0]["type"], "line": 3}]
    assert project_file("proj-3", "naver_blog.html") is None
    # Once a person has accepted the findings, the file is written again.
    await pipeline.finalize_post("ko", acknowledge_findings=True)
    assert project_file("proj-3", "naver_blog.html") is not None


@pytest.mark.asyncio
async def test_google_docs_url_is_read_as_a_draft_with_all_three_platforms_open(
    isolated_workspace, monkeypatch
) -> None:
    """INV-4, SRC-1, SRC-2: a Google Docs URL is a draft, not an existing blog post."""
    from app import platforms

    gdoc_url = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit?usp=sharing"
    assert classify_source(gdoc_url) == ("gdoc", gdoc_url)
    assert (
        classify_source(
            "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQr/edit"
        )[0]
        == "elsewhere"
    )

    async def fake_gdoc(doc_id: str) -> str:
        assert doc_id == "1AbCdEfGhIjKlMnOpQrStUvWxYz"
        return (
            "# Google Docs 초안 제목\n\n"
            + "구글 독스에서 작성한 한국어 초안 본문입니다. " * 5
        )

    monkeypatch.setattr(platforms, "google_doc_get_markdown", fake_gdoc)
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline)
    notes = await pipeline.run_stages(gdoc_url, talk_minutes=0)
    assert notes is None

    current = await pipeline.session_state()
    assert current[state.SOURCE_LANGUAGE] == "ko"
    assert current[state.SEED_TITLE] == "Google Docs 초안 제목"
    # No platform is marked as existing_source; Korean has naver_blog.html ready to review.
    pubs = current.get(state.PUBLICATIONS) or {}
    assert all(p.get("state") != "existing_source" for p in pubs.values())
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    assert rows["ko"]["next"] == "review" and rows["ko"]["can_edit"] is True


@pytest.mark.asyncio
async def test_draft_source_language_post_can_be_edited_before_finalizing(
    isolated_workspace,
) -> None:
    """Option A: when the source is a draft (.md/.txt/Google Docs), its own post is editable."""
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline)
    await pipeline.run_stages(DRAFT_KO, talk_minutes=0)

    view = await pipeline.overview()
    assert "post.ko.md" in view["editable"]
    await pipeline.finalize_post("ko")
    assert {r["language"]: r for r in (await pipeline.overview())["rows"]}["ko"][
        "finalized"
    ] is True

    edited = "# 수정된 한국어 제목\n\n다듬은 한국어 초안 본문입니다. " * 4
    await pipeline.save_edit("post.ko.md", edited, title="수정된 한국어 제목")
    rows = {r["language"]: r for r in (await pipeline.overview())["rows"]}
    assert rows["ko"]["title"] == "수정된 한국어 제목"
    assert rows["ko"]["finalized"] is False  # editing undoes finalization
    # Localized posts remain intact.
    assert rows["en"]["title"] and rows["ja"]["title"]


@pytest.mark.asyncio
async def test_a_post_is_changed_through_one_function_whoever_changes_it(
    isolated_workspace, monkeypatch
) -> None:
    """PUB-11: the editor page and the agent's two tools end in apply_post_edit."""
    from app import pipeline as pipeline_module
    from app.agent import root_agent
    from app.persistence import get_article_store
    from app.tools import articles
    from tests.unit.conftest import FakeToolContext

    calls: list[tuple[str, str, str]] = []
    real = articles.apply_post_edit

    def recorded(language, title, markdown, tool_context):
        calls.append((language, title, markdown))
        return real(language, title, markdown, tool_context)

    monkeypatch.setattr(articles, "apply_post_edit", recorded)
    monkeypatch.setattr(pipeline_module, "apply_post_edit", recorded)

    # A person, through the editor page.
    pipeline = await _pipeline_with_article()
    edited = "# 제목\n\n손으로 고친 본문입니다. [링크](https://example.com)"
    saved = await pipeline.save_edit("post.ko.md", edited, title="새 제목")
    assert calls == [("ko", "새 제목", edited + "\n")]
    by_hand = await pipeline.session_state()

    # The agent, with the tools it is given. There is no third one: the tool
    # that took a whole new text is gone.
    tools = {getattr(tool, "__name__", "") for tool in root_agent.tools}
    assert {"set_article_post_title", "replace_passage_in_article_post"} <= tools
    assert "update_article_post" not in tools
    assert not hasattr(articles, "update_article_post")
    context = FakeToolContext(by_hand)
    articles.set_article_post_title("ko", "또 다른 제목", context)
    articles.replace_passage_in_article_post("ko", "손으로", "도구로", context)
    assert [call[:2] for call in calls[1:]] == [("ko", "또 다른 제목"), ("ko", "")]
    assert calls[2][2] == (edited + "\n").replace("손으로", "도구로")

    # Whoever it was, the same things were done: the text and title in the
    # session, the structure check, a version, the file and the Naver file.
    folder = workspace_dir() / "proj-3"
    for session, title, text in (
        (by_hand, "새 제목", edited + "\n"),
        (context.state, "또 다른 제목", (edited + "\n").replace("손으로", "도구로")),
    ):
        assert session[state.localized_key("ko")] == text
        assert session[state.LOCALIZED_TITLES]["ko"] == title
        assert session[state.FIDELITY_REPORT]["ko"]["passed"] is True
        assert session[state.PUBLICATIONS]["naver_blog"]["title"] == title
    assert saved["check"] == by_hand[state.FIDELITY_REPORT]["ko"]
    stored = get_article_store().latest_version("proj-3", "ko")
    assert stored["kind"] == "edited" and "도구로 고친" in stored["markdown"]
    assert "도구로 고친" in (folder / "post.ko.md").read_text(encoding="utf-8")
    assert "도구로 고친" in (folder / "naver_blog.html").read_text(encoding="utf-8")

    # What the function refuses reaches the person as a sentence.
    await pipeline._update_state(
        {
            state.PUBLICATIONS: {
                **by_hand[state.PUBLICATIONS],
                "naver_blog": {
                    "platform": "naver_blog",
                    "language": "ko",
                    "state": "published",
                    "url": "https://blog.naver.com/someone/223456789012",
                },
            }
        }
    )
    with pytest.raises(ValueError, match="no longer be edited"):
        await pipeline.save_edit("post.ko.md", edited)
    assert len(calls) == 3  # refused before anything was changed


@pytest.mark.asyncio
async def test_a_localization_older_than_the_source_is_marked(
    isolated_workspace,
) -> None:
    """SRC-6, WEB-15: editing the source keeps what was made from it, and says so."""
    from app.workflow_agents import LocalizationReportAgent
    from tests.unit.test_workflow_gates import run_agent

    async def localized(session_state: dict) -> dict:
        """The state after the report step of a localization run."""
        events = await run_agent(
            LocalizationReportAgent(name="localization_report"), session_state
        )
        after = dict(session_state)
        for event in events:
            after.update(event.actions.state_delta or {})
        return after

    def older(session_state: dict) -> dict[str, bool]:
        rows = overview(session_state)["rows"]
        return {row["language"]: row["older_than_source"] for row in rows}

    pipeline = await _pipeline_with_article()
    await pipeline._update_state(
        {
            state.localized_key("en"): "# Title\n\nText. [link](https://example.com)\n",
            state.SLIDE_PLANS: {"en": _plan("en")},
        }
    )
    write_workspace_file("proj-3", "post.en.md", "# Title\n\nText.\n")
    before = await pipeline.session_state()
    # A localization whose source text was never recorded is not marked.
    assert older(before) == {"ko": False, "en": False, "ja": False}

    # The report step records, per language, which source text it was made from.
    made = await localized(before)
    assert made[state.LOCALIZED_FROM] == {
        "ko": state.source_digest(before),
        "en": state.source_digest(before),
    }
    assert older(made) == {"ko": False, "en": False, "ja": False}
    await pipeline._update_state({state.LOCALIZED_FROM: made[state.LOCALIZED_FROM]})

    # The source-language post is edited: the localizations and the plan stay,
    # and each localization is marked as older than the source.
    await pipeline.save_edit(
        "post.ja.md", "# 見出し\n\n書き直した本文です。[リンク](https://example.com)"
    )
    current = await pipeline.session_state()
    assert current[state.localized_key("ko")] == before[state.localized_key("ko")]
    assert current[state.localized_key("en")] == before[state.localized_key("en")]
    assert current[state.SLIDE_PLANS] == before[state.SLIDE_PLANS]
    assert older(current) == {"ko": True, "en": True, "ja": False}
    assert (await pipeline.overview())["rows"][0]["older_than_source"] is True

    # Editing a localization does not make it newer: it is still from the old text.
    await pipeline.save_edit(
        "post.ko.md", "# 제목\n\n고친 본문입니다. [링크](https://example.com)"
    )
    assert older(await pipeline.session_state())["ko"] is True

    # Localized again from the text as it is now, the mark is gone.
    again = await localized(await pipeline.session_state())
    assert older(again) == {"ko": False, "en": False, "ja": False}
    # A different article in the session keeps no record of the old one.
    assert state.LOCALIZED_FROM in state.DERIVED_FROM_SEED


MEDIUM_URL = "https://medium.com/@someone/a-post-about-agents-1a2b3c4d5e6f"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _medium_feed(*links: str) -> str:
    items = "".join(
        "<item><title>에이전트를 다룬 글</title>"
        f"<link>{link}?source=rss-0123------2</link>"
        f'<guid isPermaLink="false">https://medium.com/p/{link.rsplit("-", 1)[-1]}</guid>'
        "<content:encoded><![CDATA[<h3>왜 에이전트인가</h3>"
        "<p>에이전트는 다음에 할 일을 정하고, 언제 해도 되는지는 <code>code</code>가 "
        '정합니다. 자세한 내용은 <a href="https://example.com/a">안내서</a>에 있습니다.</p>'
        "<pre>print(1)</pre><ul><li>첫째 요점</li><li>둘째 요점</li></ul>"
        '<img src="https://medium.com/_/stat?event=post.clientViewed" width="1">'
        "]]></content:encoded></item>"
        for link in links
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>'
        f"<title>Stories by Someone on Medium</title>{items}</channel></rss>"
    )


def _medium_answers(monkeypatch, answer: httpx.Response) -> list[httpx.Request]:
    """Answer every request the platform module makes; return what it asked."""
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return answer

    def client(**kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return asked


@pytest.mark.asyncio
async def test_a_medium_post_is_read_from_the_feed_and_is_a_draft(
    isolated_workspace, monkeypatch
) -> None:
    """SRC-8, INV-4: read with one GET of the author's feed; no platform is closed."""
    asked = _medium_answers(
        monkeypatch,
        httpx.Response(
            200,
            text=_medium_feed(
                "https://medium.com/@someone/another-post-999999999999", MEDIUM_URL
            ),
        ),
    )
    assert classify_source(MEDIUM_URL) == ("medium", MEDIUM_URL)
    pipeline = _new_pipeline()
    _stand_in_agent(pipeline)
    notes = await pipeline.run_stages(MEDIUM_URL, talk_minutes=0)
    assert notes is None

    # Medium is read, once, and never written to.
    assert [(r.method, str(r.url)) for r in asked] == [
        ("GET", "https://medium.com/feed/@someone")
    ]
    assert "authorization" not in asked[0].headers

    current = await pipeline.session_state()
    assert current[state.SEED_TITLE] == "에이전트를 다룬 글"
    assert current[state.SOURCE_LANGUAGE] == "ko"
    text = current[state.SEED_MARKDOWN]
    assert text.startswith("# 에이전트를 다룬 글\n\n### 왜 에이전트인가\n\n")
    assert "`code`" in text and "[안내서](https://example.com/a)" in text
    assert "```\nprint(1)\n```" in text and "- 첫째 요점" in text
    assert "_/stat" not in text  # the tracking pixel is not an image of the post
    # The text as read is kept as the original (INV-11).
    original = project_file(current[state.PROJECT_ID], "source.original.md")
    assert original is not None and original.read_text(encoding="utf-8") == text
    # A draft: no blog platform is the source, so none is closed to writing.
    pubs = current.get(state.PUBLICATIONS) or {}
    assert all(p.get("state") != "existing_source" for p in pubs.values())
    rows = {row["language"]: row for row in (await pipeline.overview())["rows"]}
    assert rows["ko"]["next"] == "review" and rows["ko"]["can_edit"] is True


@pytest.mark.asyncio
async def test_a_medium_post_missing_from_the_feed_is_refused_in_plain_words(
    isolated_workspace, monkeypatch
) -> None:
    """SRC-8: an older post and a redirect are two different refusals."""
    from app import platforms

    pipeline = _new_pipeline()
    said = _stand_in_agent(pipeline)

    # The feed answers, and the post is not among its latest.
    asked = _medium_answers(
        monkeypatch,
        httpx.Response(
            200, text=_medium_feed("https://medium.com/@someone/newer-999999999999")
        ),
    )
    missing = await pipeline.run_stages(MEDIUM_URL, talk_minutes=0)
    assert missing.startswith("The Medium post could not be read. ")
    assert "is not in the public feed of @someone any more" in missing
    assert "an older post cannot be read from its address" in missing
    assert missing.endswith("Source document box or attach a .md file.")
    with pytest.raises(platforms.PlatformError) as not_in_feed:
        await platforms.medium_get_markdown(MEDIUM_URL)
    assert not_in_feed.value.code == "MEDIUM_POST_NOT_IN_FEED"
    assert "Source document box" in not_in_feed.value.recovery

    # A redirect is not followed, and is not mistaken for a missing post.
    asked = _medium_answers(
        monkeypatch,
        httpx.Response(
            302, headers={"location": "https://someone.example.org/feed?x=secret"}
        ),
    )
    moved = await pipeline.run_stages(MEDIUM_URL, talk_minutes=0)
    assert moved.startswith("The Medium post could not be read. ")
    assert "answered with a redirect (HTTP 302) to https://someone.example.org" in moved
    assert "is not followed" in moved and "is not in the public feed" not in moved
    assert "Whether the post is in the feed is not known." in moved
    assert "secret" not in moved
    assert [str(r.url) for r in asked] == ["https://medium.com/feed/@someone"]
    with pytest.raises(platforms.PlatformError) as redirected:
        await platforms.medium_get_markdown(MEDIUM_URL)
    assert redirected.value.code == "MEDIUM_FEED_REDIRECTED"

    for message in (missing, moved):
        assert "Traceback" not in message and "PlatformError" not in message
    # Nothing was stored and no model was asked.
    assert said == [] and await pipeline.session_state() == {}


def test_slides_of_a_run_that_stopped_before_the_posts_did_not_fail() -> None:
    """WEB-3: the source could not be read, so no slides were tried."""
    from app.pipeline import slides_progress

    def phases(view):
        entries = slides_progress(
            view, {}, ["en", "ko", "ja"], True, slides_busy=False, slides_to_come=False
        )
        return {entry["language"]: entry["phase"] for entry in entries}

    # No article was stored: every language waits; none is marked as failed.
    assert phases(overview({})) == {"en": "waiting", "ko": "waiting", "ja": "waiting"}

    # With the posts there and nothing made, the slides were asked for, could
    # have been made and are missing: that is still said as failed.
    with_posts = {
        state.PROJECT_ID: "proj-x",
        state.SEED_TITLE: "제목",
        state.SEED_MARKDOWN: "# 제목\n\n본문\n",
        state.SOURCE_LANGUAGE: "ko",
        state.localized_key("en"): "# Title\n\nBody\n",
        state.LOCALIZED_TITLES: {"en": "Title"},
    }
    assert phases(overview(with_posts)) == {
        "en": "failed",
        "ko": "failed",
        "ja": "waiting",  # no Japanese post was written
    }
