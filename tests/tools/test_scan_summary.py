"""The scan summariser has to fail when it should.

The whole value of this tool is its exit code. `make security` previously ended its scan
commands with `|| true`, so a critical vulnerability and a clean tree produced the same
result for anyone running it. These tests exist so that cannot come back quietly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.tools import scan_summary


def _report(*results: dict[str, Any]) -> dict[str, Any]:
    return {"Trivy": {"Version": "0.74.0"}, "Results": list(results)}


def _write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **reports: dict[str, Any]) -> None:
    """Point the summariser at a temporary evidence directory."""
    for name, payload in reports.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(scan_summary, "EVIDENCE_DIR", tmp_path)


def test_a_clean_report_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clean = _report({"Target": "uv.lock", "Class": "lang-pkgs", "Packages": [{}] * 83})
    _write(tmp_path, monkeypatch, **{"trivy-fs": clean, "trivy-fs-with-dev": clean})
    assert scan_summary.main() == 0


def test_a_high_vulnerability_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    risky = _report(
        {
            "Target": "uv.lock",
            "Vulnerabilities": [
                {
                    "Severity": "HIGH",
                    "PkgName": "some-package",
                    "InstalledVersion": "1.0.0",
                    "VulnerabilityID": "CVE-2026-0001",
                    "FixedVersion": "1.0.1",
                    "Title": "a real problem",
                }
            ],
        }
    )
    _write(tmp_path, monkeypatch, **{"trivy-fs": _report(), "trivy-fs-with-dev": risky})
    assert scan_summary.main() == 1


def test_a_low_finding_is_reported_but_does_not_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scanner finding nobody can act on today should not block a commit.

    It must still be printed, so accepting it stays a decision rather than becoming a habit.
    """
    low = _report(
        {
            "Target": "infra/docker/python-service.Dockerfile",
            "Misconfigurations": [
                {"Severity": "LOW", "ID": "DS-0026", "Title": "No HEALTHCHECK defined"}
            ],
            "MisconfSummary": {"Successes": 25, "Failures": 1},
        }
    )
    _write(tmp_path, monkeypatch, **{"trivy-fs": _report(), "trivy-fs-with-dev": low})
    assert scan_summary.main() == 0
    printed = capsys.readouterr().out
    assert "DS-0026" in printed
    assert "No HEALTHCHECK defined" in printed


def test_a_detected_secret_always_fails_whatever_severity_trivy_gave_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed credential is not a matter of degree."""
    leaked = _report(
        {
            "Target": ".env",
            "Secrets": [
                {"Severity": "LOW", "RuleID": "generic-api-key", "Title": "Generic API Key"}
            ],
        }
    )
    _write(tmp_path, monkeypatch, **{"trivy-fs": _report(), "trivy-fs-with-dev": leaked})
    assert scan_summary.main() == 1


def test_the_full_scope_report_decides_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default-scope report excludes npm dev dependencies - 318 of 364 here.

    A build must never pass on it while the full-scope report is failing, which is exactly
    what would happen if the two were merged or the first one won.
    """
    clean_default = _report({"Target": "web/package-lock.json", "Packages": [{}] * 46})
    failing_full = _report(
        {
            "Target": "web/package-lock.json",
            "Packages": [{}] * 355,
            "Vulnerabilities": [
                {
                    "Severity": "CRITICAL",
                    "PkgName": "a-build-tool",
                    "InstalledVersion": "2.0.0",
                    "VulnerabilityID": "CVE-2026-0002",
                    "Title": "supply chain",
                }
            ],
        }
    )
    _write(
        tmp_path,
        monkeypatch,
        **{"trivy-fs": clean_default, "trivy-fs-with-dev": failing_full},
    )
    assert scan_summary.main() == 1


def test_a_missing_report_is_stated_rather_than_treated_as_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Never scanned" must not present as "scanned and found nothing".

    Three reports now: both Trivy scopes and Semgrep. The count is asserted rather than
    merely "> 0" because a new report added without its own missing-file branch would
    otherwise pass silently - which is how an unscanned tool starts looking clean.
    """
    monkeypatch.setattr(scan_summary, "EVIDENCE_DIR", tmp_path)
    scan_summary.main()
    printed = capsys.readouterr().out
    assert printed.count("MISSING") == 3
    assert "run `make security`" in printed


def test_coverage_counts_are_printed_so_a_clean_result_can_be_read_in_proportion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "No vulnerabilities" over 46 packages reads exactly like it does over 355."""
    report = _report(
        {"Target": "uv.lock", "Packages": [{}] * 83},
        {"Target": "web/package-lock.json", "Packages": [{}] * 355},
    )
    _write(tmp_path, monkeypatch, **{"trivy-fs": report, "trivy-fs-with-dev": report})
    scan_summary.main()
    printed = capsys.readouterr().out
    assert "83 packages" in printed
    assert "355 packages" in printed


def test_a_semgrep_error_finding_fails_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Semgrep ERROR maps to HIGH, which blocks.

    The CI step used to carry `continue-on-error: true`, which made a SAST finding
    indistinguishable from a clean scan in the run summary. This is the assertion that
    keeps it distinguishable.
    """
    clean = _report()
    (tmp_path / "semgrep.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "check_id": "rules.python.lang.security.dangerous-exec",
                        "path": "services/api/api/main.py",
                        "start": {"line": 42},
                        "extra": {"severity": "ERROR", "message": "dangerous exec"},
                    }
                ],
                "paths": {"scanned": ["a.py"]},
            }
        ),
        encoding="utf-8",
    )
    _write(tmp_path, monkeypatch, **{"trivy-fs": clean, "trivy-fs-with-dev": clean})
    assert scan_summary.main() == 1


def test_a_semgrep_warning_is_reported_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """WARNING maps to MEDIUM: printed every run, does not block.

    This is the severity the 12 mutable-action-tag findings carried. They were fixed rather
    than accepted, but the mapping is what decided whether the build stopped.
    """
    clean = _report()
    (tmp_path / "semgrep.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "check_id": "rules.github-actions-mutable-action-tag",
                        "path": ".github/workflows/ci.yml",
                        "start": {"line": 33},
                        "extra": {"severity": "WARNING", "message": "mutable tag"},
                    }
                ],
                "paths": {"scanned": ["ci.yml"]},
            }
        ),
        encoding="utf-8",
    )
    _write(tmp_path, monkeypatch, **{"trivy-fs": clean, "trivy-fs-with-dev": clean})
    assert scan_summary.main() == 0
    printed = capsys.readouterr().out
    assert "mutable-action-tag" in printed
