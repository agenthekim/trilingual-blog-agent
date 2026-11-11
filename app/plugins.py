"""Policy guardrails applied to every agent through one ADK plugin.

A plugin registered on the ``App`` wraps the coordinator and every sub-agent,
so these rules cannot be skipped by adding a new agent that forgets a callback.

Rules enforced:

1. Outbound URL allowlist. Any tool argument that carries a URL must point at
   one of the supported blog platforms over https. This holds even if a tool's
   own validation is changed or a new tool is added.
2. No credentials in model output. Credential-looking strings are removed from
   model responses before they are shown, logged, or stored in the session.

Quality self-evaluation (does the translation preserve the source?) is handled
separately by the localization review loop in ``app/agent.py``.
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
