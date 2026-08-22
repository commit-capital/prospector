"""Garbage collection for the verify sandbox's pinned-base artifacts and the
sandbox image tags no pin names any more.

Every `verify_driver.prepare_base` run leaves two durable artifacts keyed by the
pinned SHA: a `pr-verify-base:<sha12>-t<tier>` image and a scrubbed clone under
`SCRATCH/base/<sha12>/`. Together they run several gigabytes, and the verify
worker re-pins daily, so without retention the sandbox disk fills and the whole
verify lane stops.

A generation is a base SHA, not a (sha, tier) pair: the clone is keyed by sha12
alone while the image tag carries the tier, so keeping or dropping a whole SHA
is what keeps the two in lockstep.

`plan` is pure and holds the policy. The pinned SHA is kept by identity, never
by rank — an unreadable timestamp must not be able to reclaim the live pin.
Alongside it one more generation survives, because the pin is read once at the
start of a verify run (`verify_pr._run_inner`) and held for the run's duration,
while `compile_preflight` builds and uses an image for current default-branch
HEAD from the app's own threads. That newest-other slot is what keeps a
concurrent reader's artifacts alive without any cross-thread locking.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pipeline import settings

SCRATCH = settings.verify_scratch()

# The image repository prepare_base tags, and the tag shape it uses.
BASE_REPO = "pr-verify-base"
# The hardened sandbox image's repository (`verify_driver.sandbox_image` names
# one tag per pnpm pin) and the label every base image carries naming the
# sandbox tag it was built FROM.
SANDBOX_REPO = "pr-verify"
SANDBOX_LABEL = "prospector.sandbox-image"
_TAG_RE = re.compile(r"^([0-9a-f]{12})-t(\d+)$")
_SHA12_RE = re.compile(r"^[0-9a-f]{12}$")

# Both stages of sandbox/Dockerfile.base carry this label, so a dangling image
# left by one of our builds can be pruned without touching anything else on the
# daemon.
LABEL = "prospector.verify-base=1"

# How many generations survive a sweep: the pin plus one more.
KEEP_GENERATIONS = 2

# How much BuildKit cache a sweep leaves alone. It covers the build that just
# finished and any build running concurrently.
BUILD_CACHE_KEEP_HOURS = 24

# `docker image ls --format {{.CreatedAt}}` renders "2026-08-10 09:00:00 +0000
# UTC" — a timezone offset followed by its abbreviation, which strptime has no
# directive for. Only the leading three fields are parsed.
_CREATED_FMT = "%Y-%m-%d %H:%M:%S %z"


@dataclass(frozen=True)
class Generation:
    """One base SHA's local artifacts: every tier's image tag, the clone
    directory, and the newest creation time across them. `created` is None when
    no artifact yielded a readable timestamp."""
    sha12: str
    images: tuple[str, ...]
    clone_dir: Path | None
    created: datetime | None


@dataclass(frozen=True)
class Plan:
    """What a sweep would do: the SHAs that survive, and the image tags and
    clone directories to remove."""
    keep: tuple[str, ...]
    images: tuple[str, ...]
    clones: tuple[Path, ...]


def _parse_created(text: str) -> datetime | None:
    """One `docker image ls` creation stamp as an instant, or None when it does
    not parse. Unreadable is a survivable state: it costs a generation its rank,
    never the pin its artifacts."""
    parts = text.split()
    if len(parts) < 3:
        return None
    try:
        return datetime.strptime(" ".join(parts[:3]), _CREATED_FMT)
    except ValueError:
        return None


def _base_images() -> dict[str, list[tuple[str, datetime | None]]]:
    """Every local base image grouped by SHA, each as (tag, created). Tags that
    do not match the prepare_base shape — an untagged `<none>`, or an unrelated
    image sharing the repository name — are skipped."""
    out: dict[str, list[tuple[str, datetime | None]]] = {}
    p = subprocess.run(
        ["docker", "image", "ls", "--filter", f"reference={BASE_REPO}",
         "--format", "{{.Tag}}\t{{.CreatedAt}}"],
        capture_output=True, text=True, env=_env())
    for line in (p.stdout or "").splitlines():
        tag, _, created = line.partition("\t")
        m = _TAG_RE.match(tag.strip())
        if m:
            out.setdefault(m.group(1), []).append(
                (f"{BASE_REPO}:{tag.strip()}", _parse_created(created)))
    return out


def sandbox_images() -> list[str]:
    """Every `pr-verify:*` tag present in the local Docker daemon, whatever pnpm
    pin each was built for. Empty when the daemon cannot answer."""
    p = subprocess.run(
        ["docker", "image", "ls", "--filter", f"reference={SANDBOX_REPO}",
         "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True, env=_env())
    if p.returncode != 0:
        return []
    return [t for t in (line.strip() for line in p.stdout.splitlines())
            if t and not t.endswith(":<none>")]


def _is_parent_of_a_base_image(tag: str) -> bool:
    """True when a base image in the daemon records `tag` as the sandbox image
    it was built FROM."""
    p = subprocess.run(
        ["docker", "image", "ls", "--filter", f"label={SANDBOX_LABEL}={tag}", "-q"],
        capture_output=True, text=True, env=_env())
    return p.returncode == 0 and bool(p.stdout.strip())


def sandbox_plan(tags: list[str], current: str,
                 parents: set[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(keep, remove) over the sandbox tags present. The active profile's tag is
    kept by identity, built or not; so is any tag a base image here names as its
    parent, which is what lets two deployments pinning different pnpm versions
    share one daemon. Everything else in the repository is occupancy."""
    keep = [current] + sorted(t for t in tags if t != current and t in parents)
    remove = tuple(t for t in tags if t not in keep)
    return tuple(keep), remove


def _clone_dirs() -> dict[str, Path]:
    """Every base clone directory under the scratch root, by SHA. This is the
    directory build_base_image builds its context in — the clone and the
    prefetched pnpm store together — so it is what a sweep removes."""
    root = SCRATCH / "base"
    if not root.is_dir():
        return {}
    return {d.name: d for d in root.iterdir()
            if d.is_dir() and _SHA12_RE.match(d.name)}


def _env() -> dict[str, str]:
    from pipeline import verify_driver
    return verify_driver.launcher_env()


def _dir_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def list_generations() -> list[Generation]:
    """Survey this machine's base artifacts, one record per SHA. A SHA with an
    image but no clone, or a clone but no image, is a generation all the same —
    a half-present generation runs nothing and is pure occupancy."""
    images = _base_images()
    clones = _clone_dirs()
    out: list[Generation] = []
    for sha in sorted(set(images) | set(clones)):
        tags = images.get(sha, [])
        clone = clones.get(sha)
        stamps = [c for _, c in tags if c is not None]
        if clone is not None:
            mtime = _dir_mtime(clone)
            if mtime is not None:
                stamps.append(mtime)
        out.append(Generation(sha12=sha, images=tuple(t for t, _ in tags),
                              clone_dir=clone,
                              created=max(stamps) if stamps else None))
    return out


# Sorts a generation with no readable timestamp oldest.
_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


def plan(generations: list[Generation], pinned_sha: str | None) -> Plan:
    """Which generations survive and what the sweep removes.

    The pinned SHA is kept by identity and holds a slot whether or not this
    machine carries its artifacts — a pin prepared on another host still means
    only one local generation survives. The remaining slots go to the most
    recently created generations, newest first, ties broken on the SHA so the
    same survey always plans the same sweep."""
    pin = pinned_sha[:12] if pinned_sha else None
    keep = [pin] if pin else []
    ranked = sorted((g for g in generations if g.sha12 != pin),
                    key=lambda g: (g.created or _OLDEST, g.sha12), reverse=True)
    keep += [g.sha12 for g in ranked[:KEEP_GENERATIONS - len(keep)]]
    doomed = [g for g in generations if g.sha12 not in keep]
    return Plan(keep=tuple(keep),
                images=tuple(t for g in doomed for t in g.images),
                clones=tuple(g.clone_dir for g in doomed if g.clone_dir is not None))


def _rmi(image: str) -> bool:
    """Remove one image, reporting whether it went. Deliberately un-forced:
    every sandbox container runs --rm, so the only thing that holds a reference
    is a phase container running right now, and refusing there is correct."""
    p = subprocess.run(["docker", "rmi", image], capture_output=True, text=True,
                       env=_env())
    return p.returncode == 0


def _prune_build_leftovers() -> None:
    """Reclaim what the two-stage base build leaves behind. Dockerfile.base
    stages a multi-gigabyte pnpm store that never enters the shipped image; the
    layers land as dangling images under the classic builder and as build cache
    under BuildKit, so both are swept. The label scopes the first to our own
    images and the age window scopes the second to cache nothing is using."""
    subprocess.run(["docker", "image", "prune", "-f", "--filter", f"label={LABEL}"],
                   capture_output=True, text=True, env=_env())
    subprocess.run(["docker", "builder", "prune", "-f", "--filter",
                    f"until={BUILD_CACHE_KEEP_HOURS:g}h"],
                   capture_output=True, text=True, env=_env())


def collect(pinned_sha: str | None, *, sandbox_tag: str | None = None,
            dry_run: bool = False) -> dict:
    """Sweep every base generation outside the retention rule, the sandbox
    tags outside `sandbox_plan` (when `sandbox_tag` names the active one), then
    reclaim the build-side leftovers. Returns
    `{ok, keep, reclaimed, sandbox_reclaimed, error}`.

    This runs inside prepare_base, so it never raises: a pin that succeeded must
    not be undone by a failure to tidy up after it. A generation is reported
    reclaimed only once every one of its images is gone — an image a live phase
    container still holds keeps its clone too, because that run reads both."""
    result: dict = {"ok": True, "keep": [], "reclaimed": [], "sandbox_reclaimed": [],
                    "error": None}
    try:
        generations = list_generations()
        p = plan(generations, pinned_sha)
        result["keep"] = list(p.keep)
        by_sha = {g.sha12: g for g in generations}
        for sha in sorted({g.sha12 for g in generations} - set(p.keep)):
            g = by_sha[sha]
            if dry_run:
                result["reclaimed"].append(sha)
                continue
            if not all(_rmi(image) for image in g.images):
                continue
            if g.clone_dir is not None:
                try:
                    shutil.rmtree(g.clone_dir)
                except OSError:
                    continue
            result["reclaimed"].append(sha)
        if sandbox_tag is not None:
            tags = sandbox_images()
            parents = {t for t in tags if t != sandbox_tag and _is_parent_of_a_base_image(t)}
            _, doomed_tags = sandbox_plan(tags, sandbox_tag, parents)
            for tag in doomed_tags:
                if dry_run or _rmi(tag):
                    result["sandbox_reclaimed"].append(tag)
        if not dry_run:
            _prune_build_leftovers()
    except (OSError, subprocess.SubprocessError) as e:
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def teardown(*, dry_run: bool = False) -> dict:
    """Remove every base artifact this machine holds — all generations, the
    pinned one included — plus every hardened sandbox image and the scratch root.
    Returns `{ok, images, kept_images, clones, scratch, error}`: `images` went,
    `kept_images` would not (a phase container still holds them), `clones` and
    `scratch` name what was removed from disk. With `dry_run`, reports what
    would go and removes nothing. Never raises."""
    from pipeline import verify_driver
    result: dict = {"ok": True, "images": [], "kept_images": [], "clones": [],
                    "scratch": None, "error": None}
    try:
        wanted = [tag for tags in _base_images().values() for tag, _ in tags]
        # The daemon's listing plus the pinned tag, which may not be built yet.
        wanted += sorted(set(sandbox_images()) | {verify_driver.sandbox_image()})
        clones = list(_clone_dirs().values())
        if dry_run:
            result["images"] = wanted
            result["clones"] = [str(c) for c in clones]
            result["scratch"] = str(SCRATCH) if SCRATCH.exists() else None
            return result
        for image in wanted:
            (result["images"] if _rmi(image) else result["kept_images"]).append(image)
        for clone in clones:
            shutil.rmtree(clone, ignore_errors=True)
            result["clones"].append(str(clone))
        if SCRATCH.exists():
            shutil.rmtree(SCRATCH)
            result["scratch"] = str(SCRATCH)
        _prune_build_leftovers()
        result["ok"] = not result["kept_images"]
    except (OSError, subprocess.SubprocessError) as e:
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"
    return result
