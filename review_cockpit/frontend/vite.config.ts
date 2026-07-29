import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

// Ports come from a per-worktree `.env` at the repo root, so multiple checkouts
// can run side by side without colliding. Defaults preserve the original
// single-checkout setup (Vite 5173, API 8787).
const cockpitRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = resolve(cockpitRoot, '..')

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, repoRoot, '')
  const vitePort = Number(env.VITE_PORT) || 5173
  const apiPort = Number(env.API_PORT) || 8787

  return {
    plugins: [react()],
    // Expose the backend port to the client so the "backend unreachable" banner
    // can name it (see src/health.ts / App.tsx).
    define: { __API_PORT__: JSON.stringify(String(apiPort)) },
    // Read `.env` from the repo root so one file drives both the Vite port and
    // the backend port (see pipeline/devserve.py).
    envDir: repoRoot,
    server: {
      port: vitePort,
      // Fail loudly on a port collision instead of silently bumping to the next
      // free port, which would leave the printed URL pointing at the wrong server.
      strictPort: true,
      // Proxy /api to THIS worktree's backend. The frontend uses relative URLs
      // (see src/api.ts), so it is permanently wired to its own API regardless of
      // which ports other worktrees happen to grab.
      proxy: {
        '/api': { target: `http://localhost:${apiPort}`, changeOrigin: true },
      },
    },
    build: { outDir: 'dist' },
  }
})
