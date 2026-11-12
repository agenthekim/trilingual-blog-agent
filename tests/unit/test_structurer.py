"""Tests for automatic Markdown structuring (app.structurer)."""

from __future__ import annotations

import pytest

from app.structurer import auto_structure_markdown, needs_markdown_structuring


def test_needs_markdown_structuring_detects_headings_correctly() -> None:
    # Already formatted Markdown does not need structuring
    formatted = "# Title\n\n## Overview\n\nSome paragraph text that goes on for a bit.\n\n- item 1\n- item 2"
    assert not needs_markdown_structuring(formatted)

    formatted_h2_only = "Some intro text without h1\n\n## Section\n\nContent here with enough characters to count as text."
    assert not needs_markdown_structuring(formatted_h2_only)

    # Short text (< 100 chars) is ignored
    short_text = "Short text"
    assert not needs_markdown_structuring(short_text)

    # Long text without headings needs structuring
    plain_text = (
        "Building Managed Lustre Storage on Google Cloud\n\n"
        "1. Overview\n\n"
        "1-1 What Is Managed Lustre?\n\n"
        "Google Cloud Managed Lustre is a fully managed parallel file system engineered for massive HPC workloads.\n\n"
        "Local SSD: low latency storage\n"
        "Managed Lustre: parallel storage\n"
    ) * 2
    assert needs_markdown_structuring(plain_text)

    # html_best_effort always triggers structuring
    assert needs_markdown_structuring("# Heading", extraction="html_best_effort")


@pytest.mark.asyncio
async def test_auto_structure_markdown_empty_or_fallback() -> None:
    assert await auto_structure_markdown("") == ""
    assert await auto_structure_markdown("   ") == "   "


@pytest.mark.asyncio
async def test_auto_structure_markdown_graceful_fallback_on_error(monkeypatch) -> None:
    from google import genai

    class FailingClient:
        class aio:
            class models:
                @staticmethod
                async def generate_content(*args, **kwargs):
                    raise RuntimeError("API quota exceeded or network down")

    monkeypatch.setattr(genai, "Client", FailingClient)

    raw_text = "Some plain text " * 20
    result = await auto_structure_markdown(raw_text)
    # On error, returns original text unchanged without crashing
    assert result == raw_text
