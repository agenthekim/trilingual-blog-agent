"""Deterministic metric: every localized language passed the structure check.

The localization pipeline ends by writing the result of the structure check
(``check_fidelity`` in ``app/markdown_checks.py``) for each target language
to the session state, under ``fidelity_report``. This metric reads the last
such report of the turn the agent played.

The score is 1 when the report names every target language of the session and
each of them passed, and 0 when a language failed or is missing. A case whose
``expected_trajectory`` lists ``fidelity_report`` among its ``state_keys``
expects a localization: with no report it scores 0. In any other case a turn
without a localization has nothing to check and scores 1. No model is
involved.
"""

REPORT_KEY = "fidelity_report"
TARGETS_KEY = "target_languages"


def _events(instance, *, played_only):
    turns = (instance.get("agent_data") or {}).get("turns") or []
    if not played_only:
        return [event for turn in turns for event in turn.get("events") or []]
    if not turns:
        return []
    events = turns[-1].get("events") or []
    last_user = max(
        (i for i, event in enumerate(events) if event.get("author") == "user"),
        default=-1,
    )
    return events[last_user + 1 :]


def _last_written(events, key):
    value = None
    for event in events:
        delta = event.get("state_delta") or {}
        if key in delta:
            value = delta[key]
    return value


def evaluate(instance):
    report = _last_written(_events(instance, played_only=True), REPORT_KEY)
    if not report:
        expected = instance.get("expected_trajectory") or {}
        if REPORT_KEY in (expected.get("state_keys") or []):
            return {
                "score": 0,
                "explanation": "a localization was expected and no structure "
                "check was reported",
            }
        return {"score": 1, "explanation": "no localization in this case"}
    # The languages the session localizes into, recorded history included.
    targets = _last_written(_events(instance, played_only=False), TARGETS_KEY) or []
    missing = sorted(language for language in targets if language not in report)
    failed = sorted(
        language
        for language, check in report.items()
        if not (isinstance(check, dict) and check.get("passed") is True)
    )
    if missing or failed:
        errors = {
            language: (report[language] or {}).get("errors")
            for language in failed
            if isinstance(report[language], dict)
        }
        return {
            "score": 0,
            "explanation": f"structure check failed for {failed}, "
            f"no result for {missing}; errors: {errors}",
        }
    return {
        "score": 1,
        "explanation": f"structure check passed for {sorted(report)}",
    }
