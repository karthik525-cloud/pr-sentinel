"""Author-maintained synthetic ground truth, independent of any model predictions.

Run from project root: python -m benchmarks.build_dataset
Only writes JSON; never executes example source code.
"""

import json
from pathlib import Path

from app.evaluation.dataset import Dataset, patch_for, validate_dataset

CASES = []


def case(
    id,
    title,
    contract,
    base,
    old,
    new,
    category=None,
    severity="medium",
    why=None,
    concepts=None,
    context=None,
    excluded=None,
    extra=None,
    span=1,
):
    head = base.replace(old, new)
    assert old in base and head != base
    files = [
        {
            "path": "src/service.py",
            "base": base,
            "head": head,
            "patch": patch_for(base, head, "src/service.py"),
        }
    ]
    context = context or {}
    files += [
        {"path": path, "base": text, "head": text, "patch": None} for path, text in context.items()
    ]
    expected = []
    if category:
        line = head[: head.index(new)].count("\n") + 1
        expected.append(
            dict(
                id=id + "-defect",
                file_path="src/service.py",
                line=line,
                start=line,
                end=line + span - 1,
                category=category,
                severity=severity,
                explanation=why,
                concepts=concepts,
            )
        )
    if extra:
        for old2, new2, cat2, why2, concepts2 in extra:
            head = head.replace(old2, new2)
            line = head[: head.index(new2)].count("\n") + 1
            expected.append(
                dict(
                    id=id + "-second",
                    file_path="src/service.py",
                    line=line,
                    start=line,
                    end=line,
                    category=cat2,
                    severity="high",
                    explanation=why2,
                    concepts=concepts2,
                )
            )
        files[0].update(head=head, patch=patch_for(base, head, "src/service.py"))
    CASES.append(
        dict(
            id=id,
            title=title,
            description=contract,
            provenance="Original hand-authored Python regression; Stage 4 benchmark v1. No model-generated labels or private code.",
            context_required=bool(context),
            context_paths=list(context),
            excluded_reason=excluded,
            files=files,
            expected=expected,
        )
    )


case(
    "inclusive-limit",
    "Simplify admission check",
    "Capacity is inclusive: a count equal to limit is allowed.",
    "def allowed(count, limit):\n    return count <= limit\n",
    "count <= limit",
    "count < limit",
    "bug",
    why="At count == limit the new predicate denies a valid request, violating the inclusive contract.",
    concepts=[["equal", "boundary", "inclusive"], ["reject", "den", "exclud"]],
)
case(
    "empty-average",
    "Inline average calculation",
    "Empty samples must yield 0.0.",
    "def average(samples):\n    return sum(samples) / len(samples) if samples else 0.0\n",
    "return sum(samples) / len(samples) if samples else 0.0",
    "return sum(samples) / len(samples)",
    "bug",
    why="An empty list now divides zero by zero and raises ZeroDivisionError instead of returning 0.0.",
    concepts=[["empty", "zero"], ["divis", "divide", "zerodivision"]],
)
case(
    "last-item",
    "Adjust item pagination",
    "Return all items starting at offset, including the final item.",
    "def remaining(items, offset):\n    return items[offset:]\n",
    "items[offset:]",
    "items[offset:-1]",
    "bug",
    why="The negative stop excludes the final item on every nonempty remaining page.",
    concepts=[["last", "final"], ["omit", "exclud", "drop", "skip"]],
)
case(
    "mutating-sort",
    "Reduce sorting allocations",
    "The caller's sequence must remain unchanged.",
    "def ordered(items):\n    return sorted(items)\n",
    "return sorted(items)",
    "items.sort()\n    return items",
    "bug",
    why="list.sort mutates caller-owned input, violating the explicit no-mutation contract.",
    concepts=[["mutat", "in-place", "in place"], ["input", "caller", "original"]],
    span=2,
)
case(
    "sql-interpolation",
    "Simplify user lookup",
    "name is attacker-controlled HTTP input; use sqlite3 parameter binding.",
    'def lookup(db, name):\n    return db.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchall()\n',
    'db.execute("SELECT id FROM users WHERE name = ?", (name,))',
    "db.execute(f\"SELECT id FROM users WHERE name = '{name}'\")",
    "security",
    "high",
    why="An attacker can inject SQL through name because interpolation replaces parameter binding.",
    concepts=[["sql", "query"], ["inject", "untrusted", "attacker"]],
)
case(
    "shell-command",
    "Support shell syntax in ping",
    "host is untrusted; ping arguments must never be interpreted by a shell.",
    'import subprocess\n\ndef ping(host):\n    return subprocess.run(["ping", "-c", "1", host], check=True)\n',
    'subprocess.run(["ping", "-c", "1", host], check=True)',
    'subprocess.run(f"ping -c 1 {host}", shell=True, check=True)',
    "security",
    "critical",
    why="A host containing shell metacharacters runs arbitrary commands with the application's privileges.",
    concepts=[["shell", "command"], ["inject", "arbitrary", "attacker"]],
)
case(
    "tls-verification",
    "Handle TLS errors",
    "Fetches must authenticate the remote HTTPS peer.",
    "def fetch(session, url):\n    return session.get(url, verify=True, timeout=5)\n",
    "verify=True",
    "verify=False",
    "security",
    "high",
    why="Disabling certificate verification allows an active attacker to impersonate the HTTPS server.",
    concepts=[["certificate", "tls", "ssl"], ["verif", "middle", "impersonat"]],
)
case(
    "unsafe-pickle",
    "Change payload serialization",
    "payload is an untrusted network byte string; json is the current wire format.",
    "import json\nimport pickle\n\ndef decode(payload):\n    return json.loads(payload)\n",
    "json.loads(payload)",
    "pickle.loads(payload)",
    "security",
    "critical",
    why="Unpickling attacker-provided bytes permits arbitrary code execution, unlike JSON parsing.",
    concepts=[["pickl", "deserial"], ["code execution", "arbitrary", "untrusted"]],
)
case(
    "missing-timeout",
    "Simplify HTTP fetch",
    "Each HTTP call must have a finite timeout; session has no default timeout.",
    "def fetch(session, url):\n    return session.get(url, timeout=5)\n",
    "session.get(url, timeout=5)",
    "session.get(url)",
    "reliability",
    "high",
    why="A stalled peer can block the worker indefinitely after removal of the sole HTTP timeout.",
    concepts=[["timeout"], ["indefinite", "hang", "unbounded", "block", "stall"]],
)
case(
    "lost-rollback",
    "Simplify transaction error handling",
    "On failure this shared database connection must be rolled back before reuse.",
    "def save(db, row):\n    try:\n        db.insert(row)\n        db.commit()\n    except Exception:\n        db.rollback()\n        raise\n",
    "db.rollback()",
    "pass",
    "reliability",
    "high",
    why="The transaction remains open after insert or commit failure, leaving the shared connection dirty for later requests.",
    concepts=[["rollback", "transaction"], ["open", "dirty", "fail", "reuse"]],
)
case(
    "swallowed-failure",
    "Make delivery tolerant",
    "Callers must learn when send fails so they do not acknowledge an undelivered message.",
    "def deliver(client, message):\n    try:\n        client.send(message)\n    except OSError:\n        raise\n    return True\n",
    "        raise",
    "        pass",
    "reliability",
    "high",
    why="A failed send is silently converted into True, causing callers to acknowledge lost messages.",
    concepts=[
        ["swallow", "suppress", "silenc", "hidden", "hide", "lost"],
        ["fail", "error", "exception", "message"],
    ],
)
case(
    "blocking-async",
    "Use familiar delay helper",
    "This coroutine serves concurrent requests on a single asyncio event loop.",
    "import asyncio\nimport time\n\nasync def wait_ready():\n    await asyncio.sleep(1)\n    return True\n",
    "await asyncio.sleep(1)",
    "time.sleep(1)",
    "performance",
    "high",
    why="Synchronous sleep blocks the event loop for one second per request, stalling all concurrent tasks.",
    concepts=[["block"], ["event loop", "async", "concurrent"]],
)
case(
    "quadratic-membership",
    "Simplify membership cache",
    "Both sequences contain up to 100,000 integer IDs; this is a hot batch path.",
    "def overlap(ids, allowed):\n    lookup = set(allowed)\n    return [item for item in ids if item in lookup]\n",
    "set(allowed)",
    "list(allowed)",
    "performance",
    "medium",
    why="List membership scans all allowed IDs for each input ID, changing linear expected work into quadratic work for equally sized inputs.",
    concepts=[["quadratic", "o(n", "linear scan", "o(m"], ["membership", "lookup", "list"]],
)
case(
    "context-unit",
    "Pass configured expiry to cache",
    "The cache API contract is documented in src/cache.py. Expiry must remain 30 seconds.",
    "from .cache import store\n\ndef cache_value(key, value):\n    return store(key, value, ttl_ms=30000)\n",
    "ttl_ms=30000",
    "ttl_ms=30",
    "bug",
    "high",
    why="store expects milliseconds, so passing 30 expires the entry after 0.03 seconds rather than the required 30 seconds.",
    concepts=[["millisecond", "0.03", "1000"], ["expir", "ttl", "second"]],
    context={
        "src/cache.py": 'def store(key, value, ttl_ms):\n    """Backend adapter: ttl_ms is milliseconds, not seconds."""\n    return {"key": key, "value": value, "ttl_ms": ttl_ms}\n'
    },
)
case(
    "context-auth",
    "Reuse role check for access",
    "The role helper's return contract is in src/roles.py. Only administrators may delete accounts.",
    "from .roles import role_for\n\ndef may_delete(user):\n    return role_for(user) == 'admin'\n",
    "role_for(user) == 'admin'",
    "bool(role_for(user))",
    "security",
    "high",
    why="The helper returns the nonempty string 'member' for ordinary users; truthiness grants them administrator deletion access.",
    concepts=[
        ["member", "nonempty", "non-empty", "truth"],
        ["access", "admin", "authoriz", "delet"],
    ],
    context={
        "src/roles.py": "def role_for(user):\n    return 'admin' if user.is_admin else 'member'\n"
    },
)
case(
    "two-regressions",
    "Streamline billing helpers",
    "Zero invoices yield mean 0; total must include every line item. These are independent helpers.",
    "def mean(values):\n    return sum(values) / len(values) if values else 0\n\n\ndef currency():\n    return 'USD'\n\n\ndef version():\n    return 1\n\n\ndef total(values):\n    return sum(values)\n",
    "return sum(values) / len(values) if values else 0",
    "return sum(values) / len(values)",
    "bug",
    "high",
    why="Empty invoices raise division by zero instead of returning the contracted zero mean.",
    concepts=[["empty", "zero"], ["divis", "divide", "zerodivision"]],
    extra=[
        (
            "return sum(values)\n",
            "return sum(values[:-1])\n",
            "bug",
            "The final line item is dropped from totals, underbilling every nonempty invoice.",
            [["last", "final"], ["drop", "omit", "exclud", "underbill"]],
        )
    ],
)
# A second multi-defect PR, in separate files, with independent categories.
case(
    "two-services",
    "Shorten service calls",
    "No session timeout default exists; HTTP calls need a finite timeout. Empty samples must return zero.",
    "def fetch(session, url):\n    return session.get(url, timeout=5)\n",
    "session.get(url, timeout=5)",
    "session.get(url)",
    "reliability",
    "high",
    why="Removing the sole network timeout permits an indefinitely stalled worker.",
    concepts=[["timeout"], ["hang", "block", "stall", "indefinite"]],
)
second_base = "def average(samples):\n    return sum(samples) / len(samples) if samples else 0\n"
second_head = "def average(samples):\n    return sum(samples) / len(samples)\n"
CASES[-1]["files"].append(
    dict(
        path="src/stats.py",
        base=second_base,
        head=second_head,
        patch=patch_for(second_base, second_head, "src/stats.py"),
    )
)
CASES[-1]["expected"].append(
    dict(
        id="two-services-average",
        file_path="src/stats.py",
        line=2,
        start=2,
        end=2,
        category="bug",
        severity="medium",
        explanation="The empty list now causes division by zero instead of the required zero result.",
        concepts=[["empty", "zero"], ["divis", "divide", "zerodivision"]],
    )
)

case(
    "clean-sorted",
    "Preserve caller ordering",
    "Return sorted values without mutating caller input.",
    "def ordered(items):\n    copied = list(items)\n    copied.sort()\n    return copied\n",
    "copied = list(items)\n    copied.sort()\n    return copied",
    "return sorted(items)",
)
case(
    "clean-sql",
    "Bind query parameter",
    "User-supplied names must use SQLite bound parameters.",
    "def lookup(db, name):\n    return db.execute(f\"SELECT id FROM users WHERE name = '{name}'\").fetchall()\n",
    "db.execute(f\"SELECT id FROM users WHERE name = '{name}'\")",
    'db.execute("SELECT id FROM users WHERE name = ?", (name,))',
)
case(
    "clean-timeout",
    "Bound HTTP wait",
    "session follows requests semantics; every call must have a finite timeout.",
    "def fetch(session, url):\n    return session.get(url)\n",
    "session.get(url)",
    "session.get(url, timeout=5)",
)
case(
    "clean-empty",
    "Handle empty samples",
    "Empty input must produce zero.",
    "def average(samples):\n    return sum(samples) / len(samples)\n",
    "return sum(samples) / len(samples)",
    "return sum(samples) / len(samples) if samples else 0",
)
case(
    "clean-context",
    "Simplify permission check",
    "src/roles.py defines the helper contract. Only administrators may proceed.",
    "from .roles import is_admin\n\ndef allowed(user):\n    return is_admin(user) is True\n",
    "is_admin(user) is True",
    "is_admin(user)",
    context={"src/roles.py": "def is_admin(user):\n    return bool(user.admin_flag)\n"},
)
case(
    "clean-set",
    "Speed up batch membership",
    "Inputs are integer identifiers; result order and duplicates must be preserved.",
    "def overlap(ids, allowed):\n    lookup = list(allowed)\n    return [item for item in ids if item in lookup]\n",
    "list(allowed)",
    "set(allowed)",
)
case(
    "ambiguous-rounding",
    "Round invoice values",
    "Currency rounding policy has not been specified.",
    "def cents(value):\n    return int(value * 100)\n",
    "int(value * 100)",
    "round(value * 100)",
    excluded="The intended rounding rule is unspecified; neither truncation nor ties-to-even can be labeled a definite regression.",
)
case(
    "ambiguous-cache",
    "Cache parsed documents",
    "The upstream mutation and invalidation contract is unavailable.",
    "def document(store, key, cache):\n    return store.read(key)\n",
    "return store.read(key)",
    "if key not in cache:\n        cache[key] = store.read(key)\n    return cache[key]",
    excluded="Staleness depends on unspecified mutation and invalidation behavior; excluded from all primary metrics.",
)

if __name__ == "__main__":
    data = dict(version="1.0", provenance="synthetic-hand-authored", cases=CASES)
    dataset = Dataset.model_validate(data)
    validate_dataset(dataset)
    Path("benchmarks/v1.json").write_text(json.dumps(data, indent=2) + "\n")
    print(f"Wrote {len(CASES)} cases")
