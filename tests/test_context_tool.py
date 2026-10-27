import base64

import httpx
import pytest

from app.config import Settings
from app.context_tool import get_file_context
from app.github import GitHubClient, GitHubError


def file_payload(text=b"one\ntwo\nthree\n", path="src/helper.py", **overrides):
    return {
        "type": "file",
        "path": path,
        "encoding": "base64",
        "size": len(text),
        "content": base64.b64encode(text).decode(),
        **overrides,
    }


async def test_exact_sha_and_fixed_repository(make_review, make_file):
    review = make_review(make_file())
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.scheme == "https" and request.url.host == "api.github.com"
        assert request.url.path == "/repos/owner/repo/contents/src/helper.py"
        assert dict(request.url.params) == {"ref": "b" * 40}
        return httpx.Response(200, json=file_payload())

    settings = Settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await get_file_context(
            GitHubClient(http, settings),
            review,
            settings,
            {"path": "src/helper.py", "start_line": 2, "end_line": 200},
        )
    assert result["ok"] is True
    assert result["end_line"] == 3
    assert result["lines"] == [{"number": 2, "text": "two"}, {"number": 3, "text": "three"}]
    assert result["head_sha"] == review.head_sha
    assert len(requests) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"path": "../secret"},
        {"path": "/etc/passwd"},
        {"path": "a/../../b"},
        {"path": "https://evil.example/file"},
        {"path": "a%2fb"},
        {"path": "a\\b"},
        {"path": "a//b"},
        {"path": "a/./b"},
        {"path": "a?ref=main"},
        {"path": "a#b"},
        {"path": "a\x00b"},
        {"path": "a\nb"},
        {"start_line": True},
        {"end_line": "10"},
        {"start_line": 0},
        {"end_line": 0},
        {"start_line": 10, "end_line": 5},
        {"owner": "other"},
        {"repository": "other"},
        {"ref": "main"},
        {"path": ".env"},
        {"path": "keys/server.pem"},
        {"end_line": 201},
    ],
)
async def test_invalid_or_unauthorized_arguments_never_fetch(overrides, make_review):
    def handler(request):
        pytest.fail("Rejected tool arguments reached GitHub")

    settings = Settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await get_file_context(
            GitHubClient(http, settings),
            make_review(),
            settings,
            {"path": "a.py", "start_line": 1, "end_line": 1} | overrides,
        )
    assert result["ok"] is False
    assert result["error"]["code"] in {"invalid_tool_arguments", "sensitive_path", "too_many_lines"}


@pytest.mark.parametrize(
    "payload,code",
    [
        (file_payload(text=b"a\x00b"), "binary_file"),
        (file_payload(text=b"\xff\xfe"), "binary_file"),
        (file_payload(size=9999999), "file_too_large"),
        (file_payload(type="dir"), "file_unavailable"),
        (file_payload(type="symlink"), "file_unavailable"),
        (file_payload(submodule_git_url="https://evil.example"), "file_unavailable"),
        (file_payload(target="../../x"), "file_unavailable"),
        (file_payload(encoding="none"), "file_unavailable"),
        (file_payload(path="other.py"), "file_unavailable"),
        (file_payload(text=b'api_key = "abcdefghijk"'), "sensitive_content"),
    ],
)
async def test_unavailable_content(payload, code, make_review):
    settings = Settings()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as http:
        result = await get_file_context(
            GitHubClient(http, settings),
            make_review(),
            settings,
            {"path": "src/helper.py", "start_line": 1, "end_line": 1},
        )
    assert result["error"]["code"] == code


@pytest.mark.parametrize(
    "payload", [file_payload(content="invalid!"), file_payload(size=True), file_payload(size=1)]
)
async def test_invalid_github_content_is_upstream_error(payload, make_review):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(GitHubError) as caught:
            await GitHubClient(http, Settings()).get_file_at_head(make_review(), "src/helper.py")
    assert caught.value.code == "invalid_upstream_response"


@pytest.mark.parametrize(
    "status,headers,code",
    [
        (404, {}, "file_not_found"),
        (302, {"Location": "https://evil.example"}, "github_upstream_failure"),
        (429, {}, "github_rate_limited"),
    ],
)
async def test_content_http_errors(status, headers, code, make_review):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers=headers)

    settings = Settings()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        if status == 404:
            result = await get_file_context(
                GitHubClient(http, settings),
                make_review(),
                settings,
                {"path": "src/helper.py", "start_line": 1, "end_line": 1},
            )
            assert result["error"]["code"] == code
        else:
            with pytest.raises(GitHubError) as caught:
                await GitHubClient(http, settings).get_file_at_head(make_review(), "src/helper.py")
            assert caught.value.code == code
    assert len(calls) == 1


async def test_context_size_and_line_bounds(make_review):
    settings = Settings(ai_max_tool_context_chars=100)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=file_payload(b"x" * 500)))
    ) as http:
        github = GitHubClient(http, settings)
        args = {"path": "src/helper.py", "start_line": 1, "end_line": 1}
        result = await get_file_context(github, make_review(), settings, args)
        assert result["error"]["code"] == "context_too_large"
        result = await get_file_context(
            github, make_review(), settings, args | {"start_line": 2, "end_line": 2}
        )
        assert result["error"]["code"] == "line_out_of_range"


async def test_content_endpoint_requires_sha(make_review):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("Network called"))
    ) as http:
        github = GitHubClient(http, Settings())
        with pytest.raises(ValueError):
            await github._get("/repos/owner/repo/contents/a.py", ref="main")
        with pytest.raises(ValueError):
            await github._get("/repos/owner/repo/contents/%2e%2e/private", ref="b" * 40)
