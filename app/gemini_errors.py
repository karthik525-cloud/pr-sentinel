"""Useful Google API diagnostics without forwarding untrusted error bodies."""

import re

from google.genai import errors

from app.ai_models import ProviderErrorDiagnostic

_API_STATUSES = frozenset(
    {
        "OK",
        "CANCELLED",
        "UNKNOWN",
        "INVALID_ARGUMENT",
        "DEADLINE_EXCEEDED",
        "NOT_FOUND",
        "ALREADY_EXISTS",
        "PERMISSION_DENIED",
        "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION",
        "ABORTED",
        "OUT_OF_RANGE",
        "UNIMPLEMENTED",
        "INTERNAL",
        "UNAVAILABLE",
        "DATA_LOSS",
        "UNAUTHENTICATED",
        "invalid_request",
        "failed_precondition",
        "parameter_unknown",
        "authentication",
        "permission_denied",
        "not_found",
        "model_not_found",
        "rate_limit_exceeded",
        "quota_exceeded",
        "too_many_requests",
        "api_error",
    }
)
_API_REASONS = frozenset(
    {
        "API_KEY_INVALID",
        "API_KEY_EXPIRED",
        "API_KEY_SERVICE_BLOCKED",
        "API_KEY_HTTP_REFERRER_BLOCKED",
        "API_KEY_IP_ADDRESS_BLOCKED",
        "SERVICE_DISABLED",
        "CONSUMER_INVALID",
        "BILLING_DISABLED",
        "MODEL_NOT_FOUND",
        "RATE_LIMIT_EXCEEDED",
        "QUOTA_EXCEEDED",
    }
)
# Only these parameter names can appear in reconstructed diagnostic messages.
_PARAMETERS = (
    ("candidate_count", ("candidate_count", "candidatecount")),
    ("temperature", ("temperature",)),
    ("top_p", ("top_p", "topp")),
    ("top_k", ("top_k", "topk")),
    ("thinking_budget", ("thinking_budget", "thinkingbudget")),
    ("thinking_level", ("thinking_level", "thinkinglevel")),
    ("max_output_tokens", ("max_output_tokens", "maxoutputtokens")),
    ("response_json_schema", ("response_json_schema", "responsejsonschema")),
    ("parameters_json_schema", ("parameters_json_schema", "parametersjsonschema")),
)


def _safe_message(message: object, status: int | None, api_status: str | None) -> str:
    # Arbitrary error messages may echo prompts, keys, code or request JSON. Avoid
    # denylist redaction: reconstruct recognized explanations using fixed text and
    # allowlisted identifiers only. Never return str(exc), details or raw message.
    text = message[:8192].lower() if isinstance(message, str) else ""
    if status == 400:
        unsupported = any(
            term in text
            for term in (
                "not supported",
                "unsupported",
                "unknown",
                "unrecognized",
                "not allowed",
            )
        )
        for parameter, aliases in _PARAMETERS:
            if any(re.search(rf"\b{alias}\b", text) for alias in aliases):
                if unsupported:
                    return f"Google rejected unsupported parameter {parameter}."
                if "invalid" in text or "out of range" in text:
                    return f"Google rejected the value or schema for {parameter}."
        if api_status in {"FAILED_PRECONDITION", "failed_precondition"}:
            return "Google reported an unmet account, region, or service prerequisite; verify free-tier eligibility without enabling billing."
        return "Google rejected the request parameters or configuration."
    if status == 401:
        return "Google rejected the authentication credentials."
    if status == 403:
        return "Google denied access to the configured model or service."
    if status == 404:
        return "The configured model or API resource was not found or does not support this API method/version."
    if status == 429:
        return "Google reported exhausted quota or a rate limit; no retry was attempted."
    if status == 504:
        return "Google reported an upstream deadline exceeded; the SDK-configured server timeout may apply. No retry was attempted."
    if status is not None and 500 <= status <= 599:
        return "Google reported an upstream service failure; no retry was attempted."
    return "Google returned an unexpected API response; raw error text was withheld."


def api_error_diagnostic(exc: errors.APIError) -> ProviderErrorDiagnostic:
    response_status = getattr(exc.response, "status_code", None)
    status = response_status if type(response_status) is int else exc.code
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    api_status = exc.status if isinstance(exc.status, str) and exc.status in _API_STATUSES else None
    # Newer error formats may put their category in error.code rather than status.
    body = exc.details.get("error", exc.details) if isinstance(exc.details, dict) else {}
    if not isinstance(body, dict):
        body = {}
    category = body.get("code")
    if api_status is None and isinstance(category, str) and category in _API_STATUSES:
        api_status = category
    reason = None
    details = body.get("details")
    for detail in details[:20] if isinstance(details, list) else ():
        if (
            isinstance(detail, dict)
            and detail.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo"
        ):
            value = detail.get("reason")
            if isinstance(value, str) and value in _API_REASONS:
                reason = value
                break
    exception_type = (
        "ClientError"
        if isinstance(exc, errors.ClientError)
        else "ServerError"
        if isinstance(exc, errors.ServerError)
        else "APIError"
    )
    return ProviderErrorDiagnostic(
        http_status=status,
        api_status=api_status,
        reason=reason,
        sdk_exception=exception_type,
        message=_safe_message(exc.message, status, api_status),
    )
