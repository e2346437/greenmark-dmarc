from datetime import datetime, timedelta, timezone

import pytest

from greenmark_dmarc import alerts
from greenmark_dmarc.config import load_settings
from greenmark_dmarc.db import Database

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)  # a Monday, 10:00 in New York


@pytest.fixture
def esettings(tmp_path):
    (tmp_path / "domains.yaml").write_text("clients:\n  Example Client:\n    - example.com\n")
    return load_settings({
        "DATA_DIR": str(tmp_path), "OFFLINE": "true", "ADMIN_PASSWORD": "pw",
        "SMTP_HOST": "smtp.example.net", "SMTP_FROM": "dmarc@example.net",
        "ALERT_TO": "support@example.org", "SUMMARY_TO": "info@example.org",
        "SUMMARY_DAY": "mon", "SUMMARY_HOUR": "7", "TZ": "America/New_York",
    })


@pytest.fixture
def edb(esettings):
    return Database(esettings.db_path)


class Outbox(list):
    def __call__(self, settings, to, subject, text, html_body=None):
        self.append({"to": to, "subject": subject, "text": text, "html": html_body})


_seq = [0]


def add_report(db, domain, days_ago, sources, imported_days_ago=None):
    """sources: list of (ip, base_domain, count, dmarc_pass)."""
    _seq[0] += 1
    begin = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    imported = (NOW - timedelta(days=imported_days_ago if imported_days_ago is not None else days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        pk = conn.execute(
            "INSERT INTO reports (org_name, report_id, domain, begin_date, end_date, p, imported_at) VALUES (?,?,?,?,?,?,?)",
            ("rep", f"r{_seq[0]}", domain, begin, begin, "none", imported),
        ).lastrowid
        for ip, base, count, ok in sources:
            conn.execute(
                "INSERT INTO records (report_pk, source_ip, source_base_domain, count, dmarc_aligned, spf_aligned, dkim_aligned, disposition) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (pk, ip, base, count, int(ok), int(ok), int(ok), "none"),
            )


GOOD = ("192.0.2.1", "mail.example.net", 100, True)


def test_pass_rate_drop_alerts_once_with_client_in_subject(edb, esettings):
    for d in range(4, 20):
        add_report(edb, "example.com", d, [GOOD])
    add_report(edb, "example.com", 1, [GOOD[:2] + (50, True), ("192.0.2.1", "mail.example.net", 50, False)])
    out = Outbox()
    alerts.process_alerts(edb, esettings, NOW, out)
    assert len(out) == 1
    assert out[0]["to"] == ["support@example.org"]
    assert "Example Client - example.com" in out[0]["subject"]
    assert "pass rate 50%" in out[0]["subject"]
    # The next polls see the same problem and stay quiet.
    for minutes in (30, 60, 90):
        alerts.process_alerts(edb, esettings, NOW + timedelta(minutes=minutes), out)
    assert len(out) == 1


def test_reminder_after_repeat_days_and_resolution(edb, esettings):
    add_report(edb, "example.com", 1, [("192.0.2.9", None, 100, False)])
    out = Outbox()
    alerts.process_alerts(edb, esettings, NOW, out)
    assert len(out) == 1
    add_report(edb, "example.com", -6, [("192.0.2.9", None, 100, False)])
    alerts.process_alerts(edb, esettings, NOW + timedelta(days=7), out)
    assert len(out) == 2 and "still happening" in out[1]["subject"]
    # Problem gone: resolved only after a quiet day, then a recurrence emails again.
    alerts.process_alerts(edb, esettings, NOW + timedelta(days=9), out)  # last time it is seen
    alerts.process_alerts(edb, esettings, NOW + timedelta(days=9, hours=1), out)
    assert alerts.open_alerts(edb)  # gone, but not for a full day yet
    alerts.process_alerts(edb, esettings, NOW + timedelta(days=10, hours=1), out)
    assert alerts.open_alerts(edb) == []
    add_report(edb, "example.com", -20, [("192.0.2.9", None, 100, False)])
    alerts.process_alerts(edb, esettings, NOW + timedelta(days=21), out)
    assert len(out) == 3 and "still happening" not in out[2]["subject"]


def test_new_unauthorized_source(edb, esettings):
    for d in range(4, 20):
        add_report(edb, "example.com", d, [GOOD[:2] + (1000, True)])
    add_report(edb, "example.com", 1, [GOOD[:2] + (1000, True), ("198.51.100.7", "spoofer.example", 12, False)])
    out = Outbox()
    alerts.process_alerts(edb, esettings, NOW, out)
    assert [m["subject"] for m in out] == ["DMARC alert: Example Client - example.com - 1 new unauthorized sender"]
    assert "spoofer.example" in out[0]["text"]


def test_new_domain_does_not_flag_every_sender(edb, esettings):
    add_report(edb, "example.com", 1, [GOOD[:2] + (1000, True), ("198.51.100.7", "other.example", 12, False)])
    out = Outbox()
    alerts.process_alerts(edb, esettings, NOW, out)
    assert not [m for m in out if "unauthorized" in m["subject"]]


def test_monitor_poll_failures_and_stale(edb, esettings):
    esettings.imap_host, esettings.imap_user, esettings.imap_password = "imap.example.net", "dmarc@example.net", "x"
    add_report(edb, "example.com", 5, [GOOD], imported_days_ago=3)
    edb.set_status("consecutive_failures", "2")
    edb.set_status("last_error", "LOGIN failed")
    out = Outbox()
    alerts.process_alerts(edb, esettings, NOW, out)
    subjects = sorted(m["subject"] for m in out)
    assert any("cannot read the report mailbox" in s for s in subjects)
    assert any("no reports in 72 hours" in s for s in subjects)
    assert all(s.startswith("DMARC alert: Greenmark IT - ") for s in subjects)


def test_failed_send_is_retried(edb, esettings):
    add_report(edb, "example.com", 1, [("192.0.2.9", None, 100, False)])

    def broken(*a):
        raise OSError("connection refused")

    alerts.process_alerts(edb, esettings, NOW, broken)
    assert "connection refused" in edb.get_status()["last_email_error"]
    out = Outbox()
    alerts.process_alerts(edb, esettings, NOW + timedelta(minutes=30), out)
    assert len(out) == 1


def test_no_email_settings_means_no_alert_rows(edb, tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "ADMIN_PASSWORD": "pw"})
    add_report(edb, "example.com", 1, [("192.0.2.9", None, 100, False)])
    assert alerts.process_alerts(edb, s, NOW, Outbox())["sent"] == 0
    assert alerts.open_alerts(edb) == []


def test_weekly_summary_once_per_slot(edb, esettings):
    add_report(edb, "example.com", 2, [GOOD])
    out = Outbox()
    monday_7am = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    assert not alerts.maybe_send_summary(edb, esettings, monday_7am - timedelta(minutes=1), out)
    assert alerts.maybe_send_summary(edb, esettings, monday_7am + timedelta(minutes=5), out)
    assert not alerts.maybe_send_summary(edb, esettings, monday_7am + timedelta(minutes=35), out)
    assert len(out) == 1
    msg = out[0]
    assert msg["to"] == ["info@example.org"]
    assert "Example Client - example.com" in msg["text"]
    assert "<table" in msg["html"]
    # Container down all week: the missed slot is skipped rather than sent days late.
    assert not alerts.maybe_send_summary(edb, esettings, monday_7am + timedelta(days=9), out)
    assert alerts.maybe_send_summary(edb, esettings, monday_7am + timedelta(days=14, minutes=1), out)


def test_settings_fall_back_to_imap_login(tmp_path):
    s = load_settings({
        "DATA_DIR": str(tmp_path), "IMAP_USER": "dmarc@example.net", "IMAP_PASSWORD": "secret",
        "SMTP_HOST": "mail.example.net", "ALERT_TO": "a@example.org; b@example.org", "SUMMARY_DAY": "Friday",
    })
    assert s.smtp_user == "dmarc@example.net" and s.smtp_password == "secret"
    assert s.smtp_from == "dmarc@example.net"
    assert s.alert_to == ["a@example.org", "b@example.org"]
    assert s.summary_weekday == 4
    assert s.email_enabled
