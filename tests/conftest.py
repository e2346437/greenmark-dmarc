from pathlib import Path

import pytest

from greenmark_dmarc.config import load_settings
from greenmark_dmarc.db import Database

FIXTURES = Path(__file__).parent / "fixtures"
GOOGLE = FIXTURES / "google.com!example.com!1794700800!1794787199.zip"
MICROSOFT = FIXTURES / "enterprise.protection.outlook.com!example.net!1794614400!1794700800.xml.gz"
ALL_DAYS = 365 * 50  # fixtures have fixed dates, so look back far enough to include them


@pytest.fixture
def settings(tmp_path):
    return load_settings({"DATA_DIR": str(tmp_path), "OFFLINE": "true", "ADMIN_PASSWORD": "pw"})


@pytest.fixture
def db(settings):
    return Database(settings.db_path)
