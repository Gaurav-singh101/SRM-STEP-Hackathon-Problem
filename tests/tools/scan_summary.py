"""Summarise Trivy reports, and fail the build on a real finding.

    uv run python -m tests.tools.scan_summary

Exists because a 50 KB JSON report is evidence nobody reads. `make security` prints this
instead, so a new vulnerability is visible in the terminal rather than discovered later by
someone diffing the report.

**The exit code is the point.** CRITICAL or HIGH fails. The Makefile previously ended its
scan commands with `|| true`, which meant a critical vulnerability and a clean tree looked
identical to anyone running `make security` - CLAUDE.md §3 forbids exactly that kind of
green.

LOW and MEDIUM are printed and do not fail, because a scanner finding that nobody can act
on today should not block a commit. They are still printed every run, so "we accepted it"
stays a decision rather than becoming a habit.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVIDENCE_DIR = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "security"

#: Severities that fail the build. Anything else is reported only.
BLOCKING = ("CRITICAL", "HIGH")

_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str
    kind: str
    target: str
    identifier: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING


def _findings(report: dict[str, Any]) -> list[Finding]:
    found: list[Finding] = []
    for result in report.get("Results") or []:
        target = str(result.get("Target", "?"))
        for vuln in result.get("Vulnerabilities") or []:
            fixed = vuln.get("FixedVersion") or "no fix available"
            found.append(
                Finding(
                    severity=str(vuln.get("Severity", "UNKNOWN")),
                    kind="vuln",
                    target=target,
                    identifier=f"{vuln.get('PkgName')} {vuln.get('InstalledVersion')}"
                    f" ({vuln.get('VulnerabilityID')})",
                    detail=f"{(vuln.get('Title') or '').strip()} [fix: {fixed}]",
                )
            )
        for misconf in result.get("Misconfigurations") or []:
            found.append(
                Finding(
                    severity=str(misconf.get("Severity", "UNKNOWN")),
                    kind="misconfig",
                    target=target,
                    identifier=str(misconf.get("ID", "?")),
                    detail=str(misconf.get("Title", "")).strip(),
                )
            )
        # Secrets are always treated as blocking regardless of the severity Trivy assigns:
        # a committed credential is not a matter of degree.
        for secret in result.get("Secrets") or []:
            found.append(
                Finding(
                    severity="CRITICAL",
                    kind="secret",
                    target=target,
                    identifier=str(secret.get("RuleID", "?")),
                    detail=str(secret.get("Title", "")).strip(),
                )
            )
    return found


def _coverage(report: dict[str, Any]) -> list[tuple[str, int, str]]:
    """Packages scanned per target, so a clean result can be read in proportion.

    Reported because "no vulnerabilities" over an eighth of the dependency tree reads
    exactly like "no vulnerabilities" over all of it.
    """
    rows = []
    for result in report.get("Results") or []:
        summary = result.get("MisconfSummary")
        note = ""
        if summary:
            note = (
                f"{summary.get('Successes', 0)} checks passed, {summary.get('Failures', 0)} failed"
            )
        rows.append((str(result.get("Target", "?")), len(result.get("Packages") or []), note))
    return rows


def _print_semgrep(path: Path) -> list[Finding]:
    """Semgrep findings, folded into the same verdict as the Trivy ones.

    Separate reader because the JSON shape differs, but the same Finding type and the same
    exit code: a SAST report that only gets printed is a report nobody acts on. Semgrep's
    own severities are ERROR/WARNING/INFO, mapped onto the Trivy scale so one rule decides
    the build rather than two.
    """
    if not path.exists():
        print(f"\nsemgrep: MISSING ({path.name}) - run `make security`")
        return []

    report = json.loads(path.read_text(encoding="utf-8"))
    results = report.get("results") or []
    scanned = len((report.get("paths") or {}).get("scanned") or [])
    print(f"\nsemgrep SAST  [{path.name}, {scanned} file(s) scanned]")

    mapping = {"ERROR": "HIGH", "WARNING": "MEDIUM", "INFO": "LOW"}
    found = []
    for result in results:
        extra = result.get("extra") or {}
        found.append(
            Finding(
                severity=mapping.get(str(extra.get("severity", "INFO")), "LOW"),
                kind="sast",
                target=f"{result.get('path')}:{(result.get('start') or {}).get('line')}",
                identifier=str(result.get("check_id", "?")).split(".")[-1],
                detail=str(extra.get("message", "")).strip().splitlines()[0][:140],
            )
        )
    if not found:
        print("  no findings")
    return found


def _print_report(path: Path, scope: str) -> list[Finding]:
    if not path.exists():
        print(f"  {scope}: MISSING ({path.name}) - run `make security`")
        return []

    report = json.loads(path.read_text(encoding="utf-8"))
    version = (report.get("Trivy") or {}).get("Version", "?")
    print(f"\n{scope}  [{path.name}, Trivy {version}]")
    for target, packages, note in _coverage(report):
        detail = f"{packages} packages" if packages else (note or "-")
        print(f"  {target}: {detail}")

    findings = _findings(report)
    if not findings:
        print("  no findings")
    return findings


def main() -> int:
    print("Trivy scan summary")
    print("=" * 70)

    semgrep = _print_semgrep(EVIDENCE_DIR / "semgrep.json")
    default = _print_report(EVIDENCE_DIR / "trivy-fs.json", "runtime dependencies only")
    full = _print_report(EVIDENCE_DIR / "trivy-fs-with-dev.json", "runtime AND dev dependencies")

    # The full-scope report decides the outcome. The default-scope one is kept for
    # comparison but must never be the thing a build passes on, because it silently
    # excludes npm dev dependencies.
    findings = (full or default) + semgrep
    if not findings:
        print("\nno findings in either scope")
        return 0

    counts = Counter(f.severity for f in findings)
    print("\n" + "=" * 70)
    print(
        "findings: "
        + ", ".join(
            f"{sev} {n}" for sev, n in sorted(counts.items(), key=lambda kv: _ORDER.get(kv[0], 9))
        )
    )

    for finding in sorted(findings, key=lambda f: _ORDER.get(f.severity, 9)):
        marker = "FAIL" if finding.blocking else "note"
        print(f"  [{marker}] {finding.severity:<8} {finding.kind:<9} {finding.target}")
        print(f"         {finding.identifier}")
        if finding.detail:
            print(f"         {finding.detail}")

    blocking = [f for f in findings if f.blocking]
    if blocking:
        print(f"\n{len(blocking)} finding(s) at {' or '.join(BLOCKING)}. Failing.", file=sys.stderr)
        return 1

    print(
        "\nNothing at CRITICAL or HIGH. The findings above are reported every run rather "
        "than suppressed, so accepting them stays a decision."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
