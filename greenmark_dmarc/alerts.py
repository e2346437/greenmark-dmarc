"""Failure alerts and the weekly summary email.

Alerts go to ALERT_TO (for example a ticketing inbox). Each problem is one "issue"
with a stable key. An issue sends one email when it starts, stays quiet while it
continues, sends a reminder every ALERT_REPEAT_DAYS if it is still going, and is
marked resolved once it has been gone for a day. If it comes back after that, it
sends a new email.
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Callable
from zoneinfo import ZoneInfo

from .config import WEEKDAYS, Settings
from .db import Database, load_clients

log = logging.getLogger(__name__)

FMT = "%Y-%m-%d %H:%M:%S"
RESOLVE_AFTER = timedelta(hours=24)

Sender = Callable[[Settings, list[str], str, str, "str | None"], None]


@dataclass
class Issue:
    key: str
    kind: str
    subject: str
    details: str
    domain: str | None = None
    client: str | None = None
    extra: dict = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---- sending -----------------------------------------------------------------


def send_email(settings: Settings, to: list[str], subject: str, text: str, html_body: str | None = None) -> None:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=(settings.smtp_from or "localhost").split("@")[-1])
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=60, context=context)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60)
    with server:
        if settings.smtp_security == "starttls":
            server.starttls(context=context)
        if settings.smtp_user and settings.smtp_password:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


# ---- finding problems ----------------------------------------------------------


def _label(domain: str, clients: dict[str, str]) -> str:
    client = clients.get(domain)
    return f"{client} - {domain}" if client else domain


def find_issues(db: Database, settings: Settings, now: datetime | None = None) -> list[Issue]:
    now = now or _now()
    clients = load_clients(settings.domains_file)
    issues: list[Issue] = []
    issues += _pass_rate_issues(db, settings, clients, now)
    issues += _new_source_issues(db, settings, clients, now)
    issues += _monitor_issues(db, settings, now)
    return issues


def _pass_rate_issues(db, settings, clients, now) -> list[Issue]:
    window_start = _ts(now - timedelta(days=settings.alert_window_days))
    baseline_start = _ts(now - timedelta(days=settings.alert_window_days + 30))
    with db.connect() as conn:
        recent = conn.execute(
            """SELECT r.domain, SUM(c.count) AS messages,
                      SUM(CASE WHEN c.dmarc_aligned THEN c.count ELSE 0 END) AS passed
               FROM reports r JOIN records c ON c.report_pk = r.id
               WHERE r.begin_date >= ? GROUP BY r.domain""",
            (window_start,),
        ).fetchall()
        baseline = {
            row["domain"]: row
            for row in conn.execute(
                """SELECT r.domain, SUM(c.count) AS messages,
                          SUM(CASE WHEN c.dmarc_aligned THEN c.count ELSE 0 END) AS passed
                   FROM reports r JOIN records c ON c.report_pk = r.id
                   WHERE r.begin_date >= ? AND r.begin_date < ? GROUP BY r.domain""",
                (baseline_start, window_start),
            )
        }
    issues = []
    for row in recent:
        messages, passed = row["messages"] or 0, row["passed"] or 0
        if messages < settings.alert_min_messages:
            continue
        rate = 100.0 * passed / messages
        if rate >= settings.alert_min_pass_rate:
            continue
        domain = row["domain"]
        before = baseline.get(domain)
        before_text = (
            f"{100.0 * before['passed'] / before['messages']:.1f}% over the 30 days before that"
            if before and before["messages"]
            else "no earlier data to compare"
        )
        sources = _failing_sources(db, domain, window_start)
        details = (
            f"Domain: {domain}\n"
            f"Client: {clients.get(domain, '(not in domains.yaml)')}\n\n"
            f"DMARC pass rate over the last {settings.alert_window_days} days is {rate:.1f}% "
            f"({messages - passed:,} of {messages:,} messages failed). "
            f"The alert threshold is {settings.alert_min_pass_rate:g}%. "
            f"For comparison: {before_text}.\n\n"
            "Top failing sources:\n" + _source_lines(sources[:10])
        )
        issues.append(
            Issue(
                key=f"passrate:{domain}",
                kind="pass_rate",
                domain=domain,
                client=clients.get(domain),
                subject=f"DMARC alert: {_label(domain, clients)} - pass rate {rate:.0f}% (below {settings.alert_min_pass_rate:g}%)",
                details=details,
            )
        )
    return issues


def _new_source_issues(db, settings, clients, now) -> list[Issue]:
    window_start = _ts(now - timedelta(days=settings.alert_window_days))
    # Only judge "new" once a domain has at least a week of history, so a freshly
    # added domain does not report every one of its senders as new.
    history_before = _ts(now - timedelta(days=settings.alert_window_days + 7))
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT r.domain,
                      COALESCE(NULLIF(c.source_base_domain, ''), c.source_ip) AS source,
                      GROUP_CONCAT(DISTINCT c.source_ip) AS ips,
                      MAX(c.source_reverse_dns) AS reverse_dns,
                      SUM(c.count) AS messages,
                      SUM(CASE WHEN c.dmarc_aligned THEN 0 ELSE c.count END) AS failed,
                      GROUP_CONCAT(DISTINCT c.disposition) AS dispositions
               FROM reports r JOIN records c ON c.report_pk = r.id
               WHERE r.begin_date >= ?
                 AND r.domain IN (SELECT domain FROM reports WHERE begin_date < ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM reports r2 JOIN records c2 ON c2.report_pk = r2.id
                     WHERE r2.domain = r.domain AND r2.begin_date < ?
                       AND COALESCE(NULLIF(c2.source_base_domain, ''), c2.source_ip)
                           = COALESCE(NULLIF(c.source_base_domain, ''), c.source_ip))
               GROUP BY r.domain, source
               HAVING failed >= ?
               ORDER BY r.domain, failed DESC""",
            (window_start, history_before, window_start, settings.alert_min_source_messages),
        ).fetchall()
    by_domain: dict[str, list] = {}
    for row in rows:
        by_domain.setdefault(row["domain"], []).append(row)
    issues = []
    for domain, sources in by_domain.items():
        n = len(sources)
        lines = []
        for s in sources[:15]:
            lines.append(
                f"- {s['source']}: {s['failed']:,} of {s['messages']:,} messages failed DMARC; "
                f"IPs {s['ips']}; reverse DNS {s['reverse_dns'] or 'none'}; "
                f"receivers did: {s['dispositions'] or 'unknown'}"
            )
        if n > 15:
            lines.append(f"- and {n - 15} more (see the dashboard)")
        details = (
            f"Domain: {domain}\n"
            f"Client: {clients.get(domain, '(not in domains.yaml)')}\n\n"
            f"{n} sending source(s) seen in the last {settings.alert_window_days} days have never sent "
            "for this domain before and are failing DMARC. Either a legitimate service was added "
            "without SPF/DKIM being set up, or someone is spoofing the domain.\n\n" + "\n".join(lines)
        )
        noun = "sender" if n == 1 else "senders"
        issues.append(
            Issue(
                key=f"newsource:{domain}",
                kind="new_source",
                domain=domain,
                client=clients.get(domain),
                subject=f"DMARC alert: {_label(domain, clients)} - {n} new unauthorized {noun}",
                details=details,
            )
        )
    return issues


def _monitor_issues(db, settings, now) -> list[Issue]:
    status = db.get_status()
    who = settings.own_client_name
    issues = []
    try:
        failures = int(status.get("consecutive_failures") or 0)
    except ValueError:
        failures = 0
    if failures >= settings.alert_poll_failures:
        issues.append(
            Issue(
                key="monitor:poll",
                kind="monitor",
                client=who,
                subject=f"DMARC alert: {who} - DMARC monitor cannot read the report mailbox",
                details=(
                    f"The last {failures} mailbox checks failed. Last error:\n{status.get('last_error', '')}\n\n"
                    f"Mailbox: {settings.imap_user} on {settings.imap_host}\n"
                    f"Last successful check: {status.get('last_poll_ok', 'never')} UTC"
                ),
            )
        )
    if settings.imap_enabled:
        with db.connect() as conn:
            last = conn.execute("SELECT MAX(imported_at) AS t FROM reports").fetchone()["t"]
        last_dt = _parse(last)
        if last_dt and now - last_dt > timedelta(hours=settings.alert_stale_hours):
            hours = int((now - last_dt).total_seconds() // 3600)
            issues.append(
                Issue(
                    key="monitor:stale",
                    kind="monitor",
                    client=who,
                    subject=f"DMARC alert: {who} - DMARC monitor has received no reports in {hours} hours",
                    details=(
                        f"The newest report was stored at {last} UTC, more than {settings.alert_stale_hours} hours ago. "
                        "Reports normally arrive daily, so the report mailbox may not be receiving mail, "
                        "or the monitor may not be reading it.\n\n"
                        f"Mailbox: {settings.imap_user} on {settings.imap_host}\n"
                        f"Last successful check: {status.get('last_poll_ok', 'never')} UTC\n"
                        f"Last check result: {status.get('last_poll_message', '')}"
                    ),
                )
            )
    return issues


def _failing_sources(db, domain, since) -> list:
    with db.connect() as conn:
        return conn.execute(
            """SELECT COALESCE(NULLIF(c.source_base_domain, ''), c.source_ip) AS source,
                      GROUP_CONCAT(DISTINCT c.source_ip) AS ips,
                      SUM(c.count) AS messages,
                      SUM(CASE WHEN c.dmarc_aligned THEN 0 ELSE c.count END) AS failed,
                      SUM(CASE WHEN c.spf_aligned THEN 0 ELSE c.count END) AS spf_fail,
                      SUM(CASE WHEN c.dkim_aligned THEN 0 ELSE c.count END) AS dkim_fail
               FROM reports r JOIN records c ON c.report_pk = r.id
               WHERE r.domain = ? AND r.begin_date >= ?
               GROUP BY source HAVING failed > 0 ORDER BY failed DESC""",
            (domain, since),
        ).fetchall()


def _source_lines(sources) -> str:
    if not sources:
        return "- none\n"
    return "".join(
        f"- {s['source']}: {s['failed']:,} of {s['messages']:,} failed "
        f"(SPF not aligned {s['spf_fail']:,}, DKIM not aligned {s['dkim_fail']:,}); IPs {s['ips']}\n"
        for s in sources
    )


# ---- alerting with dedupe ------------------------------------------------------


def process_alerts(
    db: Database, settings: Settings, now: datetime | None = None, sender: Sender | None = None
) -> dict[str, int]:
    """Find current problems and email each new one. Safe to call after every poll."""
    counts = {"sent": 0, "reminded": 0, "resolved": 0, "ongoing": 0}
    if not (settings.email_enabled and settings.alert_to):
        return counts
    now = now or _now()
    sender = send_email if sender is None else sender
    issues = {i.key: i for i in find_issues(db, settings, now)}
    with db.connect() as conn:
        rows = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM alerts")}

    footer = (
        "\n\n--\nSent by the DMARC monitor. One email is sent when a problem starts; "
        "it will not email again for the same problem unless it is still happening "
        f"after {settings.alert_repeat_days} days." if settings.alert_repeat_days else
        "\n\n--\nSent by the DMARC monitor. One email is sent when a problem starts."
    )
    for key, issue in issues.items():
        row = rows.get(key)
        is_open = row is not None and row["resolved_at"] is None
        if is_open:
            last_sent = _parse(row["last_sent"])
            due = (
                settings.alert_repeat_days > 0
                and (last_sent is None or now - last_sent >= timedelta(days=settings.alert_repeat_days))
            )
            subject = issue.subject
            if due and last_sent is not None:
                subject += f" (still happening since {row['first_seen'][:10]})"
            sent = False
            if due:
                sent = _try_send(db, settings, sender, issue, subject, footer)
                counts["reminded" if last_sent else "sent"] += int(sent)
            else:
                counts["ongoing"] += 1
            with db.connect() as conn:
                conn.execute(
                    "UPDATE alerts SET last_seen=?, subject=?, details=?, last_sent=COALESCE(?, last_sent) WHERE key=?",
                    (_ts(now), issue.subject, issue.details, _ts(now) if sent else None, key),
                )
        else:
            sent = _try_send(db, settings, sender, issue, issue.subject, footer)
            counts["sent"] += int(sent)
            with db.connect() as conn:
                conn.execute(
                    """INSERT INTO alerts (key, kind, domain, client, subject, details, first_seen, last_seen, last_sent, resolved_at)
                       VALUES (?,?,?,?,?,?,?,?,?,NULL)
                       ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, domain=excluded.domain,
                         client=excluded.client, subject=excluded.subject, details=excluded.details,
                         first_seen=excluded.first_seen, last_seen=excluded.last_seen,
                         last_sent=excluded.last_sent, resolved_at=NULL""",
                    (key, issue.kind, issue.domain, issue.client, issue.subject, issue.details,
                     _ts(now), _ts(now), _ts(now) if sent else None),
                )

    for key, row in rows.items():
        if key in issues or row["resolved_at"] is not None:
            continue
        last_seen = _parse(row["last_seen"])
        if last_seen and now - last_seen >= RESOLVE_AFTER:
            with db.connect() as conn:
                conn.execute("UPDATE alerts SET resolved_at=? WHERE key=?", (_ts(now), key))
            counts["resolved"] += 1
    return counts


def _try_send(db, settings, sender, issue: Issue, subject: str, footer: str) -> bool:
    try:
        sender(settings, settings.alert_to, subject, issue.details + footer, None)
        db.set_status("last_email_error", "")
        log.info("Alert sent: %s", subject)
        return True
    except Exception as exc:  # retried on the next poll, because last_sent stays empty
        log.warning("Could not send alert %r: %s", subject, exc)
        db.set_status("last_email_error", f"{_ts(_now())} UTC: {type(exc).__name__}: {exc}")
        return False


def open_alerts(db: Database) -> list[dict]:
    with db.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY first_seen DESC"
            )
        ]


# ---- weekly summary -------------------------------------------------------------


def summary_slot(settings: Settings, now: datetime) -> datetime:
    """The most recent scheduled send time at or before now, in UTC."""
    tz = ZoneInfo(settings.timezone)
    local = now.astimezone(tz)
    days_back = (local.weekday() - settings.summary_weekday) % 7
    slot = (local - timedelta(days=days_back)).replace(hour=settings.summary_hour, minute=0, second=0, microsecond=0)
    if slot > local:
        slot -= timedelta(days=7)
    return slot.astimezone(timezone.utc)


def maybe_send_summary(
    db: Database, settings: Settings, now: datetime | None = None, sender: Sender | None = None
) -> bool:
    """Send the weekly summary once per scheduled slot. Call it after every poll."""
    if not (settings.email_enabled and settings.summary_to):
        return False
    now = now or _now()
    slot = summary_slot(settings, now)
    if now - slot > timedelta(days=1):  # missed by more than a day (container was down): wait for next week
        return False
    if db.get_status().get("last_summary_slot") == _ts(slot):
        return False
    if send_summary(db, settings, now, sender):
        db.set_status("last_summary_slot", _ts(slot))
        return True
    return False


def send_summary(db: Database, settings: Settings, now: datetime | None = None, sender: Sender | None = None) -> bool:
    now = now or _now()
    sender = send_email if sender is None else sender
    subject, text, html_body = build_summary(db, settings, now)
    try:
        sender(settings, settings.summary_to, subject, text, html_body)
        db.set_status("last_summary_sent", _ts(now))
        db.set_status("last_email_error", "")
        log.info("Weekly summary sent to %s", ", ".join(settings.summary_to))
        return True
    except Exception as exc:
        log.warning("Could not send weekly summary: %s", exc)
        db.set_status("last_email_error", f"{_ts(_now())} UTC: {type(exc).__name__}: {exc}")
        return False


def _week_rates(db: Database, start: datetime, end: datetime) -> dict[str, dict]:
    with db.connect() as conn:
        return {
            r["domain"]: dict(r)
            for r in conn.execute(
                """SELECT r.domain, SUM(c.count) AS messages,
                          SUM(CASE WHEN c.dmarc_aligned THEN c.count ELSE 0 END) AS passed
                   FROM reports r JOIN records c ON c.report_pk = r.id
                   WHERE r.begin_date >= ? AND r.begin_date < ? GROUP BY r.domain""",
                (_ts(start), _ts(end)),
            )
        }


def build_summary(db: Database, settings: Settings, now: datetime) -> tuple[str, str, str]:
    clients = load_clients(settings.domains_file)
    rows = db.domain_summary(7, clients)
    prior = _week_rates(db, now - timedelta(days=14), now - timedelta(days=7))
    for d in rows:
        p = prior.get(d["domain"])
        d["prior_rate"] = (100.0 * p["passed"] / p["messages"]) if p and p["messages"] else None
    rows.sort(key=lambda d: ((d["client"] or "~").lower(), d["domain"]))

    week_start = _ts(now - timedelta(days=7))
    with db.connect() as conn:
        resolved = [dict(r) for r in conn.execute(
            "SELECT * FROM alerts WHERE resolved_at >= ? ORDER BY resolved_at", (week_start,))]
    still_open = open_alerts(db)
    status = db.get_status()

    total = sum(d["messages"] or 0 for d in rows)
    failed = sum(d["failed"] or 0 for d in rows)
    silent = [d for d in rows if not d["messages"]]
    local_date = now.astimezone(ZoneInfo(settings.timezone)).strftime("%b %d, %Y")
    subject = f"DMARC weekly summary - week ending {local_date}"

    def rate(v):
        return f"{v:.1f}%" if v is not None else "no data"

    def change(d):
        if d["pass_rate"] is None or d["prior_rate"] is None:
            return ""
        diff = d["pass_rate"] - d["prior_rate"]
        return "same" if abs(diff) < 0.05 else f"{diff:+.1f}"

    lines = [
        f"DMARC summary for the 7 days ending {local_date}.",
        "",
        f"{len(rows) - len(silent)} domains reported {total:,} messages; {failed:,} failed DMARC.",
        f"Open alerts: {len(still_open)}. Resolved this week: {len(resolved)}.",
        "",
    ]
    if still_open:
        lines.append("Open alerts:")
        lines += [f"- {a['subject']} (since {a['first_seen'][:10]})" for a in still_open]
        lines.append("")
    if resolved:
        lines.append("Resolved this week:")
        lines += [f"- {a['subject']}" for a in resolved]
        lines.append("")
    if silent:
        lines.append("No reports this week (check the DMARC record's rua address):")
        lines += [f"- {_label(d['domain'], clients)}" for d in silent]
        lines.append("")
    lines.append("By domain (pass rate this week, change from last week, messages, failed, policy):")
    for d in rows:
        if d["messages"]:
            lines.append(
                f"- {_label(d['domain'], clients)}: {rate(d['pass_rate'])} {change(d)}, "
                f"{d['messages']:,} messages, {d['failed']:,} failed, p={d['policy'] or '?'}"
            )
    lines += [
        "",
        f"Monitor: last successful mailbox check {status.get('last_poll_ok', 'never')} UTC.",
    ]
    if status.get("last_error"):
        lines.append(f"Last error: {status['last_error']}")
    text = "\n".join(lines)

    e = html.escape
    def tr(cells, tag="td"):
        return "<tr>" + "".join(f'<{tag} style="padding:4px 8px;border-bottom:1px solid #ddd;text-align:left">{c}</{tag}>' for c in cells) + "</tr>"

    table = [tr(["Client", "Domain", "Policy", "Messages", "Pass rate", "vs last week", "Failed", "Latest report"], "th")]
    for d in rows:
        colour = "#1a7f37" if d["pass_rate"] == 100 else ("#b42318" if d["pass_rate"] is not None and d["pass_rate"] < settings.alert_min_pass_rate else "#9a6700")
        table.append(tr([
            e(d["client"] or ""), e(d["domain"]), e(d["policy"] or ""), f"{d['messages'] or 0:,}",
            f'<span style="color:{colour}">{rate(d["pass_rate"])}</span>' if d["pass_rate"] is not None else '<span style="color:#888">no data</span>',
            e(change(d)), f"{d['failed'] or 0:,}", e((d["last_report"] or "")[:10]),
        ]))

    def ul(items):
        return "<ul>" + "".join(f"<li>{e(i)}</li>" for i in items) + "</ul>"

    parts = [
        f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">',
        f"<h2 style=\"margin:0 0 8px\">DMARC summary, week ending {e(local_date)}</h2>",
        f"<p>{len(rows) - len(silent)} domains reported {total:,} messages; {failed:,} failed DMARC. "
        f"Open alerts: {len(still_open)}. Resolved this week: {len(resolved)}.</p>",
    ]
    if still_open:
        parts += ["<h3>Open alerts</h3>", ul(f"{a['subject']} (since {a['first_seen'][:10]})" for a in still_open)]
    if resolved:
        parts += ["<h3>Resolved this week</h3>", ul(a["subject"] for a in resolved)]
    if silent:
        parts += ["<h3>No reports this week</h3>", ul(_label(d["domain"], clients) for d in silent)]
    parts += [
        '<h3>By domain</h3><table style="border-collapse:collapse">', *table, "</table>",
        f'<p style="color:#666">Monitor: last successful mailbox check {e(status.get("last_poll_ok", "never"))} UTC.'
        + (f"<br>Last error: {e(status['last_error'])}" if status.get("last_error") else "") + "</p></div>",
    ]
    return subject, text, "".join(parts)


def describe_schedule(settings: Settings) -> str:
    day = WEEKDAYS[settings.summary_weekday].capitalize()
    return f"{day} at {settings.summary_hour:02d}:00 ({settings.timezone})"
