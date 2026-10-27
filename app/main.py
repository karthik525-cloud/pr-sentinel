from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.config import Settings
from app.ai_models import AIReviewResponse
from app.gemini_provider import GeminiSession
from app.review_agent import ReviewAgent, SessionFactory
from app.github import GitHubClient, GitHubError, create_http_client
from app.models import ErrorResponse, HealthResponse, ReviewRequest, ReviewResponse


def create_app(
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
    *,
    session_factory: SessionFactory = GeminiSession,
) -> FastAPI:
    config = settings if settings is not None else Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        client = http_client if http_client is not None else create_http_client(config)
        application.state.github = GitHubClient(client, config)
        application.state.review_agent = ReviewAgent(
            application.state.github, config, session_factory
        )
        try:
            yield
        finally:
            if http_client is None:
                await client.aclose()

    application = FastAPI(title="PR Sentinel", version="0.1.0", lifespan=lifespan)

    @application.exception_handler(GitHubError)
    async def github_error_handler(request: Request, exc: GitHubError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {"code": exc.code, "message": exc.message},
            },
        )

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    @application.post(
        "/review",
        response_model=ReviewResponse,
        responses={status: {"model": ErrorResponse} for status in (403, 404, 413, 429, 502, 504)},
    )
    async def review(payload: ReviewRequest, request: Request) -> ReviewResponse:
        return await request.app.state.github.retrieve(payload)

    @application.post(
        "/review/ai",
        response_model=AIReviewResponse,
        responses={
            status: {"model": AIReviewResponse}
            for status in (403, 404, 409, 413, 422, 429, 502, 503, 504)
        },
    )
    async def ai_review(
        payload: ReviewRequest, request: Request, response: Response
    ) -> AIReviewResponse:
        status, result = await request.app.state.review_agent.run(payload)
        response.status_code = status
        return result

    return application


app = create_app()
