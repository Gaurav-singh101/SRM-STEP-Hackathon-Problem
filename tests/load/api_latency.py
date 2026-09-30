"""API latency over HTTP: p50, p95, p99 per endpoint.

    uv run python -m tests.load.api_latency --requests 400 --concurrency 8

Exists because the NFR table once claimed p95 < 200 ms on the strength of **SQL query
times**. Those came from the query-plan harness and excluded everything the endpoint does
around the query: JWKS signature verification, the Redis rate-limit check, Pydantic
serialisation, and the audit-log insert that every data read performs. A database timing
bounds one component of a request; it is not the request.

This measures the actual thing the NFR is about — a real HTTP round trip against a running
API, with a real RS256 token — and reports **p99 as well as p95**, because the PDF asks for
both and only p95 was ever discussed.

What it is not: a distributed load test. It runs from one machine against one API process,
so it measures *latency under light concurrency*, not latency at capacity. That distinction
is in the output and in the evidence file, because conflating the two is how the original
overclaim happened.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

EVIDENCE_DIR = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "load"

#: The read paths a dashboard actually hits on load. Ordered cheapest-first so a failure on
#: the simplest one is obviously a setup problem rather than a performance result.
ENDPOINTS = (
    ("healthz", "/healthz", False),
    ("fleet_summary", "/api/v1/fleet/summary?days=30", True),
    ("fleet_trend", "/api/v1/fleet/trend?days=30", True),
    ("idle_events_page", "/api/v1/idle-events?page_size=50", True),
    ("leaderboard", "/api/v1/leaderboard/idle-cost?limit=20&days=30", True),
    ("utilisation", "/api/v1/utilisation?page_size=50", True),
    ("alerts", "/api/v1/alerts?limit=50", True),
)

#: The NFR under test.
TARGET_P95_MS = 200.0
TARGET_P99_MS = 500.0


@dataclass(slots=True)
class Sample:
    name: str
    samples_ms: list[float] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def percentile(self, fraction: float) -> float | None:
        """Nearest-rank percentile.

        Nearest-rank rather than interpolated: with a few hundred samples the
        interpolation is noise dressed as precision, and p99 over 400 requests is the 4th
        slowest observation whichever way it is computed.
        """
        if not self.samples_ms:
            return None
        ordered = sorted(self.samples_ms)
        index = min(int(fraction * len(ordered)), len(ordered) - 1)
        return ordered[index]

    def summary(self) -> dict[str, object]:
        p95 = self.percentile(0.95)
        p99 = self.percentile(0.99)
        return {
            "endpoint": self.name,
            "requests": len(self.samples_ms),
            "statuses": {str(k): v for k, v in sorted(self.statuses.items())},
            "p50_ms": round(statistics.median(self.samples_ms), 1) if self.samples_ms else None,
            "p95_ms": round(p95, 1) if p95 is not None else None,
            "p99_ms": round(p99, 1) if p99 is not None else None,
            "min_ms": round(min(self.samples_ms), 1) if self.samples_ms else None,
            "max_ms": round(max(self.samples_ms), 1) if self.samples_ms else None,
            "p95_within_target": None if p95 is None else p95 < TARGET_P95_MS,
            "p99_within_target": None if p99 is None else p99 < TARGET_P99_MS,
            "errors": self.errors[:5],
        }


def _token(oidc_url: str, tenant: str, role: str) -> str:
    """Mint a real token from the local OIDC stub.

    A real signed token, not a bypass: verification is part of what the endpoint costs, and
    measuring with auth disabled would reproduce the original mistake in a new place.
    """
    body = f"tenant_id={tenant}&role={role}".encode()
    request = urllib.request.Request(f"{oidc_url}/token", data=body, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        return str(json.load(response)["access_token"])


def _time_one(url: str, headers: dict[str, str]) -> tuple[float, int, str | None]:
    request = urllib.request.Request(url, headers=headers)
    began = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
            status = response.status
        return (time.perf_counter() - began) * 1000.0, status, None
    except urllib.error.HTTPError as exc:
        exc.read()
        return (time.perf_counter() - began) * 1000.0, exc.code, None
    except Exception as exc:
        return (time.perf_counter() - began) * 1000.0, 0, str(exc)


def measure(
    base_url: str,
    path: str,
    headers: dict[str, str],
    *,
    name: str,
    requests: int,
    concurrency: int,
    warmup: int,
) -> Sample:
    url = f"{base_url}{path}"
    result = Sample(name=name)

    # Warm up and discard. The first requests pay for the connection pool, the JWKS fetch
    # and any lazy import, and including them would measure startup rather than steady state.
    for _ in range(warmup):
        _time_one(url, headers)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for elapsed_ms, status, error in pool.map(
            lambda _: _time_one(url, headers), range(requests)
        ):
            result.samples_ms.append(elapsed_ms)
            result.statuses[status] = result.statuses.get(status, 0) + 1
            if error:
                result.errors.append(error)

    return result


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # pragma: no cover
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="api_latency", description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--oidc-url", default="http://localhost:8081")
    parser.add_argument("--tenant", default="tenant-demo")
    parser.add_argument("--role", default="finance_manager")
    parser.add_argument("--requests", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=10)
    args = parser.parse_args(argv)

    try:
        token = _token(args.oidc_url, args.tenant, args.role)
    except Exception as exc:
        print(f"could not mint a token from {args.oidc_url}: {exc}", file=sys.stderr)
        print("start the OIDC stub and the API, then re-run", file=sys.stderr)
        return 2

    auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    results = []
    for name, path, needs_auth in ENDPOINTS:
        print(f"measuring {name} ...", flush=True)
        results.append(
            measure(
                args.base_url,
                path,
                auth if needs_auth else {"Accept": "application/json"},
                name=name,
                requests=args.requests,
                concurrency=args.concurrency,
                warmup=args.warmup,
            )
        )

    # Only endpoints that actually answered count towards the verdict. A 401 or a 500 is
    # fast, and letting those inflate a pass would be worse than a miss.
    authed = [r for r in results if r.name != "healthz"]
    healthy = [r for r in authed if r.statuses.get(200, 0) == len(r.samples_ms)]
    breaches = [
        r.name
        for r in healthy
        if (r.percentile(0.95) or 0) >= TARGET_P95_MS or (r.percentile(0.99) or 0) >= TARGET_P99_MS
    ]

    payload = {
        "date_utc": datetime.now(UTC).isoformat(),
        "commit": _commit(),
        "label": "local",
        "scenario": {
            "requests_per_endpoint": args.requests,
            "concurrency": args.concurrency,
            "warmup_discarded": args.warmup,
            "base_url": args.base_url,
            "role": args.role,
        },
        "targets": {"p95_ms": TARGET_P95_MS, "p99_ms": TARGET_P99_MS},
        "endpoints": [r.summary() for r in results],
        "verdict": {
            "endpoints_fully_200": [r.name for r in healthy],
            "endpoints_breaching_target": breaches,
            "all_within_target": not breaches and len(healthy) == len(authed),
        },
        "caveat": (
            "LOCAL, single-client measurement against one API process on a developer laptop. "
            "This measures HTTP latency under light concurrency, NOT latency at capacity, and "
            "is not a substitute for a distributed load test at the 100K events/s target. It "
            "does however include what the earlier SQL-only figures excluded: JWKS signature "
            "verification, the Redis rate-limit check, serialisation and the audit-log insert."
        ),
    }

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE_DIR / f"api-latency-local-{stamp}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\n{'endpoint':<20} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}  statuses")
    for r in results:
        s = r.summary()
        p50, p95, p99, mx = s["p50_ms"], s["p95_ms"], s["p99_ms"], s["max_ms"]
        statuses = ",".join(f"{k}x{v}" for k, v in s["statuses"].items())  # type: ignore[union-attr]
        print(f"{r.name:<20} {p50!s:>8} {p95!s:>8} {p99!s:>8} {mx!s:>8}  {statuses}")

    print(f"\ntargets: p95 < {TARGET_P95_MS:.0f} ms, p99 < {TARGET_P99_MS:.0f} ms")
    if breaches:
        print(f"BREACHING: {', '.join(breaches)}")
    print(f"evidence: {out}")
    print(f"\n{payload['caveat']}")

    # Non-zero when a healthy endpoint misses the target, so this can gate rather than
    # merely report. Endpoints that never returned 200 are a setup failure and also fail.
    if breaches or len(healthy) != len(authed):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
