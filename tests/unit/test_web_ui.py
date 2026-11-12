"""Web front end: the file preview and the way into the full-page editor."""

from __future__ import annotations

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
    assert 'id="text"' in page.text and "PUT" in page.text
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
        client.post(f"{base}/pdf", json={"language": "en"}),
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

    page = client.get("/").text
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
        r'<option value="[^"]+"(?: selected)?>([A-Za-z-]+)</option>', page
    )
    assert audiences == [
        "Developers",
        "Experts",
        "Students",
        "Executives",
        "Non-technical",
    ]
    lengths = re.findall(r'<option value="(\d+)">', page)
    assert lengths == ["0", "3", "5", "10", "15", "20", "30", "45", "60"]
    # Columns follow the order of the work, with the next step last.
    columns = re.findall(r"<th>([^<]+)</th>", page)
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
    # Bound to another address on purpose (--host): any name is then served.
    monkeypatch.setattr(web_ui, "ALLOWED_HOSTS", None)
    assert other.get("/").status_code == 200


def test_settings_names_the_account_that_decks_go_to(client, monkeypatch) -> None:
    from app import secrets

    monkeypatch.setattr(secrets, "get_secret", lambda name: "configured")
    monkeypatch.setattr(web_ui, "granted_account", lambda: "owner@example.com")
    monkeypatch.setattr(web_ui, "_config_cache", None)
    config = client.get("/api/config").json()
    assert config["google_slides"] is True
    assert config["slides_account"] == "owner@example.com"
    monkeypatch.setattr(web_ui, "_config_cache", None)
    page = client.get("/").text
    assert "a new file in the Google Drive of ${account}" in page


def test_requests_left_unanswered_by_a_new_message_are_said_to_be_gone(client) -> None:
    job = web_ui.JOBS[JOB_ID]
    job.pipeline.approvals = [{"id": "a", "tool": "t", "hint": "Approve?"}]
    client.post(f"/api/jobs/{JOB_ID}/message", json={"text": "do something else"})
    status = _wait(client)
    assert "1 request(s) the agent was waiting on" in status["notice"]
    assert status["reply"] == "I shortened the English title."


def _two_decks_and_a_stand_in_export(job, monkeypatch) -> list[str]:
    """Decks for English and Korean; returns the decks asked to be exported."""
    import asyncio

    from app import google_slides, state

    exported: list[str] = []

    async def fake_export_pdf(presentation_id: str) -> bytes:
        exported.append(presentation_id)
        return b"%PDF-1.4\n%fake-deck-pdf\n"

    monkeypatch.setattr(google_slides, "export_deck_pdf", fake_export_pdf)
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
    return exported


def test_a_deck_is_exported_as_pdf_on_demand_and_the_row_says_when(
    real, monkeypatch
) -> None:
    """SLD-12: asking from the Slides column writes slides.<lang>.pdf."""
    from datetime import datetime

    client, job = real
    exported = _two_decks_and_a_stand_in_export(job, monkeypatch)
    before = _row(client.get(f"/api/jobs/{job.id}").json(), "ko")
    assert before["slides_state"] == "deck"
    assert before["slides_pdf"] is None and before["pdf_exported_at"] is None

    on_demand = client.post(f"/api/jobs/{job.id}/pdf", json={"language": "ko"})
    assert on_demand.status_code == 200
    status = _wait(client, job.id)
    assert status["notice"] is None and exported == ["pres-ko-1"]
    row = _row(status, "ko")
    assert row["slides_pdf"] == "slides.ko.pdf"
    assert datetime.fromisoformat(row["pdf_exported_at"]).tzinfo is not None
    assert row["deck"]["pdf_exported_at"] == row["pdf_exported_at"]
    # The other language's deck has no PDF.
    assert _row(status, "en")["slides_pdf"] is None

    pdf_resp = client.get(f"/api/jobs/{job.id}/files/slides.ko.pdf")
    assert pdf_resp.status_code == 200
    assert pdf_resp.headers["content-type"] == "application/pdf"
    assert pdf_resp.content.startswith(b"%PDF-1.4")


def test_finalizing_a_post_does_not_touch_slides(real, monkeypatch) -> None:
    """RUN-1, SLD-12: finalizing is the main lane's; it writes no slide value."""
    import asyncio

    from app import state
    from app.pipeline import project_file

    client, job = real
    exported = _two_decks_and_a_stand_in_export(job, monkeypatch)
    # English has a PDF a person asked for; Korean has none.
    client.post(f"/api/jobs/{job.id}/pdf", json={"language": "en"})
    _wait(client, job.id)
    assert exported == ["pres-en-1"]

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

    # No deck was exported, and nothing about slides was written.
    assert exported == ["pres-en-1"]
    assert slide_values() == before
    assert project_file(PROJECT_ID, "slides.ko.pdf") is None
    assert _row(status, "ko")["slides_pdf"] is None
    assert _row(status, "en")["slides_pdf"] == "slides.en.pdf"
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
