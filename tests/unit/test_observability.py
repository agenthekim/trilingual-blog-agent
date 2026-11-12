"""OPS-1: one action a person starts is one trace, and its log records say so.

The agent is a real ADK ``Runner`` with the observability plugin; only the
model is replaced by one that says what was scripted. The spans are those the
session's in-memory collection holds (``collected_spans`` in conftest.py):
nothing is exported, and no test sets a tracer provider of its own.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.sessions import InMemorySessionService
from google.cloud.logging_v2.handlers import StructuredLogHandler
from opentelemetry import trace
from pydantic import Field

from app import google_slides, llm, observability, runs, web_ui
from app.observability import ObservabilityPlugin, _RedactionFilter, traced_span
from app.pipeline import ArticlePipeline
from tests.unit.test_approvals import ScriptedModel, _calls
from tests.unit.test_runs import _until

RUN_ID = "c" * 32
ROOT = Path(__file__).resolve().parents[2]


async def look_something_up(topic: str) -> dict[str, Any]:
    """Stands for a tool that calls a platform.

    Args:
        topic: What to look up.

    Returns:
        A dict with ``status``.
    """
    with traced_span("platform.stand_in", topic=topic):
        return {"status": "success"}


def note_a_decision(language: str, tool_context: Any) -> dict[str, Any]:
    """A person's action that needs no model."""
    tool_context.state["decision"] = language
    return {"status": "success"}


def _job() -> runs.Job:
    agent = Agent(
        name="trilingual_blog_agent",
        model=ScriptedModel(
            model="scripted",
            script=[_calls(("look_something_up", {"topic": "tracing"}))],
        ),
        instruction="Do what the user asks for.",
        tools=[look_something_up],
    )
    pipeline = ArticlePipeline(
        log=lambda _: None,
        session_service=InMemorySessionService(),
        agent_app=App(name="app", root_agent=agent, plugins=[ObservabilityPlugin()]),
    )
    return runs.Job(id=RUN_ID, pipeline=pipeline)


async def _one_action(job: runs.Job, monkeypatch) -> None:
    """An action with everything under it: an agent turn with a tool call that
    reaches a platform, a tool call for the person, and a direct model call."""

    async def answer(model: str, instruction: str, contents: str) -> str:
        return "an answer"

    monkeypatch.setattr(llm, "_call_model", answer)

    async def work() -> None:
        await job.pipeline.say("Look it up.")
        await job.pipeline.act(note_a_decision, "en")
        await llm.generate_text("structure_source", "Give it headings.", "some text")

    runs.start(job, runs.MAIN, work, "message")
    await _until(lambda: not job.busy)
    assert job.error is None and job.notice is None


def _by_name(spans: list[Any]) -> dict[str, Any]:
    return {span.name: span for span in spans}


@pytest.mark.asyncio
async def test_an_action_and_what_runs_under_it_share_one_trace(
    isolated_workspace, monkeypatch, collected_spans
) -> None:
    collected_spans.clear()
    job = _job()
    await _one_action(job, monkeypatch)

    spans = collected_spans.get_finished_spans()
    named = _by_name(spans)
    root = named["run.main.message"]
    assert root.parent is None
    assert root.attributes["run_id"] == RUN_ID and root.attributes["lane"] == "main"
    assert root.resource is not None

    # Everything the action did is in its trace: the agent's own spans, the
    # platform call its tool made, and the direct model call.
    trace_id = root.context.trace_id
    assert {span.context.trace_id for span in spans} == {trace_id}
    assert all(span.parent is not None for span in spans if span is not root)
    assert {"platform.stand_in", "model.generate_text"} <= set(named)
    from_adk = set(named) - {
        "run.main.message",
        "platform.stand_in",
        "model.generate_text",
    }
    assert any("look_something_up" in name for name in from_adk), from_adk
    # The platform call hangs under the agent's tool span, not beside it.
    by_id = {span.context.span_id: span for span in spans}
    above = by_id[named["platform.stand_in"].parent.span_id]
    assert "look_something_up" in above.name

    # Another action is another trace, also when it is started from inside
    # an action, as the slides of a run are started by its posts.
    collected_spans.clear()

    async def slides() -> None:
        with traced_span("slides.stand_in"):
            return None

    async def posts() -> None:
        runs.start(job, runs.SLIDES, slides, "run_slides")

    runs.start(job, runs.MAIN, posts, "run_posts")
    await _until(lambda: not job.busy)
    named = _by_name(collected_spans.get_finished_spans())
    first, second = named["run.main.run_posts"], named["run.slides.run_slides"]
    assert second.parent is None
    assert second.context.trace_id not in (trace_id, first.context.trace_id)
    assert named["slides.stand_in"].context.trace_id == second.context.trace_id
    assert second.attributes["lane"] == "slides"

    # An action whose result the caller waits for has its root span too.
    collected_spans.clear()
    with runs.claim(job, runs.SLIDES, "sync_deck"), traced_span("slides.stand_in"):
        pass
    named = _by_name(collected_spans.get_finished_spans())
    claimed = named["run.slides.sync_deck"]
    assert claimed.parent is None and claimed.attributes["run_id"] == RUN_ID
    assert named["slides.stand_in"].parent.span_id == claimed.context.span_id


@pytest.mark.asyncio
async def test_log_records_carry_the_trace_of_the_action(
    isolated_workspace, monkeypatch, collected_spans
) -> None:
    stream = io.StringIO()
    handler = StructuredLogHandler(stream=stream)
    handler.addFilter(_RedactionFilter())
    logger = observability.get_logger()
    logger.addHandler(handler)
    collected_spans.clear()
    try:
        await _one_action(_job(), monkeypatch)
    finally:
        logger.removeHandler(handler)

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    root = _by_name(collected_spans.get_finished_spans())["run.main.message"]
    trace_id = trace.format_trace_id(root.context.trace_id)
    assert len(records) >= 6
    for record in records:
        # No record of the action is without the action's trace.
        assert record["logging.googleapis.com/trace"].endswith(trace_id), record
        assert record["logging.googleapis.com/spanId"], record

    # Every intent has its outcome, joined by an id nothing has changed: the
    # agent's tool call, the tool call for the person, the direct model call.
    def call_ids(phase: str) -> list[str]:
        return sorted(
            record.get("function_call_id") or record["call_id"]
            for record in records
            if record.get("event_phase") == phase
        )

    intents = call_ids("INTENT")
    assert len(intents) == 3 and len(set(intents)) == 3
    assert call_ids("OUTCOME") == intents
    assert not any("REDACTED" in call_id for call_id in intents)


@pytest.mark.asyncio
async def test_a_tool_that_raises_still_leaves_an_outcome_for_its_intent(
    isolated_workspace,
) -> None:
    stream = io.StringIO()
    handler = StructuredLogHandler(stream=stream)
    logger = observability.get_logger()
    logger.addHandler(handler)

    def breaks(tool_context: Any) -> dict[str, Any]:
        raise RuntimeError("the disk is full")

    try:
        with pytest.raises(RuntimeError):
            await _job().pipeline.act(breaks)
    finally:
        logger.removeHandler(handler)
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    intent, outcome = records
    assert (intent["event_phase"], outcome["event_phase"]) == ("INTENT", "OUTCOME")
    assert intent["call_id"] == outcome["call_id"]
    assert outcome["status"] == "exception"
    assert outcome["error_code"] == "RuntimeError"


class _RecordingModel(ScriptedModel):
    """A scripted model that keeps the function responses it was given."""

    responses: list[dict[str, Any]] = Field(default_factory=list)

    async def generate_content_async(self, llm_request: Any, stream: bool = False):
        for content in llm_request.contents:
            for part in content.parts or []:
                if part.function_response:
                    self.responses.append(dict(part.function_response.response))
        async for response in super().generate_content_async(llm_request, stream):
            yield response


async def save_the_article(title: str) -> dict[str, Any]:
    """Stands for a tool with a bug nobody has found yet.

    Args:
        title: The title of the article.

    Returns:
        Nothing: it raises.
    """
    raise RuntimeError(f"cannot save {title}: token=hunter2-not-for-the-model")


@pytest.mark.asyncio
async def test_a_tool_that_raises_gives_the_model_a_guided_error(
    isolated_workspace,
) -> None:
    """OPS-5. Nothing propagates, the model reads what to do next, and the
    log has one intent and one outcome that says the call ended in an
    exception."""
    from app.agent import app as agent_app
    from app.agent import slide_planning_app
    from app.plugins import ToolFailurePlugin

    # Both applications answer an exception, after the log has recorded it.
    for application in (agent_app, slide_planning_app):
        kinds = [type(plugin) for plugin in application.plugins]
        assert kinds.index(ObservabilityPlugin) < kinds.index(ToolFailurePlugin)
        assert kinds[-1] is ToolFailurePlugin

    model = _RecordingModel(
        model="scripted",
        script=[_calls(("save_the_article", {"title": "A private draft"}))],
    )
    agent = Agent(
        name="trilingual_blog_agent",
        model=model,
        instruction="Do what the user asks for.",
        tools=[save_the_article],
    )
    pipeline = ArticlePipeline(
        log=lambda _: None,
        session_service=InMemorySessionService(),
        agent_app=App(
            name="app",
            root_agent=agent,
            plugins=[type(plugin)() for plugin in agent_app.plugins],
        ),
    )
    stream = io.StringIO()
    handler = StructuredLogHandler(stream=stream)
    logger = observability.get_logger()
    logger.addHandler(handler)
    try:
        await pipeline.say("Save it.")  # must not raise
    finally:
        logger.removeHandler(handler)

    (response,) = model.responses
    assert response["status"] == "error"
    assert response["error_code"] == "TOOL_FAILED"
    assert "save_the_article" in response["message"]
    assert "RuntimeError" in response["message"]
    # The text of the exception is not passed on: it can carry anything.
    assert "hunter2" not in str(response) and "private draft" not in str(response)
    assert "Do not call the tool again" in response["recovery_instruction"]
    assert pipeline.last_reply == "Done."  # the model had its turn after it

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    intents = [r for r in records if r.get("event_phase") == "INTENT"]
    outcomes = [r for r in records if r.get("event_phase") == "OUTCOME"]
    assert len(intents) == 1 and len(outcomes) == 1
    assert intents[0]["function_call_id"] == outcomes[0]["function_call_id"]
    assert outcomes[0]["status"] == "exception"
    assert outcomes[0]["error_code"] == "RuntimeError"
    assert "hunter2" not in stream.getvalue()


def test_spans_leave_the_process_only_when_export_is_asked_for(monkeypatch) -> None:
    import opentelemetry.exporter.cloud_trace as cloud_trace
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    made: list[Any] = []

    class StandInExporter(SpanExporter):
        def __init__(self, project_id: str | None = None) -> None:
            self.project_id, self.exported = project_id, []
            made.append(self)

        def export(self, spans: Any) -> SpanExportResult:
            self.exported.extend(spans)
            return SpanExportResult.SUCCESS

    monkeypatch.setattr(cloud_trace, "CloudTraceSpanExporter", StandInExporter)

    def one_span() -> None:
        provider = observability._tracer_provider()
        assert provider.resource.attributes["service.name"] == observability.SERVICE
        with provider.get_tracer("test").start_as_current_span("an action"):
            pass
        provider.shutdown()  # flushes what waits to be exported

    # Not asked for: ids are made, and no exporter exists at all.
    monkeypatch.delenv("BLOG_AGENT_TRACE_EXPORT", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "a-project")
    one_span()
    assert made == []

    # Asked for without a project: said in the log, and nothing is exported.
    monkeypatch.setenv("BLOG_AGENT_TRACE_EXPORT", "cloud")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT")
    one_span()
    assert made == []

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "a-project")
    one_span()
    (exporter,) = made
    assert exporter.project_id == "a-project"
    assert [span.name for span in exporter.exported] == ["an action"]


def test_the_page_and_the_script_set_up_tracing_and_outbound_calls_have_spans() -> None:
    from scripts import run_pipeline

    assert "setup_tracing()" in inspect.getsource(web_ui.main)
    script = inspect.getsource(run_pipeline.main)
    assert "setup_tracing()" in script
    assert 'root_span("run.main.run_posts"' in script
    assert 'root_span("run.slides.run_slides"' in script
    for function, span in (
        (google_slides.create_deck, "slides.create_deck"),
        (google_slides.read_presentation, "slides.read_presentation"),
        (google_slides.update_presentation, "slides.update_presentation"),
        (web_ui._draw_diagram, "diagram.draw"),
        (llm.generate_text, "model.generate_text"),
    ):
        source = inspect.getsource(function)
        assert "with traced_span(" in source and f'"{span}"' in source, span


def test_a_logger_is_not_needed_to_trace() -> None:
    # The handler reads the ids from the current span; outside a span a
    # record has none, and that is not an error.
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", None, None)
    assert _RedactionFilter().filter(record) is True


def _print_calls(path: Path) -> list[tuple[str, int]]:
    """Every use of ``print`` in a file: the function it is in, and the line."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []

    def walk(node: ast.AST, inside: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inside = node.name
        if isinstance(node, ast.Name) and node.id == "print":
            found.append((inside, node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child, inside)

    walk(tree, "")
    return found


def test_the_application_never_logs_with_print() -> None:
    # Not called, and not handed on as a default either.
    for path in sorted((ROOT / "app").rglob("*.py")):
        assert _print_calls(path) == [], path.relative_to(ROOT).as_posix()

    # What a script writes to the terminal is its output, not a log, and it
    # leaves through one function per script.
    scripts = sorted((ROOT / "scripts").glob("*.py"))
    assert scripts
    for path in scripts:
        where = [function for function, _ in _print_calls(path)]
        assert where == ["say"], f"{path.name}: print is used in {where}"


def _logging_calls(path: Path) -> list[tuple[int, str]]:
    """Where a module makes a logger of its own or logs through the root one."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "logging"
            and function.attr[:1].islower()  # getLogger, warning, basicConfig, ...
        ):
            found.append((node.lineno, ast.unparse(node)))
        elif isinstance(function, ast.Name) and function.id == "getLogger":
            found.append((node.lineno, ast.unparse(node)))
    return found


@pytest.mark.asyncio
async def test_every_logger_of_the_application_writes_structured_json(
    isolated_workspace, monkeypatch
) -> None:
    """OPS-1. No module of ``app/`` has a logger of its own, and what the
    modules that had one write now is JSON that passed the redaction."""
    # The structured logger is made in one place. The one other logger is
    # the terminal line with the access token address (SEC-7), which must
    # not be in the structured log.
    allowed = {
        "app/observability.py": ["logging.getLogger(LOGGER_NAME)"],
        "app/web_ui.py": ["logging.getLogger('tba.terminal')"],
    }
    for path in sorted((ROOT / "app").rglob("*.py")):
        name = path.relative_to(ROOT).as_posix()
        calls = [call for _, call in _logging_calls(path)]
        assert calls == allowed.get(name, []), name

    stream = io.StringIO()
    handler = StructuredLogHandler(stream=stream)
    handler.addFilter(_RedactionFilter())
    logger = observability.get_logger()
    logger.addHandler(handler)

    from google.cloud import secretmanager

    from app import redaction, secrets, structurer

    class NoService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise PermissionError("no access for owner@example.com")

    class NoDlp:
        calls = 0

        def deidentify_content(self, request: Any) -> Any:
            NoDlp.calls += 1
            raise ConnectionError("unreachable")

    async def no_answer(*args: Any, **kwargs: Any) -> str:
        raise llm.ModelCallFailed("structure_source", "no answer")

    try:
        # app/secrets.py: a lookup that fails. The name stands where a
        # value could be written by mistake, so it shows the filter at work.
        monkeypatch.setenv("SECRET_MANAGER_PROJECT", "a-project")
        monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", NoService)
        assert secrets._from_secret_manager("owner@example.com") is None
        # app/structurer.py: the model gives no answer.
        monkeypatch.setattr(llm, "generate_text", no_answer)
        kept = await structurer.auto_structure_markdown("plain text")
        assert kept.outcome == "no_answer"
        # app/redaction.py: Cloud DLP fails. Its own record passes the
        # redaction too, and does not call the failed service again.
        monkeypatch.setenv("BLOG_AGENT_ENABLE_DLP", "true")
        monkeypatch.setenv("DLP_PROJECT_ID", "a-project")
        monkeypatch.setattr(redaction, "_dlp_client", NoDlp())
        assert redaction.redact_text("call 010-1234-5678") == (
            "call [REDACTED_PHONE_NUMBER]"
        )
        assert NoDlp.calls == 1
    finally:
        logger.removeHandler(handler)

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    by_phase = {record["event_phase"]: record for record in records}
    assert set(by_phase) == {"SECRET", "STRUCTURING", "REDACTION"}
    assert by_phase["SECRET"]["secret_name"] == "[REDACTED_EMAIL_ADDRESS]"
    assert by_phase["SECRET"]["error_type"] == "PermissionError"
    assert by_phase["STRUCTURING"]["severity"] == "WARNING"
    assert by_phase["STRUCTURING"]["reason"] == "the model call failed"
    assert by_phase["REDACTION"]["error_type"] == "ConnectionError"
    assert "owner@example.com" not in stream.getvalue()


def test_the_application_sends_no_trace_context_to_other_services() -> None:
    """OPS-1. Spans are linked inside the process. Nothing in ``app/`` turns
    on an instrumentation of the HTTP client or writes a trace header, so the
    blog platforms and Google are not handed the ids of a trace."""
    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = [
            alias.name if isinstance(node, ast.Import) else node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        ]
        name = path.relative_to(ROOT).as_posix()
        assert not [
            module
            for module in imported
            if module.startswith(
                ("opentelemetry.instrumentation", "opentelemetry.propagate")
            )
        ], name
        calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "instrument" not in calls and "inject" not in calls, name
        strings = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        # Named in a docstring that says it is not sent; never set as a header.
        assert not [text for text in strings if text.strip().lower() == "traceparent"]


def test_activity_lines_are_also_structured_records(isolated_workspace, capsys) -> None:
    stream = io.StringIO()
    handler = StructuredLogHandler(stream=stream)
    handler.addFilter(_RedactionFilter())
    logger = observability.get_logger()
    logger.addHandler(handler)
    key = "AIza" + "B" * 35
    try:
        # The page: a line of the run is on the page, in the run's file, and
        # in the structured log with the run id.
        job = _job()
        job.pipeline.log = job.add_log
        job.add_log(">>> English post finalized by a person")
        job.pipeline.log(f"[writer] used the key {key}")
        job.add_log("[writer]\n" + "a long answer. " * 200)

        # A pipeline nobody gave a log function: the same record, and the
        # session stands for the run.
        alone = ArticlePipeline(
            session_service=InMemorySessionService(),
            agent_app=App(name="app", root_agent=Agent(name="a", model="scripted")),
            slide_app=App(name="slides", root_agent=Agent(name="b", model="scripted")),
            session_id="session-without-a-run",
        )
        alone.log(">>> planning the English slides")
    finally:
        logger.removeHandler(handler)

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [record["event_phase"] for record in records] == ["ACTIVITY"] * 4
    first, second, third, fourth = records
    assert first["line"] == ">>> English post finalized by a person"
    assert {record["run_id"] for record in (first, second, third)} == {RUN_ID}
    # Scrubbed like every record, and a long line is cut.
    assert key not in json.dumps(second) and "REDACTED" in second["line"]
    assert third["line"].endswith("chars total]") and len(third["line"]) < 600
    assert fourth["run_id"] == "session-without-a-run"
    assert fourth["line"] == ">>> planning the English slides"

    # The page still has every line, whole, and so has the run's file.
    assert job.log[0] == first["line"] and len(job.log) == 3
    assert len(job.log[2]) > 2000
    kept = (runs.jobs_dir() / f"{RUN_ID}.log").read_text(encoding="utf-8")
    assert len(kept.splitlines()) == 3 and key not in kept
    # Nothing was printed.
    assert capsys.readouterr().out == ""
