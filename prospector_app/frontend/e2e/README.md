# Browser journeys with Vibium

`pnpm --dir prospector_app/frontend test:e2e` builds the app, type-checks the
tests, and runs them through Node's test runner and Vibium 26.8.21. Run
`uv sync --locked` and the frontend's frozen pnpm install first. The fixture
uses the repository's `.venv/bin/python`; macOS and Linux are supported.

The managed Chrome downloads on first browser launch into
`frontend/node_modules/.cache/vibium`. Ordinary dependency installs skip
Vibium's postinstall using `pnpm-workspace.yaml`, so a frontend build does not
download a browser. CI installs it explicitly before testing. To do the same
locally, from `prospector_app/frontend`:

```bash
VIBIUM_CACHE_DIR="$PWD/node_modules/.cache/vibium" pnpm exec vibium install
```

The Linux CI job enables the user namespaces Chrome's sandbox needs on its
disposable hosted runner, following [Vibium's own CI setup](https://github.com/VibiumDev/vibium/blob/v26.8.21/.github/workflows/test.yml).
Without that setup, Ubuntu's AppArmor restriction makes Chrome exit before a
browser session exists. Chrome's sandbox remains enabled.

With an existing build, rerun just the browser tests from that directory:

```bash
node --test --test-concurrency=1 e2e/*.test.ts
```

## What the tests cover

- Find a PR by number, open its detail panel, inspect the matching evidence,
  edit a closing comment and private reason, and submit a dry-run close. Check
  the actual HTTP payload, bot/operator attribution and durable Activity
  receipt. Navigate to Activity and reload; the preview remains visible and
  the PR remains open, with zero successful upstream actions.
- Open a PR flagged malicious, select merge, and check the disabled control
  and its policy explanation. Independently send the merge request over HTTP
  and verify that the backend also refuses it and preserves the open state.

Dry runs have no confirmation dialog in the product. These tests preserve
that behavior. Live confirmation, onboarding, worker transitions, and streamed
chat are future journeys; real upstream writes, agent execution, and Docker
are outside this suite.

## Isolation and diagnostics

Each test starts a fresh backend on an OS-assigned loopback port, a disposable
SQLite store seeded through `Store`, and a fresh browser/context. The backend
receives a minimal environment, skips the checkout's `.env`, uses a generated
profile and temporary diff cache, and has no token helper. Worker/sweep startup
is disabled. GitHub reads have explicit fixture responses; unknown calls,
subprocess launches, and non-loopback TCP connections are recorded and refused.
Teardown checks that none occurred, even if the application caught an error.

Routes, payload validation, policy gates, the executor, SQL persistence, snapshot
refresh and React rendering are the production implementations. There are no
test-only app routes or browser-side API mocks.

Every test retains verbose ChromeDriver logs, including when browser startup
fails before a page exists. Launch errors also land in `launch-error.txt`.
Tests that reach a page write a final screenshot, page HTML, browser console/errors, failed
HTTP responses, server log, POST request log, and unexpected-boundary log under
`artifacts/<test-name>/`. CI uploads these as `vibium-e2e` for seven days, including
on failure. Scratch stores are removed and server/browser processes stopped.
Artifacts contain synthetic fixture data; they are gitignored.

## Exercising Vibium

The journeys exercise headless startup, isolated contexts, semantic and CSS
locators, input and select interactions, React rerenders, waits for visible
state, reloads, screenshots, and console/network events. There are no test
retries or fixed sleeps between browser actions. The short polling loop in
`fixture.ts` is only for backend startup.

When evaluating an upgrade, repeat the journeys and inspect diagnostics for
unexplained failures. Keep minimal reproducers and observed timings for Vibium
feedback; do not hide flaky behavior behind retries. The initial integration
needed explicit pnpm build-script handling. The captured React
`HydrateFallback` warning comes from Prospector, not Vibium.

Initial repeatability check (2026-09-21): three consecutive passes of both
journeys on macOS arm64, Node 26.8.1, Vibium 26.8.21 and managed Chrome
153.0.8010.52. Whole-suite times were 25.1s, 24.6s and 24.9s, with no retries,
browser runtime errors, failed app responses or unexpected external calls.
The GitHub Actions job targets Linux and Node 24. Its initial run exposed the
missing Ubuntu namespace setup described above; the app backend started, but
Chrome exited before either journey ran.
