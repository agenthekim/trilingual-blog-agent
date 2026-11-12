"""Policy guardrails applied to every agent through one ADK plugin.

A plugin registered on the ``App`` wraps the coordinator and every sub-agent,
so these rules cannot be skipped by adding a new agent that forgets a callback.

Rules enforced:

1. Outbound URL allowlist. Any tool argument that carries a URL must point at
   one of the supported blog platforms over https. This holds even if a tool's
   own validation is changed or a new tool is added.
2. No credentials in what the coordinator says. Credential-looking strings are
   removed from model responses before they are shown, logged, or stored in
   the session. Agents that write article text, or documents derived from it,
   are exempt: their output is the author's article in another form, and a
   pattern that also matches ordinary tutorial code (``API_KEY=...`` in a shell
   example) would silently damage it. Credentials in article text are searched
   for, and shown to the person, when a post is finalized or sent.

Quality self-evaluation (does the translation preserve the source?) is handled
separately by the localization review loop in ``app/agent.py``.

``ToolFailurePlugin`` is the last resort of guided error handling (OPS-5): a
tool that raises something nobody expected still answers the model with an
``error_code`` and a ``recovery_instruction``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app.observability import log_event
from app.redaction import find_secrets, redact_credentials
from app.schemas import tool_error
from app.settings import ALLOWED_ARTICLE_HOSTS

_URL_ARGUMENT_NAMES = ("url", "urls", "source_url")

# Agents whose model output is article text or a structured document built from
# it (titles, review verdicts quoting the text, slide plans). Matched by prefix
# so the per-language agents (localizer_ko_agent, ...) are covered.
_ARTICLE_WRITER_PREFIXES = (
    "localizer_",
    "refiner_",
    "title_localizer",
    "slide_plan",
    "localization_critic",
    "seed_editor",
)


def writes_article_text(agent_name: str | None) -> bool:
    """Whether this agent's output must be stored exactly as the model wrote it."""
    return bool(agent_name) and agent_name.startswith(_ARTICLE_WRITER_PREFIXES)


def _disallowed_urls(tool_args: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for name in _URL_ARGUMENT_NAMES:
        value = tool_args.get(name)
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(v for v in value if isinstance(v, str))
    blocked = []
    for url in candidates:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme != "https" or host not in ALLOWED_ARTICLE_HOSTS:
            blocked.append(url)
    return blocked


class ContentPolicyPlugin(BasePlugin):
    """URL allowlist for tool calls and credential scrubbing for model output."""

    def __init__(self) -> None:
        super().__init__(name="content_policy")

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        blocked = _disallowed_urls(tool_args)
        if not blocked:
            return None
        log_event(
            "policy: tool call blocked",
            event_phase="POLICY",
            rule="url_allowlist",
            tool=tool.name,
            blocked_count=len(blocked),
        )
        allowed = ", ".join(sorted(ALLOWED_ARTICLE_HOSTS))
        return tool_error(
            "URL_NOT_ALLOWED",
            f"Policy blocked {len(blocked)} URL(s) outside the allowed hosts.",
            f"Only https URLs on these hosts may be used: {allowed}. Ask the user "
            "for a supported URL; do not retry with the same one.",
        )

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        if writes_article_text(callback_context.agent_name):
            return None
        content = llm_response.content
        if not content or not content.parts:
            return None
        if not any(part.text and find_secrets(part.text) for part in content.parts):
            return None
        log_event(
            "policy: credential removed from model output",
            event_phase="POLICY",
            rule="no_credentials_in_output",
            agent=callback_context.agent_name,
        )
        cleaned = llm_response.model_copy(deep=True)
        cleaned.content = types.Content(
            role=content.role,
            parts=[
                types.Part.from_text(text=redact_credentials(part.text))
                if part.text and not part.thought
                else part
                for part in content.parts
            ],
        )
        return cleaned


class ToolFailurePlugin(BasePlugin):
    """Turns an exception a tool raised into a guided error for the model.

    The tools return ``tool_error`` for every failure they know of. This is
    for the one they do not know of: a bug, or a library that raises something
    new. ADK asks the plugins in the order they are registered and takes the
    first answer, then treats that answer as the tool's result, so the model
    reads what to do next instead of the run ending in a traceback.

    Register it after ``ObservabilityPlugin``: a plugin that answers ends the
    round, and the outcome record of the exception has to be written first.
    """

    def __init__(self) -> None:
        super().__init__(name="tool_failure")

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        if not isinstance(error, Exception):
            # A cancelled call is told to the plugins for cleanup only; it is
            # not a failure of the tool and nothing answers it.
            return None
        # The type of the exception only: its text could repeat article text
        # or a credential, and this result is stored in the session.
        return tool_error(
            "TOOL_FAILED",
            f"The tool {tool.name} failed unexpectedly ({type(error).__name__}).",
            "Do not call the tool again with the same arguments. Tell the user "
            "that this step failed and what they can do instead.",
        )
