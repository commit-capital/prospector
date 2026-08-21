#!/usr/bin/env node
// Prospector bootstrap — the `npx github:commit-capital/prospector` entry.
// Takes a bare machine to a running app: checks git and Node, installs uv if
// missing, clones the repository (or reuses a checkout it is run from), runs
// setup.sh, and starts `uv run prospector serve --dev`.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import process from "node:process";

const REPO_URL = "https://github.com/commit-capital/prospector.git";
const USAGE = `Usage: npx github:commit-capital/prospector [dir] [--no-serve]

  dir         where to clone (default: ./prospector; reused if it is
              already a Prospector checkout, as is the current directory)
  --no-serve  set up but don't start the app
`;

const cyan = (s) => (process.stdout.isTTY ? `\x1b[36m${s}\x1b[0m` : s);
const red = (s) => (process.stderr.isTTY ? `\x1b[31m${s}\x1b[0m` : s);
const log = (msg) => console.log(`${cyan("→")} ${msg}`);
const fail = (msg) => {
  console.error(`${red("✗")} ${msg}`);
  process.exit(1);
};

// uv's installer defaults to ~/.local/bin, which a fresh shell may not have
// on PATH yet; every child process here gets it appended.
const env = {
  ...process.env,
  PATH: `${process.env.PATH ?? ""}:${join(homedir(), ".local", "bin")}`,
};

const run = (cmd, args, opts = {}) =>
  spawnSync(cmd, args, { stdio: "inherit", env, ...opts });

const ok = (cmd, args) =>
  spawnSync(cmd, args, { stdio: "ignore", env }).status === 0;

const isCheckout = (dir) => {
  try {
    return (
      existsSync(join(dir, "setup.sh")) &&
      readFileSync(join(dir, "pyproject.toml"), "utf8").includes(
        'name = "prospector-triage"',
      )
    );
  } catch {
    return false;
  }
};

const args = process.argv.slice(2);
if (args.includes("--help") || args.includes("-h")) {
  console.log(USAGE);
  process.exit(0);
}
const serve = !args.includes("--no-serve");
const positional = args.filter((a) => !a.startsWith("-"));
if (positional.length > 1 || args.some((a) => a.startsWith("-") && a !== "--no-serve")) {
  fail(`unrecognized arguments\n\n${USAGE}`);
}

if (process.platform !== "darwin" && process.platform !== "linux") {
  fail("Prospector supports macOS and Linux (on Windows, use WSL).");
}
const nodeMajor = Number(process.versions.node.split(".")[0]);
if (nodeMajor < 24) {
  fail(`Node >= 24 is required (found ${process.versions.node}) — https://nodejs.org`);
}
if (!ok("git", ["--version"])) {
  fail("git is required — install it with your platform's package manager (macOS: xcode-select --install).");
}

if (ok("uv", ["--version"])) {
  log("uv found");
} else {
  log("installing uv (https://astral.sh/uv)");
  if (!ok("curl", ["--version"])) {
    fail("curl is required to install uv — or install uv yourself: https://docs.astral.sh/uv/getting-started/installation/");
  }
  const install = run("sh", ["-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"]);
  if (install.status !== 0 || !ok("uv", ["--version"])) {
    fail("uv install failed — install it manually: https://docs.astral.sh/uv/getting-started/installation/");
  }
}

// gh is how reads reach GitHub; missing auth blocks the first ingest, not setup.
if (!ok("gh", ["--version"])) {
  log("note: the gh CLI is not installed — Prospector reads GitHub through it. Install from https://cli.github.com/ and run `gh auth login` before your first ingest.");
} else if (!ok("gh", ["auth", "status"])) {
  log("note: gh is installed but not signed in — run `gh auth login` before your first ingest.");
}

let dir;
if (isCheckout(process.cwd())) {
  dir = process.cwd();
  log(`using this checkout (${dir})`);
} else {
  dir = resolve(positional[0] ?? "prospector");
  if (isCheckout(dir)) {
    log(`using existing checkout ${dir}`);
  } else if (existsSync(dir)) {
    fail(`${dir} already exists and is not a Prospector checkout`);
  } else {
    log(`cloning ${REPO_URL} into ${dir}`);
    if (run("git", ["clone", REPO_URL, dir]).status !== 0) fail("clone failed");
  }
}

log("running setup.sh (uv-locked Python env + frontend deps; idempotent)");
if (run("bash", ["setup.sh"], { cwd: dir }).status !== 0) fail("setup.sh failed");

if (!serve) {
  log(`ready — start the app with: cd ${dir} && uv run prospector serve --dev`);
  process.exit(0);
}
log("starting the app — open the printed frontend URL (Ctrl-C stops it)");
const served = run("uv", ["run", "prospector", "serve", "--dev"], { cwd: dir });
process.exit(served.status ?? 0);
