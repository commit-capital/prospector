"""The ONE threat-detection policy — supply-chain / malicious-PR signatures,
a durable actor blocklist, and the scan that flags a PR record.

Mirrors gates.py and taxonomy.py: a single module owns the policy, everything
else calls it. Two halves:

- **Signatures** (code, below): high-precision line patterns over a PR's diff
  that identify a known attack class, each read in time linear in the line, so
  no added line can stall the scan. The CRITICAL/HIGH ones are tuned to fire
  only on obfuscated, self-executing, or capability-smuggling code — things a
  legitimate diff never contains — so a hit is treated as a hard block.
- **Registry** (the threats registry, owned by store.py): the mutable data —
  blocklisted authors and the list of confirmed malicious PRs. It persists
  across head moves and across PRs, unlike the per-PR `threat` section (which
  the store stamps with against_head_sha and so stales when a head moves).

The first incident this was built for: author `tarun-khatri` pushed the same
obfuscated build-time payload (appended to cli/esbuild.config.mjs, with
`global['!']=…`, a `String.fromCharCode(127)` shuffle-decoder, and a smuggled
`createRequire`) across six PRs — 5174, 5270, 5128, 5201, 5187, 5209 — three of
which Greptile scored 5/5. Only merge conflicts kept them out of the gate.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence

# Build-config files where a require/global smuggle is never legitimate.
_BUILD_CONFIG = re.compile(
    r"(esbuild|vite|rollup|webpack|tsup|rspack)\b|\.config\.(mjs|cjs|js|ts)$",
    re.IGNORECASE,
)

# Severity ladder. CRITICAL or HIGH ⇒ verdict "malicious" (hard block).
# MEDIUM ⇒ "suspicious" (surfaced for a human, never auto-cleared, never a
# block on its own — these patterns can have rare benign causes).
CRITICAL, HIGH, MEDIUM = "critical", "high", "medium"

_GLOBAL_INDEX = re.compile(r"global\s*\[")
_HOISTED = re.compile(r"\]\s*=\s*(?:require|module)\b")


def _capability_smuggle(body: str) -> bool:
    """`global[<key>] = require` (or `module`) for any non-empty key, nested
    brackets included (`global[_$_1e42[0]]= require`): the line's first
    `global[`, then a `] = require` that starts at least one character on."""
    index = _GLOBAL_INDEX.search(body)
    return bool(index and _HOISTED.search(body, index.end() + 1))


_CREATE_REQUIRE_CALL = re.compile(r"createRequire\s*\(")
_CALL_OPEN = re.compile(r"\s*\(")


def _inline_create_require(body: str) -> bool:
    """`createRequire(<args>)(`: a createRequire result invoked on the spot.
    The arguments end at the first `)` after the call opens, so every call
    before that `)` shares its verdict and the `)` is judged once."""
    closed = -1
    for call in _CREATE_REQUIRE_CALL.finditer(body):
        if call.end() <= closed:
            continue
        closed = body.find(")", call.end())
        if closed < 0:
            return False
        if _CALL_OPEN.match(body, closed + 1):
            return True
    return False


# Each signature: (name, severity, description, [line matchers]). A matcher
# takes one ADDED diff line (see scan_diff) and returns something truthy when
# the line matches: a compiled pattern's `search`, or a scanner above for a
# shape whose single regex backtracks quadratically. A signature fires if ANY
# of its matchers matches an added line.
SIGNATURES: list[tuple[str, str, str, list[Callable[[str], re.Match[str] | bool | None]]]] = [
    (
        "obfuscated-self-decoder", CRITICAL,
        "Self-decoding/obfuscated payload (global['!'] marker, _$_ shuffle "
        "vars, or a fromCharCode(127) delimiter trick) — never present in "
        "honest source.",
        [
            re.compile(r"global\s*\[\s*['\"]!['\"]\s*\]\s*=").search,
            re.compile(r"_\$_[0-9a-fA-F]{4,}").search,
            re.compile(r"String\.fromCharCode\(\s*127\s*\)").search,
        ],
    ),
    (
        "capability-smuggle", HIGH,
        "Hoists require/module onto the global object so decoded code gets "
        "Node capabilities (createRequire + global[...] = require).",
        [_capability_smuggle],
    ),
    (
        "build-config-require-injection", HIGH,
        "createRequire rebound to `require` itself (the smuggle shape) or "
        "invoked inline inside an ESM build config — build-time RCE surface on "
        "maintainer and CI machines. A descriptively-named binding is ordinary "
        "monorepo build tooling and does not fire.",
        # only counted in a build-config file; see scan_diff
        [
            re.compile(r"\brequire\s*=\s*createRequire\s*\(").search,
            _inline_create_require,
        ],
    ),
    (
        "eol-churn-camouflage", MEDIUM,
        "Whole-file line-ending churn inflating the diff to bury a real change "
        "(additions ≈ deletions, both large).",
        [],  # computed from diffstat, not a line pattern; see scan_diff
    ),
    (
        "unscannable-diff", MEDIUM,
        "GitHub returned no whole patch for a file this PR adds lines to, or "
        "listed only the first 3,000 of its files, so the scan could not read "
        "every added line.",
        [],  # computed from the files the read could not carry; see scan_diff
    ),
    (
        "secret-leak", MEDIUM,
        "A live-looking credential hardcoded in an added line (API key / token "
        "/ private key). Not an attack — the author leaked their own secret — "
        "but operationally urgent: the key must be rotated and upstream notified.",
        [
            # Provider-shaped tokens (high precision — distinctive prefixes).
            re.compile(r"\bAKIA[0-9A-Z]{16}\b").search,                       # AWS access key id
            re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b").search,             # GitHub PAT/OAuth/refresh
            re.compile(r"\bxox[baprs]-[0-9]{10,}-[A-Za-z0-9-]{10,}\b").search,# Slack token
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----").search,
            # Generic SECRET=<random hex/base64 value> handled in _secret_evidence.
        ],
    ),
]

# Files where long hex/base64 strings are routinely NOT secrets (dependency
# integrity hashes, snapshots, fixtures, generated migrations, docs).
_SECRET_EXCLUDE_FILE = re.compile(
    r"(?i)(?:^|/)(?:pnpm-lock\.yaml|package-lock\.json|yarn\.lock|Cargo\.lock|go\.sum|"
    r"poetry\.lock)$|\.(?:snap|lock)$|"
    r"(?:^|/)(?:__tests__|tests?|__snapshots__|fixtures?|migrations?)(?:/)|"
    r"\.(?:test|spec)\.[tj]sx?$|\.(?:md|mdx|lock|example)$|(?:^|/)\.env\.example$"
)
# A secret-NAMED assignment ending its line: <NAME containing KEY/TOKEN/
# SECRET/...> = <value>. _secret_value reads the raw value and _looks_secret
# judges it — the name gate alone is far too loose (queryKey:, jobKey:, urlKey: …).
# _SECRET_NAME is the left side through the separator; _SECRET_VALUE is the
# line's last run of token characters, tried only where a run begins.
_SECRET_NAME = re.compile(
    r"""(?ix)
    \b[A-Za-z0-9_]*(?:SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|
                      AUTH[_-]?TOKEN|ACCESS[_-]?TOKEN|PRIVATE[_-]?KEY|
                      [A-Z0-9]_KEY|[A-Z0-9]_TOKEN)\b
    \s*[:=]\s*['"]?
    """,
)
_SECRET_VALUE = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9+/=_\-])(?P<val>[A-Za-z0-9+/=_\-]{24,})['"]?\s*[,;]?\s*$
    """,
)
_PLACEHOLDER = re.compile(
    r"(?i)(?:your|xxx+|placeholder|example|changeme|change[-_]?me|replace|strong[-_]?random|"
    r"dummy|fake|sample|redacted|none|null|true|false|test[-_]|valid[-_]?key|"
    r"\$\{|<|process\.env|import\.meta)"
)
# A real random secret value: long pure hex, or base64/base64url with the
# digit+mixed-case mix that random tokens have (not a camelCase identifier).
_HEX_SECRET = re.compile(r"^[0-9a-fA-F]{32,}$")
_B64_SECRET = re.compile(r"^[A-Za-z0-9+/_\-]{32,}={0,2}$")


def _shannon_entropy(s: str) -> float:
    import math
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_secret(value: str) -> bool:
    """A generic SECRET=value is a real leak iff the value is a long, high-
    entropy hex/base64 token — NOT a placeholder and NOT a code identifier.
    Identifiers (normalizeAgentNameKey, queryKeys) are rejected: they lack the
    digit content and entropy of a random credential."""
    v = value.strip().strip("'\"").rstrip(",;")
    if len(v) < 24 or _PLACEHOLDER.search(v):
        return False
    if _HEX_SECRET.match(v):
        return _shannon_entropy(v) >= 3.0          # 32+ hex chars, random
    if _B64_SECRET.match(v):
        # require digits AND high entropy so dictionary-ish identifiers fail
        return any(c.isdigit() for c in v) and _shannon_entropy(v) >= 4.0
    return False


def _secret_value(body: str) -> str | None:
    """The value of the line's secret-named assignment: from the end of the
    leftmost name and separator that open onto the line's last token run, to
    the run's end, when that leaves at least 24 characters."""
    run = _SECRET_VALUE.search(body)
    if not run:
        return None
    start, end = run.span("val")
    pos = 0
    while name := _SECRET_NAME.search(body, pos):
        if start <= name.end() <= end - 24:
            return body[name.end():end]
        pos = name.start() + 1
    return None


def _secret_evidence(body: str) -> bool:
    """True if this added line leaks a credential through the generic
    SECRET=<random> case. Provider patterns and the file-context exclusion are
    checked by the caller."""
    value = _secret_value(body)
    return value is not None and _looks_secret(value)

_CRITICAL_OR_HIGH = {name for name, sev, _, _ in SIGNATURES if sev in (CRITICAL, HIGH)}


_HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_GIT_HEADER = re.compile(r"diff --git a/.* b/(.*)")


def _changed_lines(diff_text: str) -> Iterator[tuple[str | None, str, str]]:
    """Yield (filename, sign, body) for every added ('+') and removed ('-')
    line of a unified diff. A hunk is read by the line counts in its `@@`
    header, so a content line is never taken for a file header whatever it
    holds; the file is tracked from each `diff --git` header and its `+++`
    line. Lines split on newline alone, so a carriage return or a Unicode line
    separator inside an added line stays part of that line. A '+' or '-' line
    outside any hunk is still yielded."""
    current: str | None = None
    old = new = 0
    for line in diff_text.split("\n"):
        if old > 0 or new > 0:
            sign = line[:1]
            if sign == "+":
                new -= 1
                yield current, "+", line[1:]
                continue
            if sign == "-":
                old -= 1
                yield current, "-", line[1:]
                continue
            if sign == " " or not line:
                old -= 1
                new -= 1
                continue
            if sign == "\\":  # "\ No newline at end of file"
                continue
            old = new = 0
        if hunk := _HUNK.match(line):
            old = int(hunk.group(1) or 1)
            new = int(hunk.group(2) or 1)
        elif header := _GIT_HEADER.match(line):
            current = header.group(1)
        elif line.startswith("+++ "):
            path = line[4:].strip()
            current = path[2:] if path.startswith("b/") else path
        elif line.startswith("+"):
            yield current, "+", line[1:]
        elif line.startswith("-") and not line.startswith("--- "):
            yield current, "-", line[1:]


def _diff_line_counts(diff_text: str) -> tuple[int, int]:
    """(additions, deletions) counted from a unified diff's content lines. Lets
    the churn-camouflage check run off the diff itself when no exact diffstat is
    supplied, so it doesn't depend on a signals field written by a later phase."""
    add = dele = 0
    for _, sign, _ in _changed_lines(diff_text or ""):
        if sign == "+":
            add += 1
        else:
            dele += 1
    return add, dele


def scan_diff(diff_text: str, *, additions: int | None = None,
              deletions: int | None = None, unread: Sequence[str] = ()) -> dict:
    """Scan a unified diff for attack signatures. Pure; no I/O.

    Returns {verdict, signatures, detail} where verdict is one of
    'malicious' (a CRITICAL/HIGH signature fired), 'suspicious' (only MEDIUM),
    or 'clear'. `detail` maps each fired signature to a short evidence string.
    Pass exact diffstat additions/deletions to override the churn-camouflage check;
    when omitted they are counted from the diff text. `unread` names what of the
    PR's diff `diff_text` does not carry (the files GitHub returned no whole
    patch for); any entry fires `unscannable-diff`.
    """
    fired: dict[str, str] = {}
    pats = {name: matchers for name, _, _, matchers in SIGNATURES}
    # (build config, secret-exempt) per path, read once however many lines it adds
    paths: dict[str | None, tuple[bool, bool]] = {}

    for fname, sign, body in _changed_lines(diff_text):
        if sign != "+":
            continue
        if fname not in paths:
            paths[fname] = (bool(fname and _BUILD_CONFIG.search(fname)),
                            bool(fname and _SECRET_EXCLUDE_FILE.search(fname)))
        build_config, secret_exempt = paths[fname]
        for name in ("obfuscated-self-decoder", "capability-smuggle"):
            if name not in fired:
                for matches in pats[name]:
                    if matches(body):
                        fired[name] = _evidence(body)
                        break
        if "build-config-require-injection" not in fired and build_config:
            if any(matches(body) for matches in pats["build-config-require-injection"]):
                fired["build-config-require-injection"] = f"{fname}: {_evidence(body)}"
        if "secret-leak" not in fired and not secret_exempt:
            if any(matches(body) for matches in pats["secret-leak"]) or _secret_evidence(body):
                fired["secret-leak"] = f"{fname or '?'}: {_evidence(body)}"

    if additions is None and deletions is None:
        additions, deletions = _diff_line_counts(diff_text)

    if additions and deletions and additions > 3000 and deletions > 3000 \
            and abs(additions - deletions) < 0.05 * max(additions, deletions):
        fired["eol-churn-camouflage"] = f"+{additions}/-{deletions}"

    if unread:
        more = f" (+{len(unread) - 3:,} more)" if len(unread) > 3 else ""
        fired["unscannable-diff"] = f"not read: {', '.join(unread[:3])}{more}"

    if _CRITICAL_OR_HIGH & fired.keys():
        verdict = "malicious"
    elif fired:
        verdict = "suspicious"
    else:
        verdict = "clear"
    return {"verdict": verdict, "signatures": sorted(fired), "detail": fired}


def _evidence(body: str) -> str:
    s = body.strip()
    return s[:120] + ("…" if len(s) > 120 else "")


# ---------------------------------------------------------------------------
# Actor blocklist (consulted against the store-level registry)
# ---------------------------------------------------------------------------
def is_blocked_actor(registry: dict, author: str | None) -> bool:
    return bool(author) and author in (registry.get("actors") or {})


def empty_registry() -> dict:
    return {"actors": {}, "incidents": []}


def block_actor(registry: dict, author: str, reason: str, *,
                added: str, incidents: list[int] | None = None) -> dict:
    """Add/refresh an actor in the blocklist. `added` is an ISO date string
    (callers pass it in — the store has no clock of its own here)."""
    actors = registry.setdefault("actors", {})
    entry = actors.setdefault(author, {})
    entry["reason"] = reason
    entry.setdefault("added", added)
    merged = sorted(set(entry.get("incidents", [])) | set(incidents or []))
    entry["incidents"] = merged
    return registry


def record_incident(registry: dict, pr: int, author: str | None,
                    head_sha: str | None, signatures: list[str], *, noticed: str) -> dict:
    """Append a confirmed malicious PR to the incident log (idempotent on pr)."""
    incidents = registry.setdefault("incidents", [])
    incidents[:] = [i for i in incidents if i.get("pr") != pr]
    incidents.append({
        "pr": pr, "author": author, "head_sha": head_sha,
        "signatures": signatures, "noticed": noticed,
    })
    incidents.sort(key=lambda i: i["pr"])
    return registry
