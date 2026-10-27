import importlib.metadata
from pathlib import Path
import re

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.check_secrets import violations
from scripts.container_entrypoint import command


@pytest.mark.parametrize("port", ["8000", "8080", "65535"])
def test_container_port(monkeypatch, port):
    monkeypatch.setenv("PORT", port)
    args = command()
    assert args[args.index("--port") + 1] == port
    assert "--no-access-log" in args


@pytest.mark.parametrize("port", ["0", "80", "65536", "bad-secret-value", "8000; touch x"])
def test_container_invalid_port_never_echoes_value(monkeypatch, port):
    monkeypatch.setenv("PORT", port)
    with pytest.raises(SystemExit) as exc:
        command()
    assert str(exc.value) == "PORT must be an integer from 1024 to 65535."


def test_publication_scanner_does_not_depend_on_live_services():
    assert violations("README.md", "known-local-secret", {"known-local-secret"})
    assert violations("app/a.py", "AIza" + "x" * 35, set())
    assert violations(".env", "", set())
    assert violations("evaluation-runs/run.json", "{}", set())
    assert not violations(".env.example", "GEMINI_API_KEY=", set())
    assert not violations("tests/a.py", 'api_key="fake-api-key"', set())


def test_runtime_lock_dependency_closure():
    """Check base + requests extra dependencies against installed metadata, offline."""
    pins = dict(
        line.split("==")
        for line in Path("requirements-runtime.lock").read_text().splitlines()
        if line and not line.startswith("#")
    )
    normalized = {canonicalize_name(k): v for k, v in pins.items()}
    for name in pins:
        for raw in importlib.metadata.requires(name) or []:
            req = Requirement(raw)
            if req.marker and not any(
                req.marker.evaluate({"extra": extra}) for extra in ("", "requests")
            ):
                continue
            key = canonicalize_name(req.name)
            assert key in normalized, (name, req.name)
            assert req.specifier.contains(normalized[key]), (name, req.name)


def test_ci_actions_are_immutable_and_have_no_publish_steps():
    workflow = Path(".github/workflows/ci.yml").read_text()
    uses = re.findall(r"uses: (\S+)", workflow)
    assert uses and all(re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", item) for item in uses)
    assert "contents: read" in workflow and "persist-credentials: false" in workflow
    assert "pull_request_target" not in workflow and "--execute" not in workflow
    assert "push:\n    branches: [main]" in workflow


def test_docker_inputs_are_allowlisted():
    ignores = Path(".dockerignore").read_text().splitlines()
    assert "**" in ignores
    allowed = [line for line in ignores if line.startswith("!")]
    assert not any(
        ".env" in line or "evaluation-runs" in line or ".git/" in line for line in allowed
    )
    dockerfile = Path("Dockerfile").read_text()
    assert "USER 10001:10001" in dockerfile and "HEALTHCHECK" in dockerfile
    assert "COPY . " not in dockerfile


def test_docker_package_directories_and_fixture_are_explicitly_included():
    rules = Path(".dockerignore").read_text().splitlines()
    for source in Path("app").rglob("*.py"):
        for parent in source.parents:
            if parent == Path("."):
                break
            # Explicit parents avoid depending on wildcard traversal behavior.
            assert f"!{parent.as_posix()}" in rules
            assert rules.index(f"!{parent.as_posix()}") < rules.index("!app/**/*.py")
    for asset in (
        "benchmarks/__init__.py",
        "benchmarks/v1.json",
        "scripts/container_entrypoint.py",
    ):
        assert Path(asset).is_file()
        assert f"!{asset}" in rules
    last_allow = max(i for i, rule in enumerate(rules) if rule.startswith("!"))
    for denied in (
        "**/.env",
        "**/.env.*",
        "**/.venv",
        "**/.git",
        "**/evaluation-runs",
        "**/__pycache__",
    ):
        assert rules.index(denied) > last_allow
    assert rules.index("app/**") < rules.index("!app/evaluation")
    assert rules.index("app/evaluation/**") < rules.index("!app/**/*.py")
