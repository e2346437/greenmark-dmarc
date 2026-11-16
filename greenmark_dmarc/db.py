"""SQLite storage for parsed DMARC aggregate reports."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import parsedmarc
import yaml

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY,
    org_name     TEXT NOT NULL,
    org_email    TEXT,
    report_id    TEXT NOT NULL,
    domain       TEXT NOT NULL,
    begin_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    p            TEXT,
    sp           TEXT,
    pct          TEXT,
    adkim        TEXT,
    aspf         TEXT,
    imported_at  TEXT NOT NULL,
    UNIQUE (org_name, report_id)
);
CREATE INDEX IF NOT EXISTS reports_domain_date ON reports (domain, begin_date);

CREATE TABLE IF NOT EXISTS records (
    id                 INTEGER PRIMARY KEY,
    report_pk          INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    source_ip          TEXT NOT NULL,
    source_reverse_dns TEXT,
    source_base_domain TEXT,
    source_name        TEXT,
    count              INTEGER NOT NULL,
    disposition        TEXT,
    dmarc_aligned      INTEGER NOT NULL,
    spf_aligned        INTEGER NOT NULL,
    dkim_aligned       INTEGER NOT NULL,
    header_from        TEXT,
    envelope_from      TEXT,
    dkim_domains       TEXT,
    dkim_results       TEXT,
    spf_domains        TEXT,
    spf_results        TEXT
);
CREATE INDEX IF NOT EXISTS records_report ON records (report_pk);

CREATE TABLE IF NOT EXISTS status (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per alert issue, so an ongoing problem sends one email (one ticket), not one per poll.
CREATE TABLE IF NOT EXISTS alerts (
    key          TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    domain       TEXT,
    client       TEXT,
    subject      TEXT NOT NULL,
    details      TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    last_sent    TEXT,
    resolved_at  TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- writing -------------------------------------------------------

    def save_aggregate_report(self, report: dict[str, Any]) -> bool:
        """Store one parsedmarc aggregate report. Returns False if it was already stored."""
        rows = parsedmarc.parsed_aggregate_reports_to_csv_rows(report)
        if not rows:
            return False
        first = rows[0]
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO reports
                   (org_name, org_email, report_id, domain, begin_date, end_date,
                    p, sp, pct, adkim, aspf, imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    first["org_name"],
                    first.get("org_email"),
                    str(first["report_id"]),
                    str(first["domain"]).lower(),
                    first["begin_date"],
                    first["end_date"],
                    first.get("p"),
                    first.get("sp"),
                    _text(first.get("pct")),
                    first.get("adkim"),
                    first.get("aspf"),
                    utcnow(),
                ),
            )
            if cur.rowcount == 0:
                return False
            report_pk = cur.lastrowid
            conn.executemany(
                """INSERT INTO records
                   (report_pk, source_ip, source_reverse_dns, source_base_domain,
                    source_name, count, disposition, dmarc_aligned, spf_aligned,
                    dkim_aligned, header_from, envelope_from, dkim_domains,
                    dkim_results, spf_domains, spf_results)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        report_pk,
                        r["source_ip_address"],
                        r.get("source_reverse_dns"),
                        r.get("source_base_domain"),
                        r.get("source_name"),
                        int(r["count"]),
                        r.get("disposition"),
                        int(bool(r["dmarc_aligned"])),
                        int(bool(r["spf_aligned"])),
                        int(bool(r["dkim_aligned"])),
                        r.get("header_from"),
                        r.get("envelope_from"),
                        _text(r.get("dkim_domains")),
                        _text(r.get("dkim_results")),
                        _text(r.get("spf_domains")),
                        _text(r.get("spf_results")),
                    )
                    for r in rows
                ],
            )
        return True

    def set_status(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO status (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ---- reading -------------------------------------------------------

    def get_status(self) -> dict[str, str]:
        with self.connect() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM status")}

    def domain_summary(self, days: int, clients: dict[str, str]) -> list[dict[str, Any]]:
        since = _since(days)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT r.domain,
                          SUM(c.count) AS messages,
                          SUM(CASE WHEN c.dmarc_aligned THEN c.count ELSE 0 END) AS passed,
                          SUM(CASE WHEN c.dmarc_aligned THEN 0 ELSE c.count END) AS failed,
                          SUM(CASE WHEN c.dmarc_aligned AND NOT c.dkim_aligned THEN c.count ELSE 0 END) AS dkim_issues,
                          COUNT(DISTINCT r.id) AS reports,
                          MAX(r.end_date) AS last_report
                   FROM reports r JOIN records c ON c.report_pk = r.id
                   WHERE r.begin_date >= ?
                   GROUP BY r.domain""",
                (since,),
            ).fetchall()
            policies = {
                r["domain"]: r["p"]
                for r in conn.execute(
                    """SELECT domain, p FROM reports r1
                       WHERE begin_date = (SELECT MAX(begin_date) FROM reports r2 WHERE r2.domain = r1.domain)"""
                )
            }
        seen = set()
        result = []
        for r in rows:
            d = dict(r)
            d["client"] = clients.get(d["domain"], "")
            d["policy"] = policies.get(d["domain"], "")
            d["pass_rate"] = (100.0 * d["passed"] / d["messages"]) if d["messages"] else None
            seen.add(d["domain"])
            result.append(d)
        # Domains in domains.yaml with no reports yet still get a row, so gaps are visible.
        for domain, client in clients.items():
            if domain not in seen:
                result.append(
                    {
                        "domain": domain, "client": client, "messages": 0, "passed": 0,
                        "failed": 0, "dkim_issues": 0, "reports": 0, "last_report": None,
                        "policy": policies.get(domain, ""), "pass_rate": None,
                    }
                )
        result.sort(key=lambda d: (-(d["failed"] or 0), d["client"].lower(), d["domain"]))
        return result

    def domain_sources(self, domain: str, days: int) -> list[dict[str, Any]]:
        since = _since(days)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT c.source_ip,
                          MAX(c.source_reverse_dns) AS reverse_dns,
                          MAX(c.source_name) AS source_name,
                          MAX(c.source_base_domain) AS base_domain,
                          SUM(c.count) AS messages,
                          SUM(CASE WHEN c.dmarc_aligned THEN 0 ELSE c.count END) AS failed,
                          SUM(CASE WHEN c.spf_aligned THEN 0 ELSE c.count END) AS spf_fail,
                          SUM(CASE WHEN c.dkim_aligned THEN 0 ELSE c.count END) AS dkim_fail,
                          GROUP_CONCAT(DISTINCT c.disposition) AS dispositions,
                          GROUP_CONCAT(DISTINCT r.org_name) AS reporters,
                          GROUP_CONCAT(DISTINCT c.envelope_from) AS envelope_from,
                          GROUP_CONCAT(DISTINCT c.dkim_domains) AS dkim_domains
                   FROM reports r JOIN records c ON c.report_pk = r.id
                   WHERE r.domain = ? AND r.begin_date >= ?
                   GROUP BY c.source_ip
                   ORDER BY failed DESC, messages DESC""",
                (domain, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def domain_reports(self, domain: str, days: int) -> list[dict[str, Any]]:
        since = _since(days)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT r.org_name, r.report_id, r.begin_date, r.end_date, r.p, r.pct,
                          SUM(c.count) AS messages,
                          SUM(CASE WHEN c.dmarc_aligned THEN 0 ELSE c.count END) AS failed
                   FROM reports r JOIN records c ON c.report_pk = r.id
                   WHERE r.domain = ? AND r.begin_date >= ?
                   GROUP BY r.id ORDER BY r.begin_date DESC, r.org_name""",
                (domain, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def totals(self) -> dict[str, int]:
        with self.connect() as conn:
            r = conn.execute(
                "SELECT (SELECT COUNT(*) FROM reports) AS reports, "
                "(SELECT COUNT(DISTINCT domain) FROM reports) AS domains"
            ).fetchone()
        return dict(r)


def load_clients(path: Path) -> dict[str, str]:
    """Read domains.yaml and return {domain: client name}.

    Format:
        clients:
          Acme Corp:
            - acme.com
            - acme.net
    """
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    mapping: dict[str, str] = {}
    for client, domains in (data.get("clients") or {}).items():
        for domain in domains or []:
            mapping[str(domain).strip().lower()] = str(client)
    return mapping


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)
