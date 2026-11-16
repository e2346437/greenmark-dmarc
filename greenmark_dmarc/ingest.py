"""Getting reports in: from the IMAP mailbox on a schedule, from a drop folder, or by upload."""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import Any

import parsedmarc

from .config import Settings
from .db import Database, utcnow

log = logging.getLogger(__name__)


def import_report_bytes(db: Database, data: bytes | str | Path, offline: bool) -> dict[str, int]:
    """Parse one report file (.xml, .xml.gz or .zip) and store it."""
    parsed = parsedmarc.parse_report_file(data, offline=offline)
    if parsed.get("report_type") != "aggregate":
        return {"saved": 0, "duplicate": 0, "skipped": 1}
    saved = db.save_aggregate_report(parsed["report"])
    return {"saved": int(saved), "duplicate": int(not saved), "skipped": 0}


def import_drop_folder(db: Database, settings: Settings) -> dict[str, int]:
    """Import any report files placed in DATA_DIR/import, then move them to done/ or failed/."""
    folder = settings.import_dir
    folder.mkdir(parents=True, exist_ok=True)
    totals = {"saved": 0, "duplicate": 0, "skipped": 0, "failed": 0}
    for path in sorted(p for p in folder.iterdir() if p.is_file()):
        try:
            result = import_report_bytes(db, path, settings.offline)
            for k, v in result.items():
                totals[k] += v
            target = folder / "done"
        except Exception as exc:  # a bad file must not stop the others
            log.warning("Could not import %s: %s", path.name, exc)
            totals["failed"] += 1
            target = folder / "failed"
        target.mkdir(exist_ok=True)
        shutil.move(str(path), str(target / path.name))
    return totals


def poll_mailbox(db: Database, settings: Settings, connection: Any = None) -> dict[str, int]:
    """Read report emails over IMAP, store them, and let parsedmarc archive the processed mail.

    Mail is only moved to the archive folder after its batch has been saved, so a
    database error leaves the email in place to be retried on the next poll.
    """
    connection = connection or parsedmarc.IMAPConnection(
        host=settings.imap_host,
        user=settings.imap_user,
        password=settings.imap_password,
        port=settings.imap_port,
        ssl=settings.imap_ssl,
        verify=settings.imap_verify,
    )
    totals = {"saved": 0, "duplicate": 0}

    def save(batch: dict[str, Any]) -> bool:
        for report in batch.get("aggregate_reports", []):
            if db.save_aggregate_report(report):
                totals["saved"] += 1
            else:
                totals["duplicate"] += 1
        return True

    parsedmarc.get_dmarc_reports_from_mailbox(
        connection,
        reports_folder=settings.imap_folder,
        archive_folder=settings.archive_folder,
        offline=settings.offline,
        save_callback=save,
        attempts_file=str(settings.data_dir / "parse_attempts.json"),
    )
    return totals


def run_once(db: Database, settings: Settings) -> None:
    db.set_status("last_poll_started", utcnow())
    try:
        dropped = import_drop_folder(db, settings)
        message = f"Drop folder: {dropped['saved']} new, {dropped['duplicate']} already stored, {dropped['failed']} failed."
        if settings.imap_enabled:
            mailed = poll_mailbox(db, settings)
            message += f" Mailbox: {mailed['saved']} new, {mailed['duplicate']} already stored."
        else:
            message += " Mailbox: not configured."
        db.set_status("last_poll_ok", utcnow())
        db.set_status("last_poll_message", message)
        db.set_status("last_error", "")
        log.info(message)
    except Exception as exc:
        log.exception("Poll failed")
        db.set_status("last_error", f"{utcnow()} UTC: {type(exc).__name__}: {exc}")


class Poller(threading.Thread):
    """Runs run_once every POLL_MINUTES in the background."""

    def __init__(self, db: Database, settings: Settings):
        super().__init__(daemon=True, name="poller")
        self.db = db
        self.settings = settings
        self.wake = threading.Event()
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            run_once(self.db, self.settings)
            self.wake.wait(self.settings.poll_minutes * 60)
            self.wake.clear()

    def poll_now(self) -> None:
        self.wake.set()
