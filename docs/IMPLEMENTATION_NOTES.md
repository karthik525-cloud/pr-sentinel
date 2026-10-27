# PR Sentinel — Agentic AI Code Review Engine

PR Sentinel is a staged project for reviewing public GitHub pull requests with an AI agent. **Stages 1–4 are implemented:** public PR retrieval, deterministic diff/finding validation, an opt-in Gemini review agent, and reproducible evaluation. `POST /review` remains retrieval-only; `POST /review/ai` runs the bounded AI workflow when explicitly configured. There is no database, frontend, code execution, GitHub write access, deployment, or billing setup.

## Requirements and installation

Python 3.12+ and a standard virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

For the exact tested dependency versions, use `python -m pip install -r requirements-dev.lock -e '.[dev]'` instead. The lock file is a version snapshot, without distribution hashes.

## Configuration

`.env.example` documents the optional environment variables. The app reads the process environment; it does not automatically load `.env` files. With bash/zsh, load a local configuration as follows (source only a file you control):

```bash
cp .env.example .env
# Edit .env locally if desired; credentials must never be committed.
set -a
source .env
set +a
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `GITHUB_TOKEN` | unset | Optional GitHub token, sent only to the fixed GitHub API host |
| `MAX_CHANGED_FILES` | `500` | Reject larger PRs; accepted range 1–3000 |
| `MAX_DIFF_BYTES` | `1000000` | Maximum sum of available patches in UTF-8 bytes |
| `MAX_UPSTREAM_BYTES` | `8000000` | Maximum decoded JSON bytes per GitHub response |

No token is required for public PRs. GitHub's unauthenticated rate limits apply. A token does not enable private repository support; private base repositories are rejected. Configuration errors stop application startup.

## Run and manually test

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Interactive API documentation: <http://127.0.0.1:8000/docs>.

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}

curl -X POST http://127.0.0.1:8000/review \
  -H 'Content-Type: application/json' \
  -d '{"pr_url":"https://github.com/psf/requests/pull/5410"}'
```

The latter is a real public PR URL; results depend on GitHub availability, current rate limits, and configured limits. Closed and merged PRs are accepted as well as open PRs.

Illustrative response (fixture values, not a live GitHub result):

```json
{
  "owner": "octocat",
  "repository": "Hello-World",
  "number": 1,
  "title": "Fix bug",
  "description": null,
  "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "changed_files": 1,
  "total_diff_bytes": 21,
  "files": [
    {
      "path": "app.py",
      "status": "modified",
      "additions": 1,
      "deletions": 1,
      "patch": "@@ -1 +1 @@\n-old\n+new",
      "patch_available": true,
      "previous_path": null
    }
  ]
}
```

## Architecture

```text
app/main.py     FastAPI routes, lifespan-managed HTTP client, error responses
app/models.py   Strict PR URL validation, upstream schemas, API schemas
app/config.py   Validated environment configuration and secret token handling
app/github.py   Endpoint allowlist, bounded HTTP reads, pagination, diff accounting
app/diff_parser.py       Immutable file/hunk/line mappings and parse diagnostics
app/findings.py          Strict candidate and overall validation report schemas
app/finding_validator.py Exact added-line checks, rejection reasons, deduplication
app/llm.py               Small provider/session protocol
app/gemini_provider.py   Official SDK adapter and native conversation history
app/review_agent.py      Bounded single-agent orchestration
app/context_tool.py      Strict, read-only file-context tool
app/context_security.py  Repository path and credential screening
app/ai_models.py         AI status, usage and structured-output schemas
app/prompts.py           System and final-phase instructions
app/smoke.py             Manual live-test CLI (dry-run by default)
app/gemini_connectivity.py Standalone one-request Gemini connectivity CLI
```

`POST /review` validates the canonical `https://github.com/{owner}/{repo}/pull/{number}` route (optional trailing slash). It rejects query strings, fragments, ports, credentials, encoded paths, other hosts, and non-PR routes. It constructs HTTPS requests to `api.github.com` for PR metadata and file pages only. GitHub response URLs and pagination Link targets are never fetched. Stage 3 separately allows the same repository's Contents API with a validated full head SHA. Redirects and environment proxy configuration are disabled. Returned code and filenames are treated solely as data.

The shared HTTPX client uses explicit 5-second connect/pool and 20-second read/write timeouts, with 20 maximum connections and 10 keep-alive connections. These are per-operation timeouts, not a whole-review deadline. Client resources are closed on application shutdown.

Pagination requests 100 files per page until the validated metadata count is reached. Missing, excess, duplicate, or malformed file records produce an upstream error rather than a successful partial response. GitHub documents a 3000-file cap, so configuration cannot exceed it. Oversized PRs fail rather than silently truncating.

### Error behavior

Validation errors use FastAPI's standard `422` response. Other errors use this shape:

```json
{"error":{"code":"pull_request_not_found","message":"Public pull request not found."}}
```

| HTTP status | Meaning |
| --- | --- |
| 403 | GitHub denied access; may include an unclassified secondary rate limit |
| 404 | Public PR not found or inaccessible |
| 413 | File count, cumulative diff bytes, or upstream response bytes exceeded |
| 429 | GitHub 429, exhausted primary quota, or 403 with Retry-After |
| 502 | Malformed/inconsistent upstream data, network failure, authentication failure, upstream error, or redirect |
| 504 | GitHub request timeout |

Upstream response bodies, tokens, and transport exception details are not exposed in error responses. There are no automatic retries; callers can retry after inspecting rate-limit status.

## Tests

```bash
source .venv/bin/activate
python -m pytest -q
python -c 'from app.main import app; print(app.title)'
```

The suite uses `httpx.MockTransport` and FastAPI's test client. All GitHub traffic is mocked; no network or credentials are needed. Coverage includes valid retrieval, URL/SSRF rejection, missing PRs, pagination, redirects, rate limits, authentication and upstream errors, timeouts, missing patches, renamed files, UTF-8 and cumulative diff limits, file/response limits, malformed data, duplicate paths, zero-file PRs, and client configuration.

## Stage 2: diff parsing and deterministic findings

The Stage 2 layer provides Python interfaces and adds no HTTP endpoints of its own. The existing `/review` route remains a retrieval-only API, including when a returned patch cannot be parsed. Parsing runs explicitly after retrieval and performs no network calls.

### Diff architecture

`parse_patch(patch)` accepts GitHub's per-file unified patch and returns immutable `DiffHunk` objects containing immutable `DiffLine` records. Each record preserves its kind, exact source text without the diff prefix, old/new line numbers where applicable, and a no-newline marker flag. Removed lines have no new number; additions have no old number; context has both.

The parser handles multiple hunks, omitted counts (default 1), zero-count ranges, pure additions/deletions, optional header section text, blank prefixed source lines, optional final framing LF, and `\ No newline at end of file` markers. A zero-count range identifies a boundary after its start coordinate, not a source line. A both-zero hunk is represented with no lines and cannot support a finding. LF separates patch records; CR and other Unicode separators in source content are preserved.

Declared old/new counts must match the body exactly. Hunk coordinates must be ordered and non-overlapping; intervening unchanged gaps must have equal lengths on both sides. Markers must immediately follow an unmarked source record, and no later record may consume the same side after its EOF marker. Invalid headers, ranges, extra records, truncation, overlapping hunks, and inconsistent markers raise `PatchParseError` with `code`, `patch_line` (1-based), and `message`. No partial mapping is returned.

`parse_file(ChangedFile)` returns a `ParsedFile` with `patch_state` equal to `parsed`, `unavailable`, or `malformed`. It preserves current path, previous path, and status even without a mapping. Missing and malformed files have an explicit `issue`; malformed files have no retained hunks, including when only a later hunk fails. Available patch additions/deletions are checked against Stage 1 file totals to detect omitted changed hunks. Added files cannot have old-side records, and removed files cannot have new-side records.

`parse_review(ReviewResponse)` adapts Stage 1 output into a `ParsedReview`, retaining PR identity and base/head SHAs. Duplicate current paths raise `ValueError` because their mappings would be ambiguous. Parsing does not mutate retrieval results. These immutable data objects can be inspected directly or converted with standard-library `dataclasses.asdict` for a future agent.

### Finding schema

`ReviewFinding` requires every field below. It rejects extra fields and type coercion, including string, float, or boolean line numbers.

| Field | Constraint |
| --- | --- |
| `file_path` | Exact current PR path, 1–4096 characters, nonblank, no NUL or line breaks |
| `line_number` | New-side integer, 1–2147483647 |
| `severity` | `critical`, `high`, `medium`, `low` |
| `category` | `bug`, `security`, `performance`, `reliability` |
| `title` | Nonblank single line, maximum 120 characters |
| `explanation` | Nonblank, maximum 4000 characters |
| `suggested_fix` | Nonblank, maximum 4000 characters |

Text fields are trimmed and reject NUL. Paths are preserved exactly, without trimming, case folding, rename aliasing, or path normalization. Length limits are applied to submitted text before trimming. There is no confidence score.

Example candidate:

```json
{
  "file_path": "src/app.py",
  "line_number": 11,
  "severity": "high",
  "category": "bug",
  "title": "Missing bounds check",
  "explanation": "An empty input raises IndexError.",
  "suggested_fix": "Check whether the input is empty before indexing."
}
```

### Validation and agent interface

```python
from app.diff_parser import parse_review
from app.finding_validator import validate_findings
from app.findings import ReviewReport
from app.models import ReviewResponse


def validate_agent_output(
    retrieved: ReviewResponse, raw_candidates: list[object]
) -> ReviewReport:
    parsed = parse_review(retrieved)
    # A future agent will inspect parsed.files/hunks/lines before proposing findings.
    return validate_findings(parsed, raw_candidates)
```

`validate_findings` independently validates each raw dictionary (or revalidates a `ReviewFinding` instance), then checks its exact current file path and actual parsed source record. A valid location must have a new-side number and kind `added`. Replacement lines are the added half of a removal/addition pair; no semantic modified-line pairing is inferred. Context lines, old-side coordinates, and numbers merely falling within a hunk or between hunks are insufficient.

`ReviewReport` contains PR identity and SHAs, accepted `findings`, rejected candidates, and the explicit `validation_scope: "schema_and_diff_location"`. Tuples serialize as JSON arrays. Each rejection contains the zero-based original `candidate_index`, a machine-readable code, and a message; schema failures include field locations and Pydantic error codes without echoing the input. Rejected payloads are not retained.

| Rejection code | Meaning |
| --- | --- |
| `schema_invalid` | Required shape, type, or field constraints failed |
| `file_not_in_pr` | Exact current path is absent; old rename paths are not aliases |
| `deleted_file` | Removed files have no new-side review anchor |
| `patch_unavailable` | GitHub supplied no patch |
| `patch_malformed` | File mapping was discarded after a parse or metadata consistency failure |
| `line_not_in_diff` | Number does not identify a provided new-side source record |
| `line_not_added` | Source record is context, not an addition/replacement |
| `duplicate_finding` | An earlier accepted candidate has the same deterministic issue identity |

Checks use the table's order, with duplicate detection last. Duplicate identity is exact path + new-side line + category + title normalized by collapsing whitespace and Unicode case folding. Severity, explanation, and fix wording do not create a new issue. The first accepted candidate wins; `duplicate_of` points to its original input index. Different paths, line numbers, categories, or normalized titles remain separate. This rule is deterministic, not semantic similarity detection: paraphrases can remain separate and distinct issues sharing the key can collide.

**A structurally valid finding is not evidence that its alleged bug is real.** This layer validates schema and location only. It does not verify explanations, severity, suggested fixes, exploitability, or runtime behavior. An empty accepted list is not a claim that the PR is bug-free.

**Deletion-only issues cannot be anchored under this schema.** The future agent must not invent a new-side line or attach an issue to unrelated context. Entire deleted files are rejected explicitly; deleted ranges within surviving files have no eligible location unless the issue independently concerns an actual added line. A future PR-level or old-side finding model would need a separate design.

Full `git diff` documents with file headers, combined merge diffs, and binary patch formats are outside this parser's input contract. GitHub truncation inside a hunk is detected; omitted changed hunks are detected when totals disagree. A syntactically valid patch with matching totals still cannot prove source completeness or correctness, and absolute coordinates are trusted from GitHub headers. Lines outside the supplied diff cannot be validated. There is no whole-file retrieval or immutable GitHub snapshot enforcement in this stage.

Stage 3 consumes these interfaces, binds results to base/head SHAs, and applies `validate_findings` to every candidate. Its provider/tool budgets and context retrieval are described below. Factual evaluation benchmarks remain future work.

## Stage 3: opt-in Gemini reviews

### Free-tier setup and model selection

Official Google documentation was checked on **2026-10-27**. The [Gemini pricing page](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash) lists standard-tier text input and output for `gemini-3.8-flash` as free, and its [model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash) lists function calling and structured outputs as supported. This is documentation verification, not a live model or account test. No live Gemini request was made during implementation.

1. Create a Gemini Developer API key in [Google AI Studio](https://aistudio.google.com/apikey), using a project **without billing enabled**. Do not upgrade the project or attach a billing account.
2. Recheck the model's current availability, free-tier eligibility, and your account's quota in AI Studio. Model IDs, region availability and quotas can change.
3. Set `GEMINI_API_KEY` locally, choose `GEMINI_MODEL` explicitly, and set `GEMINI_FREE_TIER_CONFIRMED=true` only after those checks. The documented example is `GEMINI_MODEL=gemini-3.8-flash`.
4. Keep the key in your shell environment or ignored `.env`; never paste it into repository files or API requests. Load `.env` using the configuration commands above, then restart the server.

There is **no hardcoded runtime model, model fallback, billing operation, automatic retry, or paid built-in tool**. The SDK uses the Gemini Developer API, with Vertex AI/enterprise mode explicitly disabled. AI reviews return 503 unless key, model and free-tier confirmation are all configured. Retrieval and health remain available without any Gemini key.

`GEMINI_FREE_TIER_CONFIRMED` is a local operator attestation, not a billing API check. The app cannot establish your project's billing status or force a paid project's requests onto a free tier. Keep billing disabled to enforce zero monetary cost; do not configure a paid-only model. See Google's [billing documentation](https://ai.google.dev/gemini-api/docs/billing). Free-tier data handling may allow Google to use submitted content to improve products, as described on the pricing page; use public, nonsensitive repository data only.

### Agent flow and real function calling

1. The provider validates configuration, then the existing GitHub client retrieves the PR.
2. The existing parser maps every file. Missing, malformed, or count-inconsistent patches stop the AI review as `incomplete` before any model request.
3. The agent builds one JSON context containing PR identity, title/body, SHAs and parsed source records. It rejects an oversized context in full; it never silently slices a PR.
4. Gemini receives the system instruction and one declared tool, `get_file_context(path, start_line, end_line)`, in `AUTO` calling mode. Gemini decides whether to request it.
5. The application checks the entire returned tool batch against the remaining budget, validates every call, and executes only the approved read-only operation. Each attempted call counts, including invalid calls. An over-budget batch executes no calls.
6. Tool results return to Gemini with their original call IDs/names and order. The provider retains the original model `Content`, including thought signatures; it does not reconstruct or discard signed parts. SDK automatic function execution is explicitly disabled.
7. One model request is reserved for final structured JSON, with tools absent. The final schema is derived from the Stage 2 finding model and includes `complete` plus `findings`. This separate phase avoids assuming that a model can combine tools and structured output in the same request.
8. The application strictly parses the JSON, rejects duplicate JSON keys/non-finite numbers and malformed envelopes, then independently runs every raw candidate through Stage 2 schema/location/deduplication validation. There is no fallback to accepting prose or fenced JSON.

The agent depends on the small `ReviewSession` protocol and a session factory. Gemini-specific SDK types, history and error translation stay in the provider module. Each review owns and closes its session; there is no shared conversation or cross-review history.

### Controlled source tool

The tool uses `GET /repos/{approved_owner}/{approved_repo}/contents/{validated_path}?ref={exact_head_sha}`. Owner, repository and SHA come from the retrieved PR, never from model arguments. Files outside the changed set may be read when they belong to that repository at the head commit. This supports helpers/imports without authorizing another repository or moving branch.

Paths must be repository-relative and contain no traversal segments, encoded paths, URLs, query/fragment characters, backslashes or control bytes. The application never opens them on the local filesystem. It uses only base64 content returned by the fixed GitHub API host, ignoring `download_url` and redirects. Directories, explicit symlink/submodule responses, binary/non-UTF-8 content, invalid sizes and oversized responses/files are rejected. GitHub may itself resolve an in-repository symlink to a file; there is no local symlink traversal or external-target download.

Line bounds are strict integers. Up to 200 lines are returned with their actual numbers and head SHA. An end beyond EOF is explicitly reported using the actual end and total line count; a start beyond EOF is rejected. File and snippet limits apply before source text is passed to Gemini. Credential-like paths and detected secrets are withheld.

### AI configuration and budgets

All names below are process environment variables; `.env.example` contains the defaults. The first five count/context limits may be reduced but cannot exceed the stated defaults.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | unset | Secret Gemini Developer API key |
| `GEMINI_MODEL` | unset | Explicit model ID; no automatic default |
| `GEMINI_FREE_TIER_CONFIRMED` | `false` | Confirmation of a free-tier model and non-billed project |
| `AI_MAX_TOOL_CALLS` | `3` | Total attempted tools, across all responses |
| `AI_MAX_LLM_REQUESTS` | `4` | Total analysis/final request attempts; at least 2 needed |
| `AI_MAX_CONTEXT_CHARS` | `20000` | Entire initial JSON context, including metadata and line mapping |
| `AI_MAX_TOOL_LINES` | `200` | Maximum requested lines per tool |
| `AI_MAX_CANDIDATES` | `10` | Maximum final candidate count; excess fails the review |
| `AI_THINKING_LEVEL` | unset | Optional `low`, `medium`, or `high`; unset preserves provider default |
| `AI_MAX_OUTPUT_TOKENS` | `4096` | Model response-token budget per request (maximum 8192) |
| `AI_MODEL_TIMEOUT_SECONDS` | `30` | Per-model request deadline (maximum 60) |
| `AI_REVIEW_DEADLINE_SECONDS` | `120` | Whole retrieval/tool/model/validation deadline (maximum 180) |
| `AI_MAX_FILE_BYTES` | `262144` | Maximum decoded source-file size (maximum 1000000) |
| `AI_MAX_TOOL_CONTEXT_CHARS` | `10000` | Serialized successful source-tool payload (maximum 20000) |
| `AI_MAX_RESPONSE_CHARS` | `32000` | Maximum retained model content, including signatures (maximum 64000) |
| `AI_MAX_REQUEST_CHARS` | `160000` | Pre-SDK serialized history/config per request (maximum 200000) |

The SDK request bound includes history, system instruction and schemas, measured as JSON characters before SDK wire conversion. It is not an exact billable-token count. Later calls resend bounded history. HTTP timeouts and connection limits are explicit; redirects and environment proxies are disabled. Cleanup has a separate maximum two-second grace period after the review deadline. Model request usage counts attempted provider calls, including failures or provider-side input rejection.

The last allowed request always uses the tools-disabled structured phase. If more context is required, Gemini must report `complete=false`. Tool failures are fed back when possible, but **any failed tool attempt conservatively marks the overall result incomplete**, even if a later call recovers. Quota/network failures stop execution immediately. There are no repair retries for invalid model output.

### AI API request and response

Once free-tier setup and live execution are approved, start the same server and call:

```bash
curl -X POST http://127.0.0.1:8000/review/ai \
  -H 'Content-Type: application/json' \
  -d '{"pr_url":"https://github.com/psf/requests/pull/5410"}'
```

Illustrative successful response, **not a live result**:

```json
{
  "status": "completed",
  "model": "gemini-3.8-flash",
  "report": {
    "owner": "owner",
    "repository": "repo",
    "number": 1,
    "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "validation_scope": "schema_and_diff_location",
    "findings": [],
    "rejected": []
  },
  "issues": [],
  "usage": {
    "llm_requests": 2,
    "tool_calls": 0,
    "initial_context_chars": 1200,
    "tool_context_chars": 0
  }
}
```

`report.findings` contains structurally accepted Stage 2 findings, while `report.rejected` preserves schema/location/duplicate rejection codes and candidate indices. `completed` means the bounded execution finished, **not** that all candidates were accepted, bugs were factually proven, or the PR is bug-free. `incomplete` identifies unsupported input, exhausted limits, missing context, failed tools, truncation, or a model-reported incomplete review. `error` identifies configuration, upstream or protocol failures. `issues` contains safe machine-readable execution diagnostics; `report` is null when no final envelope was validated. An incomplete response can contain findings, but must never be displayed as a completed review.

| HTTP status | Typical AI outcome |
| --- | --- |
| 200 | Completed execution, with accepted and/or rejected candidates |
| 409 | Incomplete: insufficient request budget or over-budget tool batch |
| 413 | Incomplete: context, response or candidate-count limit exceeded |
| 422 | Incomplete: unavailable/malformed patches, failed context tool, model truncation or `complete=false` |
| 429 | Error: GitHub/Gemini rate limit or exhausted quota |
| 502 | Error: malformed final JSON, blocked/invalid SDK output, upstream or transport failure |
| 503 | Error: missing key, model or free-tier confirmation |
| 504 | Model timeout (`error`) or overall deadline (`incomplete`) |

GitHub retrieval errors retain their relevant HTTP status, including 403/404/413. Invalid request URLs still use FastAPI's ordinary 422 validation response. Gemini 429 responses produce `gemini_quota_exhausted`; no automatic retry, provider fallback, quota purchase or billing upgrade occurs. Wait for the account's quota reset and initiate another review manually.

### Diagnosing Gemini API failures without exposing requests

The first reported live test failed on its first model request, before tools or the final structured-output phase. The original handler collapsed Google `APIError` exceptions into a generic `gemini_upstream_error`, losing the original status/category/message. The original response cannot be reconstructed from that summary.

Offline inspection found a likely HTTP 400 cause: the adapter explicitly sent `candidate_count=1`. Google's [Gemini 3.8 migration checklist](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.8#migration-checklist) says this parameter is unsupported in Gemini 3 and later. It has been removed; the application still rejects responses unless they contain exactly one candidate. Deprecated `temperature`, `top_p`, `top_k`, legacy `thinking_budget`, and unsupported `minimal` thinking were not being sent. Initial, tool-response and final requests end with user input, rather than a prefilled model turn. The original failure's precise cause remains unconfirmed without its discarded provider response; no live request was made during this diagnosis.

`google-genai` raises `ClientError` for HTTP 4xx, `ServerError` for HTTP 5xx, and base `APIError` for other unexpected statuses. They inherit from `APIError`. Safe diagnostics now survive provider → agent → API/CLI as `issues[].provider_error`:

```json
{
  "code": "gemini_request_rejected",
  "message": "Google rejected unsupported parameter candidate_count.",
  "file_path": null,
  "provider_error": {
    "http_status": 400,
    "api_status": "INVALID_ARGUMENT",
    "reason": null,
    "sdk_exception": "ClientError",
    "message": "Google rejected unsupported parameter candidate_count."
  }
}
```

This is a mocked diagnostic example, not the recovered live error. The upstream HTTP status is preserved separately from PR Sentinel's HTTP status: provider authentication, invalid-request, unavailable-model and service failures remain HTTP 502 to this API's caller; quota failures remain HTTP 429. The CLI now prints its corresponding application `http_status` as well as the nested provider diagnostic.

Google 400 responses use `gemini_request_rejected`, 401/403 use `gemini_access_denied`, 404 uses `gemini_model_unavailable`, 429 uses `gemini_quota_exhausted`, and other API failures use `gemini_upstream_error`. Google API status categories and recognized `google.rpc.ErrorInfo` reasons are preserved via explicit allowlists; absent/unrecognized categories remain null. Messages are bounded, reconstructed summaries using fixed wording and known parameter names. Unrecognized raw text, URLs, headers, details, prompts, code, keys and project metadata are never forwarded or logged. Some original message detail is intentionally lost to preserve this boundary. Transport/SDK parsing exceptions retain generic safe messages and do not invent an HTTP status.

Current documentation lists this model and free standard-tier input/output, but does not establish any particular project's access, regional eligibility or quota. A 400 may also indicate a prerequisite, 404 a missing model/method/version, and 5xx a transient provider failure. Inspect the new diagnostic when one is available; do not enable billing or switch models automatically. No automatic retry has been added. The standalone connectivity diagnostic below is separately opt-in. Offline tests exercise these cases through the actual SDK with mocked transports, including leak checks and preservation through the API and CLI.

### Investigating Google 504 DEADLINE_EXCEEDED

A Google HTTP 504 with `api_status: DEADLINE_EXCEEDED` and SDK `ServerError` is an upstream response. It is different from a local HTTPX/asyncio timeout (`gemini_timeout`, local HTTP 504, no fabricated Google status) or the whole-review deadline (`review_deadline_exceeded`, incomplete). PR Sentinel maps Google's upstream 504 to local HTTP 502 and preserves the upstream status in the diagnostic.

These layers are linked by the SDK. Inspection and mocked wire tests of installed `google-genai==1.75.0` confirm that `HttpOptions(timeout=30000)` sends **`X-Server-Timeout: 30`**. It also passes a 30-second request timeout to HTTPX, overriding the client's base timeout for that request. PR Sentinel wraps the call in a separate 30-second `asyncio.timeout`; the entire review has a separate 120-second deadline. Consequently, a true Google 504 may still be induced by the SDK-advertised server deadline. It does not prove independent backend failure, nor prove that the local coroutine timed out. The original elapsed time and server execution details are unavailable, so precise causation cannot be determined offline. A 1491-character initial context alone cannot establish how long reasoning or provider queueing should take.

The adapter previously left thinking unspecified. Google's [3.8 thinking/latency guidance](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.8#understanding-reasoning-levels) identifies `medium` as the default, `low` as supported with lower time-to-answer, and `minimal` as unsupported. The [thinking guide](https://ai.google.dev/gemini-api/docs/thinking#controlling-thinking) recommends lowering thinking effort rather than using a tiny output-token cutoff to reduce latency.

`AI_THINKING_LEVEL=low` is now an **explicit opt-in** for latency-focused verification; it applies to both analysis and final structured requests using the SDK's `ThinkingConfig`. Leaving it unset/blank preserves the provider default and production review quality. `minimal`, disabling thinking, and legacy numeric thinking budgets are not exposed. Lower thinking may reduce finding quality and does not guarantee completion within 30 seconds. Output tokens remain 4096 by default. Model, request/tool/context/candidate limits, retries, HTTP deadlines and overall deadline are unchanged. The local `.env` was not modified by this investigation.

The dry-run smoke plan now reports thinking selection, the per-model timeout, the SDK-advertised server timeout and the overall deadline together. These are configuration values, not measurements from the earlier live request. No live request is required to inspect them. The smallest prospective mitigation is to opt into `low` before a separately approved test, with the existing deadlines retained; do not automatically retry, extend deadlines, switch models or enable billing. No live test was performed while implementing this change.

### Security boundaries and limitations

Repository metadata and code stay in a marked untrusted JSON payload; system instructions define the only tool and explicitly forbid repository instructions from changing authority. Tool arguments never supply host, owner, repository, ref or credentials. No shell, code execution, arbitrary local reads, GitHub writes, grounding/search, hosted execution, caching service, or other paid tool is enabled.

API keys are secret configuration values, sent only in provider authentication, and never deliberately included in prompts, results or logs. Raw repository fields are checked before JSON escaping; known configured credentials and common credential patterns are blocked. This screening is heuristic: it can produce false positives and cannot guarantee detection of every secret format. Do not submit sensitive data. Prompt injection can still influence an LLM's claims; the narrow tool boundary prevents it from authorizing new capabilities, and deterministic validation does not establish factual truth.

The API has no authentication or global rate/concurrency control: bind it to localhost. Budgets are per review, not per day or account. Billing status and current quota must be checked in AI Studio. Model availability/schema behavior can change; unsupported configurations fail explicitly rather than falling back. Cancellation stops local work but cannot guarantee a provider stops computation already received. GitHub snapshots can race during retrieval, and head SHAs from some fork PRs may be unavailable through the approved base repository; there is no cross-repository fallback. Missing/binary patches and pure renames without a patch are conservatively incomplete. Deletion-only issues still have no valid new-side finding anchor.

### Manual free-tier fallback: Gemini 3.5 Flash-Lite

Official documentation checked on **2026-10-27** lists the stable model ID **`gemini-3.5-flash-lite`**, with [function calling and structured outputs supported](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite). The [pricing table](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.5-flash-lite) lists standard input and output (including thinking) as free on the Free Tier. This confirms documented eligibility, not this account's available quota or a live service guarantee. Keep the existing project without billing enabled and use its existing free-tier key/confirmation. No billing setup or paid built-in tool is needed.

The [thinking table](https://ai.google.dev/gemini-api/docs/thinking#controlling-thinking) lists `minimal` (default), `low`, `medium` and `high` for Flash-Lite. Existing explicit `low`/`medium`/`high` settings are compatible. Leaving `AI_THINKING_LEVEL` blank omits the SDK setting and inherits the fallback's `minimal` default; explicit `minimal` remains rejected by our shared settings to protect Gemini 3.8 Flash, which does not support it. No runtime code change or model-specific branch is needed to select the fallback through `GEMINI_MODEL`.

The existing provider omits unsupported `candidate_count`, deprecated sampling parameters, legacy thinking budgets and prefilled model turns. The configured 4096-token output cap and bounded initial context fit within the fallback's documented 65536 output / 1048576 input token limits. Review tools and final structured JSON remain in separate phases. Our actual-SDK mocked tests cover both models' endpoint selection, optional/supported thinking, preserved call IDs/signatures, final schema, one-request connectivity and the full retrieval/tool/validated-review flow. Mocks verify application behavior and serialization; they cannot establish live model access, availability or review accuracy.

For a temporary connectivity check, load the existing environment using the commands below, then preview this override without a network request:

```bash
GEMINI_MODEL=gemini-3.5-flash-lite AI_THINKING_LEVEL= \
  .venv/bin/python -m app.gemini_connectivity
```

After separate manual approval, exactly one live attempt can use:

```bash
GEMINI_MODEL=gemini-3.5-flash-lite AI_THINKING_LEVEL= \
  .venv/bin/python -m app.gemini_connectivity --execute
```

These assignments apply only to that command: the existing shell and local `.env` primary model are preserved. The blank thinking override intentionally inherits the fallback's minimal default for the simple OK probe; use `AI_THINKING_LEVEL=low` instead if you want explicit low effort. No requests, tools, deadlines, retries, security controls or validation budgets change. There is no automatic failover after a 503. A fast 503 on a tiny prompt is consistent with remote unavailability, but does not prove a model-specific overload; the fallback may also be unavailable. A successful OK probe would demonstrate one real generation, not the fallback's suitability for accurate code review. No live fallback request was made during this investigation.

### Standalone Gemini connectivity diagnostic

Use this to isolate model/API connectivity from PR retrieval, review prompts, tools and structured output. It shares the review SDK client factory, including the configured model, thinking selection, HTTP timeout/server timeout header, fixed API host, disabled redirects/proxies and `attempts=1` (no retries). It performs no GitHub requests and makes no model-availability or quota probe.

The CLI first validates local settings and checks that key, model and free-tier confirmation are configured. These checks do not verify credentials, account eligibility, billing state or remote model health. Like the application, it reads the process environment through `Settings.from_env`; it does not automatically load `.env`.

From the project directory, load your existing configuration, then run local checks only:

```bash
source .venv/bin/activate
set -a
source .env
set +a
python -m app.gemini_connectivity
```

This mode performs zero network requests and does not construct an SDK client. Invalid configuration exits with code 2 and cannot send a request even with `--execute`. Environment validation errors do not print their original values.

After manual approval, exactly one connectivity generation attempt is available with:

```bash
python -m app.gemini_connectivity --execute
```

It prints the local check/plan JSON, a flushed `generation_starting` record, then a flushed final `generation` result JSON. The initial `requests_sent: 0` describes only the pre-request local checks; `will_execute` and `execution_requested` indicate whether the live path will run. A configuration failure prints `skip_reason: configuration_error` and exits 2; a dry run prints `skip_reason: dry_run` and exits 0. While the model request is pending, the final result is not yet available. Ctrl+C/cancellation is reported with sanitized interruption JSON and exit code 130; whether Google received or completed that request is unknown. Unexpected CLI runner exceptions also produce a safe final JSON instead of exposing exception details. Hard termination (for example SIGKILL) or a broken output destination cannot guarantee final output. The only prompt is **`Reply with OK.`** There is no repository context, tool declaration, system instruction, response schema, or second request. Thinking selection and the existing output-token cap are retained; no small thought-token cutoff is imposed that could confuse truncation with connectivity failure. Automatic function execution remains disabled.

Results contain `elapsed_seconds`, the real upstream `http_status`, sanitized `api_status`, `generation_succeeded`, `reply_is_ok`, and `requests_attempted`. Elapsed time covers client setup, the one attempt and response processing, excluding cleanup. Google errors additionally include the safe SDK exception class, recognized reason and reconstructed message from the existing sanitizer. Local transport/wall timeouts have `error_code: local_timeout`; HTTP/API status stays null when no HTTP response was received. SDK parsing errors are reported without raw exception text.

A successful normal text generation sets `generation_succeeded=true`; `reply_is_ok` checks the exact trimmed reply. Exit code 0 requires both. Exit code 1 means a failed attempt or unexpected reply. HTTP 200 with an incomplete/empty generation is distinct from a network failure. Generated text, keys, authentication headers, raw error details and SDK request bodies are never printed. A successful connectivity check does not verify review tools, finding accuracy or later structured output compatibility.

Tests cover the actual SDK payload, exactly one request, configured thinking, timeout headers, 503/504 with no retries, local read/wall timeouts, dry-run/configuration behavior, and safe output. This diagnostic was tested only against mocked HTTP; its live mode was not executed during implementation.

### Mock tests and approved manual smoke test

```bash
python -m pytest -q
ruff check app tests
python -m compileall -q app tests
```

All tests mock external HTTP. Transport-level tests run the actual `google-genai` serialization and response handling against `httpx.MockTransport`, including function-call IDs, thought signatures, multiple calls, no retries, quotas and malformed output. A test fixture rejects accidental real HTTP calls. The original 172 cases remain, with the Stage 2 OpenAPI assertion updated solely to include the explicitly requested AI route.

First preview one review's limits (no network and no Gemini key required):

```bash
python -m app.smoke --pr-url https://github.com/psf/requests/pull/5410
```

The preview prints the model, PR, maximum requests, initial/context/history limits, output tokens and deadline. Defaults allow **at most 4 Gemini requests and 3 context-tool calls**, with **20000 initial-context characters**, **10000 source-context characters per tool**, and **160000 pre-SDK input characters per model request**. The minimum successful flow uses 2 model requests. A tool call performs one GitHub Contents request; PR metadata/files also require read-only GitHub requests.

Only after explicit approval for live execution, configure the free-tier key/model/confirmation, review the dry-run output, and run:

```bash
python -m app.smoke --pr-url https://github.com/psf/requests/pull/5410 --execute
```

`--execute` approves exactly one bounded review, not unlimited calls. The script prints the plan before execution, does not retry, and exits nonzero for incomplete/error outcomes. This command was **not executed** during implementation.

## Completed features and limitations

- Health and typed read-only review endpoints with OpenAPI documentation.
- Public PR metadata, base/head SHAs, statuses, additions, deletions, previous paths, and patches where available.
- Bounded pagination and explicit failures for invalid/incomplete responses.
- Strict URL validation, fixed endpoint construction, disabled redirects, optional token, and safe errors.
- Immutable unified diff parsing and strict review finding/report schemas.
- Deterministic added-line validation, machine-readable rejections, and duplicate identification.
- Official Gemini SDK integration with a manual, bounded read-only function loop.
- Opt-in AI endpoint, structured execution statuses, and a dry-run-first smoke command.
- Offline unit/API/SDK transport tests and a standard-library-generated diff matrix.

GitHub may omit patches for binary or large files; `patch_available: false` and `patch: null` indicate that absence. An available patch may still be truncated by GitHub; availability does not guarantee completeness, and this stage cannot determine the omission reason. The diff size limit counts returned patch bytes only. Metadata and file pages are separate reads: concurrent PR updates can produce inconsistent snapshots. Count mismatches are detected, but same-count updates are not. There is no caching, authentication for this local API, global concurrency/rate control, or whole-review time budget. Keep the server on localhost for local development.

## Future stages

1. **Completed Stage 2:** parse unified diffs, validate finding schemas and precise changed-line locations, and test deterministic edge cases.
2. **Completed Stage 3:** official `google-genai` integration, bounded function calling, and validated AI review reports. Live Gemini verification still requires explicit approval.
3. **Completed Stage 4:** versioned synthetic fixtures, deterministic replay scoring, and bounded opt-in live evaluation.
4. Add Docker and GitHub Actions after the core review pipeline is tested.

References: [GitHub pull request REST API](https://docs.github.com/en/rest/pulls/pulls), [HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/).

## Stage 4 — reproducible evaluation

Stage 4 adds an offline evaluator and a separately opted-in live runner. It does not change `/review`, `/review/ai`, the Gemini provider, prompts, validation, or their budgets. No live evaluation was performed during implementation. A previously successful user-run review is not a benchmark result.

Three kinds of evidence remain separate:

1. **Deterministic correctness tests:** Pytest verifies fixtures, scoring rules, safety limits, and the existing agent using fake sessions or the real SDK with mocked HTTP. These establish software behavior, not Gemini accuracy.
2. **Offline replay:** `python -m app.evaluation offline` evaluates saved predictions with no API calls and without reading Gemini environment configuration. Inputs declare `synthetic`, `recorded-live`, or `unverified-replay` provenance. This declaration is not cryptographic proof that an API call happened.
3. **Live evaluation:** `python -m app.evaluation live` previews by default. Only `--execute` calls Gemini, through the existing `ReviewAgent`, parser, manual context tool, `GeminiSession`, and Stage 2 validator. Fixture-backed retrieval replaces GitHub HTTP; labels and semantic rubrics never enter model context.

### Dataset and ground truth

`benchmarks/v1.json` contains **25 original artificial Python PRs**: 17 defect-bearing cases, six clean negatives, and two ambiguous cases excluded from all primary scores. There are **19 labeled defects: eight correctness (`bug`), five security, four reliability, and two performance**. Three cases require unchanged-file context, two contain multiple defects, and one contains separated diff hunks. Severity distribution is two critical, eleven high, six medium. The dataset currently has no low-severity defect.

`benchmarks/build_dataset.py` is the author-maintained source of these fixtures. Labels were constructed from explicit contracts and concrete counterexamples, independently of model predictions. Each case records an ID, title, description, provenance, complete base/head Python snapshots, GitHub-style unified patch bodies, required context files, and either expected defects or an exclusion rationale. Each defect records an ID, category, severity, canonical line, inclusive acceptable range, failure rationale, and semantic concept groups. Clean cases have an empty expected list; ambiguous cases have an explicit nonempty `excluded_reason`.

Examples include division by zero on empty input, dropping the final invoice item, SQL/shell injection, disabled TLS verification, unsafe deserialization, missing timeouts/rollback, event-loop blocking, and quadratic membership checks. Clean cases fix or safely refactor analogous behavior. Ambiguous rounding and cache invalidation policies have no defensible ground truth without additional requirements, so they are excluded rather than treated as clean.

The loader rejects duplicate IDs/paths, unsupported versions, malformed Python syntax, unsafe paths, credential-like content, diffs inconsistent with snapshots, invalid context declarations, and labels outside added lines. Patches are regenerated with standard-library `difflib` and independently parsed by Stage 2. Snapshots are parsed, never executed. Dataset hashes include labels and source snapshots. Any fixture or rubric edit changes the hash and invalidates old prediction files; intentional dataset revisions should also change the version. Live mode additionally requires the pinned reviewed dataset hash. Custom fixture files are accepted only offline. The bundled data is a repository asset; run these commands from this checkout/editable installation.

### Matching and metric definitions

Matching version: `rubric-location-bipartite-v1`. The following rules are deterministic:

- Every prediction first passes the existing strict finding schema and added-line validator. Invalid schema, wrong file, non-added line, and out-of-diff locations remain false positives rather than disappearing. Invalid prediction objects inside a valid prediction file are scoreable failures; an invalid outer file/envelope fails the CLI.
- A candidate match requires the exact file, a line within the label's accepted range, and at least one literal case-insensitive phrase from **every** concept group in the finding's title plus explanation. Whitespace in prediction prose is collapsed. Suggested fixes do not establish detection. The rubric checks issue identity rather than relying on location alone; it is a limited lexical heuristic, not a semantic truth oracle.
- A maximum-cardinality bipartite assignment makes each prediction and each defect eligible for at most one true positive. Among equally large assignments, exact canonical-line matches are preferred; remaining ties use prediction/label order. Results are reproducible for the same ordered inputs. There is no LLM judge, embedding service, or fuzzy threshold.
- A matched prediction is a TP even if category or severity is wrong. Those disagreements are explicitly recorded in the match. Unmatched rubric/location-compatible predictions are reported as duplicates and count as FPs. A recognized defect on a different valid added line is `wrong_location`; Stage 2 invalid locations retain their more specific rejection codes. Unrecognized claims are `unmatched_claim`.
- All selected non-excluded cases are scored, including errors and incomplete reviews. Empty findings on a failed positive case yield false negatives. Unselected and unattempted cases are listed, not silently treated as successful empty reviews. Excluded cases are reported but contribute nothing to metrics.

| Metric | Definition |
| --- | --- |
| TP | Number of one-to-one rubric-and-location matched pairs |
| FP | Number of predictions minus TP, including invalid and duplicate predictions |
| FN | Number of expected defects minus TP |
| Precision | TP / (TP + FP) |
| Recall | TP / (TP + FN) |
| F1 | 2 TP / (2 TP + FP + FN) |
| Clean-case false-positive rate | Selected scored clean cases with at least one prediction / selected scored clean cases |
| Location accuracy | Schema-valid predictions recognizing any labeled defect and locating one in its exact file/accepted range / all schema-valid predictions recognizing any labeled defect; wrong-file/out-of-diff predictions are in the denominator |
| Exact-location share | Matched predictions on the canonical line / TP; acceptable-range matches are also counted separately |
| Severity agreement | Matched pairs with identical severity / TP |
| Category agreement | Matched pairs with identical category / TP |
| Error / incomplete rate | Selected scored cases with that execution status / selected scored cases |

Every zero denominator yields JSON `null` / terminal `N/A`, including an all-clean/no-prediction F1. Location accuracy includes duplicates and measures placement only; it is not detection accuracy. Aggregate detection scores are micro-averaged. For per-category **detection** metrics, matched TPs/FNs use the ground-truth category; unmatched valid FPs use the predicted category. Invalid predictions form a separate `unclassified_false_positives` bucket. Thus a wrong category can earn detection credit under the true category while lowering category agreement; per-category scores are not classification accuracy.

Per-case JSON includes TP pairs with expected/predicted examples, FP reasons, missing ground-truth examples, exact/range counts, classification agreements, and execution status. The aggregate includes all of these errors rather than reporting only accepted findings. A recording proxy captures the final structured candidates only after the production agent accepts and secret-screens the envelope, preserving rejected candidates for scoring. Invalid or secret-bearing envelopes remain execution failures with no recoverable predictions; this limitation is visible in execution status/rates.

### Offline commands and synthetic example

Run the supplied demonstration without credentials or network access (choose a fresh output filename):

```bash
.venv/bin/python -m app.evaluation offline \
  --predictions benchmarks/synthetic_predictions.json \
  --output /tmp/pr-sentinel-synthetic-evaluation.json
```

The checked-in `benchmarks/synthetic_report.json` is reproducible from those inputs. It is **synthetic evaluator test data, not real Gemini performance**:

```text
SYNTHETIC — 7 scored cases; 1 excluded
TP=3 FP=3 FN=2 | precision=0.500 recall=0.600 F1=0.545
Clean-case FP rate=0.500; location accuracy=0.800
```

The examples deliberately include an exact match, a duplicate, a missing defect, an invalid location, a category/severity mismatch, an acceptable-range match, a clean-case FP, a correct clean result, and an excluded ambiguous case. `benchmarks/build_demo.py` constructs these artificial predictions from authored labels solely to exercise the evaluator. It must never be used as evidence of model performance.

Prediction files have `format_version: "1.0"`, `dataset_version`, `dataset_sha256`, `provenance`, optional `model`, and a `cases` list. Each case has `case_id`, `findings` (the existing ReviewFinding dictionaries, including invalid candidate objects if recorded), and `status` (`completed`, `incomplete`, `error`). IDs must be unique and known. Subsets are supported and coverage is explicit. Unknown/duplicate JSON keys, non-finite JSON numbers, identity mismatches, and oversized input files are rejected. No silent fallback to another dataset occurs.

Replay a saved live output with the same offline command, substituting `--predictions <run-directory>/predictions.json`. Offline exit code 0 means scoring completed, not that the model met an accuracy target; 2 means invalid inputs/configuration or an unusable/existing output destination. Output files are never silently overwritten.

### Future manually approved live evaluation

The app reads process environment, not `.env` automatically. After loading your existing configuration for a project without billing, **preview only** a single case:

```bash
GEMINI_MODEL=gemini-3.5-flash-lite AI_THINKING_LEVEL= \
  .venv/bin/python -m app.evaluation live --cases inclusive-limit
```

Or preview one defect and one clean/context case:

```bash
GEMINI_MODEL=gemini-3.5-flash-lite AI_THINKING_LEVEL= \
  .venv/bin/python -m app.evaluation live --cases inclusive-limit clean-context
```

These commands send zero requests, do not construct an SDK client, and do not require an API key. The temporary overrides do not edit `.env` or permanently change the primary model. The preview includes case identities/titles, model/thinking settings, per-review limits, maximum aggregate requests/tools, runtime versions, and an application source hash.

Only after reviewing the preview and manually approving execution, one bounded case can be run with:

```bash
GEMINI_MODEL=gemini-3.5-flash-lite AI_THINKING_LEVEL= \
  .venv/bin/python -m app.evaluation live --cases inclusive-limit \
  --execute --output evaluation-runs/flash-lite-001
```

Live mode enforces one to three distinct non-excluded cases, sequential execution, the existing configuration/free-tier confirmation, and a new output directory before any request. At default settings one case permits at most four model requests and three tools; three cases permit at most twelve model requests and nine tools. Existing context, source-size, output, HTTP, and whole-review deadline limits remain in force. There are no retries, model switches, GitHub requests, or repository publication. Quota exhaustion stops immediately; two consecutive upstream/transport failures stop the run; other errors and budget/incomplete outcomes stop immediately. A second case after a single transient upstream failure is a different planned review, not a retry of the failed case.

Each completed case checkpoints `predictions.json` and `run.json`; `plan.json` records configuration and runtime identity, and normal CLI completion adds `evaluation.json`. `run.json` preserves the application's structured review response, including sanitized errors and usage, alongside elapsed time. It does not store SDK headers, keys, hidden reasoning, or unfiltered SDK bodies. Normalized predictions preserve individually rejected candidates only after secret checks. Per-case elapsed time includes the agent's retrieval, generation, tool/validation work and cleanup; it excludes disk writes and setup. Aggregate and per-case LLM requests, tool calls, initial-context characters and tool-context characters are recorded. **Input/output tokens remain null/unavailable:** the current production session discards SDK token-usage metadata. Characters are never labeled or estimated as tokens.

Live exit code 0 means all attempted reviews completed, not perfect detection; 1 means at least one attempted review errored or was incomplete; 2 means setup/input/output failure; 130 means interruption. Completed-case checkpoints remain after interruption, but an in-flight request's outcome may be unknown. A filesystem failure can prevent the current checkpoint; there is no automatic resume or rerun. A new manual run can consume quota again. The free-tier flag remains an operator attestation, not remote billing verification.

### Interpretation and remaining limits

This is a small, synthetic, contract-rich benchmark with no blinded second reviewer, empirical calibration, or general real-world accuracy claim. Ground truth is author-reviewed reasoning, not proof of exhaustive defect enumeration. The lexical rubric can miss paraphrases or accept misleading/negated prose containing its phrases; inspect the TP/FP/FN examples before drawing conclusions. Changing rubrics after viewing outputs risks overfitting and requires a new documented dataset revision. Ambiguous cases are deliberately excluded, but other unforeseen issues may still merit adjudication.

This dataset does not cover complex dependency graphs, deletions/renames, multilingual repositories, low-severity defects, or long-context scale. Clean negatives are not proof of universally bug-free code. Fixture SHAs are deterministic snapshot identifiers, not real Git commits. No GitHub latency is measured. Live output is stochastic even when replay scoring is deterministic; record all runs and failures rather than cherry-picking successful subsets. The three-case cap limits any single live run; comparisons must use identical selected cases/configurations and disclose coverage, errors, and exclusions. Baseline trials, repeated-run uncertainty, independent labeling, and a larger held-out dataset remain future work.
