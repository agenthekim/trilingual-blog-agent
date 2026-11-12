"""The gateway for model calls that are not made by an agent (OPS-4).

Models are called from two places: the agents in ``app/agent.py`` and this
module. A direct call made here

* takes its model from the routing table in ``app.models`` by kind of task,
* retries transient errors the way the agents do (``RETRY_OPTIONS``),
* hands the text over as data, between markers with a random token made for
  the call (INV-7), with the instruction kept apart as the system instruction,
* is logged as an intent and an outcome that share a call id, with sizes only:
  neither the text nor the answer is written to a log.

``_call_model`` is the one function that reaches a model. The unit tests
refuse it (OPS-2); a test that needs an answer replaces it with a stand-in.
"""

from __future__ import annotations

import time
import uuid

from google import genai
from google.genai import types

from app.models import MODEL_ROUTES, RETRY_OPTIONS, TaskKind
from app.observability import log_event, traced_span
from app.prompts import MARKER_RULE, fenced


class ModelCallFailed(RuntimeError):
    """A direct model call did not return an answer."""


async def _call_model(model: str, instruction: str, contents: str) -> str:
    """Send one request to ``model`` and return the text of its answer."""
    client = genai.Client(http_options=types.HttpOptions(retry_options=RETRY_OPTIONS))
    response = await client.aio.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=instruction),
    )
    return response.text or ""


async def generate_text(task: TaskKind, instruction: str, text: str) -> str:
    """Ask the model for ``task`` to work on ``text`` as ``instruction`` says.

    ``instruction`` is the caller's own wording and must hold no article text.
    ``text`` is data: it is sent between markers it cannot close. Returns the
    answer, which may be empty. Raises ``ModelCallFailed`` when the call
    failed after its retries; the reason is in the log, not in the message.
    """
    model = MODEL_ROUTES[task]
    call_id = uuid.uuid4().hex[:12]
    log_event(
        "direct model call",
        event_phase="INTENT",
        call_id=call_id,
        task=task,
        model=model,
        instruction_chars=len(instruction),
        text_chars=len(text),
    )
    started = time.monotonic()
    try:
        with traced_span("model.generate_text", task=task, model=model):
            answer = await _call_model(
                model, f"{instruction}\n\n{MARKER_RULE}", fenced(text)
            )
    except Exception as exc:
        log_event(
            "direct model call",
            event_phase="OUTCOME",
            call_id=call_id,
            task=task,
            model=model,
            status="error",
            error_type=type(exc).__name__,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
        )
        raise ModelCallFailed(f"The model call for '{task}' failed.") from exc
    log_event(
        "direct model call",
        event_phase="OUTCOME",
        call_id=call_id,
        task=task,
        model=model,
        status="success",
        answer_chars=len(answer),
        latency_ms=round((time.monotonic() - started) * 1000, 1),
    )
    return answer
