# In-app verification, phase 1: boot the app and measure coverage

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The VERIFY sandbox boots the target app, runs its Playwright suite
in headless Chromium with no network, gets the report and screenshots out
safely, and produces the coverage checkpoint report: which specs are
reliable, flaky or failing, and why.

**Architecture:** A per-base Chromium build is prefetched at pin time (the
networked step) and baked into the base image. A new `e2e` sandbox phase runs
the profile's Playwright command. The phase's container is kept after exit
just long enough for the host to stream its output directory out as a tar,
which the host reads entry by entry without ever extracting it, then removes.
A pure parser turns Playwright's JSON report into per-spec results. A CLI
subcommand runs the suite N times on the pinned base and renders the report.
Nothing in this phase writes to the store or changes any outcome.

**Tech Stack:** Python 3 (uv, pytest, pyright), bash, Docker, Playwright
(the target repository's own), Chromium headless.

**Spec:** `docs/superpowers/specs/2026-10-28-in-app-verification-design.md`.
This plan covers the spec's first two build steps: image, boot and artifact
channel, then the coverage checkpoint. The regression signal, in-app repro,
Supabase screenshots and issue-fix parity get their own plans after the
operator has seen the checkpoint numbers.

## Global Constraints

- Phase containers keep every existing guarantee: `boot-probe.sh` first, the
  internal network, `--cap-drop ALL`, `no-new-privileges`, no secrets, and the
  exit code as the verdict.
- Chromium is fetched only in the networked prefetch step, never in a phase
  container. Its version follows the base's pinned Playwright
  (`pnpm-lock.yaml`).
- Chromium in its full headless mode: Playwright's `chromium` channel, not
  the default headless shell and not a DOM emulator (Task 1's `channel_env`,
  Task 6). Upstream's `headless: true` stands, and no
  virtual display goes in the image.
- Everything that comes out of a container is untrusted. Never `extractall`,
  never follow a link, and cap sizes and counts.
- Paperclip specifics live in `profile.json` (`verify.e2e`), never in code.
- No store writes and no schema bump in this phase.
- AGENTS.md conventions: `from __future__ import annotations`, precise types,
  qualified imports, and every gate green (`uv run pytest`, `uv run ruff
  check .`, pyright over the listed roots).

## Review Focus

1. **Boot fails before any spec runs** (UI build error, webServer timeout).
   This must read as "the app did not boot", not as 44 failing specs. Pinned
   in Task 5 (`test_missing_report_is_a_boot_failure`) and Task 7.
2. **A hostile tar**: symlinks, hardlinks, devices, `../` names, a 5 GB entry,
   10,000 entries. Each is skipped or capped without touching the host
   filesystem. Pinned in Task 4.
3. **A kept container leaks** when the host is killed or the phase times out.
   The orphan sweep must find stopped containers too. Pinned in Task 6
   (`test_orphan_sweep_lists_stopped_containers`).
4. **A base without Playwright** (the profile has `verify.e2e` but the
   lockfile has no `@playwright/test`). The base still builds for the other
   lanes, and e2e reports "no browser for this base" clearly. Pinned in Task 2
   and Task 7.
5. **A malformed or truncated JSON report** (the test process killed
   mid-write). Parsed defensively. Treated as a boot or infrastructure
   failure, never as passes. Pinned in Task 5.

---

### Task 1: `verify.e2e` profile section

**Files:**
- Modify: `pipeline/profile.py` (add `E2eConfig` next to `SuiteConfig`, add
  the field to `VerifyPolicy`, add `_parse_e2e`, add `"e2e"` to the allowed
  `verify` keys at the `_require_object` call near line 404)
- Modify: `profile.example.json` (`verify.e2e` example)
- Test: `pipeline/tests/test_profile.py` (replace `test_e2e_cmd_is_rejected`)

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class E2eConfig:
      config: str                      # repo-relative Playwright config, e.g. "tests/e2e/playwright.config.ts"
      boot_timeout_s: int = 600        # whole-phase timeout for one suite run
      exclude: tuple[str, ...] = ()    # spec files never run (repo-relative)
      channel_env: str | None = None   # env var the repo's config reads for a Playwright channel
  VerifyPolicy.e2e: E2eConfig | None = None
  ```

- [ ] **Step 1: Write the failing tests**

```python
class TestE2e:
    def test_e2e_defaults_to_none(self):
        assert profile.parse_profile({"version": 1}, "t").verify.e2e is None

    def test_e2e_parses(self):
        p = profile.parse_profile({"version": 1, "verify": {"e2e": {
            "config": "tests/e2e/playwright.config.ts",
            "boot_timeout_s": 900, "exclude": ["tests/e2e/agentmail.spec.ts"],
            "channel_env": "PAPERCLIP_PLAYWRIGHT_CHANNEL"}}}, "t")
        assert p.verify.e2e == profile.E2eConfig(
            config="tests/e2e/playwright.config.ts", boot_timeout_s=900,
            exclude=("tests/e2e/agentmail.spec.ts",),
            channel_env="PAPERCLIP_PLAYWRIGHT_CHANNEL")

    @pytest.mark.parametrize("bad", [
        {"config": ""}, {"config": "/abs/p.ts"}, {"config": "../x.ts"},
        {"config": "a.ts", "boot_timeout_s": 0}, {"config": "a.ts", "exclude": "x"},
        {"config": "a.ts", "cmd": "rm -rf /"}, {"config": "a.ts", "channel_env": "A=B"}])
    def test_e2e_rejects_bad_values(self, bad):
        with pytest.raises(SystemExit):
            profile.parse_profile({"version": 1, "verify": {"e2e": bad}}, "t")

    def test_e2e_cmd_stays_rejected(self):
        with pytest.raises(SystemExit, match="unknown key"):
            profile.parse_profile({"version": 1, "verify": {"e2e_cmd": "pnpm test:e2e"}}, "t")
```

- [ ] **Step 2: Run** `uv run pytest pipeline/tests/test_profile.py -k E2e -v`. Expect a FAIL, because `E2eConfig` doesn't exist.
- [ ] **Step 3: Implement `_parse_e2e(raw: object, source: str) -> E2eConfig`.** Follow `_parse_suite`'s shape: `_require_object` with keys `{"config", "boot_timeout_s", "exclude", "channel_env"}`. `channel_env` must match `^[A-Z][A-Z0-9_]*$`. Paths must be repo-relative, with no leading `/` and no `..` segment. Add a comment above `E2eConfig` in the file's style saying what a profile without the section means: no in-app lane.
- [ ] **Step 4: Add the Paperclip-shaped example** to `profile.example.json` under `verify`: `"e2e": {"config": "tests/e2e/playwright.config.ts", "channel_env": "YOURAPP_PLAYWRIGHT_CHANNEL"}`. The local `profile.json` (gitignored) gets Paperclip's real values: `PAPERCLIP_PLAYWRIGHT_CHANNEL`.
- [ ] **Step 5: Run** `uv run pytest pipeline/tests/test_profile.py -v`. Expect PASS.
- [ ] **Step 6: Commit** the `profile.py`, `profile.example.json` and test changes with the message "Add the verify.e2e profile section".

### Task 2: Chromium in the image, per base

**Files:**
- Modify: `sandbox/Dockerfile` (Chromium's system libraries and fonts)
- Modify: `sandbox/Dockerfile.base` (`COPY browsers /work/browsers` in both
  stages, `ENV PLAYWRIGHT_BROWSERS_PATH=/work/browsers`)
- Modify: `pipeline/verify_driver.py` (`playwright_version`,
  `prefetch_browsers`, a `browsers/` dir in `_build_base_image`'s context)
- Test: `pipeline/tests/test_verify_driver.py`

**Interfaces:**
- Produces:
  - `playwright_version(src: Path) -> str | None`: the exact version from
    `pnpm-lock.yaml`'s `'@playwright/test@X':` package key. Returns None when
    the lockfile has none (pure: it reads only the file).
  - `prefetch_browsers(src: Path, dest: Path) -> None`: a no-op when the
    profile has no `verify.e2e` or `playwright_version` is None.
  - `base_has_browser(image: str) -> bool`: true when
    `/work/browsers` in the image holds a `chromium*` directory. Implement
    with a `docker run --rm --network none --entrypoint sh` probe, the same
    shape as the `df` probe near line 382.

- [ ] **Step 1: Write the failing tests**

```python
def test_playwright_version_reads_the_lockfile_key(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text(
        "packages:\n\n  '@playwright/test@1.62.1':\n    resolution: {}\n")
    assert verify_driver.playwright_version(tmp_path) == "1.62.1"

def test_playwright_version_is_none_without_playwright(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text("packages:\n\n  vitest@3.0.5:\n")
    assert verify_driver.playwright_version(tmp_path) is None

def test_playwright_version_refuses_a_non_semver(tmp_path):
    (tmp_path / "pnpm-lock.yaml").write_text("  '@playwright/test@1.2.3;rm -rf':\n")
    assert verify_driver.playwright_version(tmp_path) is None

def test_prefetch_browsers_installs_chromium_only_at_the_pinned_version(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(verify_driver, "_run_build_step", lambda what, argv, env: calls.append(argv))
    monkeypatch.setattr(verify_driver, "playwright_version", lambda src: "1.62.1")
    monkeypatch.setattr(profile, "active", lambda: _profile_with_e2e())
    verify_driver.prefetch_browsers(tmp_path / "src", tmp_path / "browsers")
    script = calls[0][-1]
    assert "playwright@1.62.1 install chromium" in script
    assert "PLAYWRIGHT_BROWSERS_PATH=/work/browsers" in script
    assert "install-deps" not in script and "--with-deps" not in script

def test_prefetch_browsers_is_a_noop_without_playwright(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_driver, "playwright_version", lambda src: None)
    monkeypatch.setattr(profile, "active", lambda: _profile_with_e2e())
    monkeypatch.setattr(verify_driver, "_run_build_step", lambda *a, **k: pytest.fail("ran"))
    verify_driver.prefetch_browsers(tmp_path, tmp_path / "b")
```

- [ ] **Step 2: Run** `uv run pytest pipeline/tests/test_verify_driver.py -k "playwright or browsers" -v`. Expect FAIL.
- [ ] **Step 3: Implement.** The version regex is `^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$`. `prefetch_browsers` mirrors `prefetch_store`: it runs in `sandbox_image()` with the network, mounts `dest` at `/work/browsers`, and runs `PLAYWRIGHT_BROWSERS_PATH=/work/browsers npx -y playwright@<v> install chromium`. In `_build_base_image`, create `ctx / "browsers"` beside `pnpm-store` and call `prefetch_browsers(src, ctx / "browsers")` after `prefetch_rust`. Its docstring says why the version follows the base: browser builds are tied to the Playwright version.
- [ ] **Step 4: Update the Dockerfiles.** In `sandbox/Dockerfile`, add the Debian trixie packages Playwright's Chromium needs, with fonts so screenshots render text: `fonts-liberation fonts-noto-color-emoji libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 libxrandr2`. Confirm the list against `npx playwright install-deps --dry-run chromium` on trixie and fix any renamed package. Extend the comment at the apt block with one clause on why. In `Dockerfile.base`, add `COPY --chown=sandbox:sandbox browsers /work/browsers` in both stages (the builder for parity, the final image for use) and the `ENV`.
- [ ] **Step 5: Run** the tests from Step 2. Expect PASS. Then run `uv run python pipeline/verify_driver.py build-image`. Expect a new `pr-verify:pnpm-…-<hash>` tag, since the Dockerfile hash changed.
- [ ] **Step 6: Commit** with the message "Bake the base's pinned Chromium into the verify base image".

### Task 3: The `e2e` sandbox phase

**Files:**
- Modify: `sandbox/run-phase.sh` (new `e2e` case arm)
- Modify: `sandbox/sandbox-run.sh` (accept `e2e`, `--keep`, large limits,
  `--shm-size 1g` for e2e)
- Modify: `pipeline/verify_driver.py:1197` (`LARGE_PHASES` gains `"e2e"`)
- Test: `pipeline/tests/test_sandbox_limits.py`, `pipeline/tests/test_verify_driver.py`

**Interfaces:**
- Produces: `sandbox-run.sh --phase e2e [--patch F] --test-cmd C --keep`.
  When the run ends, `/tmp/e2e-out/` inside the container holds
  `report.json` and the screenshots. Exit codes: 0 means every spec passed,
  20 means at least one failed, and any other code is infrastructure. With
  `--keep`, the container is not `--rm`'d.

- [ ] **Step 1: Write the failing tests**

```python
def test_e2e_gets_whole_repo_limits():
    m = re.search(r"^case \"\$PHASE\" in ([^)]*)\) MEM=10g; PIDS=2048;; esac$", _script(), re.MULTILINE)
    assert "e2e" in m.group(1).split("|")

def test_e2e_gets_shared_memory_for_chromium():
    assert re.search(r'^\s*e2e\)\s*shm_args=\( --shm-size 1g \)', _script(), re.MULTILINE)

def test_keep_is_the_only_way_to_skip_rm():
    text = _script()
    assert 'rm_args=( --rm )' in text and '[ "$KEEP" = 1 ] && rm_args=()' in text
```

Add one test to `test_verify_driver.py`: `"e2e" in verify_driver.LARGE_PHASES`.

- [ ] **Step 2: Run** `uv run pytest pipeline/tests/test_sandbox_limits.py pipeline/tests/test_verify_driver.py -k "e2e or keep" -v`. Expect FAIL.
- [ ] **Step 3: Implement the `e2e` arm in `run-phase.sh`.**
  1. `apply_patch` (when unset, the base tree as pinned), then `|| exit "$SENTINEL_PATCH_CONFLICT"`.
  2. `mkdir -p /tmp/e2e-out`, then `run_test_cmd`.
  3. If `/tmp/e2e-out/report.json` is missing, exit 1, because the app never produced a report. Otherwise exit 0 on 0, and `SENTINEL_TEST_FAIL` on anything else.

  `TEST_CMD` is host-authored: Task 6 builds it from the profile. Accept `--keep` and `e2e` in `sandbox-run.sh`'s argument parsing and phase list, add `e2e` to the large-limits arm, and pass `"${shm_args[@]}"` and `"${rm_args[@]}"` into `docker run`. Update the header comments that list the large phases.
- [ ] **Step 4: Run** the tests from Step 2. Expect PASS.
- [ ] **Step 5: Commit** with the message "Add the e2e sandbox phase".

### Task 4: Reading artifacts out of a container

**Files:**
- Create: `pipeline/e2e_artifacts.py`
- Test: `pipeline/tests/test_e2e_artifacts.py`

**Interfaces:**
- Produces:
  ```python
  MAX_REPORT_BYTES = 20 * 1024 * 1024
  MAX_PNG_BYTES = 5 * 1024 * 1024
  MAX_PNGS = 200
  @dataclass(frozen=True)
  class Artifacts:
      report: bytes | None                 # report.json, None if absent or over cap
      pngs: dict[str, bytes]               # sanitized relative name -> cleaned PNG
      skipped: list[str]                   # one line per entry refused, with the reason
  def read_artifacts(stream: IO[bytes]) -> Artifacts
  def clean_png(data: bytes) -> bytes | None
  ```

- [ ] **Step 1: Write the failing tests.** Build tars in memory with `tarfile.open(fileobj=buf, mode="w")`.

```python
def test_reads_report_and_pngs(): ...          # report.json + a/b.png -> both present, skipped == []
def test_skips_symlinks_hardlinks_devices_and_dirs(): ...   # each lands in skipped, none in pngs
def test_skips_traversal_and_absolute_names(): ...          # "../x.png", "/etc/x.png" -> skipped
def test_caps_png_size_and_count(): ...         # 6 MB png skipped; 201st png skipped
def test_caps_report_size(): ...                # 21 MB report.json -> report is None, reason in skipped
def test_non_png_bytes_with_png_name_skipped(): ...
def test_clean_png_keeps_only_critical_chunks():
    png = _png_with_chunks([b"IHDR", b"tEXt", b"iTXt", b"eXIf", b"IDAT", b"IEND"])
    assert _chunk_types(clean_png(png)) == [b"IHDR", b"IDAT", b"IEND"]
def test_clean_png_rejects_bad_crc_and_truncation(): ...
def test_never_writes_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); read_artifacts(_hostile_tar())
    assert list(tmp_path.iterdir()) == []
```

Each test asserts concrete values, not just "no exception".

- [ ] **Step 2: Run** `uv run pytest pipeline/tests/test_e2e_artifacts.py -v`. Expect FAIL.
- [ ] **Step 3: Implement.** Use `tarfile.open(fileobj=stream, mode="r|")` (streaming, with no random access). For each member, accept only `isreg()` files. The name is taken as a posix path relative to the root `e2e-out/` that `docker cp` prefixes, and it must have no `..` segment and no leading `/`. Read at most cap+1 bytes with `extractfile`. `clean_png` checks the 8-byte signature, walks the chunks verifying each CRC, keeps `IHDR PLTE tRNS IDAT IEND`, and returns None on any inconsistency. The module docstring states the trust rule in one sentence: all input is attacker-controlled.
- [ ] **Step 4: Run** the tests from Step 2. Expect PASS.
- [ ] **Step 5: Commit** with the message "Read e2e artifacts from a container without trusting them".

### Task 5: Parsing Playwright's report

**Files:**
- Create: `pipeline/e2e_report.py`
- Test: `pipeline/tests/test_e2e_report.py`

**Interfaces:**
- Produces:
  ```python
  SpecStatus = Literal["passed", "failed", "flaky", "skipped", "timedOut", "interrupted"]
  @dataclass(frozen=True)
  class SpecResult:
      file: str            # repo-relative spec file
      title: str           # full title path joined with " › "
      status: SpecStatus
      error: str | None    # first line of the first error, capped at 300 chars
      attachments: list[str]   # screenshot names as they appear in Artifacts.pngs
  @dataclass(frozen=True)
  class E2eRun:
      booted: bool             # False when no parseable report exists
      results: list[SpecResult]
      problem: str | None      # why booted is False, or a parse anomaly
  def parse_report(report: bytes | None, *, output_tail: str) -> E2eRun
  ```

- [ ] **Step 1: Write the failing tests** against a trimmed real report. Generate one fixture from Paperclip's own JSON reporter output for two specs, store it in `pipeline/tests/fixtures/playwright-report.json`, and redact absolute paths.

```python
def test_parses_passed_and_failed_specs(): ...      # statuses, files, titles exact
def test_retried_pass_is_flaky(): ...               # results [failed, passed] -> "flaky"
def test_error_is_first_line_capped(): ...
def test_attachment_names_are_relative_to_e2e_out(): ...
def test_missing_report_is_a_boot_failure():
    run = parse_report(None, output_tail="Error: Timed out waiting 120000ms from config.webServer.")
    assert run.booted is False and "webServer" in run.problem
def test_truncated_json_is_a_boot_failure(): ...    # report=b'{"suites": [' -> booted False
def test_unknown_status_is_failed_not_passed(): ... # status "weird" -> "failed"
```

- [ ] **Step 2: Run** `uv run pytest pipeline/tests/test_e2e_report.py -v`. Expect FAIL.
- [ ] **Step 3: Implement.** Walk `suites` recursively, using `.get` everywhere and treating every type as unknown. `problem` for a missing report is the last `Error:` line of `output_tail`, or "no report". A status outside the literal maps to `"failed"`, so unknown data never counts as a pass.
- [ ] **Step 4: Run** the tests from Step 2. Expect PASS.
- [ ] **Step 5: Commit** with the message "Parse Playwright's JSON report into per-spec results".

### Task 6: `run_e2e` in the driver

**Files:**
- Modify: `pipeline/verify_driver.py` (`run_phase` gains `keep: bool = False`;
  new `run_e2e`; `orphaned_sandbox_containers`/`stop_orphaned_sandboxes` list
  stopped containers too)
- Test: `pipeline/tests/test_verify_driver.py`

**Interfaces:**
- Consumes: `E2eConfig` (Task 1), `read_artifacts` (Task 4), `parse_report`
  (Task 5), the `e2e` phase (Task 3).
- Produces:
  ```python
  def e2e_test_cmd(cfg: profile.E2eConfig, specs: list[str] | None = None) -> str
  def run_e2e(image: str, *, tier: int, patch: Path | None = None,
              specs: list[str] | None = None, base_sha: str = "",
              head_sha: str = "") -> tuple[int, E2eRun, Artifacts]
  ```

- [ ] **Step 1: Write the failing tests.**

```python
def test_e2e_test_cmd_is_built_from_the_profile_only():
    cfg = profile.E2eConfig(config="tests/e2e/playwright.config.ts", exclude=("tests/e2e/x.spec.ts",))
    cmd = verify_driver.e2e_test_cmd(cfg)
    assert cmd.startswith("PLAYWRIGHT_JSON_OUTPUT_NAME=/tmp/e2e-out/report.json npx playwright test")
    assert "--config tests/e2e/playwright.config.ts" in cmd
    assert "--reporter=json" in cmd and "--output /tmp/e2e-out" in cmd
    assert "--grep-invert" not in cmd                      # excludes are by file, not title
    assert "grep -vxF -e tests/e2e/x.spec.ts" in cmd

def test_e2e_test_cmd_selects_full_chromium_when_the_repo_reads_a_channel():
    cfg = profile.E2eConfig(config="c.ts", channel_env="PAPERCLIP_PLAYWRIGHT_CHANNEL")
    assert verify_driver.e2e_test_cmd(cfg).startswith(
        "PAPERCLIP_PLAYWRIGHT_CHANNEL=chromium ")

def test_e2e_test_cmd_quotes_spec_names():
    cmd = verify_driver.e2e_test_cmd(_cfg(), specs=["tests/e2e/a b.spec.ts"])
    assert "'tests/e2e/a b.spec.ts'" in cmd

def test_run_e2e_collects_then_removes_the_container(monkeypatch): ...
    # fake run_phase returns (20, tail); fake `docker cp` stdout is a tar;
    # assert the docker argv order: cp <name>:/tmp/e2e-out - , then rm -f <name>,
    # and that rm runs even when cp raises.

def test_run_e2e_timeout_still_removes_the_container(monkeypatch): ...

def test_orphan_sweep_lists_stopped_containers(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: seen.append(argv) or _Done(""))
    verify_driver.stop_orphaned_sandboxes()
    assert "-a" in seen[0]
```

- [ ] **Step 2: Run** `uv run pytest pipeline/tests/test_verify_driver.py -k "e2e or orphan" -v`. Expect FAIL.
- [ ] **Step 3: Implement.**
  - `e2e_test_cmd` uses `shlex.quote` on every path. With `specs=None`, the command lists every `*.spec.ts` under the config's directory in the container (`find … | grep -vxF -e <excluded>…`), so the host never reads the tree. When `channel_env` is set, the command is prefixed with `<channel_env>=chromium`, which selects Playwright's full Chromium in its full headless mode rather than the lighter headless shell.
  - `run_e2e` calls `run_phase("e2e", …, keep=True, timeout=cfg.boot_timeout_s, runs_tests=True)`. Then it runs `docker cp <container>:/tmp/e2e-out -` with stdout piped into `read_artifacts`, and a `finally` that always runs `docker rm -f <container>`.
  - `run_e2e` raises `RuntimeError("no browser in this base")` when `base_has_browser(image)` is False, and `RuntimeError("no verify.e2e in the profile")` when the profile lacks the section.
  - `run_phase` needs the container name back, so `_run_phase_locked` passes `--keep` through and `run_e2e` names the container itself via `container_name()`.
- [ ] **Step 4: Run** the tests from Step 2. Expect PASS.
- [ ] **Step 5: Commit** with the message "Run the e2e phase and collect its artifacts".

### Task 7: The coverage checkpoint command

**Files:**
- Create: `pipeline/e2e_coverage.py` (classification and report rendering, pure)
- Modify: `pipeline/verify_driver.py` (`main`: a new `e2e-coverage [--runs N]`
  subcommand that writes the report under `SCRATCH / "e2e-coverage"`)
- Test: `pipeline/tests/test_e2e_coverage.py`

**Interfaces:**
- Consumes: `run_e2e` (Task 6), `E2eRun`/`SpecResult` (Task 5).
- Produces:
  ```python
  Verdict = Literal["reliable", "flaky", "failing"]
  Cause = Literal["network", "secret", "sandbox", "timeout", "unknown"]
  @dataclass(frozen=True)
  class SpecCoverage:
      file: str; verdict: Verdict; passes: int; runs: int
      cause: Cause | None; evidence: str | None; screenshot: str | None
  def classify(runs: list[E2eRun]) -> list[SpecCoverage]
  def cause_hint(error: str) -> Cause
  def render(cov: list[SpecCoverage], *, base_sha: str, boot_failures: list[str]) -> str
  ```

- [ ] **Step 1: Write the failing tests.**

```python
def test_reliable_means_every_run_passed(): ...        # 3/3 -> reliable
def test_any_mixed_result_is_flaky(): ...              # 2/3, or a retried "flaky" status -> flaky
def test_never_passed_is_failing(): ...                # 0/3 -> failing, with cause and evidence
def test_a_run_that_did_not_boot_counts_against_no_spec(): ...  # booted False excluded; listed in boot_failures
@pytest.mark.parametrize("err,cause", [
    ("getaddrinfo ENOTFOUND api.openai.com", "network"),
    ("connect ECONNREFUSED 140.82.112.3:443", "network"),
    ("401 Unauthorized: invalid api key", "secret"),
    ("browserType.launch: Failed to launch: libnss3.so: cannot open shared object", "sandbox"),
    ("Test timeout of 60000ms exceeded.", "timeout"),
    ("expect(locator).toBeVisible() failed", "unknown")])
def test_cause_hint(err, cause): assert e2e_coverage.cause_hint(err) == cause
def test_render_leads_with_the_count():
    out = e2e_coverage.render(_cov(reliable=40, flaky=2, failing=2), base_sha="abc", boot_failures=[])
    assert out.splitlines()[0] == "# E2E coverage at abc: 40 of 44 specs reliable"
```

- [ ] **Step 2: Run** `uv run pytest pipeline/tests/test_e2e_coverage.py -v`. Expect FAIL.
- [ ] **Step 3: Implement.**
  - `cause_hint` is an ordered list of regexes. It's a hint for the operator, never a decision.
  - `render` writes Markdown: the headline count, then a table per verdict (spec, passes/runs, cause, evidence, screenshot path), then boot failures.
  - The `e2e-coverage` subcommand pins via `local_pin`, runs `run_e2e` `--runs` times (default 3) on the base with no patch, and saves the screenshots of failing specs next to the report. It prints the report path. It exits non-zero when every run failed to boot.
  - The report is generated output. It's never parsed back and never stored.
- [ ] **Step 4: Run** the tests from Step 2. Expect PASS. Then run all the gates: `uv run pytest`, `uv run ruff check .`, and `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`. Expect all of them green.
- [ ] **Step 5: Commit** with the message "Add the e2e coverage checkpoint command".

### Task 8: Run the checkpoint on a worker and report

Not code. This is the checkpoint the spec gates the next phases on. It needs
a verify machine with Docker. Follow the "never run branch code on the live
store" rule: this subcommand reads only the local pin and writes only
scratch, but run it with `TRIAGE_SKIP_DOTENV=1` and a local SQLite store
anyway.

- [ ] **Step 1:** `uv run python pipeline/verify_driver.py build-image`, then `prepare-base --tier 1` against a local store. Confirm `base_has_browser` is true for the new base image.
- [ ] **Step 2:** `uv run python pipeline/verify_driver.py e2e-coverage --runs 3`. Expect a report path. Open the report and look at two screenshots to confirm they render text, which proves the fonts are installed.
- [ ] **Step 3:** Bring the operator the headline count and the failing and flaky tables, grouped by cause. Recommend which `sandbox` and `timeout` causes look recoverable. Stop here: the next plan (sandbox fixes or the regression signal) depends on the operator's call.
