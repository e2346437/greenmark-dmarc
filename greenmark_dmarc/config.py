"""Settings, read from environment variables so the container is easy to configure."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _addresses(value: str | None) -> list[str]:
    return [a.strip() for a in (value or "").replace(";", ",").split(",") if a.strip()]


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _weekday(value: str | None, default: int) -> int:
    v = (value or "").strip().lower()[:3]
    return WEEKDAYS.index(v) if v in WEEKDAYS else default


@dataclass
class Settings:
    data_dir: Path
    imap_host: str | None
    imap_port: int
    imap_user: str | None
    imap_password: str | None
    imap_ssl: bool
    imap_verify: bool
    imap_folder: str
    archive_folder: str
    poll_minutes: int
    offline: bool
    admin_user: str
    admin_password: str | None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_user: str | None = None
    smtp_password: str | None = field(repr=False, default=None)
    smtp_from: str | None = None
    summary_to: list[str] = field(default_factory=list)
    alert_to: list[str] = field(default_factory=list)
    summary_weekday: int = 0
    summary_hour: int = 7
    timezone: str = "America/New_York"
    alert_window_days: int = 3
    alert_min_pass_rate: float = 90.0
    alert_min_messages: int = 20
    alert_min_source_messages: int = 5
    alert_stale_hours: int = 48
    alert_poll_failures: int = 2
    alert_repeat_days: int = 7
    own_client_name: str = "Greenmark IT"
    secret_key: str = field(repr=False, default="")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dmarc.sqlite3"

    @property
    def domains_file(self) -> Path:
        return self.data_dir / "domains.yaml"

    @property
    def import_dir(self) -> Path:
        return self.data_dir / "import"

    @property
    def imap_enabled(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    data_dir = Path(env.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings(
        data_dir=data_dir,
        imap_host=env.get("IMAP_HOST") or None,
        imap_port=_int(env.get("IMAP_PORT"), 993),
        imap_user=env.get("IMAP_USER") or None,
        imap_password=env.get("IMAP_PASSWORD") or None,
        imap_ssl=_bool(env.get("IMAP_SSL"), True),
        imap_verify=_bool(env.get("IMAP_VERIFY"), True),
        imap_folder=env.get("IMAP_FOLDER", "INBOX"),
        archive_folder=env.get("ARCHIVE_FOLDER", "Archive"),
        poll_minutes=max(1, _int(env.get("POLL_MINUTES"), 30)),
        offline=_bool(env.get("OFFLINE"), False),
        admin_user=env.get("ADMIN_USER", "admin"),
        admin_password=env.get("ADMIN_PASSWORD") or None,
        smtp_host=env.get("SMTP_HOST") or None,
        smtp_port=_int(env.get("SMTP_PORT"), 587),
        smtp_security=(env.get("SMTP_SECURITY") or "starttls").strip().lower(),
        # Same mailbox for reading and sending is the common case, so fall back to the IMAP login.
        smtp_user=env.get("SMTP_USER") or env.get("IMAP_USER") or None,
        smtp_password=env.get("SMTP_PASSWORD") or env.get("IMAP_PASSWORD") or None,
        smtp_from=env.get("SMTP_FROM") or env.get("SMTP_USER") or env.get("IMAP_USER") or None,
        summary_to=_addresses(env.get("SUMMARY_TO")),
        alert_to=_addresses(env.get("ALERT_TO")),
        summary_weekday=_weekday(env.get("SUMMARY_DAY"), 0),
        summary_hour=min(23, max(0, _int(env.get("SUMMARY_HOUR"), 7))),
        timezone=env.get("TZ") or "America/New_York",
        alert_window_days=max(1, _int(env.get("ALERT_WINDOW_DAYS"), 3)),
        alert_min_pass_rate=_float(env.get("ALERT_MIN_PASS_RATE"), 90.0),
        alert_min_messages=max(1, _int(env.get("ALERT_MIN_MESSAGES"), 20)),
        alert_min_source_messages=max(1, _int(env.get("ALERT_MIN_SOURCE_MESSAGES"), 5)),
        alert_stale_hours=max(1, _int(env.get("ALERT_STALE_HOURS"), 48)),
        alert_poll_failures=max(1, _int(env.get("ALERT_POLL_FAILURES"), 2)),
        alert_repeat_days=max(0, _int(env.get("ALERT_REPEAT_DAYS"), 7)),
        own_client_name=env.get("OWN_CLIENT_NAME") or "Greenmark IT",
    )
    settings.secret_key = env.get("SECRET_KEY") or _persistent_secret(data_dir)
    return settings


def _persistent_secret(data_dir: Path) -> str:
    """Keep the session key across restarts so logins survive a container update."""
    path = data_dir / ".secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_hex(32)
    path.write_text(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key
