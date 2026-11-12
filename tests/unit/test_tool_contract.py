"""OPS-5: what every function an agent can call as a tool has to keep to.

The tools are not listed here. They are read from the agent tree in
``app/agent.py``, so a tool that is added later is held to the same contract
without anyone remembering to add it to a test.
"""

from __future__ import annotations

import inspect
import re
import typing
from collections.abc import Callable
from typing import Any

import pytest
from google.adk.tools.function_tool import FunctionTool

from app.schemas import CodeMode, LanguageCode, PlatformName
from tests.unit.conftest import FakeToolContext

# What an argument of this name must be declared as, so that the model is
# shown the values it may use.
ENUMERATED: dict[str, Any] = {
    "language": LanguageCode,
    "source_language": LanguageCode,
    "platform": PlatformName,
    "code_mode": CodeMode,
    "languages": list[LanguageCode],
}


def _agent_tools() -> dict[str, Callable[..., Any]]:
    """Every function tool of every agent in the tree, by name."""
    from app.agent import root_agent

    found: dict[str, Callable[..., Any]] = {}
    seen: set[int] = set()

    def walk(agent: Any) -> None:
        if id(agent) in seen:
            return
        seen.add(id(agent))
        for tool in getattr(agent, "tools", None) or []:
            function = getattr(tool, "func", None) or tool
            if inspect.isfunction(function):
                found[function.__name__] = function
        for child in getattr(agent, "sub_agents", None) or []:
            walk(child)

    walk(root_agent)
    return found


TOOLS = _agent_tools()


def _arguments(tool: Callable[..., Any]) -> dict[str, inspect.Parameter]:
    """The arguments the model fills in: everything but the tool context."""
    parameters = inspect.signature(tool).parameters
    return {name: p for name, p in parameters.items() if name != "tool_context"}


def _section(doc: str, heading: str) -> str:
    """The indented block under ``Args:`` or ``Returns:`` in a docstring."""
    match = re.search(rf"^{heading}:\n((?:[ \t]+\S.*\n?|\n)+)", doc, re.M)
    return match.group(1) if match else ""


def test_the_tools_are_read_from_the_agent_tree() -> None:
    # The coordinator's tools and those of an agent below it are both found.
    assert len(TOOLS) >= 18
    assert {"read_article_post", "save_seed_article_revision"} <= set(TOOLS)


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_tool_says_what_it_does_what_each_argument_is_and_what_it_returns(
    name: str,
) -> None:
    tool = TOOLS[name]
    # The name says what is done and to what: a verb and at least one object.
    assert re.fullmatch(r"[a-z]+(_[a-z0-9]+)+", name), name

    doc = inspect.getdoc(tool) or ""
    purpose = doc.split("\n\n", 1)[0].strip()
    assert len(purpose) >= 30 and purpose.endswith("."), f"{name}: purpose"

    described = _section(doc, "Args")
    for argument in _arguments(tool):
        assert re.search(rf"^\s+{argument}: \S", described, re.M), (
            f"{name}: the docstring does not say what '{argument}' is"
        )
    returns = _section(doc, "Returns")
    assert len(returns.strip()) >= 20, f"{name}: the docstring has no Returns"
    # What comes back when the tool cannot do it is part of the contract.
    assert "error_code" in returns and "recovery_instruction" in returns, name


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tools_take_typed_arguments_and_no_defaults(name: str) -> None:
    tool = TOOLS[name]
    hints = typing.get_type_hints(tool)
    parameters = inspect.signature(tool).parameters
    assert "tool_context" in parameters, name
    assert hints.get("return") == dict[str, Any], name

    with_defaults = []
    for argument, parameter in parameters.items():
        assert argument in hints, f"{name}: '{argument}' has no type"
        assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name
        if parameter.default is not inspect.Parameter.empty:
            with_defaults.append(argument)
        if argument in ENUMERATED:
            assert hints[argument] == ENUMERATED[argument], (
                f"{name}: '{argument}' must be declared as the enumeration"
            )
    assert not with_defaults, f"{name}: defaults on {with_defaults}"


def test_the_model_is_shown_the_allowed_languages_platforms_and_code_modes() -> None:
    shown: dict[str, set[str]] = {}
    for name, tool in TOOLS.items():
        declaration = FunctionTool(tool)._get_declaration()
        assert declaration is not None, name
        properties = (declaration.parameters_json_schema or {}).get("properties") or {}
        assert set(properties) == set(_arguments(tool)), name
        for argument, schema in properties.items():
            if argument in ENUMERATED:
                declared = ENUMERATED[argument]
                if typing.get_origin(declared) is list:  # a list of them
                    (declared,) = typing.get_args(declared)
                    schema = schema.get("items") or {}
                allowed = list(typing.get_args(declared))
                assert schema.get("enum") == allowed, f"{name}.{argument}"
                shown.setdefault(argument, set()).add(name)
    # Each of the three kinds is in use, so the check above checked something.
    assert shown["language"] and shown["platform"] and shown["code_mode"]
    assert shown["languages"]


def _wrong(argument: str, annotation: Any) -> Any:
    """A value of the right Python type that the tool must not accept."""
    if argument == "languages":
        return ["en", "fr"]
    if argument in ENUMERATED:
        return "fr"
    if annotation is str:
        return ""
    if annotation == list[str]:
        return ["not a tag!"] * 9
    if annotation is int:
        return -1
    if annotation is bool:
        return False
    raise AssertionError(f"no wrong value known for {argument}: {annotation}")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(TOOLS))
async def test_a_wrong_argument_comes_back_as_a_guided_error_from_every_tool(
    name: str,
) -> None:
    tool = TOOLS[name]
    hints = typing.get_type_hints(tool)
    arguments = {arg: _wrong(arg, hints[arg]) for arg in _arguments(tool)}
    context = FakeToolContext()  # an empty session: nothing to work on either

    result = tool(**arguments, tool_context=context)  # must not raise
    if inspect.isawaitable(result):
        result = await result

    assert isinstance(result, dict), name
    assert result["status"] == "error", f"{name}: {result}"
    assert result["error_code"] and result["message"], name
    assert len(result["recovery_instruction"]) >= 10, name
    enumerated = [arg for arg in arguments if arg in ENUMERATED]
    if enumerated:
        # Checked by the input model, before anything else: the error names
        # the argument, and nothing was written or asked of a person.
        assert result["error_code"] == "INVALID_ARGUMENTS", f"{name}: {result}"
        assert any(arg in result["message"] for arg in enumerated), result
        assert context.state == {} and context.confirmation_requests == [], name
