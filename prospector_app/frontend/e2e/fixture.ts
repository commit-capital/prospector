import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { TestContext } from "node:test";
import { fileURLToPath } from "node:url";
import { browser, type Page } from "vibium";

const frontend = fileURLToPath(new URL("../", import.meta.url));
const root = fileURLToPath(new URL("../../../", import.meta.url));
// Keep the managed browser in the checkout, separate from personal browsers.
process.env.VIBIUM_CACHE_DIR = join(frontend, "node_modules/.cache/vibium");

export async function jsonLines(path: string): Promise<Record<string, unknown>[]> {
  const content = await readFile(path, "utf8").catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return "";
    throw error;
  });
  return content.trim() ? content.trim().split("\n").map(line => JSON.parse(line)) : [];
}

export async function fixture(t: TestContext) {
  const scratch = await mkdtemp(join(tmpdir(), "prospector-e2e-"));
  const artifacts = join(frontend, "e2e/artifacts", t.name.replace(/[^a-z0-9]+/gi, "-"));
  await mkdir(artifacts, { recursive: true });
  let serverLog = "";
  const server = spawn(join(root, ".venv/bin/python"), [join(frontend, "e2e/server.py")], {
    cwd: scratch,
    env: { PATH: process.env.PATH, LANG: "en_US.UTF-8", PYTHONUNBUFFERED: "1",
      PROSPECTOR_E2E_SCRATCH: scratch },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let spawnError: Error | undefined;
  const stopped = once(server, "exit").catch(error => { spawnError = error; });
  server.stdout.on("data", chunk => { serverLog = (serverLog + chunk).slice(-200_000); });
  server.stderr.on("data", chunk => { serverLog = (serverLog + chunk).slice(-200_000); });
  const resources: { bro?: Awaited<ReturnType<typeof browser.start>>; page?: Page } = {};
  const errors: string[] = [];
  const consoleLog: string[] = [];
  const httpErrors: string[] = [];
  t.after(async () => {
    try {
      if (resources.page) {
        await writeFile(join(artifacts, "page.png"), await resources.page.screenshot());
        await writeFile(join(artifacts, "page.html"), await resources.page.content());
      }
    } finally {
      try { await resources.bro?.stop(); } finally {
        if (server.exitCode === null && server.signalCode === null && !spawnError) {
          server.kill("SIGTERM");
          const kill = setTimeout(() => server.kill("SIGKILL"), 5_000);
          try { await stopped; } finally { clearTimeout(kill); }
        }
        await writeFile(join(artifacts, "server.log"), serverLog);
        await writeFile(join(artifacts, "browser.json"), JSON.stringify({ errors, consoleLog, httpErrors }, null, 2));
        const unexpected = await jsonLines(join(scratch, "unexpected.jsonl"));
        await writeFile(join(artifacts, "requests.json"), JSON.stringify(await jsonLines(join(scratch, "requests.jsonl")), null, 2));
        await writeFile(join(artifacts, "unexpected.json"), JSON.stringify(unexpected, null, 2));
        await rm(scratch, { recursive: true, force: true });
        assert.deepEqual(unexpected, [], "unhandled external boundary (see artifacts)");
        assert.deepEqual(errors, [], "browser runtime errors");
        assert.deepEqual(httpErrors, [], "failed app HTTP responses");
      }
    }
  });
  let url = "";
  for (let attempt = 0; attempt < 100; attempt++) {
    if (spawnError) throw spawnError;
    assert.equal(server.exitCode, null, `server exited during startup:\n${serverLog}`);
    const port = await readFile(join(scratch, "port"), "utf8").catch(() => "");
    if (port) {
      url = `http://127.0.0.1:${port}`;
      try {
        const response = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(500) });
        if (response.ok) break;
      } catch { /* wait for the socket to start serving */ }
    }
    if (attempt === 99) throw new Error(`server startup timed out:\n${serverLog}`);
    await delay(100);
  }
  const bro = resources.bro = await browser.start({ headless: true });
  const context = await bro.newContext();
  const page = resources.page = await context.newPage();
  page.onError(error => errors.push(String(error)));
  page.onConsole(message => consoleLog.push(`${message.type()}: ${message.text()}`));
  page.onResponse(response => {
    if (response.url().startsWith(url) && response.status() >= 400) {
      httpErrors.push(`${response.status()} ${response.url()}`);
    }
  });
  await page.setViewport({ width: 1440, height: 1000 });
  return {
    page, url,
    requests: () => jsonLines(join(scratch, "requests.jsonl")),
    api: async (path: string) => {
      const response = await fetch(`${url}${path}`);
      assert.equal(response.status, 200, path);
      return response.json();
    },
  };
}
