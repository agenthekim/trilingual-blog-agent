"""Safety of outbound requests, model output, memories and the API server.

No test here reaches a platform: every HTTP request is answered by an
``httpx.MockTransport`` handler, and credentials are fake values handed out by
a patched secret lookup.
"""

from __future__ import annotations

import asyncio
import runpy
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.adk.events import Event, EventActions
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app import memory, platforms, secrets
from app.plugins import ContentPolicyPlugin

QIITA_TOKEN = "qiita-token-for-tests"
DEVTO_KEY = "devto-key-for-tests"
FAKE_GOOGLE_KEY = "AIza" + "B" * 35
ITEM_ID = "0123456789abcdef0123"

Handler = Callable[[httpx.Request], httpx.Response]


class Wire:
    """Answers requests with a handler and remembers every request it saw."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    @property
    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


@pytest.fixture
def wire(monkeypatch) -> Callable[[Handler], Wire]:
    """Route every client the platform module builds through a mock transport.

    Only the transport is replaced. The redirect setting, headers and timeout
    are the ones the module itself passes, so a client that followed redirects
    on its own would show up as extra requests.
    """
    real_client = httpx.AsyncClient

    def install(handler: Handler) -> Wire:
        seen = Wire(handler)

        def client(**kwargs: Any) -> httpx.AsyncClient:
            return real_client(transport=httpx.MockTransport(seen), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", client)
        return seen

    return install


@pytest.fixture
def credentials(monkeypatch) -> Callable[..., None]:
    """Decide which platform credentials exist, without any real lookup."""

    def configure(*, qiita: bool, devto: bool) -> None:
        stored = {}
        if qiita:
            stored[secrets.QIITA_ACCESS_TOKEN] = QIITA_TOKEN
        if devto:
            stored[secrets.DEVTO_API_KEY] = DEVTO_KEY

        def get_secret(name: str) -> str:
            if name not in stored:
                raise secrets.MissingSecretError(
                    f"Credential {name} is not configured."
                )
            return stored[name]

        monkeypatch.setattr(secrets, "get_secret", get_secret)

    return configure


def redirect(to: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": to})


def qiita_item(**changes: Any) -> dict[str, Any]:
    item = {
        "id": ITEM_ID,
        "url": f"https://qiita.com/someone/items/{ITEM_ID}",
        "title": "Title",
        "body": "# Title\n\nBody.",
        "tags": [{"name": "python", "versions": []}],
    }
    item.update(changes)
    return item


def devto_article(**changes: Any) -> dict[str, Any]:
    article = {
        "id": 42,
        "url": "https://dev.to/someone/title-1a2b",
        "title": "Title",
        "body_markdown": "# Title\n\nBody.",
        "tags": ["python"],
    }
    article.update(changes)
    return article


NAVER_PAGE = (
    '<html><head><meta property="og:title" content="Naver title"></head><body>'
    '<p class="se-text-paragraph">First paragraph of the post.</p></body></html>'
)

# Each call takes no argument and returns the coroutine under test.
Call = Callable[[], Awaitable[Any]]


def qiita_read() -> Awaitable[Any]:
    return platforms.qiita_get_item(ITEM_ID)


def qiita_create() -> Awaitable[Any]:
    return platforms.qiita_save_item(
        title="T", body="B", tags=["python"], private=True, item_id=None
    )


def qiita_update() -> Awaitable[Any]:
    return platforms.qiita_save_item(
        title="T", body="B", tags=["python"], private=True, item_id=ITEM_ID
    )


def devto_read() -> Awaitable[Any]:
    return platforms.devto_get_article("someone", "title-1a2b")


def devto_create() -> Awaitable[Any]:
    return platforms.devto_save_article(
        title="T", body="B", tags=["python"], published=False, article_id=None
    )


def devto_update() -> Awaitable[Any]:
    return platforms.devto_save_article(
        title="T", body="B", tags=["python"], published=False, article_id="42"
    )


def devto_list_own() -> Awaitable[Any]:
    return platforms.devto_find_own_article("42")


def naver_read() -> Awaitable[Any]:
    return platforms.naver_get_post("someone", "223456789012")


def url_check() -> Awaitable[Any]:
    return platforms.fetch_status_and_title("https://m.blog.naver.com/someone/1")


AUTHENTICATED_CALLS = [
    qiita_read,
    qiita_create,
    qiita_update,
    devto_read,
    devto_create,
    devto_update,
    devto_list_own,
]
UNAUTHENTICATED_CALLS = [qiita_read, devto_read, naver_read, url_check]
EVERY_CALL = [*AUTHENTICATED_CALLS, naver_read, url_check]
SAVING_CALLS = [qiita_create, qiita_update, devto_create, devto_update]


def platform_error(call: Call) -> platforms.PlatformError:
    """Run ``call`` and return the PlatformError it must raise.

    Any other exception type fails the test: tools catch PlatformError only.
    """
    with pytest.raises(platforms.PlatformError) as caught:
        asyncio.run(call())
    error = caught.value
    assert error.code and error.message and error.recovery
    return error


def carries_credential(request: httpx.Request) -> bool:
    return "authorization" in request.headers or "api-key" in request.headers


# ---------------------------------------------------------------------------
# SEC-3 / INV-8: redirects
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("call", AUTHENTICATED_CALLS)
@pytest.mark.parametrize(
    "location",
    [
        "https://collector.example/steal",
        "https://qiita.com/api/v2/elsewhere",
        "https://dev.to/api/elsewhere",
    ],
)
def test_authenticated_clients_do_not_follow_redirects(
    call: Call, location: str, wire, credentials
) -> None:
    credentials(qiita=True, devto=True)
    seen = wire(lambda request: redirect(location))

    error = platform_error(call)

    # One request, with the credential, to the API; the redirect target, even
    # one on the same host, never receives anything.
    assert len(seen.requests) == 1
    assert carries_credential(seen.requests[0])
    assert seen.requests[0].url.host in ("qiita.com", "dev.to")
    assert "redirect" in error.message


@pytest.mark.parametrize("call", UNAUTHENTICATED_CALLS)
@pytest.mark.parametrize(
    "outside",
    [
        "https://internal.example/admin",
        "https://169.254.169.254/latest/meta-data",
        "http://qiita.com/plain-http",
        "https://qiita.com:8443/other-port",
        "https://user@dev.to/with-user-name",
        "https://qiita.com.attacker.example/lookalike",
    ],
)
def test_unauthenticated_fetch_rechecks_the_host_on_every_redirect(
    call: Call, outside: str, wire, credentials
) -> None:
    credentials(qiita=False, devto=False)

    def handler(request: httpx.Request) -> httpx.Response:
        # First hop stays on an allowed host, the second one leaves it.
        if request.url.path != "/second-hop":
            return redirect(f"https://{request.url.host}/second-hop")
        return redirect(outside)

    seen = wire(handler)

    error = platform_error(call)

    assert error.code.endswith("_REDIRECT_NOT_ALLOWED")
    assert "not followed" in error.message
    # The allowed hop was followed, the hop outside was never requested.
    assert len(seen.requests) == 2
    assert [request.url.path for request in seen.requests][1] == "/second-hop"
    assert all(request.url.scheme == "https" for request in seen.requests)
    assert not any(carries_credential(request) for request in seen.requests)


def test_unauthenticated_fetch_follows_redirects_between_allowed_hosts(
    wire, credentials
) -> None:
    credentials(qiita=False, devto=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "blog.naver.com":
            return redirect("https://m.blog.naver.com/someone/1")
        if request.url.path == "/someone/1":
            return redirect("/PostView.naver?blogId=someone&logNo=1")
        return httpx.Response(200, text=NAVER_PAGE)

    seen = wire(handler)

    status, title = asyncio.run(
        platforms.fetch_status_and_title("https://blog.naver.com/someone/1")
    )

    assert (status, title) == (200, "Naver title")
    assert seen.hosts == ["blog.naver.com", "m.blog.naver.com", "m.blog.naver.com"]


@pytest.mark.parametrize(
    "url",
    [
        "https://internal.example/admin",
        "http://qiita.com/someone/items/0123456789abcdef0123",
        "https://qiita.com:8443/x",
        "https://[::1",
        "file:///etc/passwd",
        "",
    ],
)
def test_unauthenticated_fetch_refuses_a_first_address_outside_the_allowed_hosts(
    url: str, wire
) -> None:
    seen = wire(lambda request: httpx.Response(200, text="<title>x</title>"))

    error = platform_error(lambda: platforms.fetch_status_and_title(url))

    assert error.code == "URL_NOT_ALLOWED"
    assert seen.requests == []


@pytest.mark.parametrize("call", UNAUTHENTICATED_CALLS)
def test_unauthenticated_fetch_stops_after_three_redirects(
    call: Call, wire, credentials
) -> None:
    credentials(qiita=False, devto=False)
    seen = wire(
        lambda request: redirect(f"https://{request.url.host}/hop-{len(seen.requests)}")
    )

    error = platform_error(call)

    assert error.code.endswith("_TOO_MANY_REDIRECTS")
    # The first request plus three followed redirects, and no fifth request.
    assert len(seen.requests) == 1 + platforms.MAX_PUBLIC_REDIRECTS == 4


def test_unauthenticated_fetch_accepts_exactly_three_redirects(wire) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if len(seen.requests) <= 3:
            return redirect(f"/hop-{len(seen.requests)}")
        return httpx.Response(200, text="<title>Arrived</title>")

    seen = wire(handler)

    assert asyncio.run(url_check()) == (200, "Arrived")
    assert len(seen.requests) == 4


# ---------------------------------------------------------------------------
# D15: only PlatformError leaves the module
# ---------------------------------------------------------------------------
def _raise(error: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


@pytest.mark.parametrize("call", EVERY_CALL)
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectTimeout("no connection in time"),
        httpx.ReadTimeout("no answer in time"),
        httpx.ConnectError("name resolution failed"),
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("server closed the connection"),
    ],
)
def test_network_and_shape_errors_become_platform_errors(
    call: Call, failure: Exception, wire, credentials
) -> None:
    credentials(qiita=True, devto=True)
    wire(_raise(failure))

    error = platform_error(call)

    # The class of the failure is named; its text is not repeated.
    assert str(failure) not in error.message


@pytest.mark.parametrize(
    ("call", "body"),
    [
        (qiita_read, "<html>maintenance</html>"),
        (qiita_read, "[]"),
        (qiita_read, "null"),
        (qiita_read, '{"id": "0123456789abcdef0123", "body": "text"}'),
        (qiita_read, '{"id": "0123456789abcdef0123", "title": "T", "body": null}'),
        (qiita_read, '{"title": "T", "body": "text"}'),
        (qiita_read, '{"id": "x", "title": "T", "body": "b", "tags": null}'),
        (qiita_read, '{"id": "x", "title": "T", "body": "b", "tags": ["python"]}'),
        (devto_read, "not json"),
        (devto_read, '{"id": 42, "title": "T"}'),
        (devto_read, '{"title": "T", "body_markdown": "text"}'),
        (devto_read, '{"id": 42, "title": "T", "body_markdown": "b", "tags": 7}'),
        (devto_list_own, '{"error": "unexpected"}'),
        (devto_list_own, "not json"),
    ],
)
def test_an_unusable_answer_to_a_read_becomes_a_platform_error(
    call: Call, body: str, wire, credentials
) -> None:
    credentials(qiita=True, devto=True)
    wire(lambda request: httpx.Response(200, text=body))

    error = platform_error(call)

    assert error.code.endswith("_BAD_RESPONSE")


def test_usable_answers_are_returned_as_the_platform_sent_them(
    wire, credentials
) -> None:
    credentials(qiita=True, devto=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/articles/me/all"):
            return httpx.Response(200, json=["noise", devto_article()])
        if request.url.host == "dev.to":
            return httpx.Response(200, json=devto_article())
        return httpx.Response(200, json=qiita_item())

    wire(handler)

    assert asyncio.run(qiita_read()) == qiita_item()
    assert asyncio.run(qiita_create()) == qiita_item()
    assert asyncio.run(devto_read()) == devto_article()
    assert asyncio.run(devto_create()) == devto_article()
    assert asyncio.run(devto_list_own()) == devto_article()
    assert asyncio.run(platforms.devto_find_own_article("43")) is None


def test_a_naver_page_is_read_without_any_credential(wire, credentials) -> None:
    credentials(qiita=True, devto=True)
    seen = wire(lambda request: httpx.Response(200, text=NAVER_PAGE))

    title, markdown = asyncio.run(naver_read())

    assert (title, markdown) == ("Naver title", "First paragraph of the post.")
    assert not any(carries_credential(request) for request in seen.requests)


@pytest.mark.parametrize(
    "parse",
    [
        platforms.parse_qiita_item_id,
        platforms.parse_devto_path,
        platforms.parse_naver_post,
    ],
)
@pytest.mark.parametrize("url", ["https://[::1", "", "not a url"])
def test_a_malformed_address_is_a_format_error(parse, url: str) -> None:
    with pytest.raises(platforms.PlatformError) as caught:
        parse(url)
    assert caught.value.code.endswith("_URL_FORMAT")


# ---------------------------------------------------------------------------
# D15: a create whose outcome is not known must not be retried blindly
# ---------------------------------------------------------------------------
def _answer(status: int, body: str) -> Handler:
    return lambda request: httpx.Response(status, text=body)


@pytest.mark.parametrize("call", SAVING_CALLS)
@pytest.mark.parametrize(
    "handler",
    [
        _raise(httpx.ReadTimeout("no answer in time")),
        _raise(httpx.WriteTimeout("body not fully sent")),
        _raise(httpx.ReadError("connection reset")),
        _raise(httpx.RemoteProtocolError("server closed the connection")),
        _answer(201, ""),
        _answer(201, "<html>saved</html>"),
        _answer(200, "{}"),
        _answer(201, '{"id": null, "url": null}'),
        _answer(201, '["unexpected"]'),
        _answer(303, ""),
    ],
    ids=[
        "read-timeout",
        "write-timeout",
        "read-error",
        "protocol-error",
        "empty-body",
        "html-body",
        "no-id",
        "null-id",
        "list-body",
        "redirect",
    ],
)
def test_unknown_outcome_of_a_create_is_reported_as_unknown(
    call: Call, handler: Handler, wire, credentials
) -> None:
    credentials(qiita=True, devto=True)
    seen = wire(handler)

    error = platform_error(call)

    assert error.code.endswith("_OUTCOME_UNKNOWN")
    assert "outcome is unknown" in error.message
    recovery = error.recovery.lower()
    assert "do not retry yet" in recovery and "duplicate" in recovery
    assert "ask the user to check" in recovery
    # Nothing was retried behind the caller's back.
    assert len(seen.requests) == 1


@pytest.mark.parametrize("call", SAVING_CALLS)
@pytest.mark.parametrize(
    "handler",
    [
        _raise(httpx.ConnectError("name resolution failed")),
        _raise(httpx.ConnectTimeout("no connection in time")),
        _answer(401, "{}"),
        _answer(422, '{"message": "tags is invalid"}'),
        _answer(429, ""),
    ],
    ids=["connect-error", "connect-timeout", "unauthorized", "rejected", "rate-limit"],
)
def test_a_create_that_was_not_applied_is_not_reported_as_unknown(
    call: Call, handler: Handler, wire, credentials
) -> None:
    credentials(qiita=True, devto=True)
    wire(handler)

    error = platform_error(call)

    assert not error.code.endswith("_OUTCOME_UNKNOWN")
    assert "unknown" not in error.message


def test_a_qiita_create_without_an_address_is_reported_as_unknown(
    wire, credentials
) -> None:
    credentials(qiita=True, devto=False)
    wire(lambda request: httpx.Response(201, json={"id": ITEM_ID}))

    assert platform_error(qiita_create).code == "QIITA_OUTCOME_UNKNOWN"


def test_saving_without_a_credential_sends_nothing(wire, credentials) -> None:
    credentials(qiita=False, devto=False)
    seen = wire(lambda request: httpx.Response(201, json=qiita_item()))

    assert platform_error(qiita_create).code == "QIITA_TOKEN_MISSING"
    assert platform_error(devto_create).code == "DEVTO_KEY_MISSING"
    assert seen.requests == []


# ---------------------------------------------------------------------------
# LOC-6: the policy plugin leaves article text alone
# ---------------------------------------------------------------------------
ARTICLE_WITH_CREDENTIAL_LOOKALIKES = (
    "## Setup\n\n```bash\nexport API_KEY=YOUR_API_KEY_123\n```\n\n"
    f"The sample key {FAKE_GOOGLE_KEY} is revoked.\n"
)


def _model_said(agent_name: str, text: str) -> LlmResponse | None:
    response = LlmResponse(
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)])
    )
    return asyncio.run(
        ContentPolicyPlugin().after_model_callback(
            callback_context=SimpleNamespace(agent_name=agent_name),
            llm_response=response,
        )
    )


@pytest.mark.parametrize(
    "agent_name",
    [
        "localizer_ko_agent",
        "localizer_en_agent",
        "localizer_ja_agent",
        "refiner_ko_agent",
        "refiner_en_agent",
        "refiner_ja_agent",
        "title_localizer_agent",
        "slide_plan_agent",
        "localization_critic_agent",
        "seed_editor_agent",
    ],
)
def test_article_writers_output_is_not_rewritten_by_the_policy_plugin(
    agent_name: str,
) -> None:
    # None tells the framework to keep the model's response as it is.
    assert _model_said(agent_name, ARTICLE_WITH_CREDENTIAL_LOOKALIKES) is None


@pytest.mark.parametrize("agent_name", ["trilingual_blog_agent", "some_future_agent"])
def test_coordinator_output_is_still_scrubbed(agent_name: str) -> None:
    cleaned = _model_said(agent_name, f"Your key is {FAKE_GOOGLE_KEY}, keep it safe.")

    assert cleaned is not None
    text = cleaned.content.parts[0].text
    assert FAKE_GOOGLE_KEY not in text and "[REDACTED_GOOGLE_API_KEY]" in text
    assert text.startswith("Your key is ") and text.endswith(", keep it safe.")


def test_coordinator_output_without_credentials_is_left_alone() -> None:
    assert _model_said("trilingual_blog_agent", "The Korean post is ready.") is None


# ---------------------------------------------------------------------------
# SEC-4: memories
# ---------------------------------------------------------------------------
COORDINATOR = "trilingual_blog_agent"
ARTICLE = "# 제목\n\n" + "본문 문단입니다. " * 400


def _event(
    author: str, *parts: types.Part, invocation: str = "turn-2", **more: Any
) -> Event:
    role = "user" if author == "user" else "model"
    return Event(
        author=author,
        invocation_id=invocation,
        content=types.Content(role=role, parts=list(parts)),
        **more,
    )


def _text(text: str) -> types.Part:
    return types.Part.from_text(text=text)


def _remembered(events: list[Event], invocation: str = "turn-2") -> list[Event]:
    context = SimpleNamespace(
        invocation_id=invocation, session=SimpleNamespace(events=events)
    )
    return memory._memory_events(context)


def test_memories_keep_only_what_author_and_coordinator_said() -> None:
    events = [
        _event("user", _text("An earlier turn."), invocation="turn-1"),
        _event("user", _text("Always use a formal tone. Mail me at kim@example.com.")),
        _event(
            COORDINATOR,
            _text("I will save the draft."),
            types.Part.from_function_call(
                name="save_seed_article_revision", args={"markdown": ARTICLE}
            ),
        ),
        _event(
            COORDINATOR,
            types.Part.from_function_response(
                name="save_seed_article_revision", response={"markdown": ARTICLE}
            ),
            actions=EventActions(state_delta={"seed_markdown": ARTICLE}),
        ),
        _event("localizer_ko_agent", _text(ARTICLE)),
        _event("localization_critic_agent", _text('{"reviews": []}')),
        _event("seed_editor_agent", _text("Here is the revised seed: " + ARTICLE)),
        _event(COORDINATOR, types.Part(text="private reasoning", thought=True)),
        _event(
            COORDINATOR,
            _text("Done. All three posts are ready."),
            actions=EventActions(state_delta={"localized_markdown_ko": ARTICLE}),
        ),
    ]

    kept = _remembered(events)

    assert [(event.author, event.content.parts[0].text) for event in kept] == [
        ("user", "Always use a formal tone. Mail me at [REDACTED_EMAIL_ADDRESS]."),
        (COORDINATOR, "I will save the draft."),
        (COORDINATOR, "Done. All three posts are ready."),
    ]
    # Text only: no tool call, no tool result and no state change travels along.
    for event in kept:
        assert len(event.content.parts) == 1
        assert not event.get_function_calls() and not event.get_function_responses()
        assert not event.actions.state_delta
    assert "본문" not in "".join(event.model_dump_json() for event in kept)
    # Ids are kept so that the memory service can recognise an event it has.
    assert [event.id for event in kept] == [events[1].id, events[2].id, events[8].id]


def test_a_draft_pasted_into_the_chat_is_cut_before_it_is_remembered() -> None:
    pasted = "Save this draft as the seed article.\n\n" + ARTICLE
    assert len(pasted) > 3 * memory.MAX_MEMORY_TEXT_CHARS

    [kept] = _remembered([_event("user", _text(pasted))])

    text = kept.content.parts[0].text
    assert len(text) <= memory.MAX_MEMORY_TEXT_CHARS + len("...")
    assert text.startswith("Save this draft")


def test_a_credential_at_the_cut_is_redacted_not_halved() -> None:
    lead = "x" * (memory.MAX_MEMORY_TEXT_CHARS - 10)

    [kept] = _remembered([_event("user", _text(f"{lead} {FAKE_GOOGLE_KEY} tail"))])

    assert "AIza" not in kept.content.parts[0].text


def test_only_the_latest_events_of_a_turn_are_remembered() -> None:
    events = [_event("user", _text(f"message {n}")) for n in range(50)]

    kept = _remembered(events)

    assert len(kept) == memory.MAX_MEMORY_EVENTS == 20
    assert kept[0].content.parts[0].text == "message 30"
    assert kept[-1].content.parts[0].text == "message 49"


def test_a_turn_with_nothing_to_remember_schedules_nothing() -> None:
    context = SimpleNamespace(
        invocation_id="turn-2",
        session=SimpleNamespace(events=[_event("localizer_ko_agent", _text(ARTICLE))]),
    )
    before = set(memory._background_tasks)

    asyncio.run(memory.schedule_memory_consolidation(context))

    assert memory._background_tasks == before


def test_the_remembered_coordinator_is_the_root_agent() -> None:
    # The name is repeated in app/memory.py because app/agent.py imports that
    # module; renaming the root agent must not silently empty the memories.
    from app.agent import root_agent

    assert memory.COORDINATOR_AGENT_NAME == root_agent.name


def test_what_is_sent_to_the_memory_service_is_the_filtered_list() -> None:
    sent: list[Event] = []

    async def add_events_to_memory(*, events: list[Event]) -> None:
        sent.extend(events)

    context = SimpleNamespace(
        invocation_id="turn-2",
        session=SimpleNamespace(
            events=[
                _event("user", _text("Prefer short titles.")),
                _event("localizer_ko_agent", _text(ARTICLE)),
            ]
        ),
        add_events_to_memory=add_events_to_memory,
    )

    async def turn() -> None:
        await memory.schedule_memory_consolidation(context)
        await asyncio.gather(*memory._background_tasks)

    asyncio.run(turn())

    assert [event.content.parts[0].text for event in sent] == ["Prefer short titles."]


# ---------------------------------------------------------------------------
# SEC-1: the API server started by hand
# ---------------------------------------------------------------------------
def test_api_server_listens_on_localhost_by_default(monkeypatch) -> None:
    """Run the module the way ``python -m app.fast_api_app`` does, minus the server.

    The pieces with side effects are replaced: reading ``.env``, building the
    ADK application (which opens the local session database) and uvicorn. The
    host is the one the module's own ``__main__`` block passes to uvicorn.
    """
    from fastapi import FastAPI

    started: list[dict[str, Any]] = []
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "google.adk.cli.fast_api.get_fast_api_app", lambda **kwargs: FastAPI()
    )
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: started.append(kwargs))
    monkeypatch.delenv("BLOG_AGENT_API_HOST", raising=False)

    module = runpy.run_module("app.fast_api_app", run_name="__main__")

    assert started == [{"host": "127.0.0.1", "port": 8000}]
    assert module["DEFAULT_HOST"] == "127.0.0.1"
    assert module["listen_host"]() == "127.0.0.1"

    # Listening elsewhere takes an explicit setting.
    monkeypatch.setenv("BLOG_AGENT_API_HOST", "0.0.0.0")
    assert module["listen_host"]() == "0.0.0.0"
    monkeypatch.setenv("BLOG_AGENT_API_HOST", "  ")
    assert module["listen_host"]() == "127.0.0.1"
