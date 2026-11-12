"""Tests for the gateway of direct model calls (app.llm, OPS-4)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app import llm

ROOT = Path(__file__).resolve().parents[2]

_DIRECT_CALL = re.compile(r"genai\.Client\(|\.generate_content(_async|_stream)?\(")
_AGENT_MODEL = re.compile(r"(?<!def )\b(model_for|Gemini)\(")


def _app_sources() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "app").rglob("*.py"))
    }


def test_models_are_called_from_the_agents_and_the_gateway_only() -> None:
    sources = _app_sources()

    direct = {name for name, text in sources.items() if _DIRECT_CALL.search(text)}
    assert direct == {"app/llm.py"}

    # Agents get their models in app/agent.py, from the one function in
    # app/models.py that builds them.
    with_models = {name for name, text in sources.items() if _AGENT_MODEL.search(text)}
    assert with_models == {"app/agent.py", "app/models.py"}

    # The web layer calls no platform: it does not import the module that does.
    for module in ("app/web_ui.py", "app/fast_api_app.py"):
        imported = set()
        for node in ast.walk(ast.parse(sources[module])):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported |= {f"{node.module}.{alias.name}" for alias in node.names}
                imported.add(node.module or "")
        assert "app.platforms" not in imported, module


def test_the_gateway_takes_its_model_and_its_retries_from_the_routing_table() -> None:
    from app import models

    assert models.MODEL_ROUTES["structure_source"] == models.FAST_MODEL
    assert models.model_for("localize").retry_options == models.RETRY_OPTIONS
    source = (ROOT / "app/llm.py").read_text(encoding="utf-8")
    assert "retry_options=RETRY_OPTIONS" in source
    assert "MODEL_ROUTES[task]" in source


@pytest.mark.asyncio
async def test_a_direct_model_call_is_logged_as_intent_and_outcome_with_sizes_only(
    monkeypatch,
) -> None:
    records: list[dict] = []
    monkeypatch.setattr(
        llm, "log_event", lambda message, **fields: records.append(fields)
    )
    secret_text = "본문은 로그에 남지 않습니다 " * 4

    async def answers(model: str, instruction: str, contents: str) -> str:
        return "the answer is not logged either"

    monkeypatch.setattr(llm, "_call_model", answers)
    await llm.generate_text("structure_source", "Give it structure.", secret_text)

    async def fails(model: str, instruction: str, contents: str) -> str:
        raise RuntimeError(f"upstream said no to: {secret_text}")

    monkeypatch.setattr(llm, "_call_model", fails)
    with pytest.raises(llm.ModelCallFailed) as raised:
        await llm.generate_text("structure_source", "Give it structure.", secret_text)
    assert secret_text not in str(raised.value)

    assert [r["event_phase"] for r in records] == ["INTENT", "OUTCOME"] * 2
    done, failed = records[:2], records[2:]
    assert done[0]["call_id"] == done[1]["call_id"] != failed[0]["call_id"]
    assert failed[0]["call_id"] == failed[1]["call_id"]
    assert done[0]["text_chars"] == len(secret_text)
    assert done[1]["status"] == "success" and done[1]["answer_chars"] == 31
    assert failed[1]["status"] == "error" and failed[1]["error_type"] == "RuntimeError"
    for record in records:
        assert record["task"] == "structure_source" and record["model"]
        assert "본문" not in repr(record) and "answer is not" not in repr(record)
