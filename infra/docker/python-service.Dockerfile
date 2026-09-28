# syntax=docker/dockerfile:1.7
# ===========================================================================
# One parameterised image for every Python service (CLAUDE.md S7: one Dockerfile
# pattern, not four). Select the service with --build-arg SERVICE=<name>.
#
# Security posture (CLAUDE.md S11): multi-stage so no build toolchain reaches the
# runtime, non-root user, read-only root filesystem compatible, no package
# manager left installed in the final layer.
#
# This image is also what makes the processor and simulator runnable at all on the
# development machine: Windows Application Control blocks librdkafka's native
# library there (blocker B2), while inside this Linux image it loads normally.
# ===========================================================================

ARG PYTHON_VERSION=3.12.13
# Pinned base images. Digests are resolved by `make pin-digests` once the image
# has been built at least once; the version tag is exact in the meantime so a
# rebuild cannot silently pick up a different interpreter.
FROM python:${PYTHON_VERSION}-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ---------------------------------------------------------------------------
# Builder: resolve and install dependencies from the committed lockfile.
# ---------------------------------------------------------------------------
FROM base AS builder

# uv is copied from its own published image rather than curl-installed, so the
# build does not download an unpinned script at build time.
COPY --from=ghcr.io/astral-sh/uv:0.12.4 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency metadata first, so the expensive install layer is cached and only
# reruns when the lockfile actually changes - not on every source edit.
COPY pyproject.toml uv.lock ./
COPY libs/fleet_common/pyproject.toml libs/fleet_common/
COPY services/simulator/pyproject.toml services/simulator/
COPY services/processor/pyproject.toml services/processor/
COPY services/api/pyproject.toml services/api/
COPY services/batch/pyproject.toml services/batch/

# --frozen: fail if the lockfile does not match pyproject, rather than silently
# resolving something different from what CI tested.
RUN --mount=type=cache,target=/root/.cache/uv \
    mkdir -p libs/fleet_common/fleet_common \
             services/simulator/simulator \
             services/processor/processor \
             services/api/api \
             services/batch/batch \
 && touch libs/fleet_common/fleet_common/__init__.py \
          services/simulator/simulator/__init__.py \
          services/processor/processor/__init__.py \
          services/api/api/__init__.py \
          services/batch/batch/__init__.py \
 && uv sync --all-packages --frozen --no-dev

# Now the sources, plus the schema directory: schemas/ is the single source of
# truth for the event contract (ADR-0004) and the services validate against it at
# runtime, so it ships in the image.
COPY libs/ libs/
COPY services/ services/
COPY schemas/ schemas/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --all-packages --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM base AS runtime

ARG SERVICE
# Fail the build rather than produce an image with no entrypoint.
RUN test -n "${SERVICE}" || (echo "SERVICE build-arg is required" && exit 1)

# System libraries only: no compilers, no package manager use at runtime.
# librdkafka needs libsasl2 and zlib for SASL and compression support.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libsasl2-2 \
        zlib1g \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root, with no login shell and no home directory to write into.
RUN groupadd --system --gid 10001 fleet \
 && useradd --system --uid 10001 --gid fleet --no-create-home \
            --shell /usr/sbin/nologin fleet

WORKDIR /app

COPY --from=builder --chown=root:root /app/.venv /app/.venv
COPY --from=builder --chown=root:root /app/libs /app/libs
COPY --from=builder --chown=root:root /app/services /app/services
COPY --from=builder --chown=root:root /app/schemas /app/schemas

# Application files are owned by root and only readable by the runtime user, so a
# compromised process cannot rewrite its own code.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app" \
    SERVICE_NAME=${SERVICE}

USER 10001:10001

# The module to run is baked in from the build arg, so the compose file does not
# repeat it and the image is self-describing.
ENV FLEET_SERVICE_MODULE=${SERVICE}
ENTRYPOINT ["/bin/sh", "-c", "exec python -m \"${FLEET_SERVICE_MODULE}\" \"$@\"", "--"]
