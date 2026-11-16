import re

import pytest

from greenmark_dmarc.web import create_app

from conftest import GOOGLE


@pytest.fixture
def client(settings, db):
    app = create_app(settings, db)
    app.config["TESTING"] = True
    return app.test_client()


def _csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def _login(client, password="pw"):
    return client.post("/login", data={"username": "admin", "password": password, "csrf": _csrf(client)})


def test_pages_need_login(client):
    assert client.get("/").status_code == 302
    assert client.get("/domain/example.com").status_code == 302
    assert client.get("/healthz").status_code == 200


def test_wrong_password_rejected(client):
    _login(client, "nope")
    assert client.get("/").status_code == 302


def test_login_upload_and_view(client):
    assert _login(client).status_code == 302
    token = _csrf(client, "/")
    with open(GOOGLE, "rb") as f:
        r = client.post("/upload", data={"csrf": token, "reports": (f, GOOGLE.name)},
                        content_type="multipart/form-data", follow_redirects=True)
    assert "Imported 1 new report" in r.get_data(as_text=True)
    page = client.get("/?days=365").get_data(as_text=True)
    assert "example.com" in page
    detail = client.get("/domain/example.com?days=365").get_data(as_text=True)
    assert "198.51.100.7" in detail


def test_post_without_csrf_rejected(client):
    _login(client)
    assert client.post("/upload", data={}).status_code == 400


def test_email_section_and_test_button(tmp_path, monkeypatch):
    from greenmark_dmarc import alerts
    from greenmark_dmarc.config import load_settings
    from greenmark_dmarc.db import Database

    s = load_settings({
        "DATA_DIR": str(tmp_path), "ADMIN_PASSWORD": "pw", "SMTP_HOST": "mail.example.net",
        "SMTP_FROM": "dmarc@example.net", "ALERT_TO": "support@example.org", "SUMMARY_TO": "info@example.org",
    })
    sent = []
    monkeypatch.setattr(alerts, "send_email", lambda settings, to, subject, *a: sent.append((to, subject)))
    app = create_app(s, Database(s.db_path))
    app.config["TESTING"] = True
    c = app.test_client()
    _login(c)
    page = c.get("/").get_data(as_text=True)
    assert "Failure alerts to: support@example.org" in page
    token = _csrf(c, "/")
    r = c.post("/email/test", data={"csrf": token}, follow_redirects=True)
    assert "Test email sent" in r.get_data(as_text=True)
    assert sent == [(["info@example.org", "support@example.org"], "DMARC monitor test email")]
    r = c.post("/email/summary", data={"csrf": token}, follow_redirects=True)
    assert "Weekly summary sent" in r.get_data(as_text=True)
