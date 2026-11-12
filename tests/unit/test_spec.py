"""The specification and the tests are kept in step by this file.

``docs/SPEC.md`` names, for every requirement, the unit tests that check it
(Appendix B). A requirement without a row, a row that names a test that does
not exist, or a Korean companion that lists different requirements fails here,
so the specification cannot quietly drift away from what is tested.

``README.md`` has a table that says where each capability lives, one row for
each criterion of the course rubric. It is checked here too (OPS-7): the rows
are those criteria, and what a row points to has to be there.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "SPEC.md"
KOREAN = ROOT / "docs" / "kr-SPEC.md"
README = ROOT / "README.md"
_ID = r"(?:INV|SRC|LOC|PUB|SLD|SYN|WEB|PER|SEC|OPS|RUN)-\d+"
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


_CAPABILITIES = "## Where each capability lives"
# What a path in the table ends with when it is a file.
_FILE_SUFFIXES = (".py", ".md", ".tf", ".yml", ".yaml", ".json", ".js", ".html", ".css")
_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_SETTING = re.compile(r"([A-Z][A-Z0-9_]*)=\S+")


# The criteria of the course rubric, by its names and in its order.
_CRITERIA = (
    "Comprehensive Tool Docstrings",
    "Descriptive Naming",
    "Explicit JSON Schemas",
    "Guided Error Handling",
    "Robust System Instructions",
    "History Compaction",
    "Persistent Session State",
    "Async Memory Operations",
    "Multi-Agent Patterns",
    "Strategic Model Routing",
    "Guardrails & Policy Plugins",
    "Human-in-the-Loop Hooks",
    "Structured JSON Logging",
    "Intent vs. Outcome Capture",
    "Distributed Tracing",
    "PII Redaction",
    "Automated Evaluation Suites",
    "Infrastructure as Code",
    "Secure Secret Management",
)
_AREAS = (
    "Tool & Interface Design",
    "Context & Memory",
    "Orchestration & Logic",
    "Observability & Tracing",
    "Infrastructure & CI/CD",
)
_COLUMNS = ["#", "Area", "Criterion", "Open this first", "Also"]
# No cell of the table is longer than this, so a row can be read at a glance.
_LONGEST_CELL = 200


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _capability_rows(text: str) -> list[str]:
    """The rows of the README's capability table, without its two heading rows.

    The table is one table: every line of the section that starts a row
    belongs to it, the first is its heading and the second its ruler.
    """
    assert text.count(_CAPABILITIES) == 1, "the capability table was not found"
    section = text.split(_CAPABILITIES, 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    assert _cells(rows[0]) == _COLUMNS, "the heading of the capability table changed"
    assert set(rows[1]) <= set("|:- "), "the second line of the table is not a ruler"
    assert len(rows) == len(_CRITERIA) + 2, "the table has a row too many or too few"
    return rows[2:]


def test_the_readme_table_has_the_nineteen_criteria_in_order() -> None:
    """OPS-7. One row for each criterion, by the rubric's name, under its area."""
    text = README.read_text(encoding="utf-8")
    rows = [_cells(row) for row in _capability_rows(text)]
    assert all(len(cells) == len(_COLUMNS) for cells in rows)
    assert [cells[0] for cells in rows] == [str(n) for n in range(1, 20)]
    assert [cells[2] for cells in rows] == list(_CRITERIA)
    # Four criteria to an area, the last area has the three that are left.
    assert [cells[1] for cells in rows] == [_AREAS[n // 4] for n in range(19)]
    too_long = [cells[2] for cells in rows if max(map(len, cells)) > _LONGEST_CELL]
    assert not too_long, f"a cell is longer than {_LONGEST_CELL} characters"
    # The table comes before the description of the product.
    assert text.index(_CAPABILITIES) < text.index("\n## Problem and solution")


def _is_path(span: str) -> bool:
    return "/" in span or span.endswith(_FILE_SUFFIXES)


def _texts_under(path: Path) -> list[str]:
    """The text of a file, or of every file in a folder and below it."""
    files = [path] if path.is_file() else sorted(path.rglob("*"))
    return [
        file.read_text(encoding="utf-8", errors="ignore")
        for file in files
        if file.is_file() and "__pycache__" not in file.parts
    ]


def _has_name(text: str, name: str) -> bool:
    """Whether every part of a (dotted) name is in the text as a whole word."""
    return all(re.search(rf"\b{re.escape(part)}\b", text) for part in name.split("."))


def test_every_place_the_readme_points_to_exists() -> None:
    """OPS-7. Everything the table gives in backticks is one of three things.

    - A path (it has a ``/`` or ends like a file): it exists in the repository,
      seen from its root.
    - A name (a Python identifier, or several joined by dots, as in
      ``Job.save``): one file that the same row names, or one file in a folder
      that the row names, has every part of it as a whole word.
    - A setting (``NAME=value``): ``NAME`` is found the same way.

    Anything else in backticks fails, so the table cannot hold what this test
    does not understand; and a row has to name a path.
    """
    for row in _capability_rows(README.read_text(encoding="utf-8")):
        capability = _cells(row)[2]
        spans = re.findall(r"`([^`]+)`", row)
        paths = [span for span in spans if _is_path(span)]
        assert paths, f"{capability}: the row names no file"
        # What to open first is a file, and not the largest one.
        first = re.findall(r"`([^`]+)`", _cells(row)[3])
        assert first and _is_path(first[0]), f"{capability}: no file to open first"
        assert first[0] != "app/pipeline.py", capability
        missing = [path for path in paths if not (ROOT / path).exists()]
        assert not missing, f"{capability}: not in the repository: {missing}"
        texts = [text for path in paths for text in _texts_under(ROOT / path)]
        for span in spans:
            if _is_path(span):
                continue
            setting = _SETTING.fullmatch(span)
            name = setting.group(1) if setting else span
            assert _NAME.fullmatch(name), (
                f"{capability}: `{span}` is neither a path, a name nor a setting"
            )
            assert any(_has_name(text, name) for text in texts), (
                f"{capability}: `{name}` is in none of {paths}"
            )
