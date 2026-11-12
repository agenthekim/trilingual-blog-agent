"""HTTP clients for the three blog platforms.

What each platform actually allows (verified against the live services):

* Qiita: full read/write REST API (v2). Posts are created as private drafts.
* DEV Community (dev.to): full read/write REST API (Forem v1). Posts are
  created unpublished.
* Naver Blog: no public write API and no Markdown export. Reading is a
  best-effort extraction from the mobile web page; writing is done by a human
  pasting an exported HTML file.

Every outbound call runs inside a tracing span named after the platform and
operation, and none of them log request or response bodies.

Two rules hold for every request made here:

* A request that carries a credential is never redirected. A redirect would
  hand the credential to whatever address the response names.
* A request without a credential follows at most ``MAX_PUBLIC_REDIRECTS``
  redirects, and every address on the way, the first one included, must be
  https on one of the supported hosts. The allowlist that tools apply to the
  URL a person typed would otherwise stop at the first hop.

The public functions raise ``PlatformError`` for every failure that can be
foreseen: a refused address, an HTTP error status, a timeout or a broken
connection, and a response that is not the JSON the caller needs. Tools turn
that one exception type into a guided error for the agent.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app import secrets
from app.observability import traced_span
from app.settings import ALLOWED_ARTICLE_HOSTS, HTTP_TIMEOUT_SECONDS

QIITA_API = "https://qiita.com/api/v2"
DEVTO_API = "https://dev.to/api"
_USER_AGENT = "trilingual-blog-agent/0.1"


_PLATFORM_NAMES = {
    "qiita": "Qiita",
    "devto": "DEV Community",
    "naver_blog": "Naver Blog",
}


def _name(platform: str) -> str:
    """The name people know a platform by; messages are read by people too."""
    return _PLATFORM_NAMES.get(platform, platform)


class PlatformError(Exception):
    """A platform call failed. ``recovery`` says what can be done about it."""

    def __init__(self, code: str, message: str, recovery: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery = recovery


# Redirects a fetch without a credential may follow before it gives up.
MAX_PUBLIC_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Hosts this module may contact: the blog hosts plus the two API hosts (today
# the APIs live on the blog hosts, so the union adds nothing; it is spelled out
# so that moving an API constant does not silently widen or break the check).
_FETCHABLE_HOSTS = frozenset(ALLOWED_ARTICLE_HOSTS) | {
    httpx.URL(QIITA_API).host,
    httpx.URL(DEVTO_API).host,
}

# Failures that happen before any byte of the request leaves this process, so
# a create or update that hits one of them certainly did not reach the platform.
_NOT_SENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.InvalidURL,
)

_RETRY_ONCE = (
    "Retry once. If it fails again, tell the user the platform cannot be "
    "reached right now and continue with the other platforms."
)


def _client(headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    # Redirects are never followed by httpx itself: requests with a credential
    # must not be redirected at all, and requests without one are followed hop
    # by hop in _get_public so each address can be checked first.
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT, **(headers or {})},
    )


def _allowed_public_url(url: str | httpx.URL) -> httpx.URL | None:
    """Return ``url`` parsed if it is https on a fetchable host, else ``None``.

    The address is parsed with httpx, the library that then connects to it, so
    the host that is checked is the host that is contacted. A port or a user
    name in the address is refused: neither occurs in a real post URL.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        return None
    host = parsed.host.lower().removeprefix("www.")
    if parsed.scheme != "https" or host not in _FETCHABLE_HOSTS:
        return None
    if parsed.port is not None or parsed.userinfo:
        return None
    return parsed


def _platform_of(url: httpx.URL) -> str:
    return ALLOWED_ARTICLE_HOSTS.get(url.host.lower().removeprefix("www."), "url")


def _url_not_allowed() -> PlatformError:
    allowed = ", ".join(sorted(_FETCHABLE_HOSTS))
    return PlatformError(
        "URL_NOT_ALLOWED",
        "The address is not an https address on a supported blog host, so it "
        "was not fetched.",
        f"Only https URLs on these hosts may be fetched: {allowed}. Ask the user "
        "for a supported URL; do not retry with the same one.",
    )


def _redirect_not_allowed(platform: str, location: str) -> PlatformError:
    try:
        target = httpx.URL(location)
        where = f"{target.scheme}://{target.host}"[:120]
    except (httpx.InvalidURL, TypeError):
        where = "an address that could not be read"
    return PlatformError(
        f"{platform.upper()}_REDIRECT_NOT_ALLOWED",
        f"{_name(platform)} redirected the request to {where}, which is not an https "
        "address on a supported blog host. The redirect was not followed.",
        "Do not retry with the same URL. Tell the user the page redirects "
        "elsewhere (a moved, private or login-only post does this) and ask for "
        "the current public URL of the post.",
    )


def _unknown_outcome(platform: str, what_happened: str) -> PlatformError:
    """The error for a create or update that may or may not have been applied."""
    return PlatformError(
        f"{platform.upper()}_OUTCOME_UNKNOWN",
        f"The outcome is unknown: {what_happened}, so {_name(platform)} may or may not "
        "have saved the article.",
        "Do not retry yet: a retry could create a duplicate. Ask the user to "
        "check their drafts and posts on the platform. Retry only after the user "
        "confirms the article is not there; if it is there, do not send it again.",
    )


def _transport_error(
    platform: str, exc: Exception, *, saving: bool = False
) -> PlatformError:
    """Turn an httpx failure into a guided error.

    Only the exception class is reported: the exception text can repeat the
    request address and adds nothing the agent can act on.
    """
    name = type(exc).__name__
    if saving and not isinstance(exc, _NOT_SENT_ERRORS):
        return _unknown_outcome(
            platform, f"the connection failed once the request was on its way ({name})"
        )
    not_sent = " Nothing was sent." if saving else ""
    if isinstance(exc, httpx.TimeoutException):
        return PlatformError(
            f"{platform.upper()}_TIMEOUT",
            f"{_name(platform)} did not answer within {HTTP_TIMEOUT_SECONDS:g} seconds "
            f"({name}).{not_sent}",
            _RETRY_ONCE,
        )
    return PlatformError(
        f"{platform.upper()}_NETWORK_ERROR",
        f"{_name(platform)} could not be reached ({name}).{not_sent}",
        _RETRY_ONCE,
    )


async def _get_public(
    url: str | httpx.URL, *, accept: str | None = None
) -> httpx.Response:
    """GET without a credential, following redirects by hand.

    There is deliberately no way to pass arbitrary headers: whatever is sent
    here is also sent to every address a redirect names.
    """
    target = _allowed_public_url(url)
    if target is None:
        raise _url_not_allowed()
    platform = _platform_of(target)
    try:
        async with _client({"Accept": accept} if accept else None) as client:
            for followed in range(MAX_PUBLIC_REDIRECTS + 1):
                response = await client.get(target)
                location = response.headers.get("location")
                if response.status_code not in _REDIRECT_STATUSES or not location:
                    return response
                if followed == MAX_PUBLIC_REDIRECTS:
                    break
                try:
                    target = _allowed_public_url(response.url.join(location))
                except httpx.InvalidURL:
                    target = None
                if target is None:
                    raise _redirect_not_allowed(platform, location)
    except httpx.HTTPError as exc:
        raise _transport_error(platform, exc) from exc
    raise PlatformError(
        f"{platform.upper()}_TOO_MANY_REDIRECTS",
        f"{_name(platform)} redirected the request more than {MAX_PUBLIC_REDIRECTS} "
        "times; it was given up.",
        "Do not retry with the same URL. Ask the user for the current public "
        "URL of the post.",
    )


async def _send_with_credential(
    platform: str,
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    json: Any = None,
    params: dict[str, Any] | None = None,
    saving: bool = False,
) -> httpx.Response:
    """Send one request that carries a credential; a redirect is an error.

    ``saving`` marks a create or update, where a failure after the request
    left must be reported as an unknown outcome rather than as a plain error.
    """
    try:
        async with _client(headers) as client:
            response = await client.request(method, url, json=json, params=params)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _transport_error(platform, exc, saving=saving) from exc
    if saving and response.status_code >= 500:
        # A server error says nothing about whether the write went through.
        raise _unknown_outcome(
            platform, f"{_name(platform)} answered HTTP {response.status_code}"
        )
    if 300 <= response.status_code < 400:
        answered = (
            f"{_name(platform)} answered with a redirect (HTTP {response.status_code}), "
            "which is never followed for a request that carries a credential"
        )
        if saving:
            raise _unknown_outcome(platform, answered)
        raise PlatformError(
            f"{platform.upper()}_REDIRECT_NOT_FOLLOWED",
            f"{answered}.",
            "Do not retry. Report this to the user: the address of the "
            "platform's API may have changed.",
        )
    return response


def _bad_response(platform: str, status: int, problem: str) -> PlatformError:
    return PlatformError(
        f"{platform.upper()}_BAD_RESPONSE",
        f"{_name(platform)} answered HTTP {status} but {problem}.",
        "Retry once. If it happens again, tell the user the platform's answer "
        "could not be read; do not guess the missing content.",
    )


def _is_identifier(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, str) and bool(value.strip()))


def _record_problem(
    response: httpx.Response, identifiers: tuple[str, ...], texts: tuple[str, ...]
) -> tuple[Any, str | None]:
    """Return (parsed body, what is wrong with it or ``None``)."""
    try:
        data = response.json()
    except ValueError:
        return None, "the response was not JSON"
    if not isinstance(data, dict):
        return data, "the response was not a JSON object"
    for name in identifiers:
        if not _is_identifier(data.get(name)):
            return data, f"the response had no usable '{name}'"
    for name in texts:
        if not isinstance(data.get(name), str):
            return data, f"the response had no usable '{name}'"
    return data, None


def _json_record(
    platform: str,
    response: httpx.Response,
    *,
    identifiers: tuple[str, ...] = (),
    texts: tuple[str, ...] = (),
    saving: bool = False,
) -> dict[str, Any]:
    """Return the JSON object of ``response`` after checking what callers read.

    ``identifiers`` must be non-empty strings or numbers and ``texts`` must be
    strings. Checking here keeps a changed or truncated payload from surfacing
    as a ``KeyError`` in a tool, and, for a create, from being mistaken for a
    failure that is safe to retry: the platform answered with a success status,
    so the article was most likely saved.
    """
    data, problem = _record_problem(response, identifiers, texts)
    if problem is None:
        return data
    status = response.status_code
    if saving:
        raise _unknown_outcome(
            platform, f"{_name(platform)} answered HTTP {status} but {problem}"
        )
    raise _bad_response(platform, status, problem)


def _raise_for_status(platform: str, response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    status = response.status_code
    if status in (401, 403):
        raise PlatformError(
            f"{platform.upper()}_UNAUTHORIZED",
            f"{_name(platform)} rejected the credential (HTTP {status}).",
            "Tell the user the stored credential is invalid or lacks write scope; "
            "do not retry.",
        )
    if status == 404:
        raise PlatformError(
            f"{platform.upper()}_NOT_FOUND",
            f"{_name(platform)} returned HTTP 404 for the requested article.",
            "Check the URL or article ID with the user.",
        )
    if status == 429:
        raise PlatformError(
            f"{platform.upper()}_RATE_LIMITED",
            f"{_name(platform)} rate limit reached (HTTP 429).",
            "Wait before retrying; tell the user the platform is rate limiting.",
        )
    detail = response.text[:300].replace("\n", " ")
    raise PlatformError(
        f"{platform.upper()}_HTTP_{status}",
        f"{_name(platform)} returned HTTP {status}: {detail}",
        "If the message names an invalid field (for example tags or title), fix it "
        "and retry once; otherwise report the error to the user.",
    )


def guess_language(text: str) -> str:
    """Classify text as ko, ja or en from the scripts it uses."""
    sample = text[:4000]
    hangul = len(re.findall(r"[\uac00-\ud7a3]", sample))
    kana = len(re.findall(r"[\u3040-\u30ff]", sample))
    if kana > 20 and kana >= hangul:
        return "ja"
    if hangul > 20:
        return "ko"
    return "en"


# ---------------------------------------------------------------------------
# Qiita
# ---------------------------------------------------------------------------
def _split_url(url: str) -> tuple[str, str]:
    """Return (path, query) of ``url``; both empty when it cannot be parsed."""
    try:
        parsed = urlparse(url)
        return parsed.path, parsed.query
    except (ValueError, TypeError, AttributeError):
        return "", ""


def parse_qiita_item_id(url: str) -> str:
    match = re.search(r"/(?:items|private)/([0-9a-f]{20})", _split_url(url)[0])
    if not match:
        raise PlatformError(
            "QIITA_URL_FORMAT",
            "Not a Qiita article URL.",
            "Ask for a URL of the form https://qiita.com/<user>/items/<id>.",
        )
    return match.group(1)


def _qiita_headers(required: bool) -> dict[str, str]:
    try:
        return {
            "Authorization": f"Bearer {secrets.get_secret(secrets.QIITA_ACCESS_TOKEN)}"
        }
    except secrets.MissingSecretError as exc:
        if required:
            raise PlatformError(
                "QIITA_TOKEN_MISSING",
                str(exc),
                "Tell the user to store a Qiita access token with read_qiita and "
                "write_qiita scopes; do not retry until they confirm.",
            ) from exc
        return {}


async def qiita_get_item(item_id: str) -> dict[str, Any]:
    """Read one Qiita item; the result has ``id``, ``title``, ``body``, ``tags``."""
    url = f"{QIITA_API}/items/{item_id}"
    headers = _qiita_headers(required=False)
    with traced_span("qiita.get_item", item_id=item_id):
        if headers:
            response = await _send_with_credential("qiita", "GET", url, headers)
        else:
            response = await _get_public(url)
    _raise_for_status("qiita", response)
    item = _json_record("qiita", response, identifiers=("id",), texts=("title", "body"))
    tags = item.get("tags", [])
    if not isinstance(tags, list) or not all(
        isinstance(tag, dict) and isinstance(tag.get("name"), str) for tag in tags
    ):
        raise _bad_response(
            "qiita", response.status_code, "the response had no usable 'tags'"
        )
    return item


async def qiita_save_item(
    *, title: str, body: str, tags: list[str], private: bool, item_id: str | None
) -> dict[str, Any]:
    """Create a Qiita item, or update it when ``item_id`` is given."""
    payload = {
        "title": title,
        "body": body,
        "tags": [{"name": tag, "versions": []} for tag in tags],
        "private": private,
        "tweet": False,
    }
    operation = "qiita.update_item" if item_id else "qiita.create_item"
    headers = _qiita_headers(required=True)
    if item_id:
        payload.pop("tweet")
        method, url = "PATCH", f"{QIITA_API}/items/{item_id}"
    else:
        method, url = "POST", f"{QIITA_API}/items"
    with traced_span(operation, private=private, body_chars=len(body)):
        response = await _send_with_credential(
            "qiita", method, url, headers, json=payload, saving=True
        )
    _raise_for_status("qiita", response)
    return _json_record("qiita", response, identifiers=("id", "url"), saving=True)


# ---------------------------------------------------------------------------
# DEV Community
# ---------------------------------------------------------------------------
_DEVTO_ACCEPT = "application/vnd.forem.api-v1+json"


def _devto_headers(required: bool) -> dict[str, str]:
    headers = {"Accept": _DEVTO_ACCEPT}
    try:
        headers["api-key"] = secrets.get_secret(secrets.DEVTO_API_KEY)
    except secrets.MissingSecretError as exc:
        if required:
            raise PlatformError(
                "DEVTO_KEY_MISSING",
                str(exc),
                "Tell the user to store a DEV Community API key; do not retry "
                "until they confirm.",
            ) from exc
    return headers


def parse_devto_path(url: str) -> tuple[str, str]:
    parts = [p for p in _split_url(url)[0].split("/") if p]
    if len(parts) != 2:
        raise PlatformError(
            "DEVTO_URL_FORMAT",
            "Not a DEV Community article URL.",
            "Ask for a URL of the form https://dev.to/<username>/<slug>.",
        )
    return parts[0], parts[1]


async def devto_get_article(username: str, slug: str) -> dict[str, Any]:
    url = f"{DEVTO_API}/articles/{username}/{slug}"
    headers = _devto_headers(required=False)
    with traced_span("devto.get_article", slug=slug):
        if "api-key" in headers:
            response = await _send_with_credential("devto", "GET", url, headers)
        else:
            response = await _get_public(url, accept=_DEVTO_ACCEPT)
    _raise_for_status("devto", response)
    article = _json_record(
        "devto", response, identifiers=("id",), texts=("title", "body_markdown")
    )
    tags = article.get("tags")
    if tags is not None and not isinstance(tags, (list, str)):
        raise _bad_response(
            "devto", response.status_code, "the response had no usable 'tags'"
        )
    return article


async def devto_save_article(
    *, title: str, body: str, tags: list[str], published: bool, article_id: str | None
) -> dict[str, Any]:
    """Create a DEV article, or update it when ``article_id`` is given."""
    payload = {
        "article": {
            "title": title,
            "body_markdown": body,
            "published": published,
            "tags": tags,
        }
    }
    operation = "devto.update_article" if article_id else "devto.create_article"
    headers = _devto_headers(required=True)
    if article_id:
        method, url = "PUT", f"{DEVTO_API}/articles/{article_id}"
    else:
        method, url = "POST", f"{DEVTO_API}/articles"
    with traced_span(operation, published=published, body_chars=len(body)):
        response = await _send_with_credential(
            "devto", method, url, headers, json=payload, saving=True
        )
    _raise_for_status("devto", response)
    return _json_record("devto", response, identifiers=("id",), saving=True)


async def devto_find_own_article(article_id: str) -> dict[str, Any] | None:
    """Look an article up among the key owner's posts, including unpublished drafts."""
    headers = _devto_headers(required=True)
    with traced_span("devto.list_own_articles"):
        response = await _send_with_credential(
            "devto",
            "GET",
            f"{DEVTO_API}/articles/me/all",
            headers,
            params={"per_page": 100},
        )
    _raise_for_status("devto", response)
    try:
        articles = response.json()
    except ValueError:
        articles = None
    if not isinstance(articles, list):
        raise _bad_response(
            "devto",
            response.status_code,
            "the response was not a JSON list of articles",
        )
    for article in articles:
        if isinstance(article, dict) and str(article.get("id")) == str(article_id):
            return article
    return None


# ---------------------------------------------------------------------------
# Naver Blog (read-only, best effort)
# ---------------------------------------------------------------------------
def parse_naver_post(url: str) -> tuple[str, str]:
    path, raw_query = _split_url(url)
    query = parse_qs(raw_query)
    if query.get("blogId") and query.get("logNo"):
        return query["blogId"][0], query["logNo"][0]
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[-1].isdigit():
        return parts[-2], parts[-1]
    raise PlatformError(
        "NAVER_URL_FORMAT",
        "Not a Naver Blog post URL.",
        "Ask for a URL of the form https://blog.naver.com/<blogId>/<postNumber>.",
    )


class _NaverPostParser(HTMLParser):
    """Collect title, paragraphs and code from a Naver SmartEditor mobile page."""

    _BLOCK_CLASSES = ("se-text-paragraph", "se-code-source", "se-section-title")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.blocks: list[tuple[str, str]] = []
        self._stack: list[tuple[str, str | None]] = []
        self._buffer: list[str] = []
        self._kind: str | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") == "og:title":
            self.title = attributes.get("content") or ""
        if tag in ("br",) and self._kind:
            self._buffer.append("\n")
            return
        if tag in ("meta", "img", "input", "link", "hr"):
            return
        classes = attributes.get("class") or ""
        if self._kind is None:
            for name in self._BLOCK_CLASSES:
                if name in classes:
                    self._kind, self._depth, self._buffer = name, 0, []
                    break
        elif self._kind:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._kind is None or tag in ("br", "meta", "img", "input", "link", "hr"):
            return
        if self._depth == 0:
            text = "".join(self._buffer).strip("\n")
            if text.strip():
                self.blocks.append((self._kind, text))
            self._kind = None
        else:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._kind:
            self._buffer.append(data)


def naver_html_to_markdown(html: str) -> tuple[str, str]:
    """Return (title, markdown) reconstructed from a Naver mobile post page."""
    parser = _NaverPostParser()
    parser.feed(html)
    lines: list[str] = []
    for kind, text in parser.blocks:
        if kind == "se-code-source":
            lines.append(f"```\n{text}\n```")
        elif kind == "se-section-title":
            lines.append(f"## {text.strip()}")
        else:
            lines.append(text.replace("\u200b", "").strip())
    markdown = "\n\n".join(line for line in lines if line)
    return parser.title.strip(), markdown


async def naver_get_post(blog_id: str, log_no: str) -> tuple[str, str]:
    url = f"https://m.blog.naver.com/{blog_id}/{log_no}"
    with traced_span("naver.get_post", log_no=log_no):
        response = await _get_public(url)
    _raise_for_status("naver_blog", response)
    title, markdown = naver_html_to_markdown(response.text)
    if not markdown:
        raise PlatformError(
            "NAVER_EXTRACTION_EMPTY",
            "The post page was fetched but no article text could be extracted.",
            "Ask the user to paste the article text or provide the original .md file.",
        )
    return title, markdown


async def fetch_status_and_title(url: str) -> tuple[int, str | None]:
    """GET a public page and return its HTTP status and <title> / og:title.

    Only https addresses on the supported blog hosts are fetched; anything
    else, and any network failure, raises ``PlatformError``.
    """
    target = _allowed_public_url(url)
    if target is None:
        raise _url_not_allowed()
    with traced_span("http.verify_url", host=target.host):
        response = await _get_public(target)
    title = None
    match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        response.text,
    ) or re.search(r"<title[^>]*>([^<]+)</title>", response.text)
    if match:
        title = match.group(1).strip()
    return response.status_code, title


_GDOCS_PATH = re.compile(r"^/document/(?:u/\d+/)?d/([A-Za-z0-9_-]{10,})(?:/.*)?$")


def parse_google_doc_id(url: str) -> str:
    """Return the document id of an https Google Docs document URL, or raise PlatformError."""
    try:
        parsed = httpx.URL(url.strip())
    except (httpx.InvalidURL, TypeError) as exc:
        raise PlatformError(
            "GDOCS_URL_FORMAT",
            "Not a valid Google Docs URL.",
            "Ask for an https://docs.google.com/document/d/<id>/... URL.",
        ) from exc
    host = parsed.host.lower()
    match = _GDOCS_PATH.match(parsed.path)
    if (
        parsed.scheme != "https"
        or host != "docs.google.com"
        or parsed.port is not None
        or parsed.userinfo
        or not match
    ):
        raise PlatformError(
            "GDOCS_URL_FORMAT",
            "Not a Google Docs document URL.",
            "Ask for an https://docs.google.com/document/d/<id>/... URL.",
        )
    return match.group(1)


def is_google_doc_url(url: str) -> bool:
    """Whether ``url`` is an https Google Docs document URL."""
    try:
        parse_google_doc_id(url)
        return True
    except PlatformError:
        return False


def _allowed_gdocs_export_url(url: str | httpx.URL) -> httpx.URL | None:
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        return None
    host = parsed.host.lower()
    if parsed.scheme != "https" or parsed.port is not None or parsed.userinfo:
        return None
    if host == "docs.google.com" or host.endswith(".googleusercontent.com"):
        return parsed
    return None


async def google_doc_get_markdown(doc_id: str) -> str:
    """Export a link-shared Google Docs document as Markdown."""
    current = httpx.URL(f"https://docs.google.com/document/d/{doc_id}/export?format=md")
    with traced_span("gdocs.export_markdown", doc_id=doc_id):
        async with _client() as client:
            for _hop in range(MAX_PUBLIC_REDIRECTS + 1):
                try:
                    response = await client.get(current)
                except Exception as exc:
                    raise _transport_error("google_docs", exc) from exc
                if response.status_code not in _REDIRECT_STATUSES:
                    break
                location = response.headers.get("location") or ""
                next_url = _allowed_gdocs_export_url(current.join(location))
                if next_url is None:
                    raise PlatformError(
                        "GDOCS_NOT_READABLE",
                        "This Google Docs document requires sign-in. Share it as "
                        "'Anyone with the link can view', or download it as "
                        "Markdown (.md) in Google Docs and attach the file.",
                        "Ask the user to share the Google Doc as 'Anyone with "
                        "the link can view' or provide a .md or .txt file.",
                    )
                current = next_url
            else:
                raise PlatformError(
                    "GDOCS_TOO_MANY_REDIRECTS",
                    f"Google Docs redirected more than {MAX_PUBLIC_REDIRECTS} times.",
                    "Ask the user to download the document as Markdown (.md) and attach it.",
                )
    content_type = (response.headers.get("content-type") or "").lower()
    if response.status_code >= 400 or "text/html" in content_type:
        raise PlatformError(
            "GDOCS_NOT_READABLE",
            "This Google Docs document could not be read. Make sure it exists "
            "and is shared as 'Anyone with the link can view', or download it "
            "as Markdown (.md) and attach the file.",
            "Ask the user to share the Google Doc as 'Anyone with the link can "
            "view' or provide a .md or .txt file.",
        )
    text = response.text.lstrip("\ufeff")
    if not text.strip():
        raise PlatformError(
            "GDOCS_EMPTY",
            "The Google Docs document is empty.",
            "Ask the user for a document with article text.",
        )
    return text


_MEDIUM_PATH = re.compile(r"^/(@[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")


def parse_medium_post(url: str) -> tuple[str, str]:
    """Return (author_or_pub, slug) of an https Medium post URL, or raise PlatformError."""
    try:
        parsed = httpx.URL(url.strip())
    except (httpx.InvalidURL, TypeError) as exc:
        raise PlatformError(
            "MEDIUM_URL_FORMAT",
            "Not a valid Medium URL.",
            "Ask for an https://medium.com/@author/slug URL.",
        ) from exc
    host = parsed.host.lower().removeprefix("www.")
    match = _MEDIUM_PATH.match(parsed.path.rstrip("/"))
    if (
        parsed.scheme != "https"
        or host != "medium.com"
        or parsed.port is not None
        or parsed.userinfo
        or not match
    ):
        raise PlatformError(
            "MEDIUM_URL_FORMAT",
            "Not a Medium article URL.",
            "Ask for an https://medium.com/@author/slug URL.",
        )
    return match.group(1), match.group(2)


def is_medium_url(url: str) -> bool:
    """Whether ``url`` is an https Medium post URL."""
    try:
        parse_medium_post(url)
        return True
    except PlatformError:
        return False


class _MediumHtmlToMarkdown(HTMLParser):
    """Convert Medium RSS <content:encoded> HTML into clean Markdown."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._in_pre = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4"):
            level = int(tag[1])
            self.out.append(f"\n\n{'#' * max(2, level)} ")
        elif tag in ("p", "blockquote"):
            self.out.append("\n\n" + ("> " if tag == "blockquote" else ""))
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "pre":
            self._in_pre = True
            self.out.append("\n\n```\n")
        elif tag == "code" and not self._in_pre:
            self.out.append("`")
        elif tag == "a" and d.get("href"):
            self._href = d["href"]
            self._link_text = []
        elif tag == "img" and d.get("src"):
            src = d["src"]
            if "medium.com/_/stat" not in src:
                alt = (d.get("alt") or "image").strip()
                self.out.append(f"\n\n![{alt}]({src})\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self._in_pre = False
            self.out.append("\n```\n\n")
        elif tag == "code" and not self._in_pre:
            self.out.append("`")
        elif tag == "a" and self._href:
            txt = "".join(self._link_text).strip() or self._href
            self.out.append(f"[{txt}]({self._href})")
            self._href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._link_text.append(data)
        else:
            self.out.append(data)


async def medium_get_markdown(url: str) -> str:
    """Fetch a Medium post from its RSS feed and convert it to Markdown."""
    import xml.etree.ElementTree as ET

    author_or_pub, slug = parse_medium_post(url)
    post_id = slug.rsplit("-", 1)[-1] if "-" in slug else slug
    feed_url = f"https://medium.com/feed/{author_or_pub}"
    with traced_span("medium.fetch_rss", author=author_or_pub, post_id=post_id):
        try:
            async with _client() as client:
                response = await client.get(feed_url)
        except Exception as exc:
            raise _transport_error("medium", exc) from exc
    if response.status_code >= 400:
        raise PlatformError(
            "MEDIUM_NOT_READABLE",
            f"Medium feed returned HTTP {response.status_code}.",
            "Ask the user to paste the article text or attach a .md file.",
        )
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise PlatformError(
            "MEDIUM_BAD_FEED",
            "Could not parse the Medium RSS feed.",
            "Ask the user to paste the article text or attach a .md file.",
        ) from exc
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    for item in root.findall("./channel/item"):
        link = (item.findtext("link") or "") + " " + (item.findtext("guid") or "")
        if post_id in link or slug in link:
            title = (item.findtext("title") or "").strip() or "Untitled"
            encoded = item.findtext("content:encoded", default="", namespaces=ns) or (
                item.findtext("description") or ""
            )
            parser = _MediumHtmlToMarkdown()
            parser.feed(encoded)
            body = re.sub(r"\n{3,}", "\n\n", "".join(parser.out).strip())
            if not body:
                break
            return f"# {title}\n\n{body}\n"
    raise PlatformError(
        "MEDIUM_POST_NOT_IN_FEED",
        "This Medium post was not found in the author's recent RSS feed.",
        "Copy the article text into the Source document box or attach a .md file.",
    )
