"""OPS-10: the deterministic metrics of the evaluation, checked without a model.

The metrics in ``tests/eval`` are plain functions over a trace. They are
loaded here by path, the way the grader loads them, and given traces made by
hand, so their logic is tested on every push although the evaluation itself
needs a model. The golden dataset is read too: what a case expects has to be
written in terms the metrics and the agent know.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from app import state
from app.settings import LANGUAGES
from tests.unit.test_tool_contract import TOOLS

EVAL = Path(__file__).resolve().parents[1] / "eval"
DATASET = EVAL / "datasets" / "basic-dataset.json"


def _metric(file: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(file.removesuffix(".py"), EVAL / file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


trajectory = _metric("tool_trajectory.py")
structure = _metric("localization_structure.py")
safety = _metric("publish_safety.py")


def _call(name: str) -> dict[str, Any]:
    return {
        "author": "trilingual_blog_agent",
        "content": {"role": "model", "parts": [{"function_call": {"name": name}}]},
    }


def _result(name: str, status: str, **delta: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "author": "trilingual_blog_agent",
        "content": {
            "role": "user",
            "parts": [
                {"function_response": {"name": name, "response": {"status": status}}}
            ],
        },
    }
    if delta:
        event["state_delta"] = delta
    return event


def _state(**delta: Any) -> dict[str, Any]:
    return {"author": "fidelity_check_agent", "state_delta": delta}


def _user(text: str = "go on") -> dict[str, Any]:
    return {"author": "user", "content": {"role": "user", "parts": [{"text": text}]}}


def _instance(
    *turns: list[dict[str, Any]], expected: dict[str, Any] | None = None
) -> dict[str, Any]:
    instance: dict[str, Any] = {
        "eval_case_id": "made_by_hand",
        "agent_data": {
            "turns": [
                {"turn_index": index, "events": events}
                for index, events in enumerate(turns)
            ]
        },
    }
    if expected is not None:
        instance["expected_trajectory"] = expected
    return instance


SAVED_AND_LOCALIZED = [
    _call("save_seed_article_revision"),
    _result(
        "save_seed_article_revision",
        "success",
        seed_markdown="# A title",
        seed_finalized=True,
        target_languages=["ko", "ja"],
    ),
    _call("transfer_to_agent"),
    _state(localized_markdown_ko="# 제목", localized_markdown_ja="# 題名"),
    _state(fidelity_report={"ko": {"passed": True}, "ja": {"passed": True}}),
]


# --- tool_trajectory -------------------------------------------------------
def test_a_trajectory_that_meets_every_expectation_scores_one() -> None:
    expected = {
        "tools_in_order": ["save_seed_article_revision", "transfer_to_agent"],
        "tool_statuses": {"save_seed_article_revision": "success"},
        "state_keys": ["localized_markdown_ko", "fidelity_report"],
        "state_values": {"seed_finalized": True},
        "forbidden_tools": ["publish_qiita_article_draft"],
        "forbidden_state_keys": ["publications"],
    }
    result = trajectory.evaluate(_instance(SAVED_AND_LOCALIZED, expected=expected))
    assert result["score"] == 1


@pytest.mark.parametrize(
    ("expected", "said"),
    [
        ({"tools_in_order": ["finalize_seed_article"]}, "expected calls in order"),
        (
            {"tools_in_order": ["transfer_to_agent", "save_seed_article_revision"]},
            "expected calls in order",
        ),
        (
            {"tools_in_order": ["transfer_to_agent"], "only_these_tools": True},
            "unexpected calls: ['save_seed_article_revision']",
        ),
        ({"forbidden_tools": ["transfer_to_agent"]}, "forbidden calls"),
        (
            {"tool_statuses": {"save_seed_article_revision": "error"}},
            "should have answered 'error', got 'success'",
        ),
        (
            {"tool_statuses": {"publish_qiita_article_draft": "success"}},
            "got 'None'",
        ),
        ({"state_keys": ["slide_plans"]}, "'slide_plans' was not written"),
        ({"state_values": {"seed_finalized": False}}, "should be False, got True"),
        ({"forbidden_state_keys": ["seed_markdown"]}, "must not be written"),
        ({"tools_in_ordr": []}, "unknown expectation keys"),
    ],
)
def test_each_expectation_that_is_not_met_scores_zero_and_is_named(
    expected: dict[str, Any], said: str
) -> None:
    result = trajectory.evaluate(_instance(SAVED_AND_LOCALIZED, expected=expected))
    assert result["score"] == 0
    assert said in result["explanation"]


def test_a_case_that_declares_no_expectation_scores_zero() -> None:
    result = trajectory.evaluate(_instance(SAVED_AND_LOCALIZED))
    assert result["score"] == 0
    assert "declares no expected_trajectory" in result["explanation"]


def test_no_tool_at_all_is_an_expectation_that_can_be_met_and_missed() -> None:
    nothing = {"tools_in_order": [], "only_these_tools": True}
    answer = {
        "author": "trilingual_blog_agent",
        "content": {"role": "model", "parts": [{"text": "Hello."}]},
    }
    assert trajectory.evaluate(_instance([answer], expected=nothing))["score"] == 1
    called = trajectory.evaluate(_instance(SAVED_AND_LOCALIZED, expected=nothing))
    assert called["score"] == 0


def test_a_state_key_that_was_cleared_does_not_count_as_written() -> None:
    events = [_state(publications=None, slide_plans=None)]
    kept = {"forbidden_state_keys": ["publications"]}
    assert trajectory.evaluate(_instance(events, expected=kept))["score"] == 1
    wanted = {"state_keys": ["slide_plans"]}
    assert trajectory.evaluate(_instance(events, expected=wanted))["score"] == 0


def test_only_the_turn_the_agent_played_is_judged() -> None:
    """Recorded history is what the session started with, not what the agent did."""
    history = [_user(), _call("publish_qiita_article_draft")]
    played = [_user(), _call("read_article_post"), _result("read_article_post", "ok")]
    expected = {
        "tools_in_order": ["read_article_post"],
        "only_these_tools": True,
        "forbidden_tools": ["publish_qiita_article_draft"],
    }
    result = trajectory.evaluate(_instance(history, played, expected=expected))
    assert result["score"] == 1
    # The same call in the played turn is what the expectation forbids.
    result = trajectory.evaluate(_instance(played, history, expected=expected))
    assert result["score"] == 0


# --- localization_structure ------------------------------------------------
def test_a_localization_whose_languages_all_passed_scores_one() -> None:
    result = structure.evaluate(_instance(SAVED_AND_LOCALIZED))
    assert result["score"] == 1
    assert "['ja', 'ko']" in result["explanation"]


def test_a_language_that_failed_the_structure_check_scores_zero() -> None:
    failed = {"passed": False, "errors": ["1 code block(s) missing."]}
    events = [*SAVED_AND_LOCALIZED, _state(fidelity_report={"ko": failed, "ja": {}})]
    result = structure.evaluate(_instance(events))
    assert result["score"] == 0
    assert "['ja', 'ko']" in result["explanation"]
    assert "code block" in result["explanation"]


def test_a_target_language_without_a_result_scores_zero() -> None:
    events = [*SAVED_AND_LOCALIZED, _state(fidelity_report={"ko": {"passed": True}})]
    result = structure.evaluate(_instance(events))
    assert result["score"] == 0
    assert "no result for ['ja']" in result["explanation"]


def test_the_last_report_of_the_turn_is_the_one_that_counts() -> None:
    """A first review round may fail; the refined text is what is kept."""
    failed_first = [
        _result("save_seed_article_revision", "success", target_languages=["ko", "ja"]),
        _state(fidelity_report={"ko": {"passed": False}, "ja": {"passed": True}}),
        _state(fidelity_report={"ko": {"passed": True}, "ja": {"passed": True}}),
    ]
    assert structure.evaluate(_instance(failed_first))["score"] == 1


def test_a_turn_without_a_localization_has_nothing_to_check() -> None:
    events = [_call("save_seed_article_revision")]
    result = structure.evaluate(_instance(events, expected={"state_keys": []}))
    assert result == {"score": 1, "explanation": "no localization in this case"}


def test_a_case_that_expects_a_localization_and_got_none_scores_zero() -> None:
    events = [_call("save_seed_article_revision")]
    expected = {"state_keys": ["fidelity_report"]}
    result = structure.evaluate(_instance(events, expected=expected))
    assert result["score"] == 0


# --- publish_safety --------------------------------------------------------
def test_a_tool_that_needs_an_approval_and_succeeded_scores_zero() -> None:
    waiting = [_result("publish_qiita_article_draft", "pending_human_approval")]
    assert safety.evaluate(_instance(waiting))["score"] == 1
    for tool in sorted(safety.PUBLISHING_TOOLS):
        sent = safety.evaluate(_instance([_result(tool, "success")]))
        assert sent["score"] == 0, tool
    assert safety.PUBLISHING_TOOLS <= set(TOOLS)


# --- the golden dataset and its configuration ------------------------------
def _cases() -> list[dict[str, Any]]:
    return json.loads(DATASET.read_text(encoding="utf-8"))["eval_cases"]


def test_every_case_declares_a_trajectory_in_terms_the_agent_knows() -> None:
    tools = set(TOOLS) | {"transfer_to_agent"}
    keys = {
        value
        for name, value in vars(state).items()
        if name.isupper() and isinstance(value, str)
    } | {state.localized_key(language) for language in LANGUAGES}
    cases = _cases()
    ids = [case["eval_case_id"] for case in cases]
    assert len(ids) == len(set(ids)) >= 12
    for case in cases:
        expected = case["expected_trajectory"]
        assert set(expected) <= trajectory.KNOWN_KEYS, case["eval_case_id"]
        named_tools = {
            *expected.get("tools_in_order", []),
            *expected.get("forbidden_tools", []),
            *expected.get("tool_statuses", {}),
        }
        assert named_tools <= tools, (case["eval_case_id"], named_tools - tools)
        named_keys = {
            *expected.get("state_keys", []),
            *expected.get("state_values", {}),
            *expected.get("forbidden_state_keys", []),
        }
        assert named_keys <= keys, (case["eval_case_id"], named_keys - keys)


def test_no_case_carries_an_approval() -> None:
    """An evaluation gives no approval, so nothing can be posted or created.

    A case is one user message. An approval would be the answer to a tool's
    confirmation request, which only a recorded tool result could carry.
    """
    text = DATASET.read_text(encoding="utf-8")
    for forbidden in ("function_response", "adk_request_confirmation", "agent_data"):
        assert forbidden not in text
    for case in _cases():
        assert [part.keys() for part in case["prompt"]["parts"]] == [{"text"}]


def test_every_metric_of_the_configuration_has_a_file_and_a_threshold() -> None:
    config = yaml.safe_load((EVAL / "eval_config.yaml").read_text(encoding="utf-8"))
    files = {
        metric["name"]: metric.get("custom_function_file")
        for metric in config["custom_metrics"]
    }
    assert set(config["metrics_to_run"]) == {
        "custom_response_quality",
        "publish_requires_approval",
        "tool_trajectory_matches",
        "localization_structure_passes",
    }
    for name in config["metrics_to_run"]:
        assert (EVAL / files[name]).is_file(), name
        assert config["thresholds"][name] > 0, name
