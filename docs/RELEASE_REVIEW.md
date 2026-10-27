# Stage 5 release-readiness review

Initial Stage 5 scope: local source inspection and offline/mocked verification. A subsequently authorized pre-release follow-up added the owner-confirmed MIT License, explicit Docker package traversal rules, and a live PyPI dependency audit. No Gemini requests, remote publishing, deployment, or new live benchmark runs were performed. This is production-inspired portfolio packaging, not a claim that the service is safe to expose publicly without additional controls.

## Findings and disposition

| Area | Evidence / disposition |
| --- | --- |
| Tool-call evidence gap | Fixed with bounded metadata-only `tool_traces` on the AI response. New evaluation records identify whether required file paths were successfully retrieved. Legacy missing traces remain unknown. Retrieval cannot prove correct model reasoning; file-level evidence does not prove all relevant lines were read. |
| Secret publication | `.env`, local recordings, caches, credential files, builds, logs and editor files are ignored. Docker uses an input allowlist. The offline scanner checks publishable tracked/unignored files against configured local keys without printing values, plus recognizable token/private-key patterns. This is heuristic assurance, not exhaustive secret discovery. No recognized or known configured credentials were found in the checked publication set. |
| Packaging | Version-pinned build/runtime/dev dependency sets; no resolver-driven project installation. Benchmark JSON now ships in the wheel so installed evaluation can locate it. Offline wheel build and content validation performed. Pins lack distribution hashes; Python image uses a version tag rather than a digest. These provide version reproducibility, not immutable supply-chain or bit-identical builds. |
| Dependency security | `pip check` and runtime-lock closure are offline consistency checks, not CVE scans. The approved pip-only upgrade to 26.2.2 is complete. A fresh 2026-10-27 pip-audit run covered 40 pinned packages, including all 31 runtime pins, with zero vulnerabilities and zero skips; see [the audit report](security/DEPENDENCY_AUDIT.md). No vulnerability-free claim is made; a reviewed image digest remains outstanding. Existing Starlette/HTTPX test deprecation remains. |
| CI | PR and main-branch push tests, read-only permissions, SHA-pinned actions, no persisted checkout credentials, no API keys or live calls, no publish/deploy job. Workflow has not yet run on GitHub. Pins must be reviewed periodically. |
| Resource limits | Existing per-review requests, tool batches, payload sizes, history, source bytes and deadlines remain enforced. Traces retain at most three entries, each with a path capped at 1024 characters and fixed metadata. Docker adds Uvicorn concurrency 20; this is not a global spend/rate quota. Service-wide authentication, user quotas and admission control are absent. Keep host binding on localhost. |
| URL / file safety | Fixed upstream hosts, no redirects/proxies, strict PR routes and tool paths, repository/head-SHA binding, no model-selected filesystem reads, no shell/code execution of repository data. Fixture context is dictionary-backed. No demonstrated high-severity bypass found in inspected paths/tests; this is not a penetration test. |
| Prompt injection | Repository metadata/source remain untrusted data. Manual tool dispatch and deterministic schema/location checks enforce boundaries. Instructions and heuristic secret screening do not guarantee factual correctness or immunity to prompt injection. Never use model findings as automatic merge/security approval. |
| Exception handling | SDK messages are reconstructed from safe categories. Trace exceptions retain only allowlisted codes; cancellation leaves a failure trace before propagating. No exception strings, tool arguments wholesale, source text, or prompts are saved in traces. Existing raw review findings can still contain code excerpts; recordings remain local and ignored. |
| PR coverage | AI reviews reject unavailable/malformed/oversized diffs. Added-line-only validation cannot represent all deletion-only defects. Metadata/files can race with PR updates. Retrieval endpoint lacks an overall deadline; the AI endpoint has one. Structural validation does not prove semantic accuracy or completeness. |
| Evaluation integrity | Pinned synthetic fixtures, independent authored labels, deterministic one-to-one matching, explicit exclusions, error rates and subset coverage remain. Small contract-rich cases and lexical matching are susceptible to overfitting/paraphrase/negation errors. Live provenance is an application assertion, not an authenticated provider transcript. Tokens remain unavailable; character counts are not token counts. |
| Licensing | Standard MIT LICENSE added with owner-confirmed copyright (c) 2026 Karthik Chandrakanth Naraboina. SPDX metadata and wheel license inclusion are configured. |

## Existing live evidence (not rerun)

- One general public GitHub PR review succeeded, as reported by the owner. Its complete recording is not part of the two benchmark run directories inspected here; no aggregate metrics are inferred from it.
- `flash-lite-001`: Gemini 3.5 Flash-Lite, inclusive-limit synthetic defect, 3 model requests / 1 tool call, 19.8252 seconds, completed without issues. One correct exact-line bug finding; severity high versus expected medium. Evaluator: TP 1, FP 0, FN 0.
- `flash-lite-002`: same model, clean-context synthetic case, 4 model requests / 2 tool calls, 8.1224 seconds, completed without issues and no findings/rejections. Tool-context size 639 characters. Prior traces did not record requested file paths, so retrieval of `src/roles.py` cannot be established from that run.
- Previous artifacts were not modified or backfilled. These two cases do not establish general code-review accuracy, repeatability, token costs, or robust context use.

## Publication checklist (owner approval required for publication)

1. Review the MIT license, public-facing metadata, and dependency provenance; review the fresh clean pip audit and its scope limitations before publication.
2. Run the documented offline checks and inspect the complete publication file list. Rerun the dependency audit after approved remediation; build and smoke-test Docker on a Docker-enabled machine.
3. Inspect `.gitignore`, `git status --short`, `git ls-files --others --exclude-standard`, and `python scripts/check_secrets.py`. Never force-add ignored credentials or recordings.
4. Only after approval: stage intended files, inspect `git diff --cached --stat` and the full staged diff locally, and rerun the secret scanner. A scanner pass is not a substitute for review.
5. Only after approval: create the first commit on `main`, create an empty GitHub repository, add its verified remote URL, and push `main`. No remote/commit/push was created during Stage 5.
6. Confirm GitHub CI passes; configure branch protection to require its `test` job before merging. Do not publish packages/images or deploy automatically.

Docker was unavailable locally, so container build, runtime health, non-root operation and image-layer verification remain unverified. Source-level checks and a successful Python wheel build are not a substitute for container testing.

## Stage 5 change inventory and final verification

Added: `app/tool_observability.py`, `benchmarks/__init__.py`, `tests/test_observability.py`, `tests/test_release_checks.py`, `Dockerfile`, `.dockerignore`, `.github/workflows/ci.yml`, `requirements-build.lock`, `requirements-runtime.lock`, `scripts/container_entrypoint.py`, `scripts/check_secrets.py`, `scripts/check_wheel.py`, this review, and `docs/IMPLEMENTATION_NOTES.md` (preserved previous README detail).

Behavior/configuration/documentation changes: `app/ai_models.py`, `app/review_agent.py`, `app/evaluation/live.py`, `pyproject.toml`, `.gitignore`, and `README.md`.

Formatting-only cleanup: `app/config.py`, `app/diff_parser.py`, `app/finding_validator.py`, `app/findings.py`, `app/github.py`, `app/main.py`, `tests/conftest.py`, `tests/test_diff_parser.py`, `tests/test_findings.py`, `tests/test_review.py`, and `tests/test_stage2_integration.py`. Existing tests and benchmark labels were retained; `.env` and saved live artifacts were not edited.

Final local verification: **480 tests passed in 2.25 seconds**, with one existing Starlette/HTTPX deprecation warning. Ruff lint passed; all 46 Python files passed formatting. Compilation and dependency consistency passed. A wheel was built with locally cached setuptools 84.2.1, installed into an isolated temporary target without network access, and imported outside the checkout; its 25-case packaged dataset and three-route OpenAPI both passed. Publication scanning passed for all 61 intended files. Git has an unborn `main` branch, no staged files/commits, and no remote. Docker and GitHub-hosted CI execution remain unverified.

## Pre-release follow-up — 2026-10-27

Added the standard MIT LICENSE using the owner-confirmed name, MIT project metadata, and wheel license validation. Docker build inputs now include LICENSE and explicit parent/child package rules with final secret/cache exclusions. The allowlist regression verifies every current app package directory, required benchmark assets and ordering of sensitive exclusions. Docker remains unavailable: these are static checks, not an image build.

The isolated pip-audit 2.10.1 run completed against live PyPI data: 40 pins, zero skips, 12 reported entries representing six unique advisories in pip 25.0.1. All 31 runtime pins were covered and none was flagged. Raw output and fixed-version details are retained under `docs/security/`. No lock-file or application dependency changes were made. This supersedes the earlier audit-unavailable statement; the original Stage 5 test results above remain historical.

Follow-up verification: **481 passed, one existing warning, in 2.43 seconds**; Ruff lint and all 46 Python formatting checks passed. Compilation and `pip check` passed. The rebuilt wheel was installed into a temporary target without dependencies/network, imported outside the checkout, and verified for its exact MIT LICENSE, SPDX metadata, 25-case dataset and three-route OpenAPI. The publication scan passed for 64 files. Git ignore checks confirmed `.env` and both saved evaluation runs remain excluded. SHA-256 checks confirmed all three dependency lock files are unchanged. No commits, remote creation, pushes, Gemini calls or deployments occurred.

Historical pre-upgrade verdict (superseded below): licensing and packaging-source fixes were complete, but the release was **not dependency-audit clean**. Approve a reviewed pip build-tool pin at 26.2.0 or newer, rerun the exact audit command in `security/DEPENDENCY_AUDIT.md` and all README verification commands, then verify Docker on an equipped machine. Review the staged publication set and obtain publication approval before creating any commit/remote/push. Docker builds, base-image/native-library security and hosted GitHub CI remain unverified; application behavior and lock versions were not changed in this follow-up.

## Approved pip-only upgrade — 2026-10-27

Official PyPI confirmed non-yanked pip 26.2.2 with Python >=3.10 support. Updated the sole executable pin in `requirements-build.lock` and the local project virtual environment. Docker, CI and documented installation commands already consume this shared lock; setuptools, all runtime/development pins, application code, `.env`, API keys and saved evaluations were not changed. Runtime/development lock hashes match the pre-upgrade values.

Fresh isolated pip-audit 2.10.1/PyPI result: **40 pins, zero vulnerabilities, zero skips, exit 0**, without ignore/suppression flags. All 31 runtime pins were covered. See [current audit and raw evidence](security/DEPENDENCY_AUDIT.md); prior findings are retained as history.

Verification: **481 tests passed in 2.42 seconds**, one existing Starlette/HTTPX warning; Ruff lint and formatting passed (46 Python files), compilation and pip consistency passed, and the wheel rebuilt with pip 26.2.2. Static Docker-context tests cover explicit nested package parents, benchmark assets and final secret exclusions. Docker is still absent: no image build or smoke-test success is claimed.

Current verdict: the previously reported pip dependency blocker is resolved for the freshly audited pins. Remaining publication tasks are owner review/approval, Docker verification on an equipped machine, and hosted CI verification after an approved first push. Distribution hashes and a reviewed base-image digest remain supply-chain hardening tasks; the audit does not cover native/base-image packages or unknown vulnerabilities. No commit, remote, push or deployment was created.

Installed-wheel compatibility verification also passed using pip 26.2.2 in a temporary target outside the checkout: application imports, 25-case packaged dataset and all three OpenAPI routes. The final publication secret scan passed for **65 files**; `.env` and both evaluation recordings remain Git-ignored. This follow-up changed only `requirements-build.lock`, `README.md`, this review, `docs/security/DEPENDENCY_AUDIT.md`, and the newly retained `docs/security/pip-audit-2026-10-27-pip2622.json` (plus local ignored wheel output and the approved virtual-environment pip update).
