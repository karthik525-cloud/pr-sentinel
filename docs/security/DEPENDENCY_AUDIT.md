# Current dependency audit — pip 26.2.2, 2026-10-27

**Result: zero known vulnerabilities, zero skipped packages, exit code 0.** A fresh live PyPI scan using isolated pip-audit 2.10.1 covered all 40 distinct development/build pins, including all 31 runtime pins. No advisories were ignored or suppressed. This is a clean result for these exact pins against the queried database at scan time, not a guarantee against unknown vulnerabilities.

The owner explicitly approved the pip-only upgrade from 25.0.1 to **26.2.2**. [Official PyPI release metadata](https://pypi.org/pypi/pip/26.2.2/json) confirmed availability, non-yanked distributions and Python >=3.10 compatibility. The project uses Python 3.12. The official wheel SHA-256 is `fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70`.

Only `requirements-build.lock` changed among dependency snapshots. Docker, GitHub Actions and README installation commands already consume that shared lock before later pip operations; there are no duplicate executable pip pins to update. `pyproject.toml` pins setuptools, not pip, and needs no change. The local project virtual environment was upgraded to pip 26.2.2 without dependencies; all unrelated dependency pins remain unchanged.

Fresh scan command:

```bash
/tmp/pr-sentinel-release-audit/bin/python -m pip_audit \
  -r requirements-dev.lock -r requirements-build.lock \
  --no-deps --disable-pip --progress-spinner off \
  --cache-dir /tmp/pr-sentinel-audit-pip2622-fresh \
  --format json --output /tmp/pr-sentinel-audit-pip2622.json
```

[Retained fresh JSON result](pip-audit-2026-10-27-pip2622.json). A separate fresh cache was used; the earlier six unique pip advisories and raw output remain below as historical evidence. None was returned for 26.2.2. No package-collection failures, skipped packages or unavailable package results occurred. The warnings about missing distribution hashes are retained as a reproducibility limitation, not treated as an audit failure.

Current input SHA-256:

```text
requirements-build.lock   c88019ce3442b1c0b92e4edfc90d98f75bfe96824c86ef761ae259c079c76a27
requirements-runtime.lock 4568023ae3b8c27bb74a7292a94c57cc3232254efbf9771d99ea4cfc857aadd0
requirements-dev.lock     83fa47587f0f03703920386f9b6b02c0dd0ec9f4bb93f06ee1bb3e893abb6c4f
```

Verification: **481 tests passed in 2.42 seconds**, with one existing Starlette/HTTPX deprecation warning. Ruff lint, formatting of all 46 Python files, compilation, dependency consistency, wheel build and packaged license/data checks passed. Docker remains unavailable: context rules are covered by the static regression test, not by a successful image build. OS/base-image/native-library vulnerabilities, unknown advisories and hosted CI execution remain outside this scan. No Gemini requests, commits, pushes or remote creation occurred.

---

# Historical pre-upgrade audit — 2026-10-27

Tool: PyPA **pip-audit 2.10.1**, installed in an isolated temporary virtual environment. Source: live **PyPI vulnerability service**, queried with a fresh audit cache. No application dependency was installed, upgraded, or fixed by the scan. The project lock files remain unchanged.

Command (from the repository root):

```bash
/tmp/pr-sentinel-release-audit/bin/python -m pip_audit \
  -r requirements-dev.lock -r requirements-build.lock \
  --no-deps --disable-pip --progress-spinner off \
  --cache-dir /tmp/pr-sentinel-release-audit-cache \
  --format json --output /tmp/pr-sentinel-release-audit-pypi.json
```

The retained [raw JSON](pip-audit-2026-10-27.json) is the unmodified result content, pretty-printed. Exit code **1** means findings, not scan failure. All **40 distinct pinned packages** were audited, with **zero skipped packages**. This union includes every one of the **31 runtime pins**, checked by package name and exact version. No runtime pin was flagged. The tool reported **12 entries in one package**, which are **six unique advisories** after deduplicating repeated IDs. The 26.2/26.2.0 fixed-version spellings refer to the same release.

## Findings

All six findings concern **pip 25.0.1**, pinned in `requirements-build.lock`. It is used in developer/CI/image builds and is also present in the copied runtime virtual environment. These are package-installation risks, not demonstrated HTTP endpoint vulnerabilities. The application does not invoke pip while serving reviews.

| Advisory | Affected pip versions | First fixed version | GitHub severity | Summary |
| --- | --- | --- | --- | --- |
| [CVE-2025-8869 / GHSA-4xh5-x5gv-qwph](https://github.com/advisories/GHSA-4xh5-x5gv-qwph) | <=25.2 | 25.3 | Moderate | Unsafe symlink handling in fallback tar extraction |
| [CVE-2026-1703 / GHSA-6vgw-5pg2-w6jp](https://github.com/advisories/GHSA-6vgw-5pg2-w6jp) | <26.0 | 26.0 | Low | Wheel extraction path traversal |
| [CVE-2026-3219 / GHSA-58qw-9mgm-455v](https://github.com/advisories/GHSA-58qw-9mgm-455v) | <=26.0.1 | 26.1 | Moderate | Ambiguous combined ZIP/tar interpretation |
| [CVE-2026-6357 / GHSA-jp4c-xjxw-mgf9](https://github.com/advisories/GHSA-jp4c-xjxw-mgf9) | <26.1 | 26.1 | Moderate | Post-install version-check imports can load newly installed modules |
| [CVE-2026-8643 / GHSA-wf93-45jw-7689](https://github.com/advisories/GHSA-wf93-45jw-7689) | <26.1.2 | 26.1.2 | Moderate | Entry-point names can escape the installation directory |
| [CVE-2026-13346 / GHSA-qwm4-qh6w-59xr](https://github.com/advisories/GHSA-qwm4-qh6w-59xr) | <26.2.0 | 26.2.0 | Moderate | Doubly encoded URLs from malicious indexes can escape download/install locations |

GitHub Advisory Database pages were cross-checked for the affected ranges and fixes. Original project-scoped advisory URLs returned 404; the canonical GitHub Advisory Database pages above were available. This did not prevent the PyPI scan from completing.

Applicability matters: CVE-2025-8869 concerns a fallback not used on Python versions with PEP 706, including this project's Python 3.12 baseline. CVE-2026-13346 requires a malicious package index; CVE-2026-6357 concerns self-version checks, which CI and Docker explicitly disable. These conditions reduce exposure in some workflows but are not a reason to silently ignore all pip findings. Other installation paths and local quick-start usage still need consideration.

**Historical recommendation, now superseded by the approved upgrade above:** select and test an exact pip version **26.2.0 or newer**, update the build-tool pin, rebuild/retest, and rerun the audit. Version 26.2.0 is the highest first-fixed version across these six reports; it is not a guarantee against additional advisories in future scans. No such upgrade was applied in this task.

## Scope and limitations

- Complete for the supplied pinned package union against PyPI data at scan time; no skipped or unavailable package results.
- `--no-deps --disable-pip` avoids resolving/installing arbitrary requirements. It relies on the existing complete pin files; the runtime pin subset was verified and dependency consistency checks passed separately.
- No second OSV scan was run. Native libraries, the Python interpreter, OS/base-image packages, unknown vulnerabilities and application logic are outside this package audit.
- Docker is unavailable; there is no container vulnerability/image scan or successful image-build claim.
- The initial sandboxed tool installation failed due to network restrictions; the authorized isolated installation and audit then succeeded with network access. The application environment was not upgraded.
- Dependency files still lack distribution hashes; the Python image still uses a version tag rather than a reviewed digest.

Input SHA-256:

```text
requirements-build.lock   f6245f04158768b2117f46847e920ad2e126ebebb304bb9785adc1b72707ad63
requirements-runtime.lock 4568023ae3b8c27bb74a7292a94c57cc3232254efbf9771d99ea4cfc857aadd0
requirements-dev.lock     83fa47587f0f03703920386f9b6b02c0dd0ec9f4bb93f06ee1bb3e893abb6c4f
```
