"""Run the API.

uv run python -m api
"""

from __future__ import annotations

import uvicorn

from fleet_common.settings import api_settings, app_settings


def main() -> None:
    settings = api_settings()
    uvicorn.run(
        "api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=app_settings().log_level.lower(),
    )


if __name__ == "__main__":
    main()
