"""The dashboard."""

from __future__ import annotations

import hmac
import secrets
from functools import wraps

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

from .config import Settings
from .db import Database, load_clients
from .ingest import import_report_bytes

PERIODS = [7, 30, 90, 365]
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def create_app(settings: Settings, db: Database, poller=None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("user"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapper

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    def check_csrf() -> None:
        sent = request.form.get("csrf", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Form expired. Go back, reload the page and try again.")

    def period() -> int:
        try:
            days = int(request.args.get("days", 30))
        except ValueError:
            days = 30
        return days if days in PERIODS else 30

    @app.context_processor
    def inject():
        return {"csrf_token": csrf_token, "periods": PERIODS}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            check_csrf()
            user_ok = hmac.compare_digest(request.form.get("username", ""), settings.admin_user)
            pw_ok = settings.admin_password is not None and hmac.compare_digest(
                request.form.get("password", ""), settings.admin_password
            )
            if user_ok and pw_ok:
                session.clear()
                session["user"] = settings.admin_user
                target = request.args.get("next", "")
                return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("index"))
            flash("Wrong username or password.")
        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout():
        check_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def index():
        days = period()
        clients = load_clients(settings.domains_file)
        return render_template(
            "index.html",
            days=days,
            domains=db.domain_summary(days, clients),
            status=db.get_status(),
            totals=db.totals(),
            settings=settings,
        )

    @app.get("/domain/<domain>")
    @login_required
    def domain(domain: str):
        days = period()
        domain = domain.lower()
        clients = load_clients(settings.domains_file)
        return render_template(
            "domain.html",
            days=days,
            domain=domain,
            client=clients.get(domain, ""),
            sources=db.domain_sources(domain, days),
            reports=db.domain_reports(domain, days),
        )

    @app.post("/upload")
    @login_required
    def upload():
        check_csrf()
        files = [f for f in request.files.getlist("reports") if f.filename]
        if not files:
            flash("Choose at least one report file.")
            return redirect(url_for("index"))
        saved = duplicate = failed = 0
        for f in files:
            try:
                result = import_report_bytes(db, f.read(), settings.offline)
                saved += result["saved"]
                duplicate += result["duplicate"]
            except Exception:
                failed += 1
        flash(f"Imported {saved} new report(s). {duplicate} already stored. {failed} could not be read.")
        return redirect(url_for("index"))

    @app.post("/poll")
    @login_required
    def poll():
        check_csrf()
        if poller is not None:
            poller.poll_now()
            flash("Checking the mailbox now. Refresh in a minute to see new reports.")
        return redirect(url_for("index"))

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
