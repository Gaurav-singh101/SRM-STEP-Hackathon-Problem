"""Run the OIDC stub.

uv run python -m oidc_stub
"""

from __future__ import annotations

import uvicorn

from fleet_common.settings import auth_settings
from oidc_stub.app import create_app


def main() -> None:
    issuer = auth_settings().oidc_issuer
    port = int(issuer.rsplit(":", 1)[-1]) if issuer.rsplit(":", 1)[-1].isdigit() else 8081
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")  # noqa: S104


if __name__ == "__main__":
    main()
