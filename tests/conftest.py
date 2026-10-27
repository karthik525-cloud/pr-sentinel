from collections.abc import Callable

import pytest

from app.models import ChangedFile, ReviewResponse

DEFAULT_PATCH = "@@ -10,3 +10,4 @@\n keep\n-old\n+new\n+extra\n tail"


@pytest.fixture
def make_file() -> Callable[..., ChangedFile]:
    def factory(**overrides) -> ChangedFile:
        values = dict(
            path="src/app.py",
            status="modified",
            additions=2,
            deletions=1,
            patch=DEFAULT_PATCH,
            patch_available=True,
            previous_path=None,
        )
        values.update(overrides)
        if "patch" in overrides and "patch_available" not in overrides:
            values["patch_available"] = overrides["patch"] is not None
        return ChangedFile(**values)

    return factory


@pytest.fixture
def make_review() -> Callable[..., ReviewResponse]:
    def factory(*files: ChangedFile) -> ReviewResponse:
        return ReviewResponse(
            owner="owner",
            repository="repo",
            number=1,
            title="Change",
            description=None,
            base_sha="a" * 40,
            head_sha="b" * 40,
            changed_files=len(files),
            total_diff_bytes=sum(len(file.patch.encode()) if file.patch else 0 for file in files),
            files=list(files),
        )

    return factory


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    """Fail any accidental real HTTP call, including when local credentials exist."""
    import httpx

    def blocked(*args, **kwargs):
        pytest.fail("Tests must use mocked HTTP transports")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
