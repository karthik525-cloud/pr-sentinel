"""Manual-only live review: dry-run by default, no automatic Gemini verification."""

import argparse
import asyncio
import json
import math

from app.config import Settings
from app.gemini_provider import GeminiSession
from app.github import GitHubClient, create_http_client
from app.models import ReviewRequest
from app.review_agent import ReviewAgent


async def execute(settings: Settings, request: ReviewRequest) -> int:
    async with create_http_client(settings) as http:
        status, result = await ReviewAgent(
            GitHubClient(http, settings), settings, GeminiSession
        ).run(request)
    payload = result.model_dump(mode="json")
    payload["http_status"] = status
    print(json.dumps(payload, indent=2))
    return 0 if status == 200 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan one public PR review. Add --execute only after explicit approval."
    )
    parser.add_argument("--pr-url", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Explicitly approve one live review (up to configured request limits).",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    request = ReviewRequest(pr_url=args.pr_url)
    print(
        json.dumps(
            {
                "mode": "LIVE" if args.execute else "DRY RUN: no network requests",
                "model": settings.gemini_model,
                "thinking_level": settings.ai_thinking_level or "provider_default",
                "model_timeout_seconds": settings.ai_model_timeout_seconds,
                "sdk_server_timeout_seconds": math.ceil(
                    int(settings.ai_model_timeout_seconds * 1000) / 1000
                ),
                "public_pr": request.pr_url,
                "max_gemini_requests": settings.ai_max_llm_requests,
                "max_tool_calls": settings.ai_max_tool_calls,
                "max_serialized_input_characters_per_request": settings.ai_max_request_chars,
                "max_initial_context_characters": settings.ai_max_context_chars,
                "max_source_context_characters_per_tool": settings.ai_max_tool_context_chars,
                "max_retained_response_characters_per_request": settings.ai_max_response_chars,
                "max_output_tokens_per_request": settings.ai_max_output_tokens,
                "review_deadline_seconds": settings.ai_review_deadline_seconds,
                "note": "Later requests resend bounded history plus fixed instructions and schemas. No retries. Requires a free-tier project without billing.",
            },
            indent=2,
        ),
        flush=True,
    )
    return asyncio.run(execute(settings, request)) if args.execute else 0


if __name__ == "__main__":
    raise SystemExit(main())
