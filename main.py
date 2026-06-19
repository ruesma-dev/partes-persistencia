# main.py
from __future__ import annotations

from pathlib import Path

import uvicorn

from config.logging_config import configure_logging
from config.settings import Settings
from interface_adapters.api.app import build_app


def main() -> int:
    settings = Settings()
    configure_logging(Path(settings.log_dir), settings.log_level)
    app = build_app(settings)
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
