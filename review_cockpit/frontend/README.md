# Prospector frontend

The React/Vite single-page app for Prospector. It renders the cluster board,
PR explorer and diff views, issue triage, activity history, and operator
controls backed by the FastAPI service in `../backend`.

Run the normal development stack from the repository root:

```bash
./setup.sh
uv run pr-triager serve --dev
```

Vite serves the UI on `VITE_PORT` (5173 by default) and proxies `/api` to the
backend on `API_PORT` (8787 by default). Both values come from the repository
root `.env`.

For frontend-only work:

```bash
pnpm --dir review_cockpit/frontend dev
pnpm --dir review_cockpit/frontend lint
pnpm --dir review_cockpit/frontend build
```

The production build is emitted to `review_cockpit/frontend/dist`. When that
directory exists, `uv run pr-triager serve` serves the SPA and API from one
local process.

Source layout:

- `src/main.tsx` defines routes and route-level code splitting.
- `src/views/` contains page-level screens.
- `src/components/` contains shared UI and domain components.
- `src/api.ts` and adjacent modules define the backend client and shared types.
- `src/styles.css` contains the application styling.

The browser app assumes a same-origin `/api`; do not add a second backend URL
configuration path. The deployment boundary remains local and single-operator,
as documented in the repository README.
