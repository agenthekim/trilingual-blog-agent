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


CARD = "4111111111111111"  # passes the checksum of a card number


def test_identifiers_are_not_taken_for_card_numbers() -> None:
    # Digits attached to a letter or an underscore, directly or through one
    # hyphen, are part of an identifier, wherever they are written.
    for identifier in (
        f"call_{CARD}",
        f"adk-{CARD}",
        f"fc{CARD}",
        f"{CARD}_retry",
        f"{CARD}-b",
        "0b1c2d3e-4111-1111-1111-111141111111",
        "12345678-4111-4111-8111-1111abcdef12",
    ):
        text = f"outcome of {identifier}: done"
        assert redact_text(text) == text, identifier

    # A field that holds an identifier is written as it is, whatever it
    # looks like, so an intent can be joined to its outcome.
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "intent", None, None)
    identifiers = {
        "function_call_id": CARD,
        "invocation_id": "e-010-1234-5678",
        "call_id": "123-45-6789",
        "run_id": "4111 1111 1111 1111",
    }
    record.json_fields = {
        **identifiers,
        "intended_arguments": {"call_id": f"card {CARD}", "note": f"card {CARD}"},
        "trace_id": f"{CARD} " * 40,
    }
    _RedactionFilter().filter(record)
    for key, value in identifiers.items():
        assert record.json_fields[key] == value
    # Only the fields of the record itself, and only a value short enough
    # to be an identifier: everything else is text and is scrubbed.
    assert CARD not in json.dumps(record.json_fields["intended_arguments"])
    assert CARD not in record.json_fields["trace_id"]


def test_a_card_number_in_a_message_is_still_redacted() -> None:
    for written in (
        CARD,
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
    ):
        for text in (
            f"card {written}",
            f"card:{written}.",
            f"({written})",
            f"카드 번호는 {written}입니다",
            f"{written}로 결제했습니다",
        ):
            redacted = redact_text(text)
            assert written not in redacted, text
            assert "[REDACTED_CREDIT_CARD]" in redacted, text

    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "paid with %s", (CARD,), None
    )
    record.json_fields = {"note": f"card {CARD}", "function_call_id": "call_1"}
    _RedactionFilter().filter(record)
    assert CARD not in record.getMessage()
    assert CARD not in json.dumps(record.json_fields)
    # A number that fails the checksum was never a card number.
    assert redact_text("order 4111111111111112") == "order 4111111111111112"


def test_cloud_dlp_is_called_only_when_enabled_and_a_failure_keeps_the_regex_result(
    monkeypatch,
) -> None:
    from app import redaction

    class StandInDlp:
        def __init__(self, fails: bool = False) -> None:
            self.fails, self.requests = fails, []

        def deidentify_content(self, request: dict) -> SimpleNamespace:
            self.requests.append(request)
            if self.fails:
                raise PermissionError("the API is not enabled")
            value = request["item"]["value"].replace("Kim Minsu", "[PERSON_NAME]")
            return SimpleNamespace(item=SimpleNamespace(value=value))

    text = "Kim Minsu wrote from kim@example.com"
    by_regex = "Kim Minsu wrote from [REDACTED_EMAIL_ADDRESS]"
    client = StandInDlp()
    monkeypatch.setattr(redaction, "_dlp_client", client)
    monkeypatch.setenv("DLP_PROJECT_ID", "a-project")

    # Not enabled: the regular expressions alone, and no call.
    monkeypatch.delenv("BLOG_AGENT_ENABLE_DLP", raising=False)
    assert redact_text(text) == by_regex
    assert client.requests == []

    # Enabled: called once, with what the regular expressions left.
    monkeypatch.setenv("BLOG_AGENT_ENABLE_DLP", "true")
    assert redact_text(text) == "[PERSON_NAME] wrote from [REDACTED_EMAIL_ADDRESS]"
    (request,) = client.requests
    assert request["parent"] == "projects/a-project/locations/global"
    assert request["item"]["value"] == by_regex

    # Enabled without a project: nothing to call.
    monkeypatch.delenv("DLP_PROJECT_ID")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    assert redact_text(text) == by_regex
    assert len(client.requests) == 1

    # A call that fails keeps the result of the regular expressions.
    monkeypatch.setenv("DLP_PROJECT_ID", "a-project")
    failing = StandInDlp(fails=True)
    monkeypatch.setattr(redaction, "_dlp_client", failing)
    assert redact_text(text) == by_regex
    assert len(failing.requests) == 1


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


def test_activity_lines_are_redacted_before_they_are_stored(
    isolated_workspace, monkeypatch
) -> None:
    """SEC-4. What a run keeps of its activity, on the page and in its file,
    has passed the redaction of credentials and of personal data."""
    from google.adk.sessions import InMemorySessionService

    from app import redaction, runs
    from app.pipeline import ArticlePipeline

    # Cloud DLP is enabled, and must not be asked for a line of the page.
    asked: list[str] = []
    monkeypatch.setenv("BLOG_AGENT_ENABLE_DLP", "true")
    monkeypatch.setenv("DLP_PROJECT_ID", "a-project")
    monkeypatch.setattr(
        redaction, "_redact_with_cloud_dlp", lambda text: asked.append(text) or text
    )
    monkeypatch.setattr(runs, "log_activity", lambda line, run_id: None)

    job = runs.Job(
        id="d" * 32,
        pipeline=ArticlePipeline(
            log=lambda _: None, session_service=InMemorySessionService()
        ),
    )
    key = "AIza" + "B" * 35
    job.add_log(f"[writer] mail kim@example.com or call 010-1234-5678, key {key}")
    job.add_log(f"[writer] the card {CARD} and the number 900101-1234567")
    # What a person has to read stays as it is: titles, files, addresses of
    # posts and decks, also when a long number is part of the address.
    readable = [
        ">>> English post finalized by a person: 'ADK 2.0 in 15 minutes'",
        "Saved workspace/adk-memory-0a1b2c/post.ko.md (12034 characters)",
        "Naver Blog: https://blog.naver.com/someone/223456789012",
        "Qiita: https://qiita.com/someone/items/0123456789abcdef0123",
        "DEV: https://dev.to/someone/adk-in-2026-11-12-4k2a",
        f"Deck: https://docs.google.com/presentation/d/1Ab-{CARD}_xY/edit",
        f"Opened https://example.com/posts/{CARD}?t=1731436800000 (200)",
        "Planned 15 slides in 2026-11-12 18:43:33, 3 of 3 languages done",
    ]
    for line in readable:
        job.add_log(line)

    stored = [
        json.loads(line)
        for line in (runs.jobs_dir() / f"{job.id}.log")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert stored == job.log  # the page shows what the file holds
    first, second, *rest = stored
    assert first == (
        "[writer] mail [REDACTED_EMAIL_ADDRESS] or call [REDACTED_PHONE_NUMBER], "
        "key [REDACTED_GOOGLE_API_KEY]"
    )
    assert second == (
        "[writer] the card [REDACTED_CREDIT_CARD] and the number "
        "[REDACTED_KR_RESIDENT_NUMBER]"
    )
    assert rest == readable
    assert asked == []

    # Inside an address a credential and an e-mail address are still replaced.
    assert (
        redaction.redact_stored_line(
            f"https://example.com/?to=kim@example.com&access_token={'a1' * 12}"
        )
        == "https://example.com/?to=[REDACTED_EMAIL_ADDRESS]&[REDACTED_ASSIGNED_SECRET]"
    )
