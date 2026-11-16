"""Exercises the mailbox path with a local Maildir, which uses the same parsedmarc code as IMAP."""

import mailbox
from email.message import EmailMessage

import parsedmarc

from greenmark_dmarc.ingest import poll_mailbox

from conftest import GOOGLE, MICROSOFT


def _report_email(path):
    msg = EmailMessage()
    msg["From"] = "noreply-dmarc-support@google.com"
    msg["To"] = "dmarc@example.net"
    msg["Subject"] = f"Report domain: example Submitter: test Report-ID: {path.name}"
    msg.set_content("This is an aggregate report.")
    sub = "zip" if path.suffix == ".zip" else "gzip"
    msg.add_attachment(path.read_bytes(), maintype="application", subtype=sub, filename=path.name)
    return msg


def test_poll_mailbox_saves_and_archives(db, settings, tmp_path):
    md_path = tmp_path / "Maildir"
    md = mailbox.Maildir(str(md_path), create=True)
    md.add(_report_email(GOOGLE))
    md.add(_report_email(MICROSOFT))
    md.close()

    connection = parsedmarc.MaildirConnection(str(md_path))
    result = poll_mailbox(db, settings, connection=connection)
    assert result == {"saved": 2, "duplicate": 0}
    assert db.totals() == {"reports": 2, "domains": 2}
    # Processed mail has been moved out of the inbox.
    assert len(mailbox.Maildir(str(md_path), create=False)) == 0
