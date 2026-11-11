"""Redaction, secret lookup, input schemas and the policy plugin."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import secrets
from app.observability import _RedactionFilter
from app.plugins import ContentPolicyPlugin
from app.redaction import find_secrets, redact_credentials, redact_text, redact_value
from app.schemas import (
    DevtoDraftInput,
    FetchArticleInput,
    LoadDraftInput,
    QiitaDraftInput,
)

FAKE_KEY = "AIza" + "B" * 35


def test_pii_and_credentials_are_redacted_for_logs() -> None:
    text = f"mail kim@example.com, phone 010-1234-5678, card 4111 1111 1111 1111, key {FAKE_KEY}"
    redacted = redact_text(text)
    for leaked in ("kim@example.com", "010-1234-5678", "4111 1111 1111 1111", FAKE_KEY):
        assert leaked not in redacted
    assert (
        "[REDACTED_EMAIL_ADDRESS]" in redacted
        and "[REDACTED_GOOGLE_API_KEY]" in redacted
    )


def test_ordinary_prose_and_identifiers_are_left_alone() -> None:
    text = "token internationalization, commit 0a1b2c3d4e5f60718293, order 1234 5678"
    assert redact_text(text) == text
    assert find_secrets(text) == []


def test_article_output_keeps_pii_but_loses_credentials() -> None:
    text = f"Contact kim@example.com. api_key = '{FAKE_KEY}'"
    cleaned = redact_credentials(text)
    assert "kim@example.com" in cleaned and FAKE_KEY not in cleaned


def test_find_secrets_reports_type_and_line_without_the_value() -> None:
    findings = find_secrets(
        f"line one\nAuthorization: Bearer abc123def456ghi789jkl0\n{FAKE_KEY}"
    )
    assert {(f["type"], f["line"]) for f in findings} == {
        ("BEARER_TOKEN", 2),
        ("GOOGLE_API_KEY", 3),
    }
    assert FAKE_KEY not in json.dumps(findings)


def test_nested_values_are_redacted_and_long_strings_truncated() -> None:
    value = redact_value({"a": ["kim@example.com"], "body": "x" * 2000})
    assert value["a"] == ["[REDACTED_EMAIL_ADDRESS]"]
    assert value["body"].endswith("[2000 chars total]") and len(value["body"]) < 600


def test_log_records_are_scrubbed_before_emission() -> None:
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "user %s", ("kim@example.com",), None
    )
    record.json_fields = {"args": {"token": f"Bearer {'a1' * 12}"}}
    _RedactionFilter().filter(record)
    assert "kim@example.com" not in record.getMessage()
    assert "a1a1a1" not in json.dumps(record.json_fields)


def test_secret_from_environment_takes_precedence(monkeypatch) -> None:
    secrets.get_secret.cache_clear()
    monkeypatch.setenv("QIITA_ACCESS_TOKEN", "from-env")
    assert secrets.get_secret(secrets.QIITA_ACCESS_TOKEN) == "from-env"
    secrets.get_secret.cache_clear()


def test_missing_secret_raises_with_guidance(monkeypatch) -> None:
    secrets.get_secret.cache_clear()
    monkeypatch.delenv("DEVTO_API_KEY", raising=False)
    monkeypatch.delenv("BLOG_AGENT_LOCAL_SECRETS_FILE", raising=False)
    monkeypatch.setattr(secrets, "_from_secret_manager", lambda name: None)
    with pytest.raises(secrets.MissingSecretError, match="Secret Manager"):
        secrets.get_secret(secrets.DEVTO_API_KEY)
    secrets.get_secret.cache_clear()


def test_secret_from_local_file_outside_repo(monkeypatch, tmp_path) -> None:
    secrets.get_secret.cache_clear()
    file = tmp_path / "creds.env"
    file.write_text('DEVTO_API_KEY="from-file"\n')
    monkeypatch.delenv("DEVTO_API_KEY", raising=False)
    monkeypatch.setenv("BLOG_AGENT_LOCAL_SECRETS_FILE", str(file))
    monkeypatch.setattr(secrets, "_from_secret_manager", lambda name: None)
    assert secrets.get_secret(secrets.DEVTO_API_KEY) == "from-file"
    secrets.get_secret.cache_clear()


@pytest.mark.parametrize(
    "url",
    [
        "http://qiita.com/u/items/0123456789abcdef0123",
        "https://evil.example/qiita.com",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ],
)
def test_fetch_rejects_urls_outside_the_allowlist(url: str) -> None:
    with pytest.raises(ValidationError):
        FetchArticleInput(url=url)


def test_fetch_accepts_supported_hosts() -> None:
    for url in (
        "https://qiita.com/u/items/0123456789abcdef0123",
        "https://dev.to/user/slug-1a2b",
        "https://blog.naver.com/someone/223456789012",
    ):
        assert FetchArticleInput(url=url).url == url


def test_platform_tag_rules() -> None:
    assert DevtoDraftInput(title="t", tags=["AI", "python"]).tags == ["ai", "python"]
    with pytest.raises(ValidationError):
        DevtoDraftInput(title="t", tags=["machine-learning"])
    with pytest.raises(ValidationError):
        DevtoDraftInput(title="t", tags=["a", "b", "c", "d", "e"])
    with pytest.raises(ValidationError):
        QiitaDraftInput(title="t", tags=[])
    with pytest.raises(ValidationError):
        QiitaDraftInput(title="t", tags=["two words"])


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../outside.md", "notes.txt", "~/x.md"]
)
def test_draft_paths_must_stay_inside_the_workspace(path: str) -> None:
    with pytest.raises(ValidationError):
        LoadDraftInput(relative_path=path)


def test_policy_plugin_blocks_disallowed_urls_for_any_tool(tool_context) -> None:
    plugin = ContentPolicyPlugin()
    tool = SimpleNamespace(name="some_future_tool")
    blocked = asyncio.run(
        plugin.before_tool_callback(
            tool=tool,
            tool_args={"url": "https://internal.example/admin"},
            tool_context=tool_context,
        )
    )
    assert (
        blocked["error_code"] == "URL_NOT_ALLOWED" and blocked["recovery_instruction"]
    )
    allowed = asyncio.run(
        plugin.before_tool_callback(
            tool=tool,
            tool_args={"url": "https://dev.to/a/b", "title": "x"},
            tool_context=tool_context,
        )
    )
    assert allowed is None
