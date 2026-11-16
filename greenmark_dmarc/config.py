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
