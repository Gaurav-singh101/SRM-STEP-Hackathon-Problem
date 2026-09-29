# Security scan evidence

**Measured, not claimed.** Produced by real runs of the commands below. The JSON reports
beside this file are the raw output.

| Field | Value |
|---|---|
| Date (UTC) | 2026-09-29 |
| Commit | recorded by the commit that adds this file |
| Scanner | Trivy 0.74.0 (via `aquasec/trivy:latest`, vulnerability DB downloaded at scan time) |
| Platform | local developer laptop, Docker Desktop |

Trivy is not installed on this machine, so it runs in a container. The invocation is in
`make security`; the two Windows-specific problems it works around are recorded at the
bottom of this file.

## Two scopes, because one of them is misleading on its own

| Report | Scope | Packages scanned |
|---|---|---|
| `trivy-fs.json` | Runtime dependencies only (Trivy's default), CRITICAL/HIGH/MEDIUM | 47 Python, **46** npm |
| `trivy-fs-with-dev.json` | **Runtime and dev dependencies**, CRITICAL/HIGH/MEDIUM/LOW | 83 Python, **355** npm |

Both are kept deliberately. Trivy excludes npm dev dependencies by default, so the first
report covers 46 of the 364 packages in `web/package-lock.json` — 318 of them are dev-only.
A "no vulnerabilities" line from that report alone would be true of about an eighth of the
JavaScript tree and would read as though it covered all of it.

It matters here specifically: **E6 in `docs/STRIDE.md` is CI supply-chain compromise**, and
`vite`, `eslint`, `vitest` and the rest of the build chain are exactly what the default
scan skips. The full-scope report is the one to cite.

## Result

| Check | Result |
|---|---|
| Python dependencies (`uv.lock`, 83 packages) | **no vulnerabilities** at LOW or above |
| JavaScript dependencies (`web/package-lock.json`, 355 packages incl. dev) | **no vulnerabilities** at LOW or above |
| Secret detection across the tracked tree | **none found** |
| Dockerfile misconfiguration (`infra/docker/python-service.Dockerfile`) | 26 checks, **25 pass, 1 LOW finding** |

### The one finding: DS-0026, no HEALTHCHECK defined (LOW)

**Not suppressed.** It is a fair finding and the scan output will keep reporting it, so
nobody can read this directory as a clean sheet when it is not.

It is also not going to be fixed in that Dockerfile, for a structural reason. The file is
one parameterised image shared by all four services (`ARG SERVICE`, then
`python -m ${FLEET_SERVICE_MODULE}`), which is the reuse CLAUDE.md §7 asks for. Only the
API speaks HTTP and has `/healthz` and `/readyz`. A single `HEALTHCHECK` in the shared base
would be wrong for the processor, and meaningless for the batch job, which is a one-shot
CLI that is *supposed* to exit.

Liveness belongs to the orchestrator in this architecture — Kubernetes probes from the Helm
chart, pointed at the endpoints that already exist. The concrete improvement available is a
compose-level `healthcheck` on the `api` service, since `docker-compose.yml` currently
defines healthchecks for the four infrastructure containers but not for our own. Recorded
as a gap rather than done.

## What these scans do not cover

Stated, because an evidence directory invites the reading that everything was checked.

- **No SAST yet.** Semgrep is not installed locally and has not been run here. CI runs it
  as a GitHub Action, but those artefacts are not committed, so there is no SAST evidence in
  this repository at the time of writing.
- **No DAST.** OWASP ZAP against the running API has not been run.
- **No container image scan.** These are filesystem and configuration scans of the source
  tree. The built images have not been scanned.
- **Skipped directories:** `.venv`, `web/node_modules`, `.git`, `web/dist`, `__pycache__`.
  This costs nothing for dependency coverage, because Trivy reads `uv.lock` and
  `package-lock.json` directly rather than walking the installed trees — and the counts
  above confirm it saw every locked package.
- **A clean dependency scan is a statement about today's CVE database**, not about the code.
  Three of the defects found during development — a field that always serialised zero, an
  alert latency that measured data age, and a leaderboard query that could not meet its
  latency target — are exactly the kind no scanner reports.

## Reproducing it

Both problems below cost a failed run each and are worth knowing.

**Run it from PowerShell, not Git Bash.** Git Bash's MSYS path conversion rewrites the
container path `/src` into `C:/Program Files/Git/src`, and Trivy then fails with
`lstat C:/Program Files/Git/src: no such file or directory` — which looks like a Trivy bug
and is not one.

**Skip the vendored directories.** Without `--skip-dirs`, the secret scanner walks `.venv`
and dies part-way with
`semaphore acquire: context deadline exceeded` on a `.pyc` file. It is a timeout presented
as an unknown error.

The vulnerability database is ~118 MB and is cached in the named volume `trivy-cache`, so
only the first run downloads it.
