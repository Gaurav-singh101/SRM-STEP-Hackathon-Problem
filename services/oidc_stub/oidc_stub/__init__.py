"""Local OIDC provider stub for development and tests (D4).

Not a shortcut, and not a mock. It is a real OIDC provider: it generates an RSA key
pair, publishes a genuine JWKS document and issues genuinely RS256-signed tokens. The
API verifies them through exactly the same code path it will use against Cognito or
Keycloak, so the signature, issuer, audience and algorithm checks are all exercised.

Why this rather than Keycloak in compose: D4 is still open, so nothing may commit to a
vendor (CLAUDE.md S3). A ~700 MB container that takes a minute to start would also be a
tax on every `make up` and every CI run, and it would test the provider rather than our
validation. Swapping in a real provider is three environment variables:
``OIDC_ISSUER``, ``OIDC_AUDIENCE``, ``OIDC_JWKS_URL``.

**This must never run outside development.** It mints tokens for any requested subject
without authenticating anyone, so it refuses to start unless `APP_ENV` is local or ci.
"""

from __future__ import annotations

__all__ = ["create_app"]

from oidc_stub.app import create_app
