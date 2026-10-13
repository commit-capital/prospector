# Changed-line coverage — implementation plan

Spec: `docs/superpowers/specs/2026-10-13-changed-line-coverage-design.md`.
Branch: on top of the design review (#516).

1. **Sandbox** — `sandbox/verify-coverage.mjs` (request + coverage-final.json →
   trailer); `run-phase.sh` phase `coverage`; `sandbox-run.sh` accepts the phase
   and `--coverage-tool DIR` / `--coverage-request F`, both read-only mounts;
   `sandbox/tests/test-verify-coverage.sh` runs the script on a fixture with
   host node.
2. **`verify_driver.run_phase`** — pass `coverage_tool` / `coverage_request`
   through to the launcher.
3. **`pipeline/coverage.py`** — `changed_lines`, `vitest_version`,
   `provider_dir` (fetch once, lock, complete marker), `command`,
   `parse_trailer`, `record`, `measure`, `summary`, `prompt_block`; tests in
   `pipeline/tests/test_coverage.py`.
4. **Setting** — `TRIAGE_DESIGN_COVERAGE` (bool, default on, editable) in
   `settings.py` + `settings_registry.py` + `.env.example`.
5. **Design review** — `review(..., coverage=)` fills `__COVERAGE__` and stores
   the record under `extra.coverage`; `verify_worker.run_design` measures first
   when the setting is on; tests.
6. **Display** — `pr_checks._design` appends `coverage.summary`; PRDetail's
   Design review block shows the line and uncovered ranges; `api.ts` types.
7. **Docs** — AGENTS.md design-review paragraph, `sandbox/README.md` phase list.
8. **Gates** — pytest, pyright 0, ruff, frontend build + lint on touched files,
   `sandbox/tests/test-verify-coverage.sh`; open the PR against #516's branch.
