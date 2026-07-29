# Hardened, secretless image for dynamic PR verification. Deliberately
# WITHOUT agent CLIs (claude/codex) and gh — this container never holds
# credentials and never talks to a model. It only builds and runs PR code.
FROM node:lts-trixie-slim

# postgresql-client supplies the psql binary the target repository's backup
# tests spawn for their COPY-fallback restore path (#3718: its absence failed
# that test identically in red and green).
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    git \
    jq \
    postgresql-client \
  && rm -rf /var/lib/apt/lists/*

RUN corepack enable

# The node base image already ships a `node` user at uid 1000; remove it so we
# can create our own non-root `sandbox` user at uid 1000 with home /home/sandbox.
RUN userdel -r node 2>/dev/null || true \
  && useradd --create-home --uid 1000 --shell /bin/bash sandbox \
  && mkdir -p /work \
  && chown -R sandbox:sandbox /work

ENV HOME=/home/sandbox \
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
