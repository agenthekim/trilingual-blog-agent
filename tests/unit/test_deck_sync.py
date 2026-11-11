"""A deck and its Markdown plan are kept in step only when a person asks."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app import deck_sync, google_slides, state
from app.schemas import SlideDeckPlan
from app.settings import workspace_dir
from app.tools.articles import write_workspace_file
from app.tools.slides import parse_slide_plan_markdown, render_slide_plan_markdown


def _plan() -> SlideDeckPlan:
    return SlideDeckPlan.model_validate(
        {
            "language": "ko",
            "deck_title": "에이전트 런타임",
            "audience": "software engineers",
            "opening_script": "안녕하세요. 오늘은 에이전트 런타임을 살펴봅니다.",
            "closing_script": "감사합니다. 질문 받겠습니다.",
            "slides": [
                {
                    "number": 1,
                    "title": "에이전트 루프",
                    "bullets": ["목표에서 시작", "계획하고 실행"],
                    "visual_note": "text only",
                    "speaker_script": "안녕하세요.\n\n두 번째 문단입니다.",
                },
                {
                    "number": 2,
                    "title": "가드레일 🚧",
                    "bullets": ["정책 위반 차단"],
                    "visual_note": "guardrail diagram",
                    "speaker_script": "가드레일을 설명합니다.",
                    "image_url": "mermaid:3",
                },
            ],
        }
    )


def _text(content: str) -> dict[str, Any]:
    return {
        "textElements": [{"paragraphMarker": {}}, {"textRun": {"content": content}}]
    }


def _notes(number: int, text: str) -> dict[str, Any]:
    return {
        "notesPage": {
            "notesProperties": {"speakerNotesObjectId": f"notes_{number}"},
            "pageElements": [
                {"objectId": f"notes_{number}", "shape": {"text": _text(text + "\n")}}
            ],
        }
    }


def _presentation(plan: SlideDeckPlan) -> dict[str, Any]:
    """What Google reports for a deck this application made from ``plan``."""
    view = deck_sync.plan_text(plan)
    pages = [
        {
            "objectId": "slide_000",
            "pageElements": [
                {
                    "objectId": "slide_000_title",
                    "shape": {"text": _text(view["0/title"] + "\n")},
                }
            ],
            "slideProperties": _notes(0, view["0/notes"]),
        }
    ]
    for slide in plan.slides:
        n, page = slide.number, f"slide_{slide.number:03d}"
        pages.append(
            {
                "objectId": page,
                "pageElements": [
                    {
                        "objectId": f"{page}_title",
                        "shape": {"text": _text(view[f"{n}/title"] + "\n")},
                    },
                    {
                        "objectId": f"{page}_body",
                        "shape": {"text": _text(view[f"{n}/bullets"] + "\n")},
                    },
                    {"objectId": f"{page}_image", "image": {}},
                ],
                "slideProperties": _notes(n, view[f"{n}/notes"]),
            }
        )
    pages.append(
        {
            "objectId": "slide_999",
            "pageElements": [
                {
                    "objectId": "slide_999_title",
                    "shape": {"text": _text("감사합니다\n")},
                }
            ],
            "slideProperties": _notes(999, view["999/notes"]),
        }
    )
    return {"slides": pages}


def _shapes(presentation: dict[str, Any]) -> dict[str, dict]:
    found = {}
    for page in presentation["slides"]:
        notes = page.get("slideProperties", {}).get("notesPage", {})
        for element in [*page["pageElements"], *notes.get("pageElements", [])]:
            found[element["objectId"]] = element
    return found


def _apply(presentation: dict[str, Any], requests: list[dict]) -> None:
    """Do to the fake deck what Google does with insertText and deleteText."""
    shapes = _shapes(presentation)
    for request in requests:
        ((kind, body),) = request.items()
        run = shapes[body["objectId"]]["shape"]["text"]["textElements"][1]["textRun"]
        units = run["content"].encode("utf-16-le")
        if kind == "insertText":
            at = body["insertionIndex"] * 2
            units = units[:at] + body["text"].encode("utf-16-le") + units[at:]
        else:
            span = body["textRange"]
            assert span["endIndex"] * 2 < len(units)  # the last line break stays
            units = units[: span["startIndex"] * 2] + units[span["endIndex"] * 2 :]
        run["content"] = units.decode("utf-16-le")


def _edit_deck(presentation: dict[str, Any], object_id: str, content: str) -> None:
    run = _shapes(presentation)[object_id]["shape"]["text"]["textElements"][1][
        "textRun"
    ]
    run["content"] = content + "\n"


def test_slide_plan_markdown_reads_back_as_the_same_plan() -> None:
    plan = _plan()
    assert parse_slide_plan_markdown(render_slide_plan_markdown(plan), "ko") == plan
    assert "Opening (title slide):" in render_slide_plan_markdown(plan)
    assert "Closing (thank-you slide):" in render_slide_plan_markdown(plan)

    # A file written before these sections existed still reads; they are empty.
    older = render_slide_plan_markdown(plan)
    older = older[: older.index("Closing (thank-you slide):")].replace(
        "Opening (title slide):\n\n" + plan.opening_script + "\n\n", ""
    )
    read = parse_slide_plan_markdown(older, "ko")
    assert (read.opening_script, read.closing_script) == ("", "")
    assert read.slides == plan.slides

    # Extra blank lines and a script edited by hand are fine.
    edited = render_slide_plan_markdown(plan).replace(
        "가드레일을 설명합니다.", "\n가드레일을 새로 설명합니다.\n\n한 문단 더.\n"
    )
    again = parse_slide_plan_markdown(edited, "ko")
    assert (
        again.slides[1].speaker_script == "가드레일을 새로 설명합니다.\n\n한 문단 더."
    )
    assert (
        again.slides[1].image_url == "mermaid:3" and again.slides[0] == plan.slides[0]
    )

    broken = render_slide_plan_markdown(plan).replace("Speaker script", "Script", 1)
    with pytest.raises(ValueError, match="Slide 1 has no 'Speaker script"):
        parse_slide_plan_markdown(broken, "ko")
    with pytest.raises(ValueError, match="No slide heading"):
        parse_slide_plan_markdown("# Deck\n\njust text\n", "ko")


def test_a_fresh_deck_and_its_plan_agree() -> None:
    plan = _plan()
    view, holders = deck_sync.deck_text(_presentation(plan))
    assert view == deck_sync.plan_text(plan)
    assert holders["2/notes"].object_id == "notes_2"
    assert holders["2/title"].length == len("가드레일 🚧".encode("utf-16-le")) // 2
    assert deck_sync.differences(deck_sync.plan_text(plan), view, view) == []


def test_push_carries_only_what_the_file_changed_and_keeps_the_style_anchor() -> None:
    plan = _plan()
    baseline = deck_sync.plan_text(plan)
    deck = _presentation(plan)
    # A person edits the notes of slide 1 in the deck, and in the file the
    # title and bullets of slide 2 plus the script of slide 2.
    _edit_deck(deck, "notes_1", "덱에서 직접 고친 대본")
    edited = plan.model_copy(deep=True)
    edited.slides[1].title = "가드레일과 승인 🚧🚧"
    edited.slides[1].bullets = ["정책 위반 차단", "사람의 승인 대기"]
    edited.slides[1].speaker_script = "파일에서 고친 대본"

    file_view = deck_sync.plan_text(edited)
    deck_view, holders = deck_sync.deck_text(deck)
    found = deck_sync.differences(file_view, deck_view, baseline)
    carried = deck_sync.to_carry(found, "push")
    assert sorted(d.key for d in carried) == ["2/bullets", "2/notes", "2/title"]
    summary = deck_sync.describe("push", carried, file_view, deck_view, baseline)
    assert (summary["slides"], summary["conflicts"]) == ([2], [])

    requests = deck_sync.update_requests(carried, holders)
    # New text goes in at the front of the old, so it inherits its formatting.
    assert all(
        r["insertText"]["insertionIndex"] == 0 for r in requests if "insertText" in r
    )
    _apply(deck, requests)
    after, _ = deck_sync.deck_text(deck)
    assert after["2/title"] == "가드레일과 승인 🚧🚧"
    assert after["2/bullets"] == "정책 위반 차단\n사람의 승인 대기"
    assert after["2/notes"] == "파일에서 고친 대본\n\n[Visual] guardrail diagram"
    assert after["1/notes"] == "덱에서 직접 고친 대본"  # the deck-only edit survives

    # The pull that follows brings just that deck-only edit into the file.
    pulled = deck_sync.to_carry(
        deck_sync.differences(file_view, after, baseline), "pull"
    )
    assert [d.key for d in pulled] == ["1/notes"]
    merged = deck_sync.merged_plan(edited, pulled)
    assert merged.slides[0].speaker_script == "덱에서 직접 고친 대본"
    assert merged.slides[1].speaker_script == "파일에서 고친 대본"


def test_a_field_edited_on_both_sides_is_reported_as_a_conflict() -> None:
    plan = _plan()
    baseline = deck_sync.plan_text(plan)
    deck = _presentation(plan)
    _edit_deck(deck, "notes_2", "덱 쪽 대본\n\n[Visual] new picture")
    edited = plan.model_copy(deep=True)
    edited.slides[1].speaker_script = "파일 쪽 대본"
    file_view = deck_sync.plan_text(edited)
    deck_view, _ = deck_sync.deck_text(deck)
    found = deck_sync.differences(file_view, deck_view, baseline)
    for direction in ("push", "pull"):
        carried = deck_sync.to_carry(found, direction)
        summary = deck_sync.describe(direction, carried, file_view, deck_view, baseline)
        assert summary["conflicts"] == [2] and summary["notes"] == 1
    merged = deck_sync.merged_plan(edited, deck_sync.to_carry(found, "pull"))
    assert merged.slides[1].speaker_script == "덱 쪽 대본"
    assert merged.slides[1].visual_note == "new picture"

    # Without a record of what both last held, every difference is carried.
    unknown = deck_sync.differences(file_view, deck_view, None)
    assert [d.key for d in deck_sync.to_carry(unknown, "push")] == ["2/notes"]
    assert (
        deck_sync.describe("push", unknown, file_view, deck_view, None)["conflicts"]
        == []
    )


def test_slides_missing_on_one_side_are_left_alone_and_named() -> None:
    plan = _plan()
    deck = _presentation(plan)
    del deck["slides"][2]  # slide 2 was deleted in the deck
    deck["slides"].append({"objectId": "g2f9", "pageElements": []})  # one added by hand
    file_view = deck_sync.plan_text(plan)
    deck_view, _ = deck_sync.deck_text(deck)
    found = deck_sync.differences(file_view, deck_view, file_view)
    summary = deck_sync.describe("push", found, file_view, deck_view, file_view)
    assert (
        found == [] and summary["only_in_file"] == [2] and summary["only_in_deck"] == []
    )


async def _pipeline_with_deck(monkeypatch, plan: SlideDeckPlan):
    from app.agent import app
    from app.pipeline import USER_ID, ArticlePipeline

    deck = _presentation(plan)
    written: list[list[dict]] = []

    async def read(presentation_id: str) -> dict[str, Any]:
        assert presentation_id == "deck-ko"
        return copy.deepcopy(deck)

    async def update(presentation_id: str, requests: list[dict]) -> None:
        written.append(requests)
        _apply(deck, requests)

    monkeypatch.setattr(google_slides, "read_presentation", read)
    monkeypatch.setattr(google_slides, "update_presentation", update)
    write_workspace_file("proj-9", "slides.ko.md", render_slide_plan_markdown(plan))
    pipeline = ArticlePipeline(log=lambda _: None)
    await pipeline._sessions.create_session(
        app_name=app.name,
        user_id=USER_ID,
        session_id=pipeline.session_id,
        state={
            state.PROJECT_ID: "proj-9",
            state.SOURCE_LANGUAGE: "ja",
            state.SLIDE_PLANS: {"ko": plan.model_dump()},
            state.GOOGLE_SLIDES_DECKS: {
                "ko": {"presentation_id": "deck-ko", "url": "https://docs.google.com/x"}
            },
            state.DECK_TEXT_BASELINE: {"ko": deck_sync.plan_text(plan)},
        },
    )
    return pipeline, deck, written


@pytest.mark.asyncio
async def test_saved_notes_reach_the_deck_only_after_approval(
    isolated_workspace, monkeypatch
) -> None:
    plan = _plan()
    pipeline, deck, written = await _pipeline_with_deck(monkeypatch, plan)
    edited = render_slide_plan_markdown(plan).replace(
        "가드레일을 설명합니다.", "가드레일을 다시 설명합니다."
    )
    saved = await pipeline.save_edit("slides.ko.md", edited)
    assert saved["check"] == {"passed": True, "errors": []}
    # The agent's plan of this language follows the file.
    current = await pipeline.session_state()
    record = current[state.SLIDE_PLANS]["ko"]
    assert record["slides"][1]["speaker_script"].startswith("가드레일을 다시")
    assert current[state.SLIDE_TIMING]["ko"]["slide_count"] == len(plan.slides)

    preview = await pipeline.sync_deck_text("ko", "push", apply=False)
    assert (preview["slides"], preview["notes"], preview["applied"]) == ([2], 1, False)
    assert written == []  # asking what would change writes nothing

    done = await pipeline.sync_deck_text("ko", "push", apply=True)
    assert done["applied"] and len(written) == 1
    assert deck_sync.deck_text(deck)[0]["2/notes"].startswith(
        "가드레일을 다시 설명합니다."
    )
    # Now they agree again: a second push has nothing to do and writes nothing.
    again = await pipeline.sync_deck_text("ko", "push", apply=True)
    assert again["slides"] == [] and not again["applied"] and len(written) == 1


@pytest.mark.asyncio
async def test_notes_edited_in_the_deck_are_pulled_into_the_file(
    isolated_workspace, monkeypatch
) -> None:
    plan = _plan()
    pipeline, deck, written = await _pipeline_with_deck(monkeypatch, plan)
    _edit_deck(deck, "notes_1", "덱에서 고친 대본")
    _edit_deck(deck, "slide_001_title", "덱에서 고친 제목")
    _edit_deck(deck, "notes_999", "감사합니다. 덱에서 고친 맺음말입니다.")
    path = workspace_dir() / "proj-9" / "slides.ko.md"
    before = path.read_text(encoding="utf-8")

    preview = await pipeline.sync_deck_text("ko", "pull", apply=False)
    assert (preview["slides"], preview["titles"], preview["notes"]) == ([1, 999], 1, 2)
    assert path.read_text(encoding="utf-8") == before

    await pipeline.sync_deck_text("ko", "pull", apply=True)
    pulled = parse_slide_plan_markdown(path.read_text(encoding="utf-8"), "ko")
    assert pulled.slides[0].title == "덱에서 고친 제목"
    assert pulled.slides[0].speaker_script == "덱에서 고친 대본"
    assert pulled.closing_script == "감사합니다. 덱에서 고친 맺음말입니다."
    assert pulled.slides[1] == plan.slides[1] and written == []  # the deck is only read
    assert pulled.opening_script == plan.opening_script
    # The plan of record follows the file, so a deck made again uses the pulled text.
    record = (await pipeline.session_state())[state.SLIDE_PLANS]["ko"]
    assert record["slides"][0]["title"] == "덱에서 고친 제목"
    # A push right after has nothing to carry: nothing is written back.
    assert (await pipeline.sync_deck_text("ko", "push", apply=True))["slides"] == []


@pytest.mark.asyncio
async def test_sync_needs_a_deck_and_a_readable_plan(
    isolated_workspace, monkeypatch
) -> None:
    pipeline, _, written = await _pipeline_with_deck(monkeypatch, _plan())
    with pytest.raises(ValueError, match="no Google Slides deck"):
        await pipeline.sync_deck_text("en", "push", apply=True)
    saved = await pipeline.save_edit("slides.ko.md", "# 제목만 남긴 파일\n")
    assert (
        saved["check"]["passed"] is False
        and "No slide heading" in saved["check"]["errors"][0]
    )
    with pytest.raises(ValueError, match="No slide heading"):
        await pipeline.sync_deck_text("ko", "push", apply=True)
    assert written == []


def test_a_title_emptied_in_the_deck_is_not_pulled() -> None:
    plan = _plan()
    deck = _presentation(plan)
    _edit_deck(deck, "slide_001_title", "")
    _edit_deck(deck, "notes_1", "덱에서 고친 대본")
    deck_view, _ = deck_sync.deck_text(deck)
    baseline = deck_sync.plan_text(plan)
    found = deck_sync.differences(baseline, deck_view, baseline)
    assert {d.key for d in found} == {"1/title", "1/notes"}
    pulled = deck_sync.to_carry(found, "pull")
    assert [d.key for d in pulled] == ["1/notes"]  # the emptied title stays as it was
    merged = deck_sync.merged_plan(plan, pulled)
    assert merged.slides[0].title == plan.slides[0].title
