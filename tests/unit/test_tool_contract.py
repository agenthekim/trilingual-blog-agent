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


def test_every_tool_has_a_declared_output_model_with_described_fields() -> None:
    """OPS-5. A tool that is added without an output model fails here."""
    import app.schemas as schemas
    from app.schemas import TOOL_OUTPUT_MODELS, ToolOutput

    assert set(TOOL_OUTPUT_MODELS) == set(TOOLS)
    for name, model in TOOL_OUTPUT_MODELS.items():
        assert issubclass(model, ToolOutput), name
        assert model.model_config.get("extra") == "forbid", name
        assert "status" in model.model_fields, name
        undescribed = [
            field
            for field, info in model.model_fields.items()
            if field != "status" and not info.description
        ]
        assert not undescribed, f"{model.__name__}: no description on {undescribed}"
        # The schema the model stands for can be written out as JSON Schema.
        assert model.model_json_schema()["properties"]["status"], name

    # What a tool checks its arguments through says what each argument is.
    inputs = [
        model
        for model_name, model in vars(schemas).items()
        if model_name.endswith("Input") and inspect.isclass(model)
    ]
    assert len(inputs) >= 9
    for model in inputs:
        undescribed = [
            field for field, info in model.model_fields.items() if not info.description
        ]
        assert not undescribed, f"{model.__name__}: no description on {undescribed}"


def _fits(name: str, result: dict[str, Any], status: str = "success") -> None:
    """The result has the status and validates against the tool's output model."""
    from app.schemas import TOOL_OUTPUT_MODELS

    assert result["status"] == status, f"{name}: {result}"
    # Undeclared keys are refused by the model, so this is the whole result.
    TOOL_OUTPUT_MODELS[name].model_validate(result)


@pytest.mark.asyncio
async def test_a_success_result_validates_against_the_output_model_of_every_tool(
    finalized_project, monkeypatch
) -> None:
    """OPS-5. Every tool of the agent tree is driven to success once, with the
    stand-ins the other tests use for platforms and Google Slides, and what
    it returns is held against its declared output model."""
    from app import google_slides, platforms, state
    from app.settings import workspace_dir
    from tests.unit.conftest import SEED_KO, finalize

    context = finalized_project
    checked: set[str] = set()

    async def call(
        name: str, *arguments: Any, on: Any = None, approve: Any = False
    ) -> dict[str, Any]:
        """Call the tool; with ``approve`` it first has to wait for a person."""
        target = on or context
        target.tool_confirmation = None
        result = TOOLS[name](*arguments, tool_context=target)
        if inspect.isawaitable(result):
            result = await result
        if approve is not False:
            _fits(name, result, "pending_human_approval")
            target.decline()
            declined = TOOLS[name](*arguments, tool_context=target)
            if inspect.isawaitable(declined):
                declined = await declined
            _fits(name, declined, "declined")
            target.approve(approve)
            result = TOOLS[name](*arguments, tool_context=target)
            if inspect.isawaitable(result):
                result = await result
            target.tool_confirmation = None
        _fits(name, result)
        checked.add(name)
        return result

    # The seed and the posts.
    await call("finalize_seed_article")
    await call("read_article_post", "en")
    await call("set_article_post_title", "en", "A new title")
    edited = await call("replace_passage_in_article_post", "ko", "끝입니다.", "끝.")
    assert edited["naver_file"] in ("written", "removed")

    # Posting: sign-off, the Naver file, the two platforms, addresses.
    # None stands for a call from the page, where the click is the answer.
    context.function_call_id = None
    await call("finalize_localized_post", "en", approve=None)
    finalize(context, "ko", "ja")
    await call("export_naver_blog_paste_ready_html")

    async def qiita(**kwargs: Any) -> dict[str, Any]:
        return {"id": "abc", "url": "https://qiita.com/u/private/abc"}

    async def devto(**kwargs: Any) -> dict[str, Any]:
        return {"id": 42, "url": "https://dev.to/u/t-42"}

    async def page(url: str) -> tuple[int, str]:
        return 200, "A title"

    async def item(item_id: str) -> dict[str, Any]:
        return {"id": item_id, "title": "タイトル", "body": SEED_KO, "tags": []}

    async def own_article(article_id: str) -> dict[str, Any]:
        return {"id": article_id, "title": "A new title"}

    monkeypatch.setattr(platforms, "qiita_save_item", qiita)
    monkeypatch.setattr(platforms, "devto_save_article", devto)
    monkeypatch.setattr(platforms, "fetch_status_and_title", page)
    monkeypatch.setattr(platforms, "qiita_get_item", item)
    monkeypatch.setattr(platforms, "devto_find_own_article", own_article)
    await call("publish_qiita_article_draft", ["AI"], approve=None)
    await call("publish_devto_article_draft", ["ai"], approve=None)
    await call(
        "record_manually_posted_url", "naver_blog", "https://blog.naver.com/u/123"
    )
    verified = await call("verify_published_article_urls")
    assert len(verified["checks"]) == 3 and verified["all_reachable"]

    # Slides.
    plan = {
        "language": "en",
        "deck_title": "Deck",
        "audience": "developers",
        "opening_script": "Welcome.",
        "closing_script": "Thank you.",
        "slides": [
            {
                "number": 1,
                "title": "Slide 1",
                "bullets": ["first point", "second point"],
                "visual_note": "text only",
                "speaker_script": "Script for slide 1.",
            }
        ],
    }
    await call("set_presentation_brief", "en", "software engineers", 3)
    context.state[state.SLIDE_PLANS] = {"en": plan}
    timing = await call("check_slide_script_timing", "en")
    assert timing["passed"] is False and timing["recovery_instruction"]
    await call("export_slide_plan_markdown", "en")

    async def create(plan: Any, allowed_images: Any, *tables: Any) -> dict[str, Any]:
        return {"presentation_id": "deck-en", "url": google_slides.deck_url("deck-en")}

    monkeypatch.setattr(google_slides, "create_deck", create)
    monkeypatch.setattr(google_slides, "granted_account", lambda: "owner@example.com")
    await call("create_google_slides_deck_with_speaker_notes", "en", approve=None)
    on_the_page = FakeToolContext({**context.state, state.SURFACE: state.SURFACE_WEB})
    await call(
        "request_approval_to_create_slide_decks",
        ["en", "ko"],
        0,
        "",
        on=on_the_page,
        approve=None,
    )
    await call("compile_artifact_manifest_markdown")

    # Bringing an article in, last: each of these starts another article.
    draft = workspace_dir() / "drafts" / "a-draft.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(SEED_KO, encoding="utf-8")
    await call("load_markdown_draft_file", "drafts/a-draft.md", "ko")
    await call("save_seed_article_revision", "제목", SEED_KO, "ko", "none", "", False)
    await call(
        "fetch_published_article_markdown",
        "https://qiita.com/someone/items/0123456789abcdef0123",
    )

    assert checked == set(TOOLS), sorted(set(TOOLS) - checked)
