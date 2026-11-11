# ruff: noqa: RUF001  (Japanese fixtures use full-width punctuation on purpose)
"""Deterministic checks used for self-evaluation: structure fidelity and slide timing."""

from __future__ import annotations

from app.markdown_checks import (
    check_fidelity,
    estimate_speaking_seconds,
    extract_urls,
    heading_levels,
    split_code_blocks,
)
from app.schemas import SlideDeckPlan, SlideSpec
from app.tools.slides import slide_timing_report

SOURCE = """# Title

Intro with a [link](https://example.com/a) and https://example.com/b.

## Code

```python
# set up
x = run("job")
```

## End
"""


def test_identical_text_passes() -> None:
    report = check_fidelity(SOURCE, SOURCE)
    assert report["passed"] and not report["errors"] and not report["warnings"]


def test_translated_comment_is_a_warning_not_an_error() -> None:
    localized = SOURCE.replace("# set up", "# 準備")
    report = check_fidelity(SOURCE, localized)
    assert report["passed"]
    assert any("only comments differ" in w for w in report["warnings"])


def test_altered_code_is_an_error() -> None:
    localized = SOURCE.replace('run("job")', 'run("ジョブ")')
    report = check_fidelity(SOURCE, localized)
    assert not report["passed"]
    assert any("code was altered" in e for e in report["errors"])


def test_missing_code_block_link_and_heading_are_errors() -> None:
    localized = "# Title\n\nIntro only.\n"
    errors = " ".join(check_fidelity(SOURCE, localized)["errors"])
    assert "Code block count changed" in errors
    assert "link(s) missing" in errors
    assert "Heading structure changed" in errors


def test_truncated_and_empty_translations_fail() -> None:
    long_source = SOURCE + ("Paragraph of prose. " * 200)
    assert not check_fidelity(long_source, SOURCE)["passed"]
    assert not check_fidelity(SOURCE, "   ")["passed"]


def test_unclosed_fence_is_detected() -> None:
    blocks, _, balanced = split_code_blocks("text\n```python\nx = 1\n")
    assert blocks == ["x = 1"] and not balanced
    assert not check_fidelity(SOURCE, SOURCE + "\n```\nunclosed")["passed"]


def test_urls_and_headings_ignore_code_blocks() -> None:
    text = "# H\n\n```\n# not a heading https://ignored.example\n```\n\n[a](https://kept.example/x)"
    assert extract_urls(text) == {"https://kept.example/x"}
    assert heading_levels(text) == [1]


def test_speaking_time_uses_words_for_english_and_characters_for_cjk() -> None:
    assert estimate_speaking_seconds("word " * 140, "en") == 60.0
    assert estimate_speaking_seconds("가" * 300, "ko") == 60.0
    assert estimate_speaking_seconds("あ" * 165, "ja") == 30.0


def _plan(scripts: list[str]) -> SlideDeckPlan:
    return SlideDeckPlan(
        language="en",
        deck_title="Deck",
        audience="engineers",
        opening_script="Welcome. This talk is Deck.",
        closing_script="Thank you. Questions?",
        slides=[
            SlideSpec(
                number=i,
                title=f"S{i}",
                bullets=["a", "b"],
                visual_note="text only",
                speaker_script=script,
            )
            for i, script in enumerate(scripts, start=1)
        ],
    )


def test_slide_timing_passes_for_one_minute_scripts() -> None:
    report = slide_timing_report(_plan(["word " * 140] * 3), expected_slides=3)
    assert report["passed"] and "recovery_instruction" not in report


def test_slide_timing_wants_words_for_the_title_and_thank_you_slides() -> None:
    plan = _plan(["word " * 140] * 3).model_copy(update={"opening_script": " "})
    report = slide_timing_report(plan, expected_slides=3)
    assert not report["passed"]
    assert "write opening_script" in report["recovery_instruction"]


def test_slide_timing_names_the_slides_to_fix() -> None:
    report = slide_timing_report(
        _plan(["word " * 20, "word " * 140, "word " * 400]), expected_slides=5
    )
    assert not report["passed"]
    instruction = report["recovery_instruction"]
    assert "exactly 5 slides" in instruction
    assert "lengthen the scripts of slides [1]" in instruction
    assert "shorten the scripts of slides [3]" in instruction


MERMAID_JA = """```mermaid
flowchart TD
    Agent["Agent<br/>（目標を確認）"]
    subgraph Decision["判断"]
        Selection["Tool Selection"]
    end
    Agent -->|選択| Selection
    Selection --> Done[完了]
```
"""


def test_mermaid_labels_may_be_translated() -> None:
    localized = (
        MERMAID_JA.replace("（目標を確認）", "(목표 확인)")
        .replace("判断", "판단")
        .replace("選択", "선택")
        .replace("完了", "완료")
    )
    report = check_fidelity(MERMAID_JA, localized)
    assert report["passed"], report["errors"]


def test_mermaid_structure_must_not_change() -> None:
    renamed_node = MERMAID_JA.replace("Selection[", "Choice[")
    dropped_edge = MERMAID_JA.replace("    Selection --> Done[完了]\n", "")
    for broken in (renamed_node, dropped_edge):
        report = check_fidelity(MERMAID_JA, broken)
        assert not report["passed"]
        assert "mermaid" in report["errors"][0]


TEXT_JA = """```text
search_hotels
（ホテル候補を検索）
  ↓
book_hotel
```
"""


def test_plain_text_block_translates_prose_lines_only() -> None:
    translated = TEXT_JA.replace("（ホテル候補を検索）", "(호텔 후보 검색)")
    assert check_fidelity(TEXT_JA, translated)["passed"]

    renamed_identifier = translated.replace("book_hotel", "호텔_예약")
    report = check_fidelity(TEXT_JA, renamed_identifier)
    assert not report["passed"]
    assert "book_hotel" in report["errors"][0]

    dropped_line = TEXT_JA.replace("  ↓\n", "")
    assert not check_fidelity(TEXT_JA, dropped_line)["passed"]


def test_english_plain_text_block_stays_verbatim() -> None:
    source = "```text\n$ uv sync\nResolved 12 packages\n```\n"
    changed = source.replace("Resolved 12 packages", "패키지 12개 확인")
    assert not check_fidelity(source, changed)["passed"]


def test_fence_language_must_not_change() -> None:
    source = "```python\nprint(1)\n```\n"
    report = check_fidelity(source, source.replace("python", "text"))
    assert not report["passed"]
    assert "fence language" in report["errors"][0]
