# Security policy

## Supported versions

Prospector is currently a `0.x` project without a release cadence. Security
fixes are made on the default branch; older snapshots are not supported.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through the repository's
GitHub Security tab using **Report a vulnerability**. Do not open a public issue
or pull request before maintainers have had a chance to investigate.

Include, when possible:

- the affected commit or version;
- prerequisites and a minimal reproduction;
- the security impact;
- whether the issue affects local reads, the configured GitHub App's writes,
  secret handling, command allowlists, merge gates, or sandbox isolation; and
- any proposed mitigation.

Please avoid accessing repositories, credentials, or data that you do not own
while testing. Maintainers will acknowledge the report, coordinate a fix and
disclosure, and credit reporters who want attribution.

## Deployment boundary

Prospector is a local, single-operator tool. Its backend has no application
authentication and must not be exposed to an untrusted network. Anyone who can
reach a keyed backend may be able to read triage data or invoke controlled
upstream actions. See `CLAUDE.md` and `docs/operations.md` for the full trust
and write-path model.
