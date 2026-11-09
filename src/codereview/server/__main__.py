"""Run the webhook server: ``python -m codereview.server``."""
from __future__ import annotations

from dotenv import load_dotenv

from codereview.config import Settings
from codereview.errors import ConfigError
from codereview.logging_utils import setup_logging
from codereview.server.app import create_app


def main() -> None:
    load_dotenv()
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    try:
        app = create_app(settings)
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
