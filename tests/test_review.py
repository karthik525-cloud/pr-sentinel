from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.github import GitHubClient, create_http_client
from app.main import create_app

URL = "https://github.com/octocat/Hello-World/pull/1"


def metadata(count: int = 1) -> dict[str, Any]:
    return {
        "number": 1,
        "title": "Fix bug",
        "body": None,
        "base": {"sha": "a" * 40, "repo": {"private": False}},
        "head": {"sha": "b" * 40},
        "changed_files": count,
    }


def changed_file(name: str = "app.py", patch: str = "@@ -1 +1 @@\n-old\n+new") -> dict[str, Any]:
    return {"filename": name, "status": "modified", "additions": 1, "deletions": 1, "patch": patch}


@contextmanager
def api(handler: Callable, **settings):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # TestClient drives the lifespan; the caller owns the injected HTTP client.
    with TestClient(create_app(Settings(**settings), upstream)) as client:
        yield client
    import asyncio

    asyncio.run(upstream.aclose())


def success(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json=[changed_file()] if request.url.path.endswith("/files") else metadata()
    )


def test_health():
    with api(success) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_valid_pr():
    with api(success) as client:
        response = client.post("/review", json={"pr_url": URL + "/"})
    assert response.status_code == 200
    data = response.json()
    assert (data["owner"], data["repository"], data["number"]) == ("octocat", "Hello-World", 1)
    assert data["title"] == "Fix bug"
    assert data["description"] is None
    assert data["base_sha"] == "a" * 40
    assert data["head_sha"] == "b" * 40
    assert data["files"][0] == {
        "path": "app.py",
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "patch": changed_file()["patch"],
        "patch_available": True,
        "previous_path": None,
    }
    assert data["total_diff_bytes"] == len(changed_file()["patch"].encode())


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b/pull/1",
        "https://evil.com/a/b/pull/1",
        "https://github.com.evil.com/a/b/pull/1",
        "https://github.com@evil.com/a/b/pull/1",
        "https://user@github.com/a/b/pull/1",
        "https://github.com:443/a/b/pull/1",
        "https://github.com/a/b",
        "https://github.com/a/b/issues/1",
        "https://github.com/a/b/pull/0",
        "https://github.com/a/b/pull/-1",
        "https://github.com/a/b/pull/1/files",
        "https://github.com/a/b/pull/1?x=1",
        "https://github.com/a/b/pull/1#frag",
        "https://github.com/a/../pull/1",
        "https://github.com/a/%2e%2e/pull/1",
        " https://github.com/a/b/pull/1",
        "https://github.com/a/b/pull/1\n",
        "https://127.0.0.1/a/b/pull/1",
        "https://github.com/a/b/pull/999999999999999999",
        42,
        None,
    ],
)
def test_invalid_url_never_fetches(url):
    def handler(request):
        pytest.fail("Invalid URL caused an upstream request")

    with api(handler) as client:
        assert client.post("/review", json={"pr_url": url}).status_code == 422


@pytest.mark.parametrize(
    "status,headers,expected,code",
    [
        (404, {}, 404, "pull_request_not_found"),
        (429, {}, 429, "github_rate_limited"),
        (403, {"x-ratelimit-remaining": "0"}, 429, "github_rate_limited"),
        (403, {"retry-after": "60"}, 429, "github_rate_limited"),
        (403, {}, 403, "github_access_denied"),
        (401, {}, 502, "github_authentication_failed"),
        (500, {}, 502, "github_upstream_failure"),
        (302, {"location": "https://evil.com/steal"}, 502, "github_upstream_failure"),
    ],
)
def test_http_errors(status, headers, expected, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers, text="secret upstream details")

    with api(handler) as client:
        response = client.post("/review", json={"pr_url": URL})
    assert response.status_code == expected
    assert response.json()["error"]["code"] == code
    assert len(requests) == 1
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "exception,expected", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)]
)
def test_transport_errors(exception, expected):
    def handler(request):
        raise exception("secret", request=request)

    with api(handler) as client:
        response = client.post("/review", json={"pr_url": URL})
    assert response.status_code == expected
    assert "secret" not in response.text


def test_pagination_uses_only_approved_urls():
    pages = []

    def handler(request):
        assert request.url.scheme == "https"
        assert request.url.host == "api.github.com"
        assert request.url.path.startswith("/repos/octocat/Hello-World/pulls/1")
        if request.url.path.endswith("/files"):
            page = int(request.url.params["page"])
            pages.append(page)
            assert request.url.params["per_page"] == "100"
            start, end = (0, 100) if page == 1 else (100, 101)
            return httpx.Response(
                200,
                json=[changed_file(str(i)) for i in range(start, end)],
                headers={"Link": '<https://evil.com/>; rel="next"'},
            )
        return httpx.Response(200, json=metadata(101))

    with api(handler) as client:
        response = client.post("/review", json={"pr_url": URL})
    assert response.status_code == 200
    assert len(response.json()["files"]) == 101
    assert pages == [1, 2]


def test_missing_patch_and_rename():
    def handler(request):
        file = changed_file()
        del file["patch"]
        file.update(status="renamed", previous_filename="old.py")
        return httpx.Response(
            200, json=[file] if request.url.path.endswith("/files") else metadata()
        )

    with api(handler) as client:
        response = client.post("/review", json={"pr_url": URL})
    assert response.status_code == 200
    file = response.json()["files"][0]
    assert file["patch"] is None and file["patch_available"] is False
    assert file["previous_path"] == "old.py"
    assert response.json()["total_diff_bytes"] == 0


def test_diff_byte_limit():
    def handler(request):
        return httpx.Response(
            200,
            json=[changed_file(patch="éé")] if request.url.path.endswith("/files") else metadata(),
        )

    with api(handler, max_diff_bytes=3) as client:
        response = client.post("/review", json={"pr_url": URL})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "diff_too_large"


def test_diff_limit_is_cumulative_and_inclusive():
    def handler(request):
        return httpx.Response(
            200,
            json=[changed_file("a", "ab"), changed_file("b", "cd")]
            if request.url.path.endswith("/files")
            else metadata(2),
        )

    for limit, expected in [(3, 413), (4, 200)]:
        with api(handler, max_diff_bytes=limit) as client:
            assert client.post("/review", json={"pr_url": URL}).status_code == expected


def test_changed_file_limit_before_files_fetch():
    def handler(request):
        assert not request.url.path.endswith("/files")
        return httpx.Response(200, json=metadata(501))

    with api(handler) as client:
        assert client.post("/review", json={"pr_url": URL}).status_code == 413


def test_response_byte_limit():
    with api(success, max_upstream_bytes=10) as client:
        response = client.post("/review", json={"pr_url": URL})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upstream_response_too_large"


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {**metadata(), "changed_files": "1"},
        {**metadata(), "changed_files": -1},
        {**metadata(), "number": 2},
        {**metadata(), "base": {"sha": "bad", "repo": {"private": False}}},
        {**metadata(), "base": {"sha": "a" * 40, "repo": {"private": True}}},
    ],
)
def test_invalid_metadata(data):
    with api(lambda request: httpx.Response(200, json=data)) as client:
        assert client.post("/review", json={"pr_url": URL}).status_code == 502


@pytest.mark.parametrize(
    "data",
    [
        {},
        [],
        [changed_file(), changed_file()],
        [{**changed_file(), "additions": -1}],
        [{**changed_file(), "patch": 10}],
        [{**changed_file(), "status": "surprise"}],
        [{**changed_file(), "deletions": True}],
    ],
)
def test_invalid_file_payload(data):
    def handler(request):
        return httpx.Response(200, json=data if request.url.path.endswith("/files") else metadata())

    with api(handler) as client:
        assert client.post("/review", json={"pr_url": URL}).status_code == 502


def test_duplicate_paths():
    def handler(request):
        return httpx.Response(
            200,
            json=[changed_file(), changed_file()]
            if request.url.path.endswith("/files")
            else metadata(2),
        )

    with api(handler) as client:
        assert client.post("/review", json={"pr_url": URL}).status_code == 502


def test_non_json_upstream():
    with api(lambda request: httpx.Response(200, text="not json")) as client:
        assert client.post("/review", json={"pr_url": URL}).status_code == 502


def test_zero_changed_files():
    def handler(request):
        assert not request.url.path.endswith("/files")
        return httpx.Response(200, json=metadata(0))

    with api(handler) as client:
        response = client.post("/review", json={"pr_url": URL})
    assert response.status_code == 200
    assert response.json()["files"] == []


async def test_http_client_configuration(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    settings = Settings.from_env()
    assert "test-token" not in repr(settings)
    async with create_http_client(settings) as client:
        assert client.headers["Authorization"] == "Bearer test-token"
        assert not client.follow_redirects
        assert client.timeout.connect == 5
        assert client.timeout.read == 20
    async with create_http_client(Settings()) as client:
        assert "Authorization" not in client.headers


async def test_internal_endpoint_allowlist():
    async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
        github = GitHubClient(client, Settings())
        with pytest.raises(ValueError, match="Unapproved"):
            await github._get("https://evil.com")
