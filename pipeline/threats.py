"""The ONE threat-detection policy — supply-chain / malicious-PR signatures,
a durable actor blocklist, and the scan that flags a PR record.

Mirrors gates.py and taxonomy.py: a single module owns the policy, everything
else calls it. Two halves:

- **Signatures** (code, below): high-precision regex patterns over a PR's diff
  that identify a known attack class. The CRITICAL/HIGH ones are tuned to fire
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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

# Build-config files where a require/global smuggle is never legitimate.
_BUILD_CONFIG = re.compile(
    r"(esbuild|vite|rollup|webpack|tsup|rspack)\b|\.config\.(mjs|cjs|js|ts)$",
    re.IGNORECASE,
)

# Severity ladder. CRITICAL or HIGH ⇒ verdict "malicious" (hard block).
# MEDIUM ⇒ "suspicious" (surfaced for a human, never auto-cleared, never a
# block on its own — these patterns can have rare benign causes).
CRITICAL, HIGH, MEDIUM = "critical", "high", "medium"

# Each signature: (name, severity, description, [compiled patterns]).
# Patterns are matched against ADDED diff lines only (see scan_diff). A
# signature fires if ANY of its patterns matches an added line.
SIGNATURES: list[tuple[str, str, str, list[re.Pattern]]] = [
    (
        "obfuscated-self-decoder", CRITICAL,
        "Self-decoding/obfuscated payload (global['!'] marker, _$_ shuffle "
        "vars, or a fromCharCode(127) delimiter trick) — never present in "
        "honest source.",
        [
            re.compile(r"global\s*\[\s*['\"]!['\"]\s*\]\s*="),
            re.compile(r"_\$_[0-9a-fA-F]{4,}"),
            re.compile(r"String\.fromCharCode\(\s*127\s*\)"),
        ],
    ),
    (
        "capability-smuggle", HIGH,
        "Hoists require/module onto the global object so decoded code gets "
        "Node capabilities (createRequire + global[...] = require).",
        [
            # greedy [...] so nested brackets (global[_$_1e42[0]]= require) match
            re.compile(r"global\s*\[.+\]\s*=\s*require\b"),
            re.compile(r"global\s*\[.+\]\s*=\s*module\b"),
        ],
    ),
    (
        "build-config-require-injection", HIGH,
        "createRequire rebound to `require` itself (the smuggle shape) or "
        "invoked inline inside an ESM build config — build-time RCE surface on "
        "maintainer and CI machines. A descriptively-named binding is ordinary "
        "monorepo build tooling and does not fire.",
        # only counted in a build-config file; see scan_diff
        [
            re.compile(r"\brequire\s*=\s*createRequire\s*\("),
            re.compile(r"createRequire\s*\([^)]*\)\s*\("),
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
            re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                       # AWS access key id
            re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),             # GitHub PAT/OAuth/refresh
            re.compile(r"\bxox[baprs]-[0-9]{10,}-[A-Za-z0-9-]{10,}\b"),# Slack token
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
            # Generic SECRET=<random hex/base64 value> handled in _secret_evidence.
        ],
    ),
]

# Files where long hex/base64 strings are routinely NOT secrets (dependency
# integrity hashes, snapshots, fixtures, generated migrations, docs).
_SECRET_EXCLUDE_FILE = re.compile(
    r"(?i)(?:^|/)(?:pnpm-lock\.yaml|package-lock\.json|yarn\.lock|Cargo\.lock|go\.sum|"
    r"poetry\.lock|.*\.snap|.*\.lock)$|"
    r"(?:^|/)(?:__tests__|tests?|__snapshots__|fixtures?|migrations?)(?:/)|"
    r"\.(?:test|spec)\.[tj]sx?$|\.(?:md|mdx|lock|example)$|(?:^|/)\.env\.example$"
)
# A secret-NAMED assignment: <NAME containing KEY/TOKEN/SECRET/...> = <value>.
# We capture the raw value tail and judge it in _looks_secret — the name gate
# alone is far too loose (queryKey:, jobKey:, urlKey: …).
_SECRET_ASSIGN = re.compile(
    r"""(?ix)
    \b[A-Za-z0-9_]*(?:SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|
                      AUTH[_-]?TOKEN|ACCESS[_-]?TOKEN|PRIVATE[_-]?KEY|
                      [A-Z0-9]+_KEY|[A-Z0-9]+_TOKEN)\b
    \s*[:=]\s*
    ['"]?(?P<val>[A-Za-z0-9+/=_\-]{24,})['"]?\s*[,;]?\s*$
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


def _secret_evidence(fname: str | None, body: str) -> bool:
    """True if this added line leaks a credential. Provider patterns are
    checked by the caller; this handles the generic SECRET=<random> case with
    file-context exclusion."""
    if fname and _SECRET_EXCLUDE_FILE.search(fname):
        return False
    m = _SECRET_ASSIGN.search(body)
    return bool(m and _looks_secret(m.group("val")))

_CRITICAL_OR_HIGH = {name for name, sev, _, _ in SIGNATURES if sev in (CRITICAL, HIGH)}
_PATTERNS = {name: patterns for name, _, _, patterns in SIGNATURES}


def _line_signatures(fname: str | None, body: str) -> list[str]:
    """The line-pattern signatures one added line fires: the decoder and smuggle
    patterns anywhere, the require injection only in a build-config file, and a
    secret leak outside the excluded files."""
    fired = [name for name in ("obfuscated-self-decoder", "capability-smuggle")
             if any(p.search(body) for p in _PATTERNS[name])]
    if fname and _BUILD_CONFIG.search(fname) and any(
            p.search(body) for p in _PATTERNS["build-config-require-injection"]):
        fired.append("build-config-require-injection")
    provider_hit = any(p.search(body) for p in _PATTERNS["secret-leak"])
    if (provider_hit and not (fname and _SECRET_EXCLUDE_FILE.search(fname))) \
            or _secret_evidence(fname, body):
        fired.append("secret-leak")
    return fired


@dataclass(frozen=True)
class Match:
    """Where one signature fired: the diff's target file and the 1-based line of
    the added line within the diff text (lines split on newline)."""
    signature: str
    file: str | None
    diff_line: int


def locate(diff_text: str, *, limit: int | None = None) -> list[Match]:
    """Every (signature, file, line) the line-pattern signatures fire on over the
    added lines of `diff_text`, read exactly as scan_diff reads them, in diff
    order, at most `limit` of them. The churn-camouflage and unscannable-diff
    signatures describe the whole diff and have no location."""
    out: list[Match] = []
    for lineno, fname, sign, body in _numbered_changed_lines(diff_text or ""):
        if sign != "+":
            continue
        for name in _line_signatures(fname, body):
            out.append(Match(name, fname, lineno))
            if limit is not None and len(out) >= limit:
                return out
    return out


_HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_GIT_HEADER = re.compile(r"diff --git a/.* b/(.*)")


def _changed_lines(diff_text: str) -> Iterator[tuple[str | None, str, str]]:
    """(filename, sign, body) for every changed line, as _numbered_changed_lines
    reads them."""
    for _, fname, sign, body in _numbered_changed_lines(diff_text):
        yield fname, sign, body


def _numbered_changed_lines(diff_text: str) -> Iterator[tuple[int, str | None, str, str]]:
    """Yield (line number, filename, sign, body) for every added ('+') and
    removed ('-') line of a unified diff, the line number 1-based within
    `diff_text`. A hunk is read by the line counts in its `@@`
    header, so a content line is never taken for a file header whatever it
    holds; the file is tracked from each `diff --git` header and its `+++`
    line. Lines split on newline alone, so a carriage return or a Unicode line
    separator inside an added line stays part of that line. A '+' or '-' line
    outside any hunk is still yielded."""
    current: str | None = None
    old = new = 0
    for lineno, line in enumerate(diff_text.split("\n"), start=1):
        if old > 0 or new > 0:
            sign = line[:1]
            if sign == "+":
                new -= 1
                yield lineno, current, "+", line[1:]
                continue
            if sign == "-":
                old -= 1
                yield lineno, current, "-", line[1:]
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
            yield lineno, current, "+", line[1:]
        elif line.startswith("-") and not line.startswith("--- "):
            yield lineno, current, "-", line[1:]


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

    for fname, sign, body in _changed_lines(diff_text):
        if sign != "+":
            continue
        for name in _line_signatures(fname, body):
            if name in fired:
                continue
            if name in ("build-config-require-injection", "secret-leak"):
                fired[name] = f"{fname or '?'}: {_evidence(body)}"
            else:
                fired[name] = _evidence(body)

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
    """Append a confirmed malicious PR to the incident log (idempotent on pr,
    keeping the date it was first noticed)."""
    incidents = registry.setdefault("incidents", [])
    first = next((i.get("noticed") for i in incidents if i.get("pr") == pr), None)
    incidents[:] = [i for i in incidents if i.get("pr") != pr]
    incidents.append({
        "pr": pr, "author": author, "head_sha": head_sha,
        "signatures": signatures, "noticed": first or noticed,
    })
    incidents.sort(key=lambda i: i["pr"])
    return registry
