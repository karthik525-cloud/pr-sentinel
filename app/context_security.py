"""Repository data is untrusted. Never resolve a repository path on the local disk."""

import re

from app.config import Settings

_SECRET = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{30,})|"
    r"(?i:(?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*['\"][^'\"\n]{8,}['\"])"
)


def safe_repository_path(path: str) -> bool:
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        0 < len(path) <= 1024
        and all(ord(char) >= 32 and ord(char) != 127 for char in path)
        and not any(char in path for char in "\\%:?#")
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def sensitive_path(path: str) -> bool:
    parts = path.lower().split("/")
    return any(
        part == ".env"
        or part.startswith(".env.")
        or part in {".git", ".aws", ".ssh"}
        or part.endswith((".pem", ".key", ".p12", ".pfx"))
        or part in {"credentials.json", "id_rsa", "id_ed25519"}
        for part in parts
    )


def contains_secret(text: str, settings: Settings) -> bool:
    if _SECRET.search(text):
        return True
    return any(
        secret is not None and bool(secret.get_secret_value()) and secret.get_secret_value() in text
        for secret in (settings.github_token, settings.gemini_api_key)
    )


def contains_secret_data(value: object, settings: Settings) -> bool:
    """Inspect decoded JSON strings, so escaping cannot bypass output screening."""
    if isinstance(value, str):
        return contains_secret(value, settings)
    if isinstance(value, dict):
        return any(
            contains_secret_data(key, settings) or contains_secret_data(item, settings)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret_data(item, settings) for item in value)
    return False
