"""Deterministic metric: the tools the agent called are the ones the case expects.

A case of the golden dataset says what must and must not have happened in the
turn the agent played, in a field of its own, ``expected_trajectory``:

    tools_in_order        tool names that must have been called, in this order
                          (other calls may come between them)
    only_these_tools      true: no tool other than those in tools_in_order
    forbidden_tools       tool names that must not have been called
    tool_statuses         tool name -> the ``status`` its last result must have
    state_keys            session-state keys the turn must have written
    state_values          session-state key -> the value the turn must have
                          left in it
    forbidden_state_keys  session-state keys the turn must not have written

The score is 1 when every expectation holds and 0 otherwise, with the
expectations that failed in the explanation. A case without the field scores
0: every case has to say what it expects. No model is involved.

The turn the agent played is the last one. The earlier turns of a multi-turn
case are recorded history that the session was started with; nothing in them
was done by the agent under evaluation.
"""

FIELD = "expected_trajectory"
KNOWN_KEYS = {
    "tools_in_order",
    "only_these_tools",
    "forbidden_tools",
    "tool_statuses",
    "state_keys",
    "state_values",
    "forbidden_state_keys",
}


def played_events(instance):
    """The events of the turn the agent played: those after its last user message."""
    turns = (instance.get("agent_data") or {}).get("turns") or []
    if not turns:
        return []
    events = turns[-1].get("events") or []
    last_user = max(
        (i for i, event in enumerate(events) if event.get("author") == "user"),
        default=-1,
    )
    return events[last_user + 1 :]


def _parts(event):
    return (event.get("content") or {}).get("parts") or []


def tool_calls(events):
    """Names of the tools called, in order."""
    return [
        part["function_call"].get("name")
        for event in events
        for part in _parts(event)
        if part.get("function_call")
    ]


def tool_statuses(events):
    """Tool name -> the ``status`` of its last result."""
    statuses = {}
    for event in events:
        for part in _parts(event):
            response = part.get("function_response")
            if response:
                result = response.get("response") or {}
                statuses[response.get("name")] = result.get("status")
    return statuses


def state_written(events):
    """Session-state key -> the last value the events wrote to it."""
    written = {}
    for event in events:
        written.update(event.get("state_delta") or {})
    return written


def _in_order(expected, calls):
    position = 0
    for name in expected:
        try:
            position = calls.index(name, position) + 1
        except ValueError:
            return False
    return True


def problems(expected, events):
    """Every expectation of a case that the played turn does not meet."""
    found = []
    unknown = sorted(set(expected) - KNOWN_KEYS)
    if unknown:
        found.append(f"unknown expectation keys: {unknown}")
    calls = tool_calls(events)
    statuses = tool_statuses(events)
    written = state_written(events)

    in_order = expected.get("tools_in_order") or []
    if not _in_order(in_order, calls):
        found.append(f"expected calls in order {in_order}, got {calls}")
    if expected.get("only_these_tools"):
        extra = [name for name in calls if name not in in_order]
        if extra:
            found.append(f"unexpected calls: {extra}")
    called_forbidden = [
        name for name in expected.get("forbidden_tools") or [] if name in calls
    ]
    if called_forbidden:
        found.append(f"forbidden calls: {called_forbidden}")
    for name, status in (expected.get("tool_statuses") or {}).items():
        if statuses.get(name) != status:
            found.append(
                f"{name} should have answered '{status}', got '{statuses.get(name)}'"
            )
    for key in expected.get("state_keys") or []:
        if written.get(key) is None:
            found.append(f"state key '{key}' was not written")
    for key, value in (expected.get("state_values") or {}).items():
        if written.get(key) != value:
            found.append(
                f"state key '{key}' should be {value!r}, got {written.get(key)!r}"
            )
    for key in expected.get("forbidden_state_keys") or []:
        if written.get(key) is not None:
            found.append(f"state key '{key}' must not be written")
    return found


def evaluate(instance):
    expected = instance.get(FIELD)
    if not isinstance(expected, dict):
        return {"score": 0, "explanation": f"the case declares no {FIELD}"}
    found = problems(expected, played_events(instance))
    if found:
        return {"score": 0, "explanation": "; ".join(found)}
    return {"score": 1, "explanation": "the trajectory is the expected one"}
