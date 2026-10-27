"""Bounded GitHub reads. URLs are constructed locally; upstream URLs are never followed."""

import base64
import binascii
import json
import re
from typing import Any
from urllib.parse import quote, unquote

import httpx
from pydantic import TypeAdapter, ValidationError

from app.config import Settings
from app.context_security import safe_repository_path
from app.models import (
    PR_PATTERN,
    ChangedFile,
    GitHubFile,
    PullMetadata,
    ReviewRequest,
    ReviewResponse,
)


class GitHubError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def invalid_upstream() -> GitHubError:
    return GitHubError(
        502, "invalid_upstream_response", "GitHub returned invalid or inconsistent data."
    )


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "PR-Sentinel/0.1",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token.get_secret_value()}"
    return httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(20, connect=5, pool=5),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=False,
        trust_env=False,
    )


class GitHubClient:
    def __init__(self, http: httpx.AsyncClient, settings: Settings):
        self.http = http
        self.settings = settings

    async def _get(
        self,
        path: str,
        *,
        page: int | None = None,
        ref: str | None = None,
        byte_limit: int | None = None,
    ) -> Any:
        # Defense in depth: even internal callers cannot request arbitrary endpoints.
        pr_endpoint = re.fullmatch(
            r"/repos/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/pulls/[1-9][0-9]*(?:/files)?",
            path,
        )
        contents_endpoint = re.fullmatch(
            r"/repos/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/contents/(.+)",
            path,
        )
        if ref is not None:
            if (
                not contents_endpoint
                or not re.fullmatch(r"[0-9a-f]{40}", ref)
                or not safe_repository_path(unquote(contents_endpoint[1]))
            ):
                raise ValueError("Unapproved GitHub content endpoint")
            params = {"ref": ref}
        else:
            if not pr_endpoint:
                raise ValueError("Unapproved GitHub API endpoint")
            params = {"per_page": 100, "page": page} if page is not None else None
        limit = (
            min(byte_limit, self.settings.max_upstream_bytes)
            if byte_limit is not None
            else self.settings.max_upstream_bytes
        )
        try:
            async with self.http.stream(
                "GET",
                f"https://api.github.com{path}",
                params=params,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                if status == 404:
                    raise GitHubError(
                        404, "pull_request_not_found", "Public pull request not found."
                    )
                if status == 429 or (
                    status == 403
                    and (
                        response.headers.get("x-ratelimit-remaining") == "0"
                        or "retry-after" in response.headers
                    )
                ):
                    raise GitHubError(
                        429, "github_rate_limited", "GitHub rate limit reached. Try again later."
                    )
                if status == 403:
                    raise GitHubError(
                        403,
                        "github_access_denied",
                        "GitHub denied access; a secondary rate limit may apply.",
                    )
                if status == 401:
                    raise GitHubError(
                        502,
                        "github_authentication_failed",
                        "GitHub authentication failed. Check the server token.",
                    )
                if not 200 <= status < 300:
                    raise GitHubError(
                        502,
                        "github_upstream_failure",
                        "GitHub request failed or returned an unexpected redirect.",
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > limit:
                        raise GitHubError(
                            413,
                            "upstream_response_too_large",
                            "GitHub response exceeds the configured byte limit.",
                        )
                    content.extend(chunk)
                try:
                    return json.loads(content)
                except (ValueError, UnicodeDecodeError):
                    raise invalid_upstream() from None
        except httpx.TimeoutException:
            raise GitHubError(504, "github_timeout", "GitHub request timed out.") from None
        except httpx.RequestError:
            raise GitHubError(
                502, "github_network_error", "Could not communicate with GitHub."
            ) from None

    async def retrieve(self, request: ReviewRequest) -> ReviewResponse:
        match = PR_PATTERN.fullmatch(request.pr_url)
        assert match is not None  # ReviewRequest has already validated the URL.
        owner, repository, number_text = match.groups()
        number = int(number_text)
        path = f"/repos/{owner}/{repository}/pulls/{number}"
        try:
            metadata = PullMetadata.model_validate(await self._get(path))
        except ValidationError:
            raise invalid_upstream() from None
        if metadata.number != number:
            raise invalid_upstream()
        if metadata.changed_files > self.settings.max_changed_files:
            raise GitHubError(
                413,
                "too_many_changed_files",
                "Pull request exceeds the configured changed-file limit.",
            )

        files: list[ChangedFile] = []
        paths: set[str] = set()
        diff_bytes = 0
        # GitHub's file endpoint is capped at 3000. Bound pages using validated metadata;
        # do not follow Link URLs, which could redirect requests or leak credentials.
        for page in range(1, (metadata.changed_files + 99) // 100 + 1):
            try:
                batch = TypeAdapter(list[GitHubFile]).validate_python(
                    await self._get(f"{path}/files", page=page),
                    strict=True,
                )
            except ValidationError:
                raise invalid_upstream() from None
            if len(batch) != min(100, metadata.changed_files - len(files)):
                raise invalid_upstream()
            for file in batch:
                if file.filename in paths:
                    raise invalid_upstream()
                paths.add(file.filename)
                diff_bytes += len(file.patch.encode("utf-8")) if file.patch is not None else 0
                if diff_bytes > self.settings.max_diff_bytes:
                    raise GitHubError(
                        413,
                        "diff_too_large",
                        "Pull request exceeds the configured total diff byte limit.",
                    )
                files.append(
                    ChangedFile(
                        path=file.filename,
                        status=file.status,
                        additions=file.additions,
                        deletions=file.deletions,
                        patch=file.patch,
                        patch_available=file.patch is not None,
                        previous_path=file.previous_filename,
                    )
                )
        return ReviewResponse(
            owner=owner,
            repository=repository,
            number=number,
            title=metadata.title,
            description=metadata.body,
            base_sha=metadata.base.sha,
            head_sha=metadata.head.sha,
            changed_files=len(files),
            total_diff_bytes=diff_bytes,
            files=files,
        )

    async def get_file_at_head(self, review: ReviewResponse, path: str) -> str:
        """Read UTF-8 content at this PR's exact head; never use download_url."""
        if not safe_repository_path(path):
            raise GitHubError(422, "unauthorized_path", "Expected a safe repository-relative path.")
        if (
            not re.fullmatch(r"[A-Za-z0-9-]+", review.owner)
            or not re.fullmatch(
                r"[A-Za-z0-9_.-]+",
                review.repository,
            )
            or review.repository in {".", ".."}
        ):
            raise ValueError("Invalid approved repository identity")
        endpoint = f"/repos/{review.owner}/{review.repository}/contents/{quote(path, safe='/')}"
        try:
            data = await self._get(
                endpoint,
                ref=review.head_sha,
                byte_limit=self.settings.ai_max_file_bytes * 2 + 8192,
            )
        except GitHubError as exc:
            if exc.status == 404:
                raise GitHubError(
                    404, "file_not_found", "File is unavailable at the PR head commit."
                ) from None
            if exc.status == 413:
                raise GitHubError(
                    413, "file_too_large", "File response exceeds the context limit."
                ) from None
            raise
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("path") != path:
            raise GitHubError(
                422, "file_unavailable", "Requested path is not an available regular file."
            )
        if data.get("submodule_git_url") or data.get("target"):
            raise GitHubError(
                422, "file_unavailable", "Submodule and explicit symlink responses are unsupported."
            )
        size = data.get("size")
        if type(size) is not int or size < 0:
            raise invalid_upstream()
        if size > self.settings.ai_max_file_bytes:
            raise GitHubError(413, "file_too_large", "File exceeds the context byte limit.")
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise GitHubError(422, "file_unavailable", "GitHub did not supply base64 file content.")
        try:
            raw = base64.b64decode(
                data["content"].replace("\n", "").replace("\r", ""), validate=True
            )
        except (ValueError, binascii.Error):
            raise invalid_upstream() from None
        if len(raw) != size:
            raise invalid_upstream()
        if len(raw) > self.settings.ai_max_file_bytes:
            raise GitHubError(413, "file_too_large", "File exceeds the context byte limit.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise GitHubError(422, "binary_file", "Only UTF-8 text files are supported.") from None
        if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
            raise GitHubError(
                422, "binary_file", "Binary/control-byte content cannot be used as source context."
            )
        return text
