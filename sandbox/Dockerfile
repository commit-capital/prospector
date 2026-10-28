# Hardened, secretless image for dynamic PR verification. Deliberately
# WITHOUT agent CLIs (claude/codex) and gh — this container never holds
# credentials and never talks to a model. It only builds and runs PR code.
# Node 24: the target's engines require >=24.11.0, and corepack ships with Node 24
# but not with later majors.
FROM node:24-trixie-slim

# postgresql-client supplies the psql binary the target repository's backup
# tests spawn for their COPY-fallback restore path (#3718: its absence failed
# that test identically in red and green). rustup, a C toolchain, cmake and
# pkg-config build a tree's Rust crates, native ones among them (ring,
# aws-lc-sys); rustup installs no toolchain here — a base build installs the one
# the tree pins (`verify_driver.prefetch_rust`). python3 is node-gyp's
# interpreter: a native npm package with no prebuilt binary (cpu-features,
# under ssh2) compiles from its install script, and a script that fails fails
# the whole install. The libraries and fonts are what the Chromium a base bakes
# in (`verify_driver.prefetch_browsers`) links against and renders text with.
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    ca-certificates \
    cmake \
    fonts-liberation \
    fonts-noto-color-emoji \
    git \
    jq \
    libasound2t64 \
    libatk-bridge2.0-0t64 \
    libatk1.0-0t64 \
    libatspi2.0-0t64 \
    libcairo2 \
    libcups2t64 \
    libdbus-1-3 \
    libdrm2 \
    libfontconfig1 \
    libfreetype6 \
    libgbm1 \
    libglib2.0-0t64 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    pkg-config \
    postgresql-client \
    python3 \
    rustup \
  && rm -rf /var/lib/apt/lists/*

RUN corepack enable

# The node base image already ships a `node` user at uid 1000; remove it so we
# can create our own non-root `sandbox` user at uid 1000 with home /home/sandbox.
RUN userdel -r node 2>/dev/null || true \
  && useradd --create-home --uid 1000 --shell /bin/bash sandbox \
  && mkdir -p /work \
  && chown -R sandbox:sandbox /work

ENV HOME=/home/sandbox \
  RUSTUP_HOME=/work/rust/rustup \
  CARGO_HOME=/work/rust/cargo \
  PNPM_HOME=/home/sandbox/.local/share/pnpm \
  PATH=/home/sandbox/.local/share/pnpm:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /work
USER sandbox

# Cache pnpm at build time, where the network is available: corepack resolves the
# package manager over the network, and a phase container runs with no egress.
# The version is repository policy (the target repository's `packageManager`
# pin, profile verify.pnpm_version — `verify_driver.py build-image` passes it),
# so a clone of that repository resolves from this cache; corepack falls back
# to this same version as the default for a tree that pins nothing. Runs as
# `sandbox` so the cache lands in the HOME the sandbox user reads at run time.
ARG PNPM_VERSION=9.15.4
RUN corepack prepare "pnpm@${PNPM_VERSION}" --activate

# No default action — the sandbox is always driven by sandbox-run.sh, which
# mounts and runs run-phase.sh explicitly.
CMD ["bash", "-lc", "echo 'pr-verify image: run via sandbox-run.sh' && exit 0"]
