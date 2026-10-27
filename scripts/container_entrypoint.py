"""Validate the port and replace PID 1 with Uvicorn; never echo environment values."""

import os
import sys


def command() -> list[str]:
    try:
        port = int(os.environ.get("PORT", "8000"))
        if not 1024 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("PORT must be an integer from 1024 to 65535.") from None
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--no-access-log",
        "--limit-concurrency",
        "20",
    ]


if __name__ == "__main__":
    os.execv(sys.executable, command())
