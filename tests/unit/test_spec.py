"""The specification and the tests are kept in step by this file.

``docs/SPEC.md`` names, for every requirement, the unit tests that check it
(Appendix B). A requirement without a row, a row that names a test that does
not exist, or a Korean companion that lists different requirements fails here,
so the specification cannot quietly drift away from what is tested.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "SPEC.md"
KOREAN = ROOT / "docs" / "kr-SPEC.md"
_ID = r"(?:INV|SRC|LOC|PUB|SLD|SYN|WEB|PER|SEC|OPS)-\d+"
_ROW = re.compile(rf"^\| ({_ID}) \| (.+) \|$", re.M)


def _requirements(text: str) -> list[str]:
    """Ids defined in sections 2 and 5, in the order they are written.

    A marker, not a heading, ends the part that is read, so the Korean
    companion can translate its headings.
    """
    assert "<!-- requirements:end -->" in text, "the end marker is missing"
    body = text.split("<!-- requirements:end -->", 1)[0]
    return [match.group(1) for match in _ROW.finditer(body)]


def _traceability(text: str) -> dict[str, str]:
    table = text.split("<!-- traceability:begin -->", 1)[1].split(
        "<!-- traceability:end -->", 1
    )[0]
    return {match.group(1): match.group(2) for match in _ROW.finditer(table)}


def _existing_tests() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "tests" / "unit").glob("test_*.py"):
        names.update(
            re.findall(r"^(?:async )?def (test_\w+)\(", path.read_text("utf-8"), re.M)
        )
    return names


def test_requirement_ids_are_unique_and_every_one_is_traced() -> None:
    text = SPEC.read_text(encoding="utf-8")
    defined = _requirements(text)
    assert len(defined) > 60, "the requirement tables were not found"
    assert len(defined) == len(set(defined)), "a requirement id is used twice"
    traced = _traceability(text)
    assert sorted(traced) == sorted(defined)


def test_every_test_named_in_the_specification_exists() -> None:
    existing = _existing_tests()
    for requirement, cell in _traceability(SPEC.read_text(encoding="utf-8")).items():
        named = re.findall(r"`(test_\w+)`", cell)
        if not named:
            # A requirement no unit test can check says why, in words.
            assert cell.startswith("No unit test: "), requirement
            assert len(cell) > len("No unit test: ") + 15, requirement
            continue
        missing = [name for name in named if name not in existing]
        assert not missing, f"{requirement} names tests that do not exist: {missing}"


def test_requirements_marked_as_not_built_are_listed_as_open_work() -> None:
    text = SPEC.read_text(encoding="utf-8")
    body, open_work = text.split("## 8. Open questions and work not built", 1)
    not_built = re.findall(rf"^\| ({_ID}) \| .*Not built", body, re.M)
    for requirement in not_built:
        assert requirement in open_work.split("## Appendix A", 1)[0]


def test_the_korean_companion_lists_the_same_requirements() -> None:
    english = _requirements(SPEC.read_text(encoding="utf-8"))
    korean = _requirements(KOREAN.read_text(encoding="utf-8"))
    assert korean == english
