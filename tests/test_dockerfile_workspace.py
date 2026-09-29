"""Guard: the Dockerfile's workspace list must track the uv workspace.

The service image copies each member's ``pyproject.toml`` individually so the expensive
dependency layer stays cached across source edits. The cost of that optimisation is a
hand-maintained list, and a hand-maintained list drifts.

It did: adding ``services/oidc_stub`` as a workspace member left the Dockerfile behind,
and the only symptom was ``uv sync --frozen`` failing inside the build with an opaque
exit code 2 - discovered while rebuilding images for a demo, which is the worst possible
moment. These tests move that failure to CI, where it costs nothing.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "infra" / "docker" / "python-service.Dockerfile"
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"


def workspace_members() -> set[str]:
    """Members declared in the root pyproject, expanded from any globs."""
    with ROOT_PYPROJECT.open("rb") as handle:
        config = tomllib.load(handle)

    declared: set[str] = set()
    for pattern in config["tool"]["uv"]["workspace"]["members"]:
        if "*" in pattern:
            declared.update(
                str(path.parent.relative_to(REPO_ROOT)).replace("\\", "/")
                for path in REPO_ROOT.glob(f"{pattern}/pyproject.toml")
            )
        else:
            declared.add(pattern)
    return declared


def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def dockerfile_copied_members() -> set[str]:
    """Members whose pyproject.toml the Dockerfile copies into the build context."""
    pattern = re.compile(r"^COPY\s+(\S+)/pyproject\.toml\s", re.MULTILINE)
    return set(pattern.findall(dockerfile_text()))


def test_dockerfile_exists() -> None:
    assert DOCKERFILE.is_file(), f"Dockerfile not found at {DOCKERFILE}"


def test_every_workspace_member_has_its_pyproject_copied() -> None:
    """The failure this test exists to prevent.

    ``uv sync --frozen`` resolves the whole workspace, so a member whose pyproject is
    absent from the build context fails the image build outright.
    """
    missing = workspace_members() - dockerfile_copied_members()
    assert missing == set(), (
        f"workspace members missing a COPY in the Dockerfile: {sorted(missing)}. "
        "Add both the COPY line and the package directory in the mkdir/touch block."
    )


def test_dockerfile_copies_no_unknown_member() -> None:
    """A stale COPY for a removed member would fail the build just as loudly."""
    extra = dockerfile_copied_members() - workspace_members()
    assert extra == set(), f"Dockerfile copies pyproject files for non-members: {sorted(extra)}"


def test_every_member_gets_a_placeholder_package_directory() -> None:
    """``uv sync`` needs each member's package directory to exist before the sources are
    copied, or the build fails on a member it cannot import."""
    text = dockerfile_text()
    for member in sorted(workspace_members()):
        package = member.split("/")[-1]
        # fleet_common lives at libs/fleet_common/fleet_common; services follow the same
        # shape, so the placeholder path is always <member>/<package name>.
        expected = f"{member}/{package}"
        assert expected in text, (
            f"Dockerfile has no placeholder directory for {member} "
            f"(expected a mention of {expected!r} in the mkdir/touch block)"
        )


# --------------------------------------------------------------------------
# Security posture of the image (CLAUDE.md S11)
# --------------------------------------------------------------------------


def test_image_runs_as_a_non_root_user() -> None:
    text = dockerfile_text()
    assert "USER 10001:10001" in text, "the runtime stage must drop to a non-root user"


def test_image_uses_no_latest_tag() -> None:
    """CLAUDE.md S11: pinned images, never `latest`.

    An unpinned base means a rebuild is not reproducible: the same Dockerfile can
    produce a different interpreter tomorrow.
    """
    stage_names: set[str] = set()

    for line in dockerfile_text().splitlines():
        stripped = line.strip()
        if not (stripped.startswith("FROM ") or stripped.startswith("COPY --from=ghcr")):
            continue

        assert ":latest" not in stripped, f"unpinned image: {stripped}"

        if not stripped.startswith("FROM "):
            continue

        parts = stripped.split()
        image = parts[1]
        if len(parts) >= 4 and parts[2].upper() == "AS":
            stage_names.add(parts[3])

        # A FROM that names an earlier stage carries no tag, and correctly so - only
        # external images need pinning.
        if image in stage_names:
            continue
        assert ":" in image or "$" in image, f"untagged external base image: {stripped}"


def test_build_fails_without_a_service_argument() -> None:
    """An image with no entrypoint would start and do nothing, which is harder to
    diagnose than a failed build."""
    assert 'test -n "${SERVICE}"' in dockerfile_text()


def test_schemas_ship_in_the_image() -> None:
    """schemas/ is the single source of truth for the event contract (ADR-0004) and the
    services validate against it at runtime."""
    assert "COPY schemas/ schemas/" in dockerfile_text()


@pytest.mark.parametrize("forbidden", ["apt-get install -y build-essential", "gcc"])
def test_runtime_stage_has_no_build_toolchain(forbidden: str) -> None:
    """Multi-stage exists so a compiler never reaches the runtime image."""
    runtime = dockerfile_text().split("FROM base AS runtime", 1)
    assert len(runtime) == 2, "runtime stage not found"
    assert forbidden not in runtime[1]
