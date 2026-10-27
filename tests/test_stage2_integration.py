import asyncio

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.diff_parser import parse_review
from app.finding_validator import validate_findings
from app.main import create_app
from app.models import ReviewResponse


def test_retrieval_to_parsing_and_validation_preserves_stage1_api(make_file, make_review):
    source = make_review(
        make_file(status="renamed", previous_path="old.py"),
        make_file(path="binary.bin", patch=None, additions=0, deletions=0),
        make_file(path="broken.py", patch="@@ -1,5 +1,5 @@\n x"),
    )

    def handler(request):
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": file.path,
                        "status": file.status,
                        "additions": file.additions,
                        "deletions": file.deletions,
                        "patch": file.patch,
                        "previous_filename": file.previous_path,
                    }
                    for file in source.files
                ],
            )
        return httpx.Response(
            200,
            json={
                "number": source.number,
                "title": source.title,
                "body": source.description,
                "base": {"sha": source.base_sha, "repo": {"private": False}},
                "head": {"sha": source.head_sha},
                "changed_files": len(source.files),
            },
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with TestClient(create_app(Settings(), upstream)) as client:
            response = client.post(
                "/review", json={"pr_url": "https://github.com/owner/repo/pull/1"}
            )
            assert response.status_code == 200
            assert response.json() == source.model_dump()
            assert set(client.get("/openapi.json").json()["paths"]) == {
                "/health",
                "/review",
                "/review/ai",
            }
    finally:
        asyncio.run(upstream.aclose())
    retrieved = ReviewResponse.model_validate(response.json())
    parsed = parse_review(retrieved)
    assert [file.patch_state for file in parsed.files] == ["parsed", "unavailable", "malformed"]
    candidate = dict(
        file_path="src/app.py",
        line_number=11,
        severity="medium",
        category="reliability",
        title="Guard empty input",
        explanation="This operation fails for empty input.",
        suggested_fix="Return early for empty input.",
    )
    report = validate_findings(
        parsed,
        [
            candidate,
            candidate | {"file_path": "old.py"},
            candidate | {"file_path": "binary.bin"},
            candidate | {"file_path": "broken.py"},
        ],
    )
    assert len(report.findings) == 1
    assert [item.code for item in report.rejected] == [
        "file_not_in_pr",
        "patch_unavailable",
        "patch_malformed",
    ]
