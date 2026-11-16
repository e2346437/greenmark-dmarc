import shutil

from greenmark_dmarc.db import load_clients
from greenmark_dmarc.ingest import import_drop_folder, import_report_bytes

from conftest import ALL_DAYS, GOOGLE, MICROSOFT


def test_import_google_zip_and_microsoft_gz(db):
    assert import_report_bytes(db, GOOGLE, offline=True)["saved"] == 1
    assert import_report_bytes(db, MICROSOFT.read_bytes(), offline=True)["saved"] == 1
    summary = {d["domain"]: d for d in db.domain_summary(ALL_DAYS, {})}
    assert summary["example.com"]["messages"] == 15
    assert summary["example.com"]["failed"] == 3
    assert summary["example.net"]["messages"] == 8
    assert summary["example.net"]["failed"] == 0
    # One message passed DMARC only through SPF because DKIM failed.
    assert summary["example.net"]["dkim_issues"] == 1


def test_duplicate_report_is_not_stored_twice(db):
    import_report_bytes(db, GOOGLE, offline=True)
    again = import_report_bytes(db, GOOGLE, offline=True)
    assert again == {"saved": 0, "duplicate": 1, "skipped": 0}
    assert db.totals()["reports"] == 1


def test_failing_sources_listed_first(db):
    import_report_bytes(db, GOOGLE, offline=True)
    sources = db.domain_sources("example.com", ALL_DAYS)
    assert sources[0]["source_ip"] == "198.51.100.7"
    assert sources[0]["failed"] == 3
    assert sources[1]["failed"] == 0


def test_client_names_and_domains_without_reports(db, settings):
    settings.domains_file.write_text(
        "clients:\n  Acme:\n    - Example.com\n  Quiet Client:\n    - example.org\n"
    )
    clients = load_clients(settings.domains_file)
    assert clients == {"example.com": "Acme", "example.org": "Quiet Client"}
    import_report_bytes(db, GOOGLE, offline=True)
    rows = {d["domain"]: d for d in db.domain_summary(ALL_DAYS, clients)}
    assert rows["example.com"]["client"] == "Acme"
    assert rows["example.org"]["messages"] == 0  # listed so a missing feed is visible


def test_drop_folder(db, settings):
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(GOOGLE, settings.import_dir / GOOGLE.name)
    (settings.import_dir / "junk.xml").write_text("not a report")
    result = import_drop_folder(db, settings)
    assert result["saved"] == 1 and result["failed"] == 1
    assert (settings.import_dir / "done" / GOOGLE.name).exists()
    assert (settings.import_dir / "failed" / "junk.xml").exists()
