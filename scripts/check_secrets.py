"""Offline publication check. Print filenames/counts only, never matching values.

Heuristic defense in depth, not proof that arbitrary secrets are absent.
"""

import os
from pathlib import Path
import re
import subprocess

TOKEN = re.compile(
    r"\b(?:AIza[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
PEM = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]{40,}-----END")
FORBIDDEN = {".env", ".venv", "evaluation-runs", ".aws", ".ssh", "credentials.json"}


def configured_secrets(root: Path) -> set[str]:
    values = {os.getenv(name, "") for name in ("GEMINI_API_KEY", "GITHUB_TOKEN")}
    env = root / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            match = re.fullmatch(
                r"\s*(?:export\s+)?(?:GEMINI_API_KEY|GITHUB_TOKEN)\s*=\s*(.*?)\s*", line
            )
            if match:
                values.add(match[1].strip("\"'"))
    return {value for value in values if len(value) >= 8}


def violations(path: str, data: str, secrets: set[str]) -> bool:
    parts = Path(path).parts
    forbidden = any(
        p in FORBIDDEN or (p.startswith(".env.") and p != ".env.example") for p in parts
    )
    return (
        forbidden or bool(TOKEN.search(data) or PEM.search(data)) or any(s in data for s in secrets)
    )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root
        )
        .decode()
        .split("\0")
    )
    secrets = configured_secrets(root)
    failures = []
    for path in sorted(set(filter(None, paths))):
        file = root / path
        if file.is_symlink() or not file.is_file():
            failures.append(path)
            continue
        if violations(path, file.read_bytes().decode("utf-8", errors="replace"), secrets):
            failures.append(path)
    if failures:
        print(f"Publication check failed: {len(failures)} file(s); values withheld.")
        for path in failures:
            print(path)
        return 1
    print(
        f"Publication check passed: {len(set(filter(None, paths)))} files; no known configured secrets or recognized credential patterns found."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
