# Approach

## Goal

One Docker container that:

1. Polls an IMAP mailbox for DMARC aggregate reports on a schedule.
2. Parses them and stores one row per report record in SQLite.
3. Serves a dashboard behind a single admin password.

It is meant for one operator watching many domains, not for end-client logins.

## What parsedmarc gives us (checked against parsedmarc 11.3.0)

- **It works as a Python library**, not only as a CLI. The functions we use:
  - `parse_report_file(path_or_bytes, offline=True)` parses a `.xml`, `.xml.gz` or `.zip` report and returns `{"report_type": "aggregate", "report": {...}}`.
  - `parsed_aggregate_reports_to_csv_rows(report)` flattens a report into one dict per record, with fields such as `domain`, `org_name`, `report_id`, `begin_date`, `end_date`, `source_ip_address`, `source_reverse_dns`, `count`, `spf_aligned`, `dkim_aligned`, `dmarc_aligned`, `disposition`, `header_from` and the raw DKIM and SPF results.
  - `IMAPConnection(host, user, password, port=993)` plus `get_dmarc_reports_from_mailbox(connection, reports_folder="INBOX", archive_folder="Archive")` reads a mailbox, parses every report email and moves processed mail to an archive folder.
  - `MSGraphConnection` and `GmailConnection` exist too, so Microsoft 365 or Gmail mailboxes can be added later.
- **There is no SQLite output.** Built-in outputs are Elasticsearch, OpenSearch, Splunk, PostgreSQL, Kafka, S3, syslog, GELF, Loki, Log Analytics and webhooks. We therefore write our own small SQLite layer from the flattened rows. That is about one table and an insert, and it keeps the image to one container.
- **Licence:** Apache-2.0. Requires Python 3.11 or later.
- **Tested:** a real Microsoft (Enterprise Outlook) `.xml.gz` report and a Google-style `.zip` report both parse correctly offline. The Google-style fixture in `tests/fixtures/` is synthetic (example.com and documentation IP ranges), so no real domains or addresses are in this repository.

## Design

- **Container:** Python slim image, parsedmarc, a small Flask app served by Waitress, with server-rendered pages, SQLite on a mounted volume (`/data`).
- **Ingest loop:** every N minutes (default 30), connect over IMAP, call `get_dmarc_reports_from_mailbox`, and insert each record into SQLite. A unique key on `(org_name, report_id)` prevents duplicates if a report is seen twice.
- **Tables:** `reports` (one row per report: org, report_id, domain, date range, published policy), `records` (one row per source IP and result, linked to a report), and `domains` (domain to client-name mapping, loaded from an optional `domains.yaml`).
- **Dashboard:** list of domains with message counts and DMARC pass rate for the last 7 and 30 days; a per-domain view of sending sources, showing which pass, which fail, and why (SPF, DKIM or alignment); highlighting of any source with failures.
- **Auth:** one admin username and password from environment variables, with a session cookie.
- **Config:** environment variables only (`IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER`, `ARCHIVE_FOLDER`, `POLL_MINUTES`, `ADMIN_PASSWORD`, `DATA_DIR`), which suits Unraid templates.
- **Reverse DNS:** on by default so sources show a hostname. Set `OFFLINE=true` to skip lookups.

## Deployment notes

- When client domains send reports to a mailbox on another domain, that domain must publish an authorization record, for example `clientdomain.com._report._dmarc.reportsdomain.net TXT "v=DMARC1"`, or a wildcard `*._report._dmarc.reportsdomain.net`. Without it, some senders will not deliver the reports.
- An Unraid template is in `unraid/` and a compose example is in `docker-compose.example.yml`.
