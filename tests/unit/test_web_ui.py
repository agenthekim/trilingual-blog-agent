"""Web front end: the file preview and the way into the full-page editor."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import web_ui
from app.settings import workspace_dir

JOB_ID = "0123456789abcdef0123456789abcdef"
PROJECT_ID = "demo-abc123"


def _browser() -> TestClient:
    """A client that reaches the server under its own local address."""
    return TestClient(web_ui.web, base_url="http://127.0.0.1:8502")


class FakePipeline:
    """Only what the file routes ask of a pipeline."""

    session_id = "session-of-the-fake"

    def __init__(self) -> None:
        self.saved: list[tuple[str, str, str | None]] = []
        self.synced: list[tuple[str, str, bool]] = []
        self.approvals: list[dict[str, Any]] = []
        self.said: list[str] = []
        self.last_reply = ""
        self.slide_phases: dict[str, str] = {}

    async def overview(self) -> dict[str, Any]:
        return {
            "project_id": PROJECT_ID,
            "editable": ["post.ko.md", "slides.ko.md"],  # post.ja.md is the source
            "rows": [],
        }

    async def session_state(self) -> dict[str, Any]:
        return {}

    async def mark_surface(self, minutes=None, audience=None) -> None:
        return None

    async def take_slide_request(self) -> dict[str, Any] | None:
        return None

    async def say(self, text: str) -> None:
        self.said.append(text)
        self.last_reply = "I shortened the English title."

    async def save_edit(self, name: str, markdown: str, title: str | None = None):
        self.saved.append((name, markdown, title))
        return {"saved": name, "check": None, "secrets": []}

    async def sync_deck_text(
        self, language: str, direction: str, apply: bool, change_id: str | None = None
    ):
        if language == "en":
            raise ValueError("There is no Google Slides deck for this language yet.")
        self.synced.append((language, direction, apply, change_id))
        return {"direction": direction, "slides": [2], "applied": apply}


@pytest.fixture
def client():
    folder = workspace_dir() / PROJECT_ID
    folder.mkdir(parents=True)
    for name in ("post.ko.md", "post.ja.md", "slides.ko.md"):
        (folder / name).write_text(f"# {name}\n\n<script>alert(1)</script>\n", "utf-8")
    job = web_ui.Job(id=JOB_ID, pipeline=FakePipeline())
    web_ui.JOBS[JOB_ID] = job
    yield _browser()
    web_ui.JOBS.clear()


def _wait(client: TestClient, job_id: str = JOB_ID) -> dict[str, Any]:
    """The status of a run once its background action has ended."""
    import time

    for _ in range(200):
        status = client.get(f"/api/jobs/{job_id}").json()
        if not status["busy"]:
            return status
        time.sleep(0.02)
    raise AssertionError("the action did not end")


def test_preview_links_to_the_editor_only_for_files_a_person_may_change(client) -> None:
    for name in ("post.ko.md", "slides.ko.md"):
        page = client.get(f"/api/jobs/{JOB_ID}/files/{name}")
        assert page.status_code == 200
        assert (
            f'{name} &middot; <a href="/edit?job={JOB_ID}&amp;file={name}">edit</a>'
            ' &middot; <a href="?raw=1">raw text</a>' in page.text
        )
        # Article text is shown under a policy that lets no script run.
        policy_header = page.headers["content-security-policy"]
        for rule in (
            "default-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ):
            assert rule in policy_header

    source = client.get(f"/api/jobs/{JOB_ID}/files/post.ja.md")
    assert "/edit?" not in source.text and "raw text" in source.text


def test_editor_page_is_served_and_saves_through_the_file_api(client) -> None:
    page = client.get("/edit", params={"job": JOB_ID, "file": "post.ko.md"})
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert 'id="text"' in page.text
    # The page holds no script of its own; the one it loads does the saving.
    assert '<script src="/static/edit.js"></script>' in page.text
    assert "PUT" in client.get("/static/edit.js").text
    # The page itself carries no article text; it loads it as plain text.
    assert "alert(1)" not in page.text
    raw = client.get(f"/api/jobs/{JOB_ID}/files/post.ko.md", params={"raw": 1})
    assert raw.headers["content-type"].startswith("text/plain")

    saved = client.put(
        f"/api/jobs/{JOB_ID}/files/post.ko.md",
        json={"markdown": "# 새 본문\n", "title": "새 제목"},
    )
    assert saved.json()["saved"] == "post.ko.md"
    assert web_ui.JOBS[JOB_ID].pipeline.saved == [
        ("post.ko.md", "# 새 본문\n", "새 제목")
    ]


def test_deck_sync_writes_only_when_the_request_says_a_person_approved(client) -> None:
    url = f"/api/jobs/{JOB_ID}/deck/sync"
    asked = client.post(url, json={"language": "ko", "direction": "push"})
    assert asked.json() == {"direction": "push", "slides": [2], "applied": False}
    # An approval must say which changes it approves.
    blind = client.post(
        url, json={"language": "ko", "direction": "pull", "approved": True}
    )
    assert blind.status_code == 400
    done = client.post(
        url,
        json={
            "language": "ko",
            "direction": "pull",
            "approved": True,
            "change_id": "c1",
        },
    )
    assert done.json()["applied"] is True
    job = web_ui.JOBS[JOB_ID]
    assert job.pipeline.synced == [
        ("ko", "push", False, None),
        ("ko", "pull", True, "c1"),
    ]
    assert job.busy is False

    assert (
        client.post(url, json={"language": "en", "direction": "push"}).status_code
        == 400
    )
    assert (
        client.post(url, json={"language": "ko", "direction": "both"}).status_code
        == 422
    )
    # The slides lane is working: the deck is not touched meanwhile.
    job.lanes["slides"].busy = True
    for approved in (False, True):
        busy = client.post(
            url,
            json={
                "language": "ko",
                "direction": "push",
                "approved": approved,
                "change_id": "c1",
            },
        )
        assert busy.status_code == 409
        assert busy.json()["detail"] == web_ui.BUSY["slides"]
    # A request that was turned away must not clear a flag it did not set.
    assert job.lanes["slides"].busy is True and len(job.pipeline.synced) == 2


def test_mermaid_blocks_are_shown_as_images_drawn_once(client, monkeypatch) -> None:
    monkeypatch.setenv("BLOG_AGENT_RENDER_MERMAID", "true")
    code = 'flowchart TD\n    A["<b>Plan</b>"] --> B\n'
    folder = workspace_dir() / PROJECT_ID
    (folder / "post.ko.md").write_text(
        f"# 글\n\n```mermaid\n{code}```\n\n끝.\n", encoding="utf-8"
    )
    drawn: list[str] = []

    async def draw(source: str) -> bytes | None:
        drawn.append(source)
        return b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"

    monkeypatch.setattr(web_ui, "_draw_diagram", draw)
    page = client.get(f"/api/jobs/{JOB_ID}/files/post.ko.md").text
    diagram_id = web_ui._diagram_id(code)
    assert f'src="/api/jobs/{JOB_ID}/diagrams/{diagram_id}"' in page
    # The source stays on the page, as text, under the drawing.
    assert "&lt;b&gt;Plan&lt;/b&gt;" in page and "<b>Plan</b>" not in page

    for _ in range(2):  # the second request is answered from what was kept
        image = client.get(f"/api/jobs/{JOB_ID}/diagrams/{diagram_id}")
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/svg+xml")
        assert "default-src 'none'" in image.headers["content-security-policy"]
    assert drawn == [code]

    # A digest that matches no diagram of this article draws nothing.
    assert client.get(f"/api/jobs/{JOB_ID}/diagrams/{'0' * 32}").status_code == 404
    assert client.get(f"/api/jobs/{JOB_ID}/diagrams/..%2Fx").status_code == 404
    assert len(drawn) == 1

    monkeypatch.setenv("BLOG_AGENT_RENDER_MERMAID", "false")
    plain = client.get(f"/api/jobs/{JOB_ID}/files/post.ko.md").text
    assert "/diagrams/" not in plain and "flowchart TD" in plain


def _main_lane_attempts(client, base: str) -> list[Any]:
    return [
        client.post(f"{base}/message", json={"text": "shorten the title"}),
        client.post(f"{base}/finalize", json={"language": "en"}),
        client.post(
            f"{base}/url",
            json={"platform": "naver_blog", "url": "https://blog.naver.com/a/1"},
        ),
        client.put(f"{base}/files/post.ko.md", json={"markdown": "# 글\n"}),
        client.post(
            f"{base}/draft",
            json={"platform": "devto", "tags": ["ai"], "approved": True},
        ),
    ]


def _slides_lane_attempts(client, base: str) -> list[Any]:
    return [
        client.post(f"{base}/deck", json={"language": "en", "approved": True}),
        client.put(f"{base}/files/slides.ko.md", json={"markdown": "# deck\n"}),
        client.post(f"{base}/deck/sync", json={"language": "ko", "direction": "push"}),
    ]


def test_while_the_agent_works_nothing_else_starts_and_one_sentence_says_so(
    client,
) -> None:
    job = web_ui.JOBS[JOB_ID]
    job.lanes["main"].busy = job.lanes["slides"].busy = True
    base = f"/api/jobs/{JOB_ID}"
    for response in _main_lane_attempts(client, base):
        assert response.status_code == 409
        assert response.json()["detail"] == web_ui.BUSY["main"]
    for response in _slides_lane_attempts(client, base):
        assert response.status_code == 409
        assert response.json()["detail"] == web_ui.BUSY["slides"]
    assert job.lanes["main"].busy and job.lanes["slides"].busy
    assert job.pipeline.saved == [] and job.pipeline.said == []
    assert job.pipeline.synced == []


def test_a_busy_lane_refuses_its_own_actions_and_names_itself(client) -> None:
    job = web_ui.JOBS[JOB_ID]
    base = f"/api/jobs/{JOB_ID}"
    # The sentence of each lane names that lane, and the two differ.
    assert "main lane" in web_ui.BUSY["main"] and "slides lane" in web_ui.BUSY["slides"]

    # The slides lane works: its own actions are refused, the main lane's run.
    job.lanes["slides"].busy = True
    for response in _slides_lane_attempts(client, base):
        assert response.status_code == 409
        assert response.json()["detail"] == web_ui.BUSY["slides"]
    saved = client.put(f"{base}/files/post.ko.md", json={"markdown": "# 글\n"})
    assert saved.status_code == 200 and job.pipeline.saved[0][0] == "post.ko.md"
    status = client.get(base).json()
    assert status["busy"] is True
    assert status["lanes"]["slides"]["busy"] is True
    assert status["lanes"]["main"]["busy"] is False
    assert "posts_busy" not in status

    # The main lane works: the other way round.
    job.lanes["slides"].busy, job.lanes["main"].busy = False, True
    for response in _main_lane_attempts(client, base):
        assert response.status_code == 409
        assert response.json()["detail"] == web_ui.BUSY["main"]
    notes = client.put(f"{base}/files/slides.ko.md", json={"markdown": "# deck\n"})
    assert notes.status_code == 200 and job.pipeline.saved[-1][0] == "slides.ko.md"
    asked = client.post(
        f"{base}/deck/sync", json={"language": "ko", "direction": "push"}
    )
    assert asked.status_code == 200
    # An action that was let in gave its lane back; the other stays as it was.
    assert job.lanes["slides"].busy is False and job.lanes["main"].busy is True


def test_the_agents_reply_to_a_request_in_words_is_shown(client) -> None:
    sent = client.post(f"/api/jobs/{JOB_ID}/message", json={"text": "shorten it"})
    assert sent.json() == {"accepted": True}
    status = _wait(client)
    assert status["reply"] == "I shortened the English title."
    assert status["notice"] is None and status["error"] is None


def test_there_is_one_page_and_the_classic_layout_is_gone(client) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "Trilingual Blog Agent" in page.text
    assert client.get("/v1").status_code == 404
    assert client.get("/v2").status_code == 404
    assert "/v1" not in page.text


def test_settings_reports_only_whether_each_credential_is_configured(
    client, monkeypatch
) -> None:
    from app import secrets

    def get_secret(name: str) -> str:
        if name == secrets.DEVTO_API_KEY:
            return "the-real-key-value"
        raise secrets.MissingSecretError(name)

    monkeypatch.setattr(secrets, "get_secret", get_secret)
    monkeypatch.setattr(web_ui, "_config_cache", None)
    answer = client.get("/api/config")
    config = answer.json()
    assert [(p["language"], p["platform"], p["how"]) for p in config["platforms"]] == [
        ("Korean", "Naver Blog", "paste"),
        ("English", "DEV Community", "api"),
        ("Japanese", "Qiita", "api"),
    ]
    assert [p["configured"] for p in config["platforms"]] == [True, True, False]
    assert config["google_slides"] is False
    # Neither a value nor the name of a secret leaves the server.
    for word in ("the-real-key-value", "DEVTO_API_KEY", "QIITA", "OAUTH"):
        assert word not in answer.text


# --- with the real pipeline: what a click does, end to end, without a model ---


@pytest.fixture
def real(monkeypatch):
    """A run whose article is localized and waiting for review, as after a run."""
    import asyncio

    from app import state
    from app.pipeline import ArticlePipeline
    from tests.unit.conftest import SEED_KO

    job = web_ui.Job(id="f" * 32, pipeline=ArticlePipeline())
    job.pipeline.log = job.add_log
    web_ui.JOBS[job.id] = job
    asyncio.run(
        job.pipeline._update_state(
            {
                state.PROJECT_ID: PROJECT_ID,
                state.SEED_TITLE: "제목",
                state.SEED_MARKDOWN: SEED_KO,
                state.SEED_FINALIZED: True,
                state.SOURCE_LANGUAGE: "ko",
                state.TARGET_LANGUAGES: ["en", "ja"],
                state.localized_key("en"): SEED_KO.replace("제목", "Title"),
                state.localized_key("ja"): SEED_KO.replace("제목", "タイトル"),
                state.LOCALIZED_TITLES: {"en": "Title", "ja": "タイトル"},
            }
        )
    )
    yield _browser(), job
    web_ui.JOBS.clear()


def _row(status: dict[str, Any], language: str) -> dict[str, Any]:
    return next(r for r in status["overview"]["rows"] if r["language"] == language)


def test_posting_from_the_page_needs_a_finalized_post_and_carries_the_persons_choice(
    real, monkeypatch
) -> None:
    from app import platforms

    client, job = real
    sent: list[dict] = []

    async def fake_save(**kwargs):
        sent.append(kwargs)
        return {"id": 42, "url": "https://dev.to/u/t-42"}

    monkeypatch.setattr(platforms, "devto_save_article", fake_save)
    url = f"/api/jobs/{job.id}/draft"
    body = {"platform": "devto", "tags": ["ai"], "approved": True, "public": True}

    refused = client.post(url, json=body)
    assert (
        refused.status_code == 409 and "not been finalized" in refused.json()["detail"]
    )
    unapproved = client.post(url, json={**body, "approved": False})
    assert unapproved.status_code == 400 and sent == []

    done = client.post(f"/api/jobs/{job.id}/finalize", json={"language": "en"})
    assert done.json()["finalized"] is True
    assert _row(_wait(client, job.id), "en")["next"] == "post"

    assert client.post(url, json=body).json() == {"accepted": True}
    status = _wait(client, job.id)
    assert status["notice"] is None and status["error"] is None
    assert sent[0]["published"] is True and sent[0]["title"] == "Title"
    assert _row(status, "en")["status"] == "published"
    assert _row(status, "en")["next"] == "public"
    assert status["overview"]["manifest"] == "manifest.md"


def test_a_post_that_did_not_happen_is_said_on_the_page(real, monkeypatch) -> None:
    from app import platforms

    client, job = real

    async def rejected(**kwargs):
        raise platforms.PlatformError(
            "DEVTO_UNAUTHORIZED", "The API key was rejected.", "x"
        )

    monkeypatch.setattr(platforms, "devto_save_article", rejected)
    client.post(f"/api/jobs/{job.id}/finalize", json={"language": "en"})
    client.post(
        f"/api/jobs/{job.id}/draft",
        json={"platform": "devto", "tags": ["ai"], "approved": True},
    )
    status = _wait(client, job.id)
    assert status["notice"] == (
        "Nothing was posted on DEV Community. The API key was rejected."
    )
    assert _row(status, "en")["status"] == "not_posted"


def test_tags_are_checked_against_the_platforms_rule_before_anything_runs(
    real,
) -> None:
    client, job = real
    url = f"/api/jobs/{job.id}/draft"
    five = client.post(
        url,
        json={"platform": "devto", "tags": ["a", "b", "c", "d", "e"], "approved": True},
    )
    assert five.status_code == 400 and "up to four tags" in five.json()["detail"]
    spaced = client.post(
        url, json={"platform": "qiita", "tags": ["two words"], "approved": True}
    )
    assert spaced.status_code == 400 and "without spaces" in spaced.json()["detail"]
    none = client.post(url, json={"platform": "qiita", "tags": [], "approved": True})
    assert none.status_code == 400 and "one to five tags" in none.json()["detail"]
    assert job.busy is False


def test_finalizing_text_with_credential_findings_asks_the_person_first(real) -> None:
    import asyncio

    from app import state

    client, job = real
    key = "AIza" + "B" * 35
    current = asyncio.run(job.pipeline.session_state())
    asyncio.run(
        job.pipeline._update_state(
            {
                state.localized_key("en"): current[state.localized_key("en")]
                + f"\nkey {key}\n"
            }
        )
    )
    url = f"/api/jobs/{job.id}/finalize"
    asked = client.post(url, json={"language": "en"})
    body = asked.json()
    assert asked.status_code == 200 and body["needs_acknowledgement"] is True
    assert body["finalized"] is False and body["findings"][0]["line"] > 1
    assert key not in asked.text
    assert _row(_wait(client, job.id), "en")["finalized"] is False

    accepted = client.post(url, json={"language": "en", "acknowledge_findings": True})
    assert accepted.json()["finalized"] is True
    assert _row(_wait(client, job.id), "en")["finalized"] is True


def test_a_registered_address_is_recorded_and_checked(real, monkeypatch) -> None:
    from app import platforms

    client, job = real

    async def fake_fetch(url: str):
        return 404, None

    monkeypatch.setattr(platforms, "fetch_status_and_title", fake_fetch)
    url = f"/api/jobs/{job.id}/url"
    client.post(
        url, json={"platform": "naver_blog", "url": "https://qiita.com/a/items/b"}
    )
    status = _wait(client, job.id)
    assert status["notice"].startswith("The address was not recorded.")
    assert "Naver Blog" in status["notice"]

    client.post(
        url,
        json={
            "platform": "naver_blog",
            "url": "https://blog.naver.com/someone/223456789012",
        },
    )
    status = _wait(client, job.id)
    assert "did not answer when checked (HTTP 404)" in status["notice"]
    assert _row(status, "ko")["status"] == "published"


def test_a_deck_is_created_for_the_language_that_was_asked_for(
    real, monkeypatch
) -> None:
    import asyncio

    from app import google_slides, state
    from tests.unit.test_pipeline import _plan

    client, job = real
    asyncio.run(
        job.pipeline._update_state(
            {state.SLIDE_PLANS: {"en": _plan("en"), "ja": _plan("ja")}}
        )
    )
    created: list[str] = []

    async def fake_create(plan, allowed_images):
        created.append(plan.language)
        return {"presentation_id": "abc", "url": google_slides.deck_url("abc")}

    monkeypatch.setattr(google_slides, "create_deck", fake_create)
    url = f"/api/jobs/{job.id}/deck"
    assert client.post(url, json={"language": "ja"}).status_code == 400  # not approved
    assert client.post(url, json={"approved": True}).status_code == 422  # which one?
    client.post(url, json={"language": "ja", "approved": True})
    status = _wait(client, job.id)
    assert created == ["ja"] and status["notice"] is None
    assert _row(status, "ja")["deck"]["url"].endswith("/d/abc/edit")
    assert _row(status, "en")["deck"] is None

    client.post(url, json={"language": "ko", "approved": True})
    assert "no Korean slide plan" in _wait(client, job.id)["notice"]
    assert created == ["ja"]


def test_a_run_is_reopened_from_its_record_after_a_restart(real) -> None:
    client, job = real
    job.add_log(">>> a line from before the restart")
    job.talk_languages, job.wants_decks = ["en", "ko"], True
    job.lanes["main"].busy = True  # it was working when the server stopped
    job.save()
    before = client.get(f"/api/jobs/{job.id}").json()

    web_ui.JOBS.clear()  # a new process knows no runs
    after = client.get(f"/api/jobs/{job.id}").json()
    assert after["busy"] is False
    assert after["notice"].startswith("The server stopped while this run was working.")
    assert after["log"] == [">>> a line from before the restart"]
    assert after["overview"] == before["overview"]
    assert [step["name"] for step in after["progress"]] == [
        "Source document",
        "Transform",
        "Prepare posts",
        "Prepare slides",
        "Summarize",
    ]
    assert [step["name"] for step in after["post_progress"]] == [
        "Review & edit",
        "Finalize posts",
        "Approve & draft",
        "Publish posts",
        "Verify & complete",
    ]
    # The notice is said once, not on every reopening.
    web_ui.JOBS.clear()
    again = client.get(f"/api/jobs/{job.id}").json()
    assert again["notice"].count("The server stopped") == 1

    assert client.get(f"/api/jobs/{'0' * 32}").status_code == 404
    assert client.get("/api/jobs/..%2F..%2Fetc").status_code == 404


def test_an_interrupted_run_offers_the_slides_that_are_missing(
    real, monkeypatch
) -> None:
    """RUN-4: the record keeps the options of the run; after a server stop the
    run says which lane was interrupted, names the languages whose slides
    are not there, and the page asks for all of them in one approved request
    with those options. Nothing is made before a person approves (INV-6)."""
    import asyncio

    from app import google_slides, state
    from tests.unit.test_pipeline import _plan

    client, job = real
    base = f"/api/jobs/{job.id}"
    options = {
        "minutes": 10,
        "audience": "students",
        "languages": ["en", "ko", "ja"],
        "decks": True,
    }
    job.talk_languages, job.wants_decks = options["languages"], options["decks"]
    job.talk_minutes, job.audience = options["minutes"], options["audience"]
    # English was finished; Korean was being planned when the server stopped.
    asyncio.run(
        job.pipeline._update_state(
            {
                state.SLIDE_PLANS: {"en": _plan("en"), "ko": _plan("ko")},
                state.GOOGLE_SLIDES_DECKS: {
                    "en": {
                        "presentation_id": "abc",
                        "url": google_slides.deck_url("abc"),
                    }
                },
            }
        )
    )
    job.lanes["slides"].busy, job.lanes["slides"].action = True, "run_slides"
    job.pipeline.slide_phases["ko"] = "planning"
    job.save()
    assert _record(job.id)["options"] == options
    # While the lane works nothing is missing yet: the slides are being made.
    assert client.get(base).json()["missing_slides"] == []

    web_ui.JOBS.clear()  # a new process knows no runs
    made: list[Any] = []
    status = client.get(base).json()
    reopened = web_ui.JOBS[job.id]

    async def make_slides(languages, minutes, audience, create_deck):
        made.append((languages, minutes, audience, create_deck))
        return None

    monkeypatch.setattr(reopened.pipeline, "make_slides", make_slides)
    # The lane that was interrupted says so, and only that lane.
    assert status["busy"] is False
    assert [part["code"] for part in status["lanes"]["slides"]["notices"]] == [
        "interrupted_slides"
    ]
    assert status["lanes"]["main"]["notice"] is None
    # Korean has a plan and no deck, Japanese has nothing: decks were asked for.
    assert status["options"] == options
    assert status["missing_slides"] == ["ko", "ja"]
    # The slides step is one step, not done; no language has a mark of its own.
    assert "slides_progress" not in status
    assert {"name": "Prepare slides", "done": False} in status["progress"]
    # Reopening started nothing, and an unapproved request starts nothing.
    asked = {
        "languages": status["missing_slides"],
        "minutes": 10,
        "audience": "students",
    }
    assert client.post(f"{base}/slides", json=asked).status_code == 400
    assert made == [] and not reopened.lanes["slides"].busy

    # Approved, the missing languages are one request with the run's options.
    answer = client.post(f"{base}/slides", json={**asked, "approved": True})
    assert answer.status_code == 200
    after = _wait(client, job.id)
    assert made == [(["ko", "ja"], 10, "students", True)]
    assert after["lanes"]["slides"]["notice"] is None  # said once, then gone

    # Without decks asked for, a plan is enough; a language without a post
    # has nothing slides could be made from.
    reopened.wants_decks = False
    assert client.get(base).json()["missing_slides"] == ["ja"]
    asyncio.run(reopened.pipeline._update_state({state.localized_key("ja"): ""}))
    assert client.get(base).json()["missing_slides"] == []

    # The page offers it with the dialog every slide request uses: one
    # dialog for a list of languages, one request for all of them.
    script = (web_ui.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "requestSlides(missing, data.options)" in script
    assert script.count("async function requestSlides(rows, options)") == 1
    assert script.count('act("slides", {') == 1
    assert "languages: rows.map((row) => row.language)" in script


def test_a_session_is_read_back_by_a_new_process(tmp_path, monkeypatch) -> None:
    """The local session store is a file: what one process wrote, the next reads."""
    import asyncio

    from app import state
    from app.app_utils.services import get_session_service
    from app.pipeline import ArticlePipeline

    monkeypatch.delenv("USE_IN_MEMORY_SESSION")
    monkeypatch.setenv("LOCAL_SESSION_DB", str(tmp_path / "sessions.sqlite3"))
    get_session_service.cache_clear()

    async def write() -> str:
        first = ArticlePipeline(log=lambda _: None)
        await first._update_state({state.SEED_TITLE: "남아 있는 제목"})
        return first.session_id

    session_id = asyncio.run(write())
    get_session_service.cache_clear()  # as if the server had been started again

    async def read() -> dict:
        second = ArticlePipeline(log=lambda _: None, session_id=session_id)
        return await second.session_state()

    assert asyncio.run(read())[state.SEED_TITLE] == "남아 있는 제목"


def test_the_page_carries_the_wording_and_the_column_order_that_were_agreed(
    client,
) -> None:
    import re

    # The texts are in the page and in the English catalog it loads.
    page = client.get("/").text + client.get("/static/i18n/en.json").text
    for text in (
        "<h1>Trilingual Blog Agent</h1>",
        "Automatically transform an article into blog posts and "
        "presentation slides across three languages.",
        "Source document",
        "Share a post URL, a .md file, or your document.",
        "Slide options",
        "Choose the slide length to create Google Slides in your configured Google Drive.",
        "Ask the agent for a change",
        "Qiita takes one to five tags without spaces",
        "DEV Community takes up to four tags, lowercase letters and digits only",
    ):
        assert text in page
    audiences = re.findall(
        r'<option value="[^"]+"(?: selected)? data-i18n="audience\.\w+">([A-Za-z-]+)</option>',
        page,
    )
    assert audiences == [
        "Developers",
        "Experts",
        "Students",
        "Executives",
        "Non-technical",
    ]
    lengths = re.findall(r'<option value="(\d+)" data-i18n="minutes', page)
    assert lengths == ["0", "3", "5", "10", "15", "20", "30", "45", "60"]
    # Columns follow the order of the work, with the next step last.
    columns = re.findall(r'<th data-i18n="col\w+">([^<]+)</th>', page)
    assert columns == ["Language", "Title", "Files", "Slides", "Next step"]
    # Inside the progress card: title -> status bar (#live) -> bar 1 (#steps) -> bar 2 (#nextSteps).
    assert (
        page.index('id="articleTitle"')
        < page.index('id="live"')
        < page.index('id="steps"')
        < page.index('id="nextSteps"')
    )
    # Links carry no arrow symbol, and nothing is listed under the table.
    assert "↗" not in page and "Talk and summary" not in page
    # No name of a secret is written into the page.
    for word in ("QIITA_", "DEVTO_", "OAUTH"):
        assert word not in page


def test_the_server_answers_only_to_its_own_name(client, monkeypatch) -> None:
    """With no login, a page that points its own name at this machine is refused."""
    other = TestClient(web_ui.web, base_url="http://attacker.example")
    for response in (
        other.get("/"),
        other.get(f"/api/jobs/{JOB_ID}"),
        other.post(f"/api/jobs/{JOB_ID}/finalize", json={"language": "en"}),
    ):
        assert response.status_code == 400
    assert client.get("/").status_code == 200
    assert (
        TestClient(web_ui.web, base_url="http://localhost:8502").get("/").status_code
        == 200
    )
    # Bound to another address on purpose (--host): any name is then answered,
    # and what it gets depends on the access token (SEC-7).
    monkeypatch.setattr(web_ui, "ALLOWED_HOSTS", None)
    assert other.get("/").status_code == 200
    monkeypatch.setattr(web_ui, "ACCESS_TOKEN", "a-token-0123456789")
    assert other.get("/").status_code == 401


def test_another_address_needs_the_token(client, monkeypatch) -> None:
    """SEC-7: bound to another address, every route needs the access token.
    The first visit exchanges it for a cookie; the token is shown once, in the
    terminal, and is in no answer, no structured record and no activity log."""
    import hmac
    import logging

    from app import observability
    from app.redaction import redact_text

    monkeypatch.setattr(web_ui, "ALLOWED_HOSTS", web_ui.ALLOWED_HOSTS)
    monkeypatch.setattr(web_ui, "ACCESS_TOKEN", None)

    class Kept(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            fields = getattr(record, "json_fields", None) or {}
            self.lines.append(f"{record.getMessage()} {fields}")

    terminal, structured = Kept(), Kept()
    web_ui._TERMINAL.addHandler(terminal)
    observability.get_logger().addHandler(structured)
    compared: list[int] = []
    real_compare = hmac.compare_digest

    def compare(a: bytes, b: bytes) -> bool:
        compared.append(1)
        return real_compare(a, b)

    monkeypatch.setattr(web_ui.hmac, "compare_digest", compare)
    try:
        # A token of the operator's must be one nobody guesses.
        for weak in ("short1", "no-digit-in-this-token", "has a space 1234567"):
            with pytest.raises(ValueError, match="BLOG_AGENT_UI_TOKEN"):
                web_ui.configure_access("0.0.0.0", 8502, weak)
        assert web_ui.ACCESS_TOKEN is None and terminal.lines == []

        # Without one, a token is made at start; the address is shown once.
        web_ui.configure_access("0.0.0.0", 8502, None)
        token = web_ui.ACCESS_TOKEN
        assert token and web_ui.usable_token(token) and len(token) >= 32
        assert web_ui.ALLOWED_HOSTS is None
        assert len(terminal.lines) == 1
        assert f":8502/?access_token={token}" in terminal.lines[0]
        assert token != web_ui.new_access_token()

        base = f"/api/jobs/{JOB_ID}"
        job = web_ui.JOBS[JOB_ID]
        routes = [
            "/",
            "/edit",
            "/static/app.js",
            "/static/i18n/en.json",
            "/api/config",
            "/api/jobs",
            base,
            f"{base}/files/post.ko.md",
            "/no-such-route",
        ]

        def refused(response: Any) -> bool:
            return (
                response.status_code == 401
                and "set-cookie" not in response.headers
                and "location" not in response.headers  # nothing to loop on
                and "access token" in response.text
            )

        # No token and no cookie: nothing is served, static files included.
        other = TestClient(web_ui.web, base_url="http://machine.example:8502")
        for path in routes:
            assert refused(other.get(path)), path
        assert refused(other.post(f"{base}/finalize", json={"language": "en"}))
        assert refused(other.put(f"{base}/files/post.ko.md", json={"markdown": "x"}))
        # The page's own requests get the sentence with its code (WEB-13).
        assert other.get(base).json()["code"] == "needs_access_token"
        assert other.get("/").headers["content-type"].startswith("text/plain")
        # A wrong token, a wrong cookie, and a token sent as the cookie.
        assert refused(other.get("/?access_token=" + "0" * len(token)))
        assert refused(other.get("/?access_token="))
        for wrong in ("0" * 64, token):
            other.cookies.set(web_ui.ACCESS_COOKIE, wrong)
            assert refused(other.get("/")), wrong
            other.cookies.clear()
        # The token does not open a request that changes something.
        assert refused(
            other.post(f"{base}/finalize?access_token={token}", json={"language": "en"})
        )

        # The first visit with the token: a cookie that scripts cannot read
        # and other sites cannot send, and the same address without the token.
        first = other.get(f"/edit?access_token={token}&job={JOB_ID}&file=post.ko.md")
        assert first.status_code == 200
        cookie = first.headers["set-cookie"]
        assert cookie.startswith(f"{web_ui.ACCESS_COOKIE}=")
        assert "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
        assert "Path=/" in cookie
        assert f'content="0; url=/edit?job={JOB_ID}&amp;file=post.ko.md"' in first.text
        assert first.headers["referrer-policy"] == "no-referrer"
        assert first.headers["cache-control"] == "no-store"
        # An address that would lead to another server is made this server's.
        asked = {
            "type": "http",
            "method": "GET",
            "path": "//elsewhere.example/",
            "query_string": f"access_token={token}".encode(),
            "headers": [],
        }
        away = web_ui._exchange(web_ui.Request(asked), token)
        assert b'url=/%2Felsewhere.example/"' in away.body
        assert b"url=//" not in away.body and b'href="//' not in away.body

        # With the cookie everything works as on localhost: the page, its
        # files, the editor and a preview in a new tab, and the actions.
        answers = [first]
        assert token.encode() not in away.body
        for path in routes[:-1]:
            if path == "/api/config":
                continue  # asks the secret store; not this test's business
            answer = other.get(path)
            assert answer.status_code == 200, path
            answers.append(answer)
        saved = other.put(f"{base}/files/post.ko.md", json={"markdown": "# 새 글"})
        assert saved.status_code == 200 and job.pipeline.saved
        answers.append(saved)
        assert other.get("/no-such-route").status_code == 404
        job.add_log(">>> a line of the run")

        # The token is in no answer, in no structured record and in no
        # activity log; the terminal showed it once and only once.
        for answer in answers:
            assert token not in answer.text
            assert token not in str(answer.headers)
        assert not any(token in line for line in structured.lines)
        assert not any(token in line for line in job.log)
        assert sum(token in line for line in terminal.lines) == 1
        # Tokens and cookies are compared in constant time.
        assert compared
        # Should the address ever reach a log record, the filter hides it.
        hidden = redact_text(terminal.lines[0])
        assert token not in hidden and "REDACTED" in hidden

        # A token of the operator's is used as it is.
        web_ui.configure_access("192.0.2.7", 8503, "operator-token-0123456789")
        assert web_ui.ACCESS_TOKEN == "operator-token-0123456789"
        assert (
            "http://192.0.2.7:8503/?access_token=operator-token" in terminal.lines[-1]
        )
        assert refused(other.get("/"))  # the cookie of the earlier token
    finally:
        for handler in list(web_ui._TERMINAL.handlers):
            if handler is terminal or isinstance(handler, web_ui._ToTerminal):
                web_ui._TERMINAL.removeHandler(handler)
        observability.get_logger().removeHandler(structured)


def test_localhost_needs_no_token(client, monkeypatch) -> None:
    """SEC-7, SEC-1: on localhost nothing changes. No token is made, asked
    for or set, and the server still answers only to its own name."""
    monkeypatch.setattr(web_ui, "ALLOWED_HOSTS", web_ui.ALLOWED_HOSTS)
    monkeypatch.setattr(web_ui, "ACCESS_TOKEN", web_ui.ACCESS_TOKEN)
    for host in ("127.0.0.1", "localhost", "::1"):
        # Even a configured token is not used on localhost.
        web_ui.configure_access(host, 8502, "operator-token-0123456789")
        assert web_ui.ACCESS_TOKEN is None
        assert web_ui.ALLOWED_HOSTS == web_ui.LOOPBACK_HOSTS
    # Nothing was shown: the terminal has no handler of this server's.
    assert not any(
        isinstance(handler, web_ui._ToTerminal) for handler in web_ui._TERMINAL.handlers
    )

    for path in ("/", "/edit", "/static/app.js", f"/api/jobs/{JOB_ID}"):
        answer = client.get(path)
        assert answer.status_code == 200, path
        assert "set-cookie" not in answer.headers
    # A token in the address means nothing here: the page is served as it is.
    with_token = client.get("/?access_token=anything")
    assert with_token.status_code == 200 and "set-cookie" not in with_token.headers
    assert with_token.text == client.get("/").text
    # The host-name check stays as it is.
    other = TestClient(web_ui.web, base_url="http://attacker.example")
    assert other.get("/").status_code == 400


def test_settings_names_the_account_that_decks_go_to(client, monkeypatch) -> None:
    from app import secrets

    monkeypatch.setattr(secrets, "get_secret", lambda name: "configured")
    monkeypatch.setattr(web_ui, "granted_account", lambda: "owner@example.com")
    monkeypatch.setattr(web_ui, "_config_cache", None)
    config = client.get("/api/config").json()
    assert config["google_slides"] is True
    assert config["slides_account"] == "owner@example.com"
    monkeypatch.setattr(web_ui, "_config_cache", None)
    # The dialog that asks for the approval takes the account into its text.
    script = client.get("/static/app.js").text
    assert 't("driveOf", {account})' in script
    texts = client.get("/static/i18n/en.json").json()
    assert texts["driveOf"] == "a new file in the Google Drive of {account}"


def test_requests_left_unanswered_by_a_new_message_are_said_to_be_gone(client) -> None:
    job = web_ui.JOBS[JOB_ID]
    job.pipeline.approvals = [{"id": "a", "tool": "t", "hint": "Approve?"}]
    client.post(f"/api/jobs/{JOB_ID}/message", json={"text": "do something else"})
    status = _wait(client)
    assert "1 request(s) the agent was waiting on" in status["notice"]
    assert status["reply"] == "I shortened the English title."


def _two_decks(job) -> None:
    """Decks for English and Korean."""
    import asyncio

    from app import state

    asyncio.run(
        job.pipeline._update_state(
            {
                state.GOOGLE_SLIDES_DECKS: {
                    "en": {
                        "presentation_id": "pres-en-1",
                        "url": "https://docs.google.com/presentation/d/pres-en-1/edit",
                    },
                    "ko": {
                        "presentation_id": "pres-ko-1",
                        "url": "https://docs.google.com/presentation/d/pres-ko-1/edit",
                    },
                }
            }
        )
    )


def test_nothing_exports_a_deck_as_pdf(real) -> None:
    """Decision 52: no tool, no function, no route and no field of a PDF."""
    from pathlib import Path

    from app import google_slides
    from app.pipeline import SLIDE_ACTIONS
    from app.tools import slides
    from tests.unit.test_tool_contract import TOOLS

    client, job = real
    _two_decks(job)

    # The agent has no tool for it, and the instruction names none.
    assert TOOLS and not [name for name in TOOLS if "pdf" in name.lower()]
    from app.prompts import COORDINATOR_INSTRUCTION

    assert "pdf" not in COORDINATOR_INSTRUCTION.lower()

    # No function exports one, and no code of the application names one.
    for module in (google_slides, slides):
        assert not [name for name in vars(module) if "pdf" in name.lower()]
    for source in sorted(Path(web_ui.__file__).parent.rglob("*.py")):
        text = source.read_text(encoding="utf-8")
        for gone in ("export_deck_pdf", "slide_pdf_file", "export_google_slides_pdf"):
            assert gone not in text, (source.name, gone)

    # The rows and the deck records of the status carry nothing about one.
    status = client.get(f"/api/jobs/{job.id}").json()
    for row in status["overview"]["rows"]:
        assert "slides_pdf" not in row and "pdf_exported_at" not in row
    assert _row(status, "ko")["slides_state"] == "deck"
    assert _row(status, "ko")["slides_actions"] == ["open_deck", "notes"]

    # The page has no route, no action and no script for it.
    for body in ({"language": "ko"}, {}):
        refused = client.post(f"/api/jobs/{job.id}/pdf", json=body)
        assert refused.status_code in (404, 405)
    assert not job.lanes["slides"].busy
    assert not {"pdf", "export_pdf"} & set(SLIDE_ACTIONS)
    for name in ("app.js", "edit.js", "index.html", "i18n/en.json"):
        page = (web_ui.STATIC_DIR / name).read_text(encoding="utf-8")
        assert "pdf" not in page.lower(), name

    # A PDF an earlier version left in the folder is not a file of the article.
    left = workspace_dir() / PROJECT_ID / "slides.ko.pdf"
    left.parent.mkdir(parents=True, exist_ok=True)
    left.write_bytes(b"%PDF-1.4\n\xff\xfe left from an earlier version")
    assert client.get(f"/api/jobs/{job.id}/files/slides.ko.pdf").status_code == 404
    assert left.exists()


def test_finalizing_a_post_does_not_touch_slides(real) -> None:
    """RUN-1: finalizing is the main lane's; it writes no slide value."""
    import asyncio

    from app import state

    client, job = real
    _two_decks(job)

    slide_keys = (
        state.SLIDE_PLANS,
        state.SLIDE_TIMING,
        state.GOOGLE_SLIDES_DECKS,
        state.DECK_TEXT_BASELINE,
    )

    def slide_values() -> dict[str, Any]:
        current = asyncio.run(job.pipeline.session_state())
        return {key: current.get(key) for key in slide_keys}

    before = slide_values()
    for language in ("en", "ko"):
        resp = client.post(f"/api/jobs/{job.id}/finalize", json={"language": language})
        assert resp.status_code == 200 and resp.json()["finalized"] is True
    status = _wait(client, job.id)

    # Nothing about slides was written, and the slides lane did nothing.
    assert slide_values() == before
    assert _row(status, "en")["finalized"] and _row(status, "ko")["finalized"]
    assert not status["lanes"]["slides"]["busy"]


def _wait_for_lane(client: TestClient, job_id: str, lane: str) -> dict[str, Any]:
    """The status of a run once the action of one lane has ended."""
    import time

    for _ in range(200):
        status = client.get(f"/api/jobs/{job_id}").json()
        if not status["lanes"][lane]["busy"]:
            return status
        time.sleep(0.02)
    raise AssertionError(f"the action of the {lane} lane did not end")


def test_posts_can_be_saved_finalized_and_posted_while_slides_are_made(
    real, monkeypatch
) -> None:
    from app import platforms
    from app.tools.articles import write_workspace_file
    from tests.unit.conftest import SEED_KO

    client, job = real
    english = SEED_KO.replace("제목", "Title")
    write_workspace_file(PROJECT_ID, "post.en.md", english)
    sent: list[dict] = []

    async def fake_save(**kwargs):
        sent.append(kwargs)
        return {"id": 42, "url": "https://dev.to/u/t-42"}

    monkeypatch.setattr(platforms, "devto_save_article", fake_save)
    base = f"/api/jobs/{job.id}"
    job.lanes["slides"].busy = True  # slides are being made, for minutes

    edited = english.replace("끝입니다.", "끝입니다. 고쳤습니다.")
    saved = client.put(f"{base}/files/post.en.md", json={"markdown": edited})
    assert saved.status_code == 200 and saved.json()["saved"] == "post.en.md"
    done = client.post(f"{base}/finalize", json={"language": "en"})
    assert done.status_code == 200 and done.json()["finalized"] is True
    posted = client.post(
        f"{base}/draft", json={"platform": "devto", "tags": ["ai"], "approved": True}
    )
    assert posted.json() == {"accepted": True}

    status = _wait_for_lane(client, job.id, "main")
    assert status["lanes"]["main"]["notice"] is None and status["error"] is None
    assert "고쳤습니다" in sent[0]["body"] and sent[0]["published"] is False
    assert _row(status, "en")["status"] == "draft"
    # The slides lane was neither used nor released by any of this.
    assert status["lanes"]["slides"]["busy"] is True and status["busy"] is True
    refused = client.post(f"{base}/deck", json={"language": "en", "approved": True})
    assert refused.status_code == 409
    assert refused.json()["detail"] == web_ui.BUSY["slides"]


def test_the_page_runs_no_inline_script(client) -> None:
    """SEC-8: script comes from this server's own files, and a header says so."""
    import re

    for address, script in (("/", "/static/app.js"), ("/edit", "/static/edit.js")):
        page = client.get(address)
        assert page.status_code == 200
        policy = page.headers["content-security-policy"]
        rules = dict(
            rule.strip().split(" ", 1) for rule in policy.split(";") if rule.strip()
        )
        # Without 'unsafe-inline' a browser runs neither a script written into
        # the page nor an ``onclick=`` attribute.
        assert rules["script-src"] == "'self'"
        assert rules["default-src"] == "'self'"
        assert rules["base-uri"] == "'none'"
        assert page.headers["cache-control"] == "no-store"
        # The page itself agrees with its header: script elements loaded
        # from files (the texts, then the page's own), none with script
        # written in it, and no handler written on an element.
        scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", page.text, re.S)
        assert scripts == [(' src="/static/i18n.js"', ""), (f' src="{script}"', "")]
        assert not re.search(r"<[^>]*\son[a-z]+\s*=", page.text)
        assert "javascript:" not in page.text and "<style" not in page.text
        assert client.get(script).status_code == 200
    # Nothing the scripts do needs evaluating text as code.
    for script in ("i18n.js", "app.js", "edit.js"):
        text = (web_ui.STATIC_DIR / script).read_text(encoding="utf-8")
        assert not re.search(
            r"\beval\(|new Function\(|\.innerHTML\b|document\.write", text
        )


def test_the_files_of_the_page_are_served_fresh_and_by_name_only(client) -> None:
    for name, kind in (
        ("app.css", "text/css"),
        ("edit.css", "text/css"),
        ("i18n.js", "text/javascript"),
        ("app.js", "text/javascript"),
        ("edit.js", "text/javascript"),
        ("i18n/en.json", "application/json"),
        ("i18n/ko.json", "application/json"),
        ("i18n/ja.json", "application/json"),
    ):
        answer = client.get(f"/static/{name}")
        assert answer.status_code == 200, name
        charset = "" if kind == "application/json" else "; charset=utf-8"
        assert answer.headers["content-type"] == kind + charset
        assert answer.headers["cache-control"] == "no-store"
        assert answer.headers["x-content-type-options"] == "nosniff"
        assert answer.text == (web_ui.STATIC_DIR / name).read_text(encoding="utf-8")
    # Every file a page asks for is one of these, and exists.
    for page in ("index.html", "edit.html"):
        text = (web_ui.STATIC_DIR / page).read_text(encoding="utf-8")
        for asked in re.findall(r'(?:src|href)="(/static/[^"]+)"', text):
            assert client.get(asked).status_code == 200, asked
    # The pages are served at their own addresses, with the policy; nothing
    # else in the folder, and nothing outside it, is served from here.
    for name in (
        "index.html",
        "edit.html",
        "nothing.js",
        "..%2Fweb_ui.py",
        "APP.JS",
        "i18n/xx.json",
        "i18n/app.js",
        "i18n%2F..%2Fapp.js",
        "en.json",
    ):
        assert client.get(f"/static/{name}").status_code == 404, name
    assert client.get("/static/%2e%2e/web_ui.py").status_code in (400, 404)


def test_a_preview_is_shown_in_the_language_of_the_page(client) -> None:
    """WEB-13: the words around a file come from the catalogs of the page."""
    import json

    address = f"/api/jobs/{JOB_ID}/files/post.ko.md"
    for lang in ("en", "ko", "ja"):
        texts = json.loads(
            (web_ui.STATIC_DIR / "i18n" / f"{lang}.json").read_text(encoding="utf-8")
        )
        page = client.get(address, params={"lang": lang}).text
        assert f'<html lang="{lang}">' in page
        assert f'&amp;file=post.ko.md">{texts["previewEdit"]}</a>' in page
        assert f'<a href="?raw=1">{texts["previewRaw"]}</a>' in page
    assert ">편집</a>" in client.get(address, params={"lang": "ko"}).text
    # Without a language, or with one the page does not have, English.
    for answer in (client.get(address), client.get(address, params={"lang": "../en"})):
        assert '<html lang="en">' in answer.text
        assert ">edit</a>" in answer.text and ">raw text</a>" in answer.text
    # The file itself is shown as it is, whatever the language of the page.
    raw = client.get(address, params={"raw": 1, "lang": "ja"}).text
    assert raw == client.get(address, params={"raw": 1}).text


def test_each_row_offers_the_slide_action_its_state_allows(real, monkeypatch) -> None:
    """WEB-5: the server says what the Slides column offers, per language."""
    import asyncio
    import copy

    from app import google_slides, state
    from app.pipeline import SLIDE_ACTIONS, slide_actions
    from tests.unit.test_pipeline import _plan

    client, job = real
    base = f"/api/jobs/{job.id}"

    def offered(language: str) -> list[str]:
        return _row(client.get(base).json(), language)["slides_actions"]

    # Without a plan: slides can be asked for, and nothing else.
    assert offered("en") == offered("ko") == offered("ja") == ["create_slides"]

    # With a plan: its speaker notes, and a deck from it.
    plans = {"en": _plan("en"), "ja": _plan("ja")}
    asyncio.run(job.pipeline._update_state({state.SLIDE_PLANS: plans}))
    assert offered("en") == offered("ja") == ["notes", "create_deck"]
    assert offered("ko") == ["create_slides"]

    # With a deck: the deck and the notes, and no making it again.
    async def fake_create(plan, allowed_images):
        return {"presentation_id": "abc", "url": google_slides.deck_url("abc")}

    monkeypatch.setattr(google_slides, "create_deck", fake_create)
    client.post(f"{base}/deck", json={"language": "ja", "approved": True})
    assert _wait(client, job.id)["notice"] is None
    assert offered("ja") == ["open_deck", "notes"]
    assert offered("en") == ["notes", "create_deck"]  # another language's deck

    # The speaker notes changed after the deck was made: the deck can be updated.
    changed = copy.deepcopy(plans)
    changed["ja"]["slides"][0]["title"] = "A title written later"
    asyncio.run(job.pipeline._update_state({state.SLIDE_PLANS: changed}))
    row = _row(client.get(base).json(), "ja")
    assert row["deck_outdated"] is True
    assert row["slides_actions"] == ["open_deck", "notes", "update_deck"]

    # While the slides lane works on a language its row offers nothing and
    # says where the lane is; the other rows keep what they offer.
    job.pipeline.slide_phases["en"] = "planning"
    job.lanes["slides"].busy = True
    status = client.get(base).json()
    assert _row(status, "en")["slides_progress"] == "planning"
    assert _row(status, "en")["slides_actions"] == []
    assert _row(status, "ja")["slides_progress"] is None
    assert _row(status, "ja")["slides_actions"][-1] == "update_deck"
    # A phase left from work that ended is not shown as work under way.
    job.lanes["slides"].busy = False
    status = client.get(base).json()
    assert _row(status, "en")["slides_progress"] is None
    assert _row(status, "en")["slides_actions"] == ["notes", "create_deck"]

    # Every state offers only actions that have a name, and the page knows
    # each name and no other: it renders the list and decides nothing.
    offers = {
        action
        for slides_state in ("none", "plan", "deck")
        for outdated in (False, True)
        for action in slide_actions(slides_state, outdated)
    }
    assert offers == set(SLIDE_ACTIONS)
    # No PDF export and no making existing slides again (decisions 52, 53).
    assert not {"pdf", "export_pdf", "recreate"} & set(SLIDE_ACTIONS)
    script = (web_ui.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert sorted(re.findall(r'action === "(\w+)"', script)) == sorted(SLIDE_ACTIONS)
    # "recreate" is in the script only in the names of the suggestions that
    # ask the agent in words (WEB-9); no button says "Re-create…".
    assert "exportPdf" not in script
    assert set(re.findall(r"\w*recreate\w*", script, re.I)) == {
        f"recreate_slides_{language}" for language in ("en", "ko", "ja")
    }
    texts = (web_ui.STATIC_DIR / "i18n" / "en.json").read_text(encoding="utf-8")
    assert "Export PDF" not in texts and "Re-create…" not in texts
    # "Create slides…" asks behind the approval of every slide request,
    # "Update deck…" opens the notes' editor.
    assert re.search(r'"create_slides"\)[^\n]*requestSlides\(\[row\]\)', script)
    assert re.search(r'"update_deck"\)[^\n]*/edit\?job=[^\n]*&push=1', script)


def test_no_route_translates_what_a_person_typed(client) -> None:
    """WEB-9: what a person typed is kept as typed; no model rewrites it."""
    import ast

    # The route that translated typed requests is gone, and no other route
    # is named for requests, translating or localizing.
    gone = client.post("/api/prompts/localize", json={"texts": ["Shorten it"]})
    assert gone.status_code == 404
    paths = sorted({route.path for route in web_ui.web.routes})
    assert paths and not [
        path for path in paths if re.search(r"prompt|locali[sz]e|translat", path)
    ]
    assert not hasattr(web_ui, "PromptLocalizeRequest")
    assert not hasattr(web_ui, "localize_prompts")

    # The web layer has no way to a model: it imports neither a model client
    # nor the gateway nor the table of models (OPS-4).
    server = (web_ui.STATIC_DIR.parent / "web_ui.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(server)):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {f"{node.module}.{alias.name}" for alias in node.names}
            imported.add(node.module or "")
    for module in ("google.genai", "app.llm", "app.models"):
        assert not [name for name in imported if name.startswith(module)], module

    # The page asks nobody for a translation, reads no request with regular
    # expressions to write it again, and keeps no translation of an entry.
    script = (web_ui.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for removed in (
        "/api/prompts",
        "parseSlidePromptIntent",
        "formatSlidePromptIntent",
        "localizeBuiltInPrompt",
        "localizePromptItem",
        "isConversationalReply",
        "ensurePromptHistoryLocalized",
        ".i18n",
        "i18n:legacy",
    ):
        assert removed not in script, removed
    # The only addresses the page calls are the settings and the runs.
    called = set(re.findall(r"api\([\"`](/api/[a-z]+)", script))
    assert called == {"/api/config", "/api/jobs"}


def test_the_suggestions_offer_to_make_each_languages_slides_again(client) -> None:
    """WEB-9, WEB-5: with no button, the request is offered among the suggestions."""
    import json

    script = (web_ui.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    names = re.findall(
        r'"(\w+)"', re.search(r"const SUGGESTIONS = \[([^\]]*)\];", script).group(1)
    )
    catalogs = {
        code: json.loads(client.get(f"/static/i18n/{code}.json").text)
        for code in ("en", "ko", "ja")
    }
    # How each catalog calls the three languages inside a sentence.
    called = {
        "en": {"en": "English", "ko": "Korean", "ja": "Japanese"},
        "ko": {"en": "영어", "ko": "한국어", "ja": "일본어"},
        "ja": {"en": "英語", "ko": "韓国語", "ja": "日本語"},
    }
    seen = set()
    for language in ("en", "ko", "ja"):
        name = f"recreate_slides_{language}"
        # After the three that were there, so an unused row of chips is unchanged.
        assert name in names[3:], name
        for code, texts in catalogs.items():
            text = texts[f"prompt.{name}"]
            # It names its language and no other, and asks for slides and deck.
            assert [key for key, word in called[code].items() if word in text] == [
                language
            ], (code, name)
            assert "Google Slides" in text, (code, name)
            seen.add(text)
    assert len(seen) == 9
    # The English ones are the sentence the instruction gives the agent as an
    # example, which leads to the approval and to no deck by itself
    # (test_a_message_to_the_agent_creates_no_deck).
    from app.prompts import COORDINATOR_INSTRUCTION

    instruction = " ".join(COORDINATOR_INSTRUCTION.split())
    assert catalogs["en"]["prompt.recreate_slides_en"] in instruction
    # A suggestion fills the message box and sends nothing.
    fill = re.search(
        r"\nfunction fillPromptInput\(text\) \{\n(.*?)\n\}\n", script, re.S
    )
    assert "input.value = text" in fill.group(1)
    assert "send" not in fill.group(1).lower() and "fetch" not in fill.group(1)


def test_suggestions_come_from_the_catalogs_and_typed_requests_stay_as_typed(
    client,
) -> None:
    """WEB-9: chips are suggestions; the modal has suggestions and my history."""
    import json

    script = (web_ui.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    names = re.findall(
        r'"(\w+)"', re.search(r"const SUGGESTIONS = \[([^\]]*)\];", script).group(1)
    )
    assert names and len(names) == len(set(names))
    catalogs = {
        code: json.loads(client.get(f"/static/i18n/{code}.json").text)
        for code in ("en", "ko", "ja")
    }
    for code, texts in catalogs.items():
        # A suggestion is a text of the catalog, one per name, in each language.
        assert sorted(key[7:] for key in texts if key.startswith("prompt.")) == sorted(
            names
        ), code

    def body(function: str) -> str:
        found = re.search(
            rf"\nfunction {function}\([^)]*\) \{{\n(.*?)\n\}}\n", script, re.S
        )
        assert found, function
        return found.group(1)

    # Chips: suggestions only, the most used first; nothing a person typed.
    # The row holds three, so unused it shows the first three of the list.
    chips = body("renderPromptChips")
    assert "orderedSuggestions().slice(0, SUGGESTION_CHIPS)" in chips
    assert "getPromptHistory" not in chips
    assert re.search(r"\nconst SUGGESTION_CHIPS = 3;\n", script)
    assert names[:3] == ["shorten_title", "informal_intro", "retranslate"]
    assert "a.at - b.at" in body("orderedSuggestions")
    assert "count(b.name) - count(a.name)" in body("orderedSuggestions")
    # How often a suggestion was sent is counted under its name.
    record = body("recordPromptUsage")
    assert "use[name] = " in record and "SUGGESTION_USE_KEY" in record
    # What a person typed is kept as it was typed: the text, how often and
    # when, and nothing else; an earlier entry is read without its other fields.
    assert "list.push({text: clean, count: 1, lastUsed: Date.now()})" in record
    kept = re.search(r"\.map\(\(item\) => \(\{([^}]*)\}\)\)", body("getPromptHistory"))
    assert re.findall(r"(\w+):", kept.group(1)) == ["text", "count", "lastUsed"]
    # Typed text is shown as it is, never through a text of the catalogs.
    modal = body("renderPromptsModal")
    assert "fillPromptInput(item.text)}, item.text)" in modal

    # The modal: "Suggestions", then "My history (as typed)".
    page = client.get("/").text
    assert (
        page.index('data-i18n="suggestionsLabel">Suggestions<')
        < page.index('id="suggestionPromptsList"')
        < page.index('data-i18n="myHistoryLabel">My history (as typed)<')
        < page.index('id="historyPromptsList"')
    )


def _record(job_id: str) -> dict[str, Any]:
    import json

    from app import runs

    return json.loads((runs.jobs_dir() / f"{job_id}.json").read_text("utf-8"))


def test_listing_runs_reads_records_only(isolated_workspace, monkeypatch) -> None:
    """WEB-14: "Recent sources" is made from the run records and nothing else."""
    import asyncio
    import json

    from app import runs, state
    from app.pipeline import ArticlePipeline

    address = "https://qiita.com/author/items/0123456789abcdef0123"
    draft = "# A draft of mine\n\n" + "It says something worth reading. " * 20
    try:
        post = web_ui._new_job([], False, address)
        mine = web_ui._new_job(["en"], True, draft)
        post.created, mine.created = 1000.0, 2000.0
        mine.save()
        # The kind is known from what was handed in; nothing was read for it.
        assert (post.source_kind, post.source_label) == ("qiita", "Qiita")
        assert (mine.source_kind, mine.source_label) == ("draft", "")
        assert post.title == "" and post.source_chars is None

        # The title and the length reach the record when the source is
        # stored, before the localization has begun.
        class Stop(Exception):
            pass

        async def stored(source_text: str, notes: list[str]) -> None:
            await post.pipeline._update_state(
                {state.SEED_TITLE: "Qiitaの記事", state.SEED_MARKDOWN: "x" * 4321}
            )

        async def localization(text: str) -> None:
            raise Stop

        monkeypatch.setattr(post.pipeline, "_store_source", stored)
        monkeypatch.setattr(post.pipeline, "say", localization)
        with pytest.raises(Stop):
            asyncio.run(post.pipeline.run_posts(address))
        assert _record(post.id)["title"] == "Qiitaの記事"
        assert _record(post.id)["source_chars"] == 4321

        # A record written before these fields were kept, a record without a
        # source, and a file that is no record at all.
        old_id, empty_id = "a" * 32, "b" * 32
        old = {"id": old_id, "session_id": "s", "created": 500.0}
        (runs.jobs_dir() / f"{old_id}.json").write_text(
            json.dumps({**old, "source_text": "https://dev.to/author/a-post"}), "utf-8"
        )
        (runs.jobs_dir() / f"{empty_id}.json").write_text(
            json.dumps({**old, "id": empty_id}), "utf-8"
        )
        (runs.jobs_dir() / f"{'c' * 32}.json").write_text("not a record", "utf-8")

        # A new process holds no run. Listing opens none and reads no session.
        web_ui.JOBS.clear()

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("listing the runs opened a run or read a session")

        monkeypatch.setattr(runs, "reopen", refuse)
        monkeypatch.setattr(runs, "ArticlePipeline", refuse)
        monkeypatch.setattr(ArticlePipeline, "session_state", refuse)
        listed = _browser().get("/api/jobs").json()["jobs"]
        assert not web_ui.JOBS
    finally:
        web_ui.JOBS.clear()

    assert listed == [
        {
            "job_id": mine.id,
            "created": 2000.0,
            "title": "A draft of mine",
            "kind": "draft",
            "label": "",
            "chars": len(draft),
            "address": "",
        },
        {
            "job_id": post.id,
            "created": 1000.0,
            "title": "Qiitaの記事",
            "kind": "qiita",
            "label": "Qiita",
            "chars": 4321,
            "address": address,
        },
        # The older record is listed with what it has.
        {
            "job_id": old_id,
            "created": 500.0,
            "title": "",
            "kind": "devto",
            "label": "DEV Community",
            "chars": None,
            "address": "https://dev.to/author/a-post",
        },
    ]
    # The list carries no source text; the page keeps no list of its own and
    # writes nothing of it into the browser.
    assert "source_text" not in json.dumps(listed)
    script = (web_ui.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for removed in (
        "tba.source_history",
        "tba.source_hidden",
        "SOURCE_HISTORY_KEY",
        "SOURCE_HIDDEN_KEY",
        "recordSourceUsage",
        "saveSourceHistory",
    ):
        assert removed not in script, removed
    render = script.split("function render(data) {", 1)[1].split("\nfunction ", 1)[0]
    assert "localStorage" not in render  # nothing is stored on every poll


def test_a_hidden_run_is_not_listed_and_its_files_remain(isolated_workspace) -> None:
    """WEB-14: hiding takes an entry off the list and leaves everything else."""
    from app import runs

    draft = "# Kept\n\n" + "The text of the article that stays. " * 20
    client = _browser()
    try:
        kept = web_ui._new_job([], False, draft)
        other = web_ui._new_job([], False, draft.replace("Kept", "Other"))
        kept.add_log(">>> a line of the run")
        folder = workspace_dir() / PROJECT_ID
        folder.mkdir(parents=True)
        (folder / "post.en.md").write_text("# Kept\n", "utf-8")
        files = lambda: sorted(  # noqa: E731
            path.name for path in runs.jobs_dir().iterdir() if kept.id in path.name
        )
        before = files()
        assert before == [f"{kept.id}.json", f"{kept.id}.log"]

        # The source is fetched when an entry is picked, as it was handed in.
        assert client.get(f"/api/jobs/{kept.id}/source").json() == {
            "source_text": draft
        }
        assert {
            entry["job_id"] for entry in client.get("/api/jobs").json()["jobs"]
        } == {
            kept.id,
            other.id,
        }
        assert client.post(f"/api/jobs/{kept.id}/hide").json() == {"hidden": True}
        listed = client.get("/api/jobs").json()["jobs"]
        assert [entry["job_id"] for entry in listed] == [other.id]

        # Its record, its log and its article files are where they were, and
        # a new process still opens it by its address and still hides it.
        assert files() == before and _record(kept.id)["hidden"] is True
        assert (folder / "post.en.md").read_text("utf-8") == "# Kept\n"
        web_ui.JOBS.clear()
        assert client.get(f"/api/jobs/{kept.id}").status_code == 200
        assert client.get(f"/api/jobs/{kept.id}/source").json()["source_text"] == draft
        assert web_ui.JOBS[kept.id].hidden is True
        assert [e["job_id"] for e in client.get("/api/jobs").json()["jobs"]] == [
            other.id
        ]
        for unknown in ("0" * 32, "..%2F..%2Fetc"):
            assert client.post(f"/api/jobs/{unknown}/hide").status_code == 404
            assert client.get(f"/api/jobs/{unknown}/source").status_code == 404
    finally:
        web_ui.JOBS.clear()
