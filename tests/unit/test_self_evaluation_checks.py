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


def test_slide_timing_names_the_slides_to_fix() -> None:
    report = slide_timing_report(
        _plan(["word " * 20, "word " * 140, "word " * 400]), expected_slides=5
    )
    assert not report["passed"]
    instruction = report["recovery_instruction"]
    assert "exactly 5 slides" in instruction
    assert "lengthen the scripts of slides [1]" in instruction
    assert "shorten the scripts of slides [3]" in instruction
