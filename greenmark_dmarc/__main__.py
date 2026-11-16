"""Container entry point: start the background poller and the web server."""

from __future__ import annotations

import logging
import os
import sys

from waitress import serve

from .config import load_settings
from .db import Database
from .ingest import Poller
from .web import create_app


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    if not settings.admin_password:
        logging.error("ADMIN_PASSWORD is not set. Set it and restart the container.")
        return 1
    db = Database(settings.db_path)
    poller = Poller(db, settings)
    poller.start()
    app = create_app(settings, db, poller)
    port = int(os.environ.get("PORT", "8080"))
    logging.info("Dashboard on port %s. Data in %s.", port, settings.data_dir)
    serve(app, host="0.0.0.0", port=port, threads=4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
