"""The rules for finalizing and posting hold wherever a post could be sent from.

These tests call the tool functions the agent calls and the policy functions
they share with the web page and the script. No model, no network.
"""

from __future__ import annotations

import asyncio

import pytest

from app import platforms, policy, state
from app.persistence import get_article_store
from app.tools import articles, publish
from tests.unit.conftest import SEED_KO, FakeToolContext, finalize

FAKE_KEY = "AIza" + "B" * 35


def _saves(monkeypatch, name: str, answer: dict) -> list[dict]:
    calls: list[dict] = []

    async def fake_save(**kwargs):
        calls.append(kwargs)
        return answer

    monkeypatch.setattr(platforms, name, fake_save)
    return calls


def _never(monkeypatch, name: str) -> None:
    async def fail(**kwargs):
        raise AssertionError("nothing may be sent here")

    monkeypatch.setattr(platforms, name, fail)


# --- before anything can be sent -------------------------------------------


def test_posting_needs_a_finalized_seed_and_a_localization(
    tool_context: FakeToolContext,
) -> None:
    result = asyncio.run(publish.publish_devto_article_draft(["ai"], tool_context))
    assert result["error_code"] == "SEED_NOT_FINALIZED"
    articles.save_seed_article_revision(
        "제목", SEED_KO, "ko", "none", "", True, tool_context
    )
    result = asyncio.run(publish.publish_devto_article_draft(["ai"], tool_context))
    assert result["error_code"] == "LOCALIZATION_MISSING"


def test_a_post_nobody_finalized_is_refused_by_the_tool_itself(
    finalized_project, monkeypatch
) -> None:
    """Not only the web page: the tool the agent calls refuses, approved or not."""
    _never(monkeypatch, "qiita_save_item")
    result = asyncio.run(publish.publish_qiita_article_draft(["AI"], finalized_project))
    assert result["error_code"] == "NOT_FINALIZED"
    assert finalized_project.confirmation_requests == []

    finalized_project.approve({"visibility": "public"})
    result = asyncio.run(publish.publish_qiita_article_draft(["AI"], finalized_project))
    assert result["error_code"] == "NOT_FINALIZED"


def test_a_change_between_request_and_approval_is_caught_when_the_approval_arrives(
    finalized_project, monkeypatch
) -> None:
    _never(monkeypatch, "devto_save_article")
    finalize(finalized_project, "en")
    asked = asyncio.run(publish.publish_devto_article_draft(["ai"], finalized_project))
    assert asked["status"] == "pending_human_approval"

    # The agent, or a person, changes the text while the approval is open.
    finalized_project.state[state.localized_key("en")] += "\nOne more line.\n"
    finalized_project.approve()
    result = asyncio.run(publish.publish_devto_article_draft(["ai"], finalized_project))
    assert result["error_code"] == "NOT_FINALIZED"


def test_changing_the_title_undoes_the_finalization(finalized_project) -> None:
    finalized_project.state[state.LOCALIZED_TITLES] = {"en": "A title"}
    finalize(finalized_project, "en")
    assert policy.is_finalized(finalized_project.state, "en")
    finalized_project.state[state.LOCALIZED_TITLES] = {"en": "Another title"}
    assert not policy.is_finalized(finalized_project.state, "en")


def test_the_title_sent_is_the_finalized_one_and_the_model_has_no_say(
    finalized_project, monkeypatch
) -> None:
    import inspect

    for tool in (
        publish.publish_qiita_article_draft,
        publish.publish_devto_article_draft,
    ):
        assert list(inspect.signature(tool).parameters) == ["tags", "tool_context"]

    calls = _saves(
        monkeypatch, "devto_save_article", {"id": 1, "url": "https://dev.to/u/t"}
    )
    finalized_project.state[state.LOCALIZED_TITLES] = {"en": "The finalized title"}
    finalize(finalized_project, "en")
    finalized_project.approve()
    done = asyncio.run(publish.publish_devto_article_draft(["ai"], finalized_project))
    assert done["status"] == "success"
    assert calls[0]["title"] == "The finalized title"
    assert done["publication"]["title"] == "The finalized title"


def test_the_platform_an_article_was_read_from_is_never_written_to(
    tool_context, monkeypatch
) -> None:
    async def fake_item(item_id):
        return {"id": item_id, "title": "記事", "body": "本文です。" * 20, "tags": []}

    monkeypatch.setattr(platforms, "qiita_get_item", fake_item)
    _never(monkeypatch, "qiita_save_item")
    url = "https://qiita.com/user/items/0123456789abcdef0123"
    asyncio.run(articles.fetch_published_article_markdown(url, tool_context))

    assert policy.finalize_refusal(tool_context.state, "ja").code == (
        "ALREADY_PUBLISHED_SOURCE"
    )
    tool_context.approve({"visibility": "public"})
    result = asyncio.run(publish.publish_qiita_article_draft(["AI"], tool_context))
    assert result["error_code"] == "ALREADY_PUBLISHED_SOURCE"
    recorded = publish.record_manually_posted_url(
        "qiita", "https://qiita.com/user/items/ffff456789abcdef0123", tool_context
    )
    assert recorded["error_code"] == "ALREADY_PUBLISHED_SOURCE"
    assert tool_context.state[state.PUBLICATIONS]["qiita"]["url"] == url
    assert policy.next_step(tool_context.state, "ja") == "original"


def test_a_draft_written_in_japanese_can_be_finalized_and_posted_on_qiita(
    tool_context, monkeypatch
) -> None:
    """The source language is posted like the others unless it came from its platform."""
    seed = "# 見出し\n\n" + "これは日本語で書かれた下書きの本文です。" * 5 + "\n"
    articles.save_seed_article_revision(
        "見出し", seed, "ja", "none", "", True, tool_context
    )
    assert policy.finalize_refusal(tool_context.state, "ja") is None
    finalize(tool_context, "ja")
    calls = _saves(
        monkeypatch,
        "qiita_save_item",
        {"id": "abc", "url": "https://qiita.com/u/private/abc"},
    )
    tool_context.approve()
    done = asyncio.run(publish.publish_qiita_article_draft(["AI"], tool_context))
    assert done["status"] == "success" and calls[0]["title"] == "見出し"


# --- approval ---------------------------------------------------------------


def test_posting_pauses_for_human_approval_and_sends_nothing(
    finalized_project, monkeypatch
) -> None:
    _never(monkeypatch, "qiita_save_item")
    finalize(finalized_project, "ja")
    result = asyncio.run(publish.publish_qiita_article_draft(["AI"], finalized_project))
    assert result["status"] == "pending_human_approval"
    (request,) = finalized_project.confirmation_requests
    assert "Qiita" in request["hint"] and "tags: AI" in request["hint"]
    assert request["payload"]["platform"] == "qiita"
    assert request["payload"]["action"] == "post"
    assert "qiita" not in (finalized_project.state.get(state.PUBLICATIONS) or {})


def test_a_declined_approval_sends_nothing(finalized_project, monkeypatch) -> None:
    _never(monkeypatch, "devto_save_article")
    finalize(finalized_project, "en")
    finalized_project.decline()
    result = asyncio.run(publish.publish_devto_article_draft(["ai"], finalized_project))
    assert result["status"] == "declined"


def test_an_approved_post_creates_a_draft_and_the_next_one_updates_it(
    finalized_project, monkeypatch
) -> None:
    calls = _saves(
        monkeypatch,
        "devto_save_article",
        {"id": 42, "url": "https://dev.to/user/t-temp-slug-42"},
    )
    finalize(finalized_project, "en")
    finalized_project.approve()
    first = asyncio.run(publish.publish_devto_article_draft(["AI"], finalized_project))
    assert first["status"] == "success" and first["publication"]["state"] == "draft"
    assert calls[0]["published"] is False and calls[0]["article_id"] is None
    assert calls[0]["tags"] == ["ai"]
    asyncio.run(publish.publish_devto_article_draft(["ai"], finalized_project))
    assert (
        calls[1]["article_id"] == "42"
    )  # the second approval updates, never duplicates
    stored = get_article_store().list_publications(
        finalized_project.state[state.PROJECT_ID]
    )
    assert [p["platform"] for p in stored] == ["devto"]


def test_only_the_approving_person_can_make_a_post_public(
    finalized_project, monkeypatch
) -> None:
    calls = _saves(
        monkeypatch, "devto_save_article", {"id": 42, "url": "https://dev.to/user/t-42"}
    )
    finalize(finalized_project, "en")
    # An approval that asks for a public post: the choice rides on the approval.
    finalized_project.approve({"visibility": "public"})
    posted = asyncio.run(publish.publish_devto_article_draft(["ai"], finalized_project))
    assert calls[0]["published"] is True
    assert posted["publication"]["state"] == "published"
    assert "public" in posted["message"]

    # Once public it is never posted again, whatever the approval says.
    again = asyncio.run(publish.publish_devto_article_draft(["ai"], finalized_project))
    assert again["error_code"] == "ALREADY_PUBLISHED" and len(calls) == 1
    assert policy.finalize_refusal(finalized_project.state, "en").code == (
        "ALREADY_PUBLISHED"
    )


def test_an_approval_without_a_choice_means_a_private_draft(
    finalized_project, monkeypatch
) -> None:
    calls = _saves(
        monkeypatch,
        "qiita_save_item",
        {"id": "abc", "url": "https://qiita.com/u/private/abc"},
    )
    finalize(finalized_project, "ja")
    for payload in (None, {}, {"visibility": "anything else"}, "public"):
        finalized_project.approve(payload)
        saved = asyncio.run(
            publish.publish_qiita_article_draft(["AI"], finalized_project)
        )
        assert saved["publication"]["state"] == "draft"
    assert [call["private"] for call in calls] == [True] * 4


def test_a_platform_error_becomes_a_guided_tool_error(
    finalized_project, monkeypatch
) -> None:
    async def unauthorized(**kwargs):
        raise platforms.PlatformError("QIITA_UNAUTHORIZED", "rejected", "do not retry")

    monkeypatch.setattr(platforms, "qiita_save_item", unauthorized)
    finalize(finalized_project, "ja")
    finalized_project.approve()
    result = asyncio.run(publish.publish_qiita_article_draft(["AI"], finalized_project))
    assert (result["status"], result["error_code"]) == ("error", "QIITA_UNAUTHORIZED")
    assert result["recovery_instruction"] == "do not retry"


@pytest.mark.parametrize(
    ("tool", "language", "tags"),
    [
        (publish.publish_devto_article_draft, "en", ["a", "b", "c", "d", "e"]),
        (publish.publish_devto_article_draft, "en", ["has-hyphen"]),
        (publish.publish_qiita_article_draft, "ja", []),
        (publish.publish_qiita_article_draft, "ja", ["two words"]),
    ],
)
def test_tags_follow_each_platforms_own_rule(
    finalized_project, tool, language, tags
) -> None:
    finalize(finalized_project, language)
    result = asyncio.run(tool(tags, finalized_project))
    assert result["error_code"] == "INVALID_ARGUMENTS"
    assert finalized_project.confirmation_requests == []


# --- credential-looking values ---------------------------------------------


def test_text_with_a_credential_is_not_finalized_or_sent_unless_acknowledged(
    finalized_project, monkeypatch
) -> None:
    key = state.localized_key("ja")
    finalized_project.state[key] += f"\napi_key = '{FAKE_KEY}'\n"

    refusal = policy.finalize_refusal(finalized_project.state, "ja")
    assert refusal.code == "SECRET_IN_ARTICLE"
    assert "line" in refusal.message and FAKE_KEY not in str(refusal)
    assert policy.findings(finalized_project.state, "ja")[0]["type"]

    # Finalized without the acknowledgement (as old data might be): still refused.
    finalize(finalized_project, "ja")
    _never(monkeypatch, "qiita_save_item")
    finalized_project.approve()
    result = asyncio.run(publish.publish_qiita_article_draft(["AI"], finalized_project))
    assert result["error_code"] == "SECRET_IN_ARTICLE" and FAKE_KEY not in str(result)

    # A person who saw the findings says they are not real: then it may go.
    assert policy.finalize_refusal(finalized_project.state, "ja", True) is None
    marks = dict(finalized_project.state[state.FINALIZED_POSTS])
    marks["ja"] = policy.finalization(finalized_project.state, "ja", True)
    finalized_project.state[state.FINALIZED_POSTS] = marks
    calls = _saves(
        monkeypatch,
        "qiita_save_item",
        {"id": "abc", "url": "https://qiita.com/u/private/abc"},
    )
    result = asyncio.run(publish.publish_qiita_article_draft(["AI"], finalized_project))
    assert result["status"] == "success" and len(calls) == 1


# --- finalizing through the tool (chat and script) --------------------------


def test_the_finalize_tool_records_nothing_until_a_person_confirms(
    finalized_project,
) -> None:
    tool = publish.finalize_localized_post
    assert tool("fr", finalized_project)["error_code"] == "UNKNOWN_LANGUAGE"

    asked = tool("en", finalized_project)
    assert asked["status"] == "pending_human_approval"
    (request,) = finalized_project.confirmation_requests
    assert request["payload"] == {
        "action": "finalize",
        "language": "en",
        "title": "제목",
    }
    assert not policy.is_finalized(finalized_project.state, "en")

    finalized_project.decline()
    assert tool("en", finalized_project)["status"] == "declined"
    assert not policy.is_finalized(finalized_project.state, "en")

    finalized_project.approve()
    done = tool("en", finalized_project)
    assert done["status"] == "success" and done["finalized"] is True
    assert policy.is_finalized(finalized_project.state, "en")
    assert policy.next_step(finalized_project.state, "en") == "post"


def test_a_plain_yes_does_not_accept_credential_findings(finalized_project) -> None:
    tool = publish.finalize_localized_post
    finalized_project.state[state.localized_key("en")] += f"\ntoken: {FAKE_KEY}\n"
    asked = tool("en", finalized_project)
    assert asked["status"] == "pending_human_approval"
    assert "look like credentials" in finalized_project.confirmation_requests[0]["hint"]

    finalized_project.approve()  # yes, but nothing said about the findings
    assert tool("en", finalized_project)["error_code"] == "SECRET_IN_ARTICLE"
    assert not policy.is_finalized(finalized_project.state, "en")

    finalized_project.approve({"acknowledge_findings": True})
    assert tool("en", finalized_project)["status"] == "success"
    mark = finalized_project.state[state.FINALIZED_POSTS]["en"]
    assert mark["acknowledged_findings"] is True


# --- Naver: export and URL --------------------------------------------------


def test_the_naver_export_carries_nothing_that_can_run_code(finalized_project) -> None:
    hostile = (
        "\n\n<script>alert(1)</script>\n\n"
        '<img src="https://example.com/a.png" onerror="alert(2)">\n\n'
        '<a href="javascript:alert(3)">link</a> <a href="https://example.com">ok</a>\n\n'
        '<iframe src="https://example.com"></iframe>\n\n'
        "```html\n<script>shown as text</script>\n```\n"
    )
    finalized_project.state[state.SEED_MARKDOWN] += hostile
    result = publish.export_naver_blog_paste_ready_html(finalized_project)
    html = open(result["file"], encoding="utf-8").read()
    assert "<script" not in html and "onerror" not in html
    assert "javascript:" not in html and "<iframe" not in html
    assert "alert(1)" not in html
    assert 'src="https://example.com/a.png"' in html
    assert 'href="https://example.com"' in html
    assert "&lt;script&gt;shown as text&lt;/script&gt;" in html  # code stays readable
    assert '<pre style="' in html and '<h2 style="' in html


def test_exporting_again_never_forgets_a_registered_address(finalized_project) -> None:
    publish.export_naver_blog_paste_ready_html(finalized_project)
    url = "https://blog.naver.com/someone/223456789012"
    publish.record_manually_posted_url("naver_blog", url, finalized_project)
    again = publish.export_naver_blog_paste_ready_html(finalized_project)
    assert again["status"] == "success"
    record = finalized_project.state[state.PUBLICATIONS]["naver_blog"]
    assert record["state"] == "published" and record["url"] == url


def test_a_registered_address_must_be_on_the_platforms_own_site(
    finalized_project,
) -> None:
    record = publish.record_manually_posted_url
    elsewhere = record("naver_blog", "https://example.org/x", finalized_project)
    assert elsewhere["status"] == "error"
    wrong_site = record(
        "naver_blog",
        "https://qiita.com/u/items/0123456789abcdef0123",
        finalized_project,
    )
    assert wrong_site["error_code"] == "URL_ON_ANOTHER_SITE"
    assert "Naver Blog" in wrong_site["message"]
    plain_http = record("naver_blog", "http://blog.naver.com/a/1", finalized_project)
    assert plain_http["status"] == "error"
    good = record(
        "naver_blog", "https://blog.naver.com/someone/223456789012", finalized_project
    )
    assert good["publication"]["state"] == "published"


# --- one article per session -------------------------------------------------


def test_another_article_in_the_same_session_keeps_nothing_of_the_first(
    finalized_project, monkeypatch
) -> None:
    first_project = finalized_project.state[state.PROJECT_ID]
    finalize(finalized_project, "en", "ja")
    finalized_project.state[state.PUBLICATIONS] = {
        "devto": {"platform": "devto", "state": "draft", "article_id": "42"}
    }
    finalized_project.state[state.SLIDE_PLANS] = {"en": {"deck_title": "Old"}}
    finalized_project.state[state.GOOGLE_SLIDES_DECKS] = {"en": {"url": "x"}}

    async def fake_item(item_id):
        return {
            "id": item_id,
            "title": "別の記事",
            "body": "別の本文です。" * 20,
            "tags": [],
        }

    monkeypatch.setattr(platforms, "qiita_get_item", fake_item)
    url = "https://qiita.com/user/items/0123456789abcdef0123"
    asyncio.run(articles.fetch_published_article_markdown(url, finalized_project))

    current = finalized_project.state
    assert current[state.PROJECT_ID] != first_project
    assert list(current[state.PUBLICATIONS]) == ["qiita"]  # no draft id carried over
    for key in (state.FINALIZED_POSTS, state.SLIDE_PLANS, state.GOOGLE_SLIDES_DECKS):
        assert not current.get(key)
    assert not current.get(state.localized_key("en"))
    assert not current.get(state.localized_key("ja"))


def test_a_revised_seed_discards_what_was_made_from_the_old_text(
    finalized_project,
) -> None:
    finalize(finalized_project, "en")
    finalized_project.state[state.PUBLICATIONS] = {
        "devto": {"platform": "devto", "state": "draft", "article_id": "42"}
    }
    project = finalized_project.state[state.PROJECT_ID]
    articles.save_seed_article_revision(
        "제목",
        SEED_KO + "\n새 문단입니다.\n",
        "ko",
        "none",
        "",
        True,
        finalized_project,
    )
    current = finalized_project.state
    assert current[state.PROJECT_ID] == project  # the same article, revised
    assert not current.get(state.localized_key("en"))
    assert not current.get(state.FINALIZED_POSTS)
    # The draft on the platform is still this article's: it is updated, not doubled.
    assert current[state.PUBLICATIONS]["devto"]["article_id"] == "42"
