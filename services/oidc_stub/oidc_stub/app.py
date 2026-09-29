"""The OIDC stub application.

Endpoints:
  GET  /.well-known/openid-configuration   discovery document
  GET  /jwks.json                          public signing keys
  POST /token                              issue a token for a subject/tenant/role
  GET  /healthz

The signing key is generated in memory at startup and never written to disk, so there
is no private key in the repository or in an image layer (CLAUDE.md S3). A restart
rotates it, which is a feature: it exercises the API's JWKS cache invalidation.
"""

from __future__ import annotations

import base64
import time
import uuid
from typing import Annotated, Any, Final

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from fleet_common.auth import Role
from fleet_common.settings import api_settings, app_settings, auth_settings

#: Key id published in the JWKS and set in every token header, so a verifier can pick
#: the right key rather than trying all of them.
KEY_ID: Final[str] = "fleet-local-stub-1"
ALGORITHM: Final[str] = "RS256"
DEFAULT_TTL_SECONDS: Final[int] = 3600

#: Environments where minting unauthenticated tokens is acceptable.
PERMITTED_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"local", "ci"})


def _b64url_uint(value: int) -> str:
    """Encode an integer as base64url, per RFC 7518 for JWK RSA parameters."""
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class _Signer:
    """Holds the in-memory RSA key pair and issues tokens."""

    def __init__(self) -> None:
        # 2048 bits: the RFC 7518 minimum for RS256 and enough for a local stub.
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict[str, Any]:
        numbers = self._private_key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": ALGORITHM,
                    "kid": KEY_ID,
                    "n": _b64url_uint(numbers.n),
                    "e": _b64url_uint(numbers.e),
                }
            ]
        }

    def issue(
        self,
        *,
        subject: str,
        tenant_id: str,
        role: Role,
        ttl_seconds: int,
        issuer: str,
        audience: str,
    ) -> tuple[str, int]:
        now = int(time.time())
        expires_at = now + ttl_seconds
        claims = {
            "sub": subject,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "nbf": now,
            "exp": expires_at,
            "jti": str(uuid.uuid4()),
            # The two claims the application actually authorises on. tenant_id lives in
            # the token precisely so it can never be taken from a request parameter.
            "tenant_id": tenant_id,
            "role": role.value,
        }
        token = jwt.encode(
            claims,
            self._private_key,
            algorithm=ALGORITHM,
            headers={"kid": KEY_ID},
        )
        return token, expires_at


def create_app() -> FastAPI:
    """Build the stub, refusing to start outside a development environment."""
    settings = app_settings()
    if settings.app_env not in PERMITTED_ENVIRONMENTS:
        raise RuntimeError(
            f"the OIDC stub issues tokens without authenticating anyone and must not "
            f"run with APP_ENV={settings.app_env!r}; permitted: "
            f"{sorted(PERMITTED_ENVIRONMENTS)}"
        )

    auth = auth_settings()
    signer = _Signer()

    app = FastAPI(
        title="Fleet OIDC stub (development only)",
        description=(
            "A local OIDC provider for development and tests. Issues real RS256 "
            "tokens so the API's verification path is genuinely exercised. Not a "
            "production identity provider and refuses to start outside local/ci."
        ),
        version="1.0.0",
    )

    # The dashboard calls /token directly with fetch, so the browser needs CORS. A real
    # OIDC provider would not: the authorization-code flow uses redirects, which are not
    # subject to CORS at all. This is therefore a property of the stub, not something the
    # eventual provider will need - and the origins come from the API's own allow-list so
    # this cannot become a wildcard by accident.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=api_settings().cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.get("/.well-known/openid-configuration")
    def discovery() -> dict[str, Any]:
        """Standard discovery document, so a client can be pointed here unmodified."""
        return {
            "issuer": auth.oidc_issuer,
            "jwks_uri": auth.oidc_jwks_url,
            "token_endpoint": f"{auth.oidc_issuer}/token",
            "id_token_signing_alg_values_supported": [ALGORITHM],
            "response_types_supported": ["token"],
            "subject_types_supported": ["public"],
            "grant_types_supported": ["client_credentials"],
            "claims_supported": ["sub", "iss", "aud", "exp", "iat", "tenant_id", "role"],
        }

    @app.get("/jwks.json")
    def jwks() -> dict[str, Any]:
        """Public keys. Only the public half is ever exposed."""
        return signer.jwks()

    @app.post("/token")
    def issue_token(
        tenant_id: Annotated[str, Form()] = "tenant-demo",
        role: Annotated[str, Form()] = Role.FINANCE_MANAGER.value,
        subject: Annotated[str, Form()] = "dev-user",
        ttl_seconds: Annotated[int, Form()] = DEFAULT_TTL_SECONDS,
    ) -> JSONResponse:
        """Mint a token. Development only - no credential is checked."""
        try:
            parsed_role = Role(role)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"unknown role {role!r}; valid: {[r.value for r in Role]}",
            ) from exc

        if not 1 <= ttl_seconds <= 86_400:
            raise HTTPException(status_code=400, detail="ttl_seconds must be between 1 and 86400")

        token, expires_at = signer.issue(
            subject=subject,
            tenant_id=tenant_id,
            role=parsed_role,
            ttl_seconds=ttl_seconds,
            issuer=auth.oidc_issuer,
            audience=auth.oidc_audience,
        )
        return JSONResponse(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": ttl_seconds,
                "expires_at": expires_at,
                "tenant_id": tenant_id,
                "role": parsed_role.value,
            }
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
