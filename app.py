
from __future__ import annotations

import calendar
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import smtplib
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import bcrypt
import click
import psycopg
from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, url_for)
from flask_wtf.csrf import CSRFError, CSRFProtect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from werkzeug.exceptions import HTTPException

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ════════════════════════════════════════════════════════════════════════════
# App & database
# ════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or "dev-only-change-me",
    PREFERRED_URL_SCHEME="https",
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    WTF_CSRF_TIME_LIMIT=None,
    WTF_CSRF_HEADERS=["X-CSRFToken"],
)
csrf = CSRFProtect(app)
IS_PROD = os.environ.get("FLASK_ENV") == "production" or bool(os.environ.get("VERCEL"))
APP_URL = os.environ.get("APP_URL") or (
    f"https://{os.environ['VERCEL_PROJECT_PRODUCTION_URL']}" if os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") else "http://127.0.0.1:5000")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or BASE_DIR / "uploads")
MAX_UPLOAD = (4 if os.environ.get("VERCEL") else 10) * 1024 * 1024
ALLOWED_MIME = re.compile(r"^(image/(png|jpe?g|gif|webp)|application/pdf|text/(plain|csv)|application/(msword|vnd\.openxmlformats-officedocument\.(wordprocessingml\.document|spreadsheetml\.sheet|presentationml\.presentation)|vnd\.ms-excel|zip))$")


def _dsn(url: str) -> str:
    """Drop Prisma-style ?schema= which libpq doesn't understand."""
    u = urlparse(url)
    query = "&".join(p for p in u.query.split("&") if p and not p.startswith("schema="))
    return u._replace(query=query).geturl()


pool = ConnectionPool(
    _dsn(os.environ.get("DATABASE_URL", "postgresql://orbit:orbit@localhost:5433/orbit")),
    min_size=0, max_size=int(os.environ.get("DB_POOL_SIZE", "3" if os.environ.get("VERCEL") else "10")), open=False,
    timeout=8, max_idle=30,
    # prepare_threshold=None: required when DATABASE_URL is a pgbouncer/pooled (Neon) URL
    kwargs={"row_factory": dict_row, "autocommit": True, "options": "-c timezone=UTC", "prepare_threshold": None},
)


def conn() -> psycopg.Connection:
    if "conn" not in g:
        if pool.closed:
            pool.open()
        g.conn = pool.getconn()
    return g.conn


@app.teardown_appcontext
def _release(_exc):
    c = g.pop("conn", None)
    if c is not None:
        pool.putconn(c)


def q(sql, *args):
    return conn().execute(sql, args).fetchall()


def q1(sql, *args):
    return conn().execute(sql, args).fetchone()


def run(sql, *args):
    return conn().execute(sql, args).rowcount


def tx():
    return conn().transaction()


def new_id() -> str:
    return "c" + secrets.token_hex(12)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


HAS_UPDATED = {"Plan", "Business", "User", "Role", "BusinessMember", "Module", "ModuleField", "CustomRecord", "Attendance", "Schedule"}
NO_ID = {"RolePermission", "BusinessModule", "ScheduleAssignee"}


def insert(table: str, **cols):
    if table not in NO_ID:
        cols.setdefault("id", new_id())
    if table in HAS_UPDATED:
        cols.setdefault("updatedAt", now())
    keys = list(cols)
    sql = 'INSERT INTO "%s" (%s) VALUES (%s) RETURNING *' % (
        table, ", ".join('"%s"' % k for k in keys), ", ".join(["%s"] * len(keys)))
    return q1(sql, *cols.values())


def update(table: str, row_id: str, **cols):
    if table in HAS_UPDATED:
        cols["updatedAt"] = now()
    sets = ", ".join('"%s" = %%s' % k for k in cols)
    return q1('UPDATE "%s" SET %s WHERE id = %%s RETURNING *' % (table, sets), *cols.values(), row_id)


# ════════════════════════════════════════════════════════════════════════════
# Errors
# ════════════════════════════════════════════════════════════════════════════

class UserError(Exception):
    """A validation / business-rule error shown to the user (flash on pages, JSON on /api)."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def is_api() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


@app.errorhandler(UserError)
def _user_error(e: UserError):
    if is_api():
        return jsonify(error=str(e)), e.code
    flash(str(e), "error")
    return redirect(request.referrer or url_for("home"))


@app.errorhandler(psycopg.errors.UniqueViolation)
def _unique(_e):
    return _user_error(UserError("A record with these details already exists", 409))


@app.errorhandler(psycopg.errors.ForeignKeyViolation)
def _fk(_e):
    return _user_error(UserError("This item is still in use and can't be removed", 409))


@app.errorhandler(CSRFError)
def _csrf(_e):
    return _user_error(UserError("Your form expired. Please try again.", 400))


@app.errorhandler(Exception)
def _unhandled(e: Exception):
    if isinstance(e, HTTPException):
        return _http(e)
    app.logger.exception("Unhandled error on %s %s", request.method, request.path)
    if is_api():
        return jsonify(error="Something went wrong on our side."), 500
    return render_template("error.html", code=500, message="Something went wrong on our side. Please try again."), 500


@app.errorhandler(HTTPException)
def _http(e: HTTPException):
    if is_api():
        return jsonify(error=e.description), e.code
    return render_template("error.html", code=e.code, message=e.description), e.code


@app.after_request
def _headers(resp: Response):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


# ════════════════════════════════════════════════════════════════════════════
# Catalog (seed defaults — at runtime everything is read from the DB)
# ════════════════════════════════════════════════════════════════════════════

ACTIONS = ["VIEW", "CREATE", "UPDATE", "DELETE", "APPROVE", "EXPORT"]
CUSTOM_ACTIONS = ["VIEW", "CREATE", "UPDATE", "DELETE", "EXPORT"]
FIELD_TYPES = ["TEXT", "TEXTAREA", "NUMBER", "DATE", "TIME", "DATETIME", "DROPDOWN", "CHECKBOX", "FILE", "EMAIL", "PHONE"]
FIELD_LABELS = {"TEXT": "Text", "TEXTAREA": "Long text", "NUMBER": "Number", "DATE": "Date", "TIME": "Time", "DATETIME": "Date & time",
                "DROPDOWN": "Dropdown", "CHECKBOX": "Checkbox", "FILE": "File upload", "EMAIL": "Email", "PHONE": "Phone"}
LEAVE_TYPES = ["Casual", "Sick", "Earned", "Unpaid", "Other"]
EVENT_CATEGORIES = [("Meeting", "#6366f1"), ("Shift", "#0ea5e9"), ("Training", "#10b981"), ("Deadline", "#ef4444"), ("Holiday", "#f59e0b"), ("Other", "#64748b")]
ICONS = ["Box", "ClipboardList", "DoorOpen", "CheckSquare", "Package", "FileBarChart", "FolderKanban", "FileText", "Users", "Clock", "Plane",
         "CalendarDays", "History", "Truck", "Wrench", "ShoppingCart", "Receipt", "Briefcase", "Building2", "Car", "HeartPulse", "GraduationCap", "MessageSquare", "Star"]

BUILTIN_MODULES = [
    ("employees", "Employee Management", "Team members, roles and departments", "Users", 10, ["VIEW", "CREATE", "UPDATE", "DELETE", "EXPORT"]),
    ("attendance", "Attendance Management", "Check-in/out, reports and corrections", "Clock", 20, ["VIEW", "CREATE", "UPDATE", "APPROVE", "EXPORT"]),
    ("leave", "Leave Management", "Leave requests and approvals", "Plane", 30, ["VIEW", "CREATE", "APPROVE", "EXPORT"]),
    ("scheduling", "Schedule Management", "Calendar, events and team schedules", "CalendarDays", 40, ["VIEW", "CREATE", "UPDATE", "DELETE"]),
    ("activity_logs", "Daily Activity Logs", "Immutable audit trail of actions", "History", 50, ["VIEW", "EXPORT"]),
]
BUILTIN_PAGES = {"employees": "employees", "attendance": "attendance", "leave": "leave", "scheduling": "schedule", "activity_logs": "tenant_logs"}

ROLE_TEMPLATES = [
    ("owner", "Business Owner", "Full control of the business workspace", True, 100),
    ("admin", "Business Admin", "Manages people and day-to-day operations", False, 80),
    ("manager", "Manager", "Manages a team, approves requests", False, 50),
    ("employee", "Employee", "Standard member access", False, 10),
]
DEFAULT_GRANTS = {
    "admin": {
        "employees": {"VIEW": "ALL", "CREATE": "ALL", "UPDATE": "ALL", "DELETE": "ALL", "EXPORT": "ALL"},
        "attendance": {"VIEW": "ALL", "CREATE": "OWN", "UPDATE": "ALL", "APPROVE": "ALL", "EXPORT": "ALL"},
        "leave": {"VIEW": "ALL", "CREATE": "OWN", "APPROVE": "ALL", "EXPORT": "ALL"},
        "scheduling": {"VIEW": "ALL", "CREATE": "ALL", "UPDATE": "ALL", "DELETE": "ALL"},
        "activity_logs": {"VIEW": "ALL", "EXPORT": "ALL"},
        "*custom": {"VIEW": "ALL", "CREATE": "ALL", "UPDATE": "ALL", "DELETE": "ALL", "EXPORT": "ALL"},
    },
    "manager": {
        "employees": {"VIEW": "ALL", "CREATE": "ALL", "UPDATE": "ALL"},
        "attendance": {"VIEW": "ALL", "CREATE": "OWN", "APPROVE": "ALL", "EXPORT": "ALL"},
        "leave": {"VIEW": "ALL", "CREATE": "OWN", "APPROVE": "ALL"},
        "scheduling": {"VIEW": "ALL", "CREATE": "ALL", "UPDATE": "ALL", "DELETE": "ALL"},
        "activity_logs": {"VIEW": "ALL"},
        "*custom": {"VIEW": "ALL", "CREATE": "ALL", "UPDATE": "ALL", "EXPORT": "ALL"},
    },
    "employee": {
        "employees": {"VIEW": "ALL"},
        "attendance": {"VIEW": "OWN", "CREATE": "OWN"},
        "leave": {"VIEW": "OWN", "CREATE": "OWN"},
        "scheduling": {"VIEW": "OWN"},
        "activity_logs": {"VIEW": "OWN"},
        "*custom": {"VIEW": "OWN", "CREATE": "OWN", "UPDATE": "OWN"},
    },
}

CUSTOM_TEMPLATES = [
    ("daily_work_report", "Daily Work Report", "What each person worked on today", "ClipboardList", [
        ("employee_name", "Employee Name", "TEXT", True, []), ("date", "Date", "DATE", True, []),
        ("work_description", "Work Description", "TEXTAREA", True, []), ("start_time", "Start Time", "TIME", False, []),
        ("end_time", "End Time", "TIME", False, []), ("status", "Status", "DROPDOWN", True, ["Completed", "In Progress", "Blocked"]),
        ("attachments", "Attachments", "FILE", False, [])]),
    ("visitor_entry", "Visitor Entry", "Front-desk visitor register", "DoorOpen", [
        ("visitor_name", "Visitor Name", "TEXT", True, []), ("contact_number", "Contact Number", "PHONE", True, []),
        ("purpose", "Purpose", "TEXT", True, []), ("entry_time", "Entry Time", "DATETIME", True, []), ("exit_time", "Exit Time", "DATETIME", False, [])]),
    ("tasks", "Task Management", "Track tasks and owners", "CheckSquare", [
        ("title", "Title", "TEXT", True, []), ("assignee", "Assignee", "TEXT", False, []), ("due_date", "Due Date", "DATE", False, []),
        ("priority", "Priority", "DROPDOWN", False, ["Low", "Medium", "High"]), ("status", "Status", "DROPDOWN", True, ["Pending", "In Progress", "Done"]),
        ("notes", "Notes", "TEXTAREA", False, [])]),
    ("inventory", "Inventory Management", "Stock items and quantities", "Package", [
        ("item", "Item", "TEXT", True, []), ("sku", "SKU", "TEXT", False, []), ("quantity", "Quantity", "NUMBER", True, []),
        ("unit_price", "Unit Price", "NUMBER", False, []), ("category", "Category", "DROPDOWN", False, ["Raw material", "Finished goods", "Consumables"]),
        ("in_stock", "In Stock", "CHECKBOX", False, [])]),
    ("work_reports", "Work Reports", "Periodic reports for management", "FileBarChart", [
        ("title", "Report Title", "TEXT", True, []), ("period", "Period", "DROPDOWN", True, ["Weekly", "Monthly", "Quarterly"]),
        ("summary", "Summary", "TEXTAREA", True, []), ("file", "Report File", "FILE", False, [])]),
    ("projects", "Project Management", "Projects, owners and milestones", "FolderKanban", [
        ("name", "Project Name", "TEXT", True, []), ("owner", "Owner", "TEXT", False, []), ("start_date", "Start Date", "DATE", False, []),
        ("end_date", "End Date", "DATE", False, []), ("stage", "Stage", "DROPDOWN", False, ["Planning", "Active", "On hold", "Done"])]),
    ("documents", "Document Management", "Store and share business documents", "FileText", [
        ("title", "Title", "TEXT", True, []), ("category", "Category", "DROPDOWN", False, ["Policy", "Contract", "Invoice", "Other"]),
        ("file", "File", "FILE", True, []), ("notes", "Notes", "TEXTAREA", False, [])]),
]
DEFAULT_MODULE_KEYS = ["attendance", "leave", "scheduling", "activity_logs", "tasks"]


# ════════════════════════════════════════════════════════════════════════════
# Time helpers (attendance days = calendar days in the business timezone)
# ════════════════════════════════════════════════════════════════════════════

def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def to_local(d: dt.datetime, tz: str) -> dt.datetime:
    return d.replace(tzinfo=dt.timezone.utc).astimezone(zone(tz))


def business_day(d: dt.datetime, tz: str) -> dt.date:
    return to_local(d, tz).date()


def local_minutes(d: dt.datetime, tz: str) -> int:
    l = to_local(d, tz)
    return l.hour * 60 + l.minute


def hhmm(s: str) -> int:
    h, m = (s or "0:0").split(":")[:2]
    return int(h) * 60 + int(m)


def zoned_to_utc(day: dt.date, minutes: int, tz: str) -> dt.datetime:
    local = dt.datetime.combine(day, dt.time(), tzinfo=zone(tz)) + dt.timedelta(minutes=minutes)
    return local.astimezone(dt.timezone.utc).replace(tzinfo=None)


def parse_local_dt(s: str, tz: str) -> dt.datetime:
    """'YYYY-MM-DDTHH:MM' wall clock in tz -> naive UTC."""
    d = dt.datetime.fromisoformat(s)
    return d.replace(tzinfo=zone(tz)).astimezone(dt.timezone.utc).replace(tzinfo=None)


def parse_day(s: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        raise UserError("Invalid date")


def each_day(a: dt.date, b: dt.date):
    while a <= b:
        yield a
        a += dt.timedelta(days=1)


def fmt_minutes(m: int) -> str:
    return f"{(m or 0) // 60}h {(m or 0) % 60:02d}m"


def occurrences(e: dict, start: dt.datetime, end: dt.datetime, limit: int = 500):
    """Expand a (possibly recurring) schedule into occurrences overlapping [start, end]."""
    s0, e0, rec = e["startAt"], e["endAt"], e["recurrence"]
    dur = e0 - s0
    if rec == "NONE":
        return [(s0, e0)] if e0 >= start and s0 <= end else []
    until = min(e["recurrenceUntil"], end) if e.get("recurrenceUntil") else end
    out, i = [], 0
    if rec in ("DAILY", "WEEKLY") and start > s0:
        step = 1 if rec == "DAILY" else 7
        i = max(0, ((start - dur - s0).days // step) - 1)
    while len(out) < limit and i < 5000:
        if rec == "DAILY":
            s = s0 + dt.timedelta(days=i)
        elif rec == "WEEKLY":
            s = s0 + dt.timedelta(weeks=i)
        else:
            y, m = divmod(s0.month - 1 + i, 12)
            if s0.day > calendar.monthrange(s0.year + y, m + 1)[1]:
                i += 1
                continue
            s = s0.replace(year=s0.year + y, month=m + 1)
        i += 1
        if s > until:
            break
        if s + dur >= start:
            out.append((s, s + dur))
    return out


# ════════════════════════════════════════════════════════════════════════════
# Auth: passwords, sessions, tokens, rate limiting, mail
# ════════════════════════════════════════════════════════════════════════════

SESSION_COOKIE = "orbit_session"
SESSION_DAYS = 30
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(12)).decode()


def verify_password(pw: str, h: str | None) -> bool:
    try:
        return bool(h) and bcrypt.checkpw(pw.encode(), h.encode())
    except ValueError:
        return False


def check_password_rules(pw: str):
    if len(pw) < 8 or len(pw) > 128 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        raise UserError("Password needs 8+ characters with at least one letter and one number", 422)


def clean_email(s: str | None) -> str:
    e = (s or "").strip().lower()
    if not EMAIL_RE.match(e) or len(e) > 200:
        raise UserError("Enter a valid email address", 422)
    return e


def clean_name(s: str | None, label="Name", min_len=2, max_len=100) -> str:
    v = (s or "").strip()
    if len(v) < min_len or len(v) > max_len:
        raise UserError(f"{label} must be {min_len}–{max_len} characters", 422)
    return v


def client_ip() -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0] or request.remote_addr or "local").strip()


_buckets: dict[str, list] = {}


def rate_limit(key: str, limit: int, window_s: int):
    """Fixed-window limiter (per process). Use Redis when running multiple instances."""
    t = now().timestamp()
    b = _buckets.get(key)
    if not b or b[1] < t:
        _buckets[key] = [1, t + window_s]
        return
    b[0] += 1
    if b[0] > limit:
        raise UserError("Too many attempts. Please wait a few minutes and try again.", 429)


def send_mail(to: str, subject: str, body: str):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print(f"\n[dev mail] to={to}\n  subject: {subject}\n  " + body.replace("\n", "\n  ") + "\n", flush=True)
        return
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ.get("MAIL_FROM", "Orbit <no-reply@orbit.local>"), to, subject
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    with (smtplib.SMTP_SSL(host, port) if port == 465 else smtplib.SMTP(host, port)) as s:
        if port != 465:
            s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)


def issue_token(user_id: str, kind: str, ttl_minutes: int) -> str:
    token = secrets.token_urlsafe(32)
    run('UPDATE "AuthToken" SET "usedAt" = %s WHERE "userId" = %s AND type = %s AND "usedAt" IS NULL', now(), user_id, kind)
    insert("AuthToken", tokenHash=sha256(token), userId=user_id, type=kind, expiresAt=now() + dt.timedelta(minutes=ttl_minutes))
    return token


def consume_token(token: str, kind: str) -> str | None:
    row = q1('SELECT * FROM "AuthToken" WHERE "tokenHash" = %s', sha256(token or ""))
    if not row or row["type"] != kind or row["usedAt"] or row["expiresAt"] < now():
        return None
    run('UPDATE "AuthToken" SET "usedAt" = %s WHERE id = %s', now(), row["id"])
    return row["userId"]


def send_verification(user: dict):
    token = issue_token(user["id"], "EMAIL_VERIFY", 60 * 24)
    send_mail(user["email"], "Verify your email", f"Hi {user['name']},\n\nConfirm your email address:\n{APP_URL}/verify-email?token={token}\n\nThis link expires in 24 hours.")


def start_session(resp: Response, user_id: str, business_id: str | None) -> Response:
    token = secrets.token_urlsafe(32)
    expires = now() + dt.timedelta(days=SESSION_DAYS)
    insert("Session", tokenHash=sha256(token), userId=user_id, businessId=business_id, ip=client_ip(),
           userAgent=(request.headers.get("user-agent") or "")[:300], expiresAt=expires)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Lax", secure=IS_PROD, path="/",
                    expires=expires.replace(tzinfo=dt.timezone.utc))
    return resp


def current_session():
    """Session row joined with its user, memoised per request. None if missing/expired/inactive."""
    if "sess" in g:
        return g.sess
    g.sess = None
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        s = q1('SELECT * FROM "Session" WHERE "tokenHash" = %s', sha256(token))
        if s and s["expiresAt"] > now():
            u = q1('SELECT * FROM "User" WHERE id = %s', s["userId"])
            if u and u["isActive"]:
                if (now() - s["lastSeenAt"]).total_seconds() > 3600:  # sliding expiry, at most one write/hour
                    run('UPDATE "Session" SET "lastSeenAt" = %s, "expiresAt" = %s WHERE id = %s', now(), now() + dt.timedelta(days=SESSION_DAYS), s["id"])
                s["user"] = u
                g.sess = s
    return g.sess


def default_business(user_id: str, preferred: str | None = None) -> str | None:
    rows = q('''SELECT m."businessId" FROM "BusinessMember" m JOIN "Business" b ON b.id = m."businessId"
                WHERE m."userId" = %s AND m."isActive" AND b.status = 'ACTIVE' ORDER BY m."joinedAt"''', user_id)
    ids = [r["businessId"] for r in rows]
    if preferred in ids:
        return preferred
    return ids[0] if ids else None


# ════════════════════════════════════════════════════════════════════════════
# Audit log & notifications
# ════════════════════════════════════════════════════════════════════════════

def audit(action: str, module: str, description: str, *, user=None, business=None, entity_type=None, entity_id=None, metadata=None):
    try:
        if user is None and "sess" in g and g.sess:
            user = g.sess["user"]
        if business is None and "ctx" in g:
            business = g.ctx.business
        insert("ActivityLog", businessId=business and business["id"], businessName=business and business["name"],
               userId=user and user["id"], userName=user and user["name"], userEmail=user and user["email"],
               action=action, module=module, entityType=entity_type, entityId=entity_id, description=description,
               metadata=Jsonb(metadata) if metadata is not None else None, ip=client_ip())
    except Exception as exc:  # never break the user's action because of logging, but make it loud
        print("[audit] failed:", exc, flush=True)


def notify(user_ids, kind: str, title: str, body: str | None = None, link: str | None = None, business_id: str | None = None):
    for uid in {u for u in user_ids if u}:
        insert("Notification", userId=uid, businessId=business_id, type=kind, title=title, body=body, link=link)


def users_with_permission(business_id: str, module_key: str, action: str):
    rows = q('''SELECT DISTINCT m."userId" FROM "BusinessMember" m JOIN "Role" r ON r.id = m."roleId"
                WHERE m."businessId" = %s AND m."isActive" AND (r."fullAccess" OR EXISTS (
                  SELECT 1 FROM "RolePermission" rp JOIN "Permission" p ON p.id = rp."permissionId" JOIN "Module" mo ON mo.id = p."moduleId"
                  WHERE rp."roleId" = r.id AND mo.key = %s AND p.action = %s))''', business_id, module_key, action)
    return [r["userId"] for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# Tenant context & access decorators
# ════════════════════════════════════════════════════════════════════════════

class Ctx:
    """Everything the current request may do inside the active workspace."""

    def __init__(self, user, session, business, member, role, modules, grants):
        self.user, self.session, self.business, self.member, self.role = user, session, business, member, role
        self.all_modules = modules
        self.enabled = {m["key"] for m in modules}
        self.grants = grants
        self.is_super = user["isSuperAdmin"]
        self.full = self.is_super or bool(role and role["fullAccess"])
        self.modules = [m for m in modules if self.full or f'{m["key"]}:VIEW' in grants]
        self.department = member and member["department"]
        self.rank = 1000 if self.full else (role["rank"] if role else 0)

    def has(self, key):
        return key in self.enabled

    def can(self, key, action):
        if key not in self.enabled:
            return None
        return "ALL" if self.full else self.grants.get(f"{key}:{action}")

    def module(self, key):
        return next((m for m in self.all_modules if m["key"] == key), None)

    def name(self, key, fallback=""):
        m = self.module(key)
        return m["name"] if m else fallback


def business_modules(business_id: str):
    rows = q('''SELECT m.*, bm."customName", bm."customDescription" FROM "Module" m
                LEFT JOIN "BusinessModule" bm ON bm."moduleId" = m.id AND bm."businessId" = %s
                WHERE m."isEnabled" AND (m."isMandatory" OR bm."isEnabled") ORDER BY m."sortOrder", m.name''', business_id)
    for r in rows:
        r["name"] = r["customName"] or r["name"]
        r["description"] = r["customDescription"] or r["description"]
    return rows


def load_ctx() -> Ctx:
    if "ctx" in g:
        return g.ctx
    s = current_session()
    if not s:
        abort(redirect(url_for("login", next=request.full_path))) if not is_api() else abort(401, "Authentication required")
    if not s["businessId"]:
        abort(redirect(url_for("admin_home" if s["user"]["isSuperAdmin"] else "welcome"))) if not is_api() else abort(403, "No active workspace")
    user = s["user"]
    business = q1('SELECT * FROM "Business" WHERE id = %s', s["businessId"])
    if not business:
        abort(404, "Workspace not found")
    member = q1('SELECT * FROM "BusinessMember" WHERE "businessId" = %s AND "userId" = %s', business["id"], user["id"])
    if not user["isSuperAdmin"]:
        if not member or not member["isActive"]:
            abort(403, "You are not a member of this workspace")
        if business["status"] != "ACTIVE":
            abort(403, "This workspace is suspended. Contact the platform administrator.")
    role = q1('SELECT * FROM "Role" WHERE id = %s', member["roleId"]) if member else None
    grants = {}
    if role:
        for r in q('''SELECT mo.key, p.action, rp.scope FROM "RolePermission" rp JOIN "Permission" p ON p.id = rp."permissionId"
                      JOIN "Module" mo ON mo.id = p."moduleId" WHERE rp."roleId" = %s''', role["id"]):
            k = f'{r["key"]}:{r["action"]}'
            if r["scope"] == "ALL" or k not in grants:
                grants[k] = r["scope"]
    g.ctx = Ctx(user, s, business, member, role, business_modules(business["id"]), grants)
    g.tz = business["timezone"]
    return g.ctx


def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not current_session():
            if is_api():
                abort(401, "Authentication required")
            return redirect(url_for("login", next=request.full_path))
        return f(*a, **kw)
    return w


def admin_required(f):
    @wraps(f)
    def w(*a, **kw):
        s = current_session()
        if not s:
            return redirect(url_for("login", next=request.full_path))
        if not s["user"]["isSuperAdmin"]:
            abort(404)
        return f(*a, **kw)
    return w


def tenant(module: str | None = None, action: str | None = None):
    """Auth -> tenant -> module access -> permission. Puts the scope in g.scope."""
    def deco(f):
        @wraps(f)
        def w(*a, **kw):
            ctx = load_ctx()
            g.scope = "ALL"
            if module:
                if not ctx.has(module):
                    abort(404 if not is_api() else 403, "This feature is not enabled for your workspace")
                g.scope = ctx.can(module, action or "VIEW")
                if not g.scope:
                    abort(404 if not is_api() else 403, "You don't have permission to do that")
            return f(*a, **kw)
        return w
    return deco


def require(key: str, action: str) -> str:
    ctx = load_ctx()
    scope = ctx.can(key, action)
    if not scope:
        raise UserError("You don't have permission to do that", 403)
    return scope


# ════════════════════════════════════════════════════════════════════════════
# Template context
# ════════════════════════════════════════════════════════════════════════════

ADMIN_NAV = [
    ("admin_home", "Overview", "LayoutDashboard", ""), ("admin_businesses", "Businesses", "Building2", "Platform"),
    ("admin_users", "Users", "Users", "Platform"), ("admin_modules", "Properties & modules", "Box", "Configuration"),
    ("admin_roles", "Roles & permissions", "Shield", "Configuration"), ("admin_plans", "Plans", "Receipt", "Configuration"),
    ("admin_logs", "Audit logs", "History", "Monitoring"), ("admin_announcements", "Announcements", "MessageSquare", "Monitoring"),
]


def tenant_nav(ctx: Ctx):
    items = [{"href": url_for("dashboard"), "label": "Dashboard", "icon": "LayoutDashboard", "section": ""}]
    for m in ctx.modules:
        if m["kind"] == "BUILTIN" and m["key"] in BUILTIN_PAGES:
            items.append({"href": url_for(BUILTIN_PAGES[m["key"]]), "label": m["name"], "icon": m["icon"], "section": "Workspace"})
    for m in ctx.modules:
        if m["kind"] == "CUSTOM":
            items.append({"href": url_for("records", key=m["key"]), "label": m["name"], "icon": m["icon"], "section": "Modules"})
    if ctx.full:
        items.append({"href": url_for("settings"), "label": "Business settings", "icon": "Settings", "section": "Manage"})
    if ctx.is_super:
        items.append({"href": url_for("admin_home"), "label": "Platform admin", "icon": "Shield", "section": "Manage"})
    return items


@app.context_processor
def _inject():
    s = current_session() if request.endpoint not in (None, "static") else None
    shell = None
    if s:
        user = s["user"]
        ctx = g.get("ctx")
        if ctx is None and s["businessId"] and not request.path.startswith("/admin"):
            try:
                ctx = load_ctx()
            except HTTPException:
                ctx = None
        if request.path.startswith("/admin") or (ctx is None and user["isSuperAdmin"]):
            nav = [{"href": url_for(e), "label": l, "icon": i, "section": sec} for e, l, i, sec in ADMIN_NAV]
            ws = None
        elif ctx:
            nav, ws = tenant_nav(ctx), ctx.business
        else:
            nav, ws = [], None
        if user["isSuperAdmin"]:
            workspaces = q('SELECT id, name FROM "Business" ORDER BY name LIMIT 200')
        else:
            workspaces = q('''SELECT b.id, b.name FROM "BusinessMember" m JOIN "Business" b ON b.id = m."businessId"
                              WHERE m."userId" = %s AND m."isActive" AND b.status = 'ACTIVE' ORDER BY b.name''', user["id"])
        shell = {"user": user, "nav": nav, "workspace": ws, "workspaces": workspaces,
                 "role": (ctx.role["name"] if ctx and ctx.role else ("Super Admin" if user["isSuperAdmin"] else None)),
                 "is_admin_area": request.path.startswith("/admin")}
    return {"shell": shell, "FIELD_LABELS": FIELD_LABELS, "FIELD_TYPES": FIELD_TYPES, "ICONS": ICONS, "EVENT_CATEGORIES": EVENT_CATEGORIES,
            "LEAVE_TYPES": LEAVE_TYPES, "ACTIONS": ACTIONS, "fmt_minutes": fmt_minutes, "today": dt.date.today()}


@app.template_filter("kebab")
def _kebab(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z0-9])", "-", name or "box").lower()


@app.template_filter("local")
def _local(d, fmt="%d %b %Y, %H:%M"):
    if not d:
        return "—"
    if isinstance(d, dt.datetime):
        return to_local(d, g.get("tz", "UTC")).strftime(fmt)
    return d.strftime("%d %b %Y")


@app.template_filter("date")
def _date(d):
    return d.strftime("%d %b %Y") if d else "—"


@app.template_filter("initials")
def _initials(name):
    return "".join(p[0].upper() for p in (name or "?").split()[:2])


@app.template_filter("hue")
def _hue(name):
    return sum(ord(c) for c in (name or "")) % 360


@app.template_filter("display")
def _display(v, ftype):
    if v is None or v == "" or v == []:
        return "—"
    if ftype == "CHECKBOX":
        return "Yes" if v else "No"
    if ftype == "FILE":
        return ", ".join(f["name"] for f in v) if isinstance(v, list) else "—"
    if ftype == "DATETIME":
        return str(v).replace("T", " ")
    return str(v)


def to_csv_response(rows: list[dict], columns: list[tuple[str, str]], filename: str) -> Response:
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow([label for _, label in columns])
    for r in rows:
        out = []
        for key, _ in columns:
            v = r.get(key)
            s = "; ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else str(v))
            if s[:1] in ("=", "+", "-", "@", "\t", "\r"):  # spreadsheet formula injection
                s = "'" + s
            out.append(s)
        w.writerow(out)
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def paginate(total: int, size: int):
    page = max(0, request.args.get("page", 0, type=int))
    return page, size, max(1, -(-total // size))


def args_without(*keys, **extra):
    d = {k: v for k, v in request.args.items() if k not in keys and v}
    d.update({k: v for k, v in extra.items() if v not in (None, "")})
    return d


app.jinja_env.globals["args_without"] = args_without


# ════════════════════════════════════════════════════════════════════════════
# File storage (local disk; files are only served through a permission check)
# ════════════════════════════════════════════════════════════════════════════

def save_upload(business_id: str, fs) -> dict:
    data = fs.read()
    if len(data) > MAX_UPLOAD:
        raise UserError(f"{fs.filename} is larger than {MAX_UPLOAD // 1024 // 1024} MB", 422)
    mime = fs.mimetype or "application/octet-stream"
    if not ALLOWED_MIME.match(mime):
        raise UserError(f"File type {mime} is not allowed", 422)
    key = f"{business_id}/{secrets.token_hex(16)}"
    path = UPLOAD_DIR / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    row = insert("FileAttachment", businessId=business_id, uploadedById=g.sess["user"]["id"], filename=(fs.filename or "file")[:255],
                 mimeType=mime, size=len(data), storageKey=key)
    return {"id": row["id"], "name": row["filename"], "size": row["size"]}


def delete_stored(keys):
    for k in keys:
        try:
            (UPLOAD_DIR / k).unlink(missing_ok=True)
        except OSError:
            pass


# ════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.get("/")
def home():
    s = current_session()
    if not s:
        return redirect(url_for("login"))
    if s["businessId"]:
        return redirect(url_for("dashboard"))
    return redirect(url_for("admin_home" if s["user"]["isSuperAdmin"] else "welcome"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if current_session():
            return redirect(url_for("home"))
        return render_template("auth.html", mode="login", google=bool(os.environ.get("GOOGLE_CLIENT_ID")))
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    rate_limit(f"login:{client_ip()}:{email}", 8, 600)
    user = q1('SELECT * FROM "User" WHERE email = %s', email)
    if not user or not verify_password(password, user["passwordHash"]):
        audit("LOGIN_FAILED", "auth", f"Failed login for {email}", user=user)
        flash("Invalid email or password", "error")
        return render_template("auth.html", mode="login", email=email), 401
    if not user["isActive"]:
        flash("Your account has been deactivated", "error")
        return render_template("auth.html", mode="login", email=email), 403
    if not user["emailVerifiedAt"]:
        return render_template("auth.html", mode="login", email=email, unverified=True), 403
    last = q1('SELECT "businessId" FROM "Session" WHERE "userId" = %s ORDER BY "lastSeenAt" DESC LIMIT 1', user["id"])
    bid = default_business(user["id"], last and last["businessId"])
    run('UPDATE "User" SET "lastLoginAt" = %s WHERE id = %s', now(), user["id"])
    business = q1('SELECT id, name FROM "Business" WHERE id = %s', bid) if bid else None
    audit("LOGIN", "auth", f"{user['name']} signed in", user=user, business=business)
    nxt = request.args.get("next") or ""
    target = nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("home")
    return start_session(redirect(target), user["id"], bid)


@app.post("/logout")
def logout():
    s = current_session()
    if s:
        audit("LOGOUT", "auth", f"{s['user']['name']} signed out", user=s["user"])
        run('DELETE FROM "Session" WHERE id = %s', s["id"])
    resp = redirect(url_for("login"))
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.route("/register", methods=["GET", "POST"])
@app.route("/register-business", methods=["GET", "POST"], endpoint="register_business")
def register():
    business_mode = request.endpoint == "register_business"
    if request.method == "GET":
        return render_template("auth.html", mode="register_business" if business_mode else "register")
    f = request.form
    rate_limit(f"register:{client_ip()}", 10, 3600)
    name, email, pw = clean_name(f.get("name")), clean_email(f.get("email")), f.get("password") or ""
    check_password_rules(pw)
    if pw != f.get("confirm"):
        raise UserError("Passwords don't match", 422)
    if q1('SELECT 1 FROM "User" WHERE email = %s', email):
        raise UserError("An account with this email already exists", 409)
    with tx():
        user = insert("User", name=name, email=email, passwordHash=hash_password(pw))
        business = None
        if business_mode:
            bname = clean_name(f.get("businessName"), "Business name", 2, 120)
            slug = slugify(bname)
            if q1('SELECT 1 FROM "Business" WHERE slug = %s', slug):
                slug = f"{slug}-{secrets.token_hex(2)}"
            free = q1('SELECT id FROM "Plan" WHERE name = %s', "Free")
            business = insert("Business", name=bname, slug=slug, industry=f.get("industry") or None, email=email,
                              timezone=valid_tz(f.get("timezone")), planId=free and free["id"])
            insert("BusinessMember", businessId=business["id"], userId=user["id"], roleId=system_role("owner")["id"], department="Management")
            enable_default_modules(business["id"])
    if os.environ.get("AUTO_VERIFY_EMAIL", "").lower() in ("1", "true", "yes") or (IS_PROD and not os.environ.get("SMTP_HOST")):
        # No mail server configured: nobody could ever click the link, so don't lock the user out.
        update("User", user["id"], emailVerifiedAt=now())
    else:
        send_verification(user)
    if business:
        audit("REGISTER_BUSINESS", "auth", f"{business['name']} registered by {name}", user=user, business=business, entity_type="Business", entity_id=business["id"])
        notify([r["id"] for r in q('SELECT id FROM "User" WHERE "isSuperAdmin"')], "BUSINESS_REGISTERED", f"New business: {business['name']}",
               link=url_for("admin_business", bid=business["id"]))
    else:
        audit("REGISTER", "auth", f"{name} registered", user=user, entity_type="User", entity_id=user["id"])
    return render_template("auth.html", mode="message", ok=True, title="Check your inbox",
                           message="Your account was created. Open the verification link we emailed you, then sign in.")


@app.get("/verify-email")
def verify_email():
    uid = consume_token(request.args.get("token", ""), "EMAIL_VERIFY")
    if not uid:
        return render_template("auth.html", mode="message", ok=False, title="Verification failed", message="This verification link is invalid or has expired.")
    user = update("User", uid, emailVerifiedAt=now())
    audit("EMAIL_VERIFIED", "auth", f"{user['email']} verified", user=user)
    return render_template("auth.html", mode="message", ok=True, title="Email verified", message="You can now sign in.")


@app.post("/resend-verification")
def resend_verification():
    rate_limit(f"resend:{client_ip()}", 5, 900)
    user = q1('SELECT * FROM "User" WHERE email = %s', (request.form.get("email") or "").strip().lower())
    if user and not user["emailVerifiedAt"]:
        send_verification(user)
    flash("If that account needs verification, a new link is on its way.", "success")
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("auth.html", mode="forgot")
    rate_limit(f"forgot:{client_ip()}", 5, 900)
    user = q1('SELECT * FROM "User" WHERE email = %s', (request.form.get("email") or "").strip().lower())
    if user and user["isActive"]:
        token = issue_token(user["id"], "PASSWORD_RESET", 60)
        send_mail(user["email"], "Reset your password", f"Hi {user['name']},\n\nReset your password here:\n{APP_URL}/reset-password?token={token}\n\nThis link expires in 1 hour.")
    return render_template("auth.html", mode="message", ok=True, title="Check your inbox", message="If an account exists for that email, a reset link has been sent.")


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    token = request.values.get("token", "")
    if request.method == "GET":
        return render_template("auth.html", mode="reset", token=token)
    pw = request.form.get("password") or ""
    check_password_rules(pw)
    if pw != request.form.get("confirm"):
        raise UserError("Passwords don't match", 422)
    uid = consume_token(token, "PASSWORD_RESET")
    if not uid:
        return render_template("auth.html", mode="message", ok=False, title="Link expired", message="This reset link is invalid or has expired.")
    user = update("User", uid, passwordHash=hash_password(pw), emailVerifiedAt=now())
    run('DELETE FROM "Session" WHERE "userId" = %s', uid)  # sign out everywhere
    audit("PASSWORD_RESET", "auth", f"{user['email']} reset their password", user=user)
    return render_template("auth.html", mode="message", ok=True, title="Password updated", message="Please sign in with your new password.")


@app.post("/switch")
@login_required
def switch_workspace():
    s = g.sess
    bid = request.form.get("businessId") or None
    if bid:
        b = q1('SELECT * FROM "Business" WHERE id = %s', bid)
        if not b:
            abort(403)
        if not s["user"]["isSuperAdmin"]:
            m = q1('SELECT * FROM "BusinessMember" WHERE "businessId" = %s AND "userId" = %s', bid, s["userId"])
            if not m or not m["isActive"] or b["status"] != "ACTIVE":
                abort(403)
        else:
            audit("ADMIN_ENTER_WORKSPACE", "admin", f"Super admin opened workspace {b['name']}", business=b)
    run('UPDATE "Session" SET "businessId" = %s WHERE id = %s', bid, s["id"])
    return redirect(url_for("dashboard") if bid else url_for("admin_home" if s["user"]["isSuperAdmin"] else "welcome"))


@app.get("/welcome")
@login_required
def welcome():
    rows = q('''SELECT b.id, b.name, r.name AS role FROM "BusinessMember" m JOIN "Business" b ON b.id = m."businessId" JOIN "Role" r ON r.id = m."roleId"
                WHERE m."userId" = %s AND m."isActive" AND b.status = 'ACTIVE' ORDER BY b.name''', g.sess["userId"])
    return render_template("welcome.html", memberships=rows)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    s = g.sess
    if request.method == "POST":
        if request.form.get("form") == "name":
            update("User", s["userId"], name=clean_name(request.form.get("name")))
            flash("Profile saved", "success")
        else:
            if not verify_password(request.form.get("currentPassword") or "", s["user"]["passwordHash"]):
                raise UserError("Current password is incorrect")
            pw = request.form.get("newPassword") or ""
            check_password_rules(pw)
            if pw != request.form.get("confirm"):
                raise UserError("Passwords don't match")
            update("User", s["userId"], passwordHash=hash_password(pw))
            run('DELETE FROM "Session" WHERE "userId" = %s AND id <> %s', s["userId"], s["id"])
            audit("PASSWORD_CHANGED", "auth", f"{s['user']['email']} changed their password")
            flash("Password changed. Other sessions were signed out.", "success")
        return redirect(url_for("profile"))
    sessions = q('SELECT * FROM "Session" WHERE "userId" = %s AND "expiresAt" > %s ORDER BY "lastSeenAt" DESC', s["userId"], now())
    return render_template("profile.html", sessions=sessions)


@app.get("/notifications")
@login_required
def notifications():
    unread = request.args.get("filter") == "unread"
    rows = q(f'''SELECT n.*, b.name AS business FROM "Notification" n LEFT JOIN "Business" b ON b.id = n."businessId"
                 WHERE n."userId" = %s {'AND n."readAt" IS NULL' if unread else ''} ORDER BY n."createdAt" DESC LIMIT 200''', g.sess["userId"])
    return render_template("notifications.html", items=rows, unread=unread)


@app.post("/notifications/read")
@login_required
def notifications_read():
    ids = request.form.getlist("id") or (request.get_json(silent=True) or {}).get("ids") or []
    if request.form.get("all") or (request.get_json(silent=True) or {}).get("all"):
        run('UPDATE "Notification" SET "readAt" = %s WHERE "userId" = %s AND "readAt" IS NULL', now(), g.sess["userId"])
    elif ids:
        run('UPDATE "Notification" SET "readAt" = %s WHERE "userId" = %s AND id = ANY(%s)', now(), g.sess["userId"], list(ids))
    if is_api() or request.is_json:
        return jsonify(ok=True)
    return redirect(request.referrer or url_for("notifications"))


@app.get("/api/notifications")
@login_required
def api_notifications():
    uid = g.sess["userId"]
    try:
        run_reminders(uid)
    except Exception as exc:
        print("[reminders]", exc, flush=True)
    items = q('SELECT id, title, body, link, "readAt", "createdAt" FROM "Notification" WHERE "userId" = %s ORDER BY "createdAt" DESC LIMIT 8', uid)
    unread = q1('SELECT count(*) AS n FROM "Notification" WHERE "userId" = %s AND "readAt" IS NULL', uid)["n"]
    for i in items:
        i["createdAt"] = i["createdAt"].isoformat() + "Z"
        i["readAt"] = i["readAt"] and i["readAt"].isoformat()
    return jsonify(items=items, unread=unread)


def run_reminders(user_id: str):
    """Lazy event reminders, generated when a user's notifications are fetched."""
    t, horizon = now(), now() + dt.timedelta(hours=24)
    rows = q('''SELECT s.*, a."lastRemindedFor" FROM "ScheduleAssignee" a JOIN "Schedule" s ON s.id = a."scheduleId"
                WHERE a."userId" = %s AND NOT s."isCancelled" AND s."reminderMinutes" IS NOT NULL AND s."startAt" <= %s
                AND (s.recurrence <> 'NONE' OR s."startAt" >= %s)''', user_id, horizon, t)
    for s in rows:
        occ = occurrences(s, t, horizon)
        if not occ:
            continue
        start = occ[0][0]
        if start - dt.timedelta(minutes=s["reminderMinutes"]) > t or s["lastRemindedFor"] == start:
            continue
        run('UPDATE "ScheduleAssignee" SET "lastRemindedFor" = %s WHERE "scheduleId" = %s AND "userId" = %s', start, s["id"], user_id)
        b = q1('SELECT timezone FROM "Business" WHERE id = %s', s["businessId"])
        notify([user_id], "EVENT_REMINDER", f"Reminder: {s['title']}",
               body=f"Starts {to_local(start, b['timezone']).strftime('%d %b %H:%M')}" + (f" · {s['location']}" if s["location"] else ""),
               link="/app/schedule", business_id=s["businessId"])


# ════════════════════════════════════════════════════════════════════════════
# Shared helpers: roles, members, modules
# ════════════════════════════════════════════════════════════════════════════

def slugify(s: str) -> str:
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", s.lower().strip()))[:48] or "business"


def valid_tz(name: str | None) -> str:
    try:
        ZoneInfo(name or "UTC")
        return name or "UTC"
    except Exception:
        return "UTC"


def system_role(key: str):
    r = q1('SELECT * FROM "Role" WHERE "businessId" IS NULL AND key = %s', key)
    if not r:
        raise RuntimeError(f"Missing system role {key}; run `flask --app app seed`")
    return r


def roles_for_business(bid: str):
    return q('SELECT * FROM "Role" WHERE "businessId" IS NULL OR "businessId" = %s ORDER BY rank DESC, name', bid)


def role_usable(bid: str, role_id: str):
    r = q1('SELECT * FROM "Role" WHERE id = %s', role_id)
    if not r or (r["businessId"] and r["businessId"] != bid):
        raise UserError("Invalid role for this business")
    return r


def enable_default_modules(bid: str):
    for m in q('SELECT id FROM "Module" WHERE "isDefault" AND "isEnabled"'):
        run('INSERT INTO "BusinessModule" ("businessId", "moduleId") VALUES (%s, %s) ON CONFLICT DO NOTHING', bid, m["id"])


def add_member(bid: str, email: str, name: str, role_id: str, password: str | None = None, department=None, job_title=None):
    """Add a person to a business, creating the account if needed (with a set-password email when no password given)."""
    b = q1('''SELECT b.*, p.name AS plan_name, p."maxUsers" FROM "Business" b LEFT JOIN "Plan" p ON p.id = b."planId" WHERE b.id = %s''', bid)
    if not b:
        abort(404)
    seats = q1('SELECT count(*) AS n FROM "BusinessMember" WHERE "businessId" = %s AND "isActive"', bid)["n"]
    if b["maxUsers"] and seats >= b["maxUsers"]:
        raise UserError(f"The {b['plan_name']} plan allows {b['maxUsers']} active users. Upgrade the plan to add more.")
    role_usable(bid, role_id)
    user = q1('SELECT * FROM "User" WHERE email = %s', email)
    created = False
    if not user:
        if password:
            check_password_rules(password)
        user = insert("User", email=email, name=name, emailVerifiedAt=now(), passwordHash=hash_password(password or secrets.token_urlsafe(24)))
        created = True
    existing = q1('SELECT * FROM "BusinessMember" WHERE "businessId" = %s AND "userId" = %s', bid, user["id"])
    if existing and existing["isActive"]:
        raise UserError("This person is already a member of the business", 409)
    if existing:
        update("BusinessMember", existing["id"], isActive=True, roleId=role_id, department=department or None, jobTitle=job_title or None)
    else:
        insert("BusinessMember", businessId=bid, userId=user["id"], roleId=role_id, department=department or None, jobTitle=job_title or None)
    if created and not password:
        token = issue_token(user["id"], "PASSWORD_RESET", 60 * 72)
        send_mail(email, f"You've been added to {b['name']}", f"Hi {name},\n\nYou've been added to {b['name']} on Orbit. Set your password:\n{APP_URL}/reset-password?token={token}")
    else:
        send_mail(email, f"You've been added to {b['name']}", f"Hi {name},\n\nYou now have access to {b['name']} on Orbit. Sign in at {APP_URL}/login")
    return user


def provision_permissions(module_id: str, key: str, actions, custom=True):
    """Create permission rows for a module and grant platform role templates their defaults."""
    roles = q('SELECT * FROM "Role" WHERE "businessId" IS NULL AND "isSystem"')
    for action in actions:
        p = q1('SELECT * FROM "Permission" WHERE "moduleId" = %s AND action = %s', module_id, action) or \
            insert("Permission", moduleId=module_id, action=action, key=f"{key}:{action}", description=f"{action} on {key}")
        for r in roles:
            grants = DEFAULT_GRANTS.get(r["key"], {})
            scope = (grants.get(key) or (grants.get("*custom") if custom else None) or {}).get(action)
            if scope:
                run('INSERT INTO "RolePermission" ("roleId", "permissionId", scope) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING', r["id"], p["id"], scope)


# ════════════════════════════════════════════════════════════════════════════
# SUPER ADMIN
# ════════════════════════════════════════════════════════════════════════════

@app.get("/admin")
@admin_required
def admin_home():
    since = dt.datetime.combine(dt.date.today() - dt.timedelta(days=13), dt.time())
    logins = q('SELECT "createdAt" FROM "ActivityLog" WHERE action = %s AND "createdAt" >= %s', "LOGIN", since)
    days = [since.date() + dt.timedelta(days=i) for i in range(14)]
    trend = {"labels": [d.strftime("%b %d") for d in days], "values": [sum(1 for l in logins if l["createdAt"].date() == d) for d in days]}
    plans = q('SELECT p.name, count(b.id) AS n FROM "Plan" p LEFT JOIN "Business" b ON b."planId" = p.id GROUP BY p.name, p."priceCents" ORDER BY p."priceCents"')
    stats = q1('''SELECT (SELECT count(*) FROM "Business") AS businesses, (SELECT count(*) FROM "Business" WHERE status = 'ACTIVE') AS active,
                  (SELECT count(*) FROM "User") AS users, (SELECT count(*) FROM "Module" WHERE "isEnabled") AS modules''')
    newest = q('''SELECT b.*, (SELECT count(*) FROM "BusinessMember" m WHERE m."businessId" = b.id) AS members FROM "Business" b ORDER BY "createdAt" DESC LIMIT 5''')
    recent = q('SELECT * FROM "ActivityLog" ORDER BY "createdAt" DESC LIMIT 8')
    return render_template("admin/overview.html", stats=stats, trend=trend, plans=plans, newest=newest, recent=recent, logins=len(logins))


@app.route("/admin/businesses", methods=["GET", "POST"])
@admin_required
def admin_businesses():
    if request.method == "POST":
        f = request.form
        name = clean_name(f.get("name"), "Business name", 2, 120)
        slug = slugify(name)
        if q1('SELECT 1 FROM "Business" WHERE slug = %s', slug):
            slug = f"{slug}-{secrets.token_hex(2)}"
        with tx():
            b = insert("Business", name=name, slug=slug, email=(f.get("email") or None), industry=f.get("industry") or None,
                       timezone=valid_tz(f.get("timezone")), planId=f.get("planId") or None)
            for mid in f.getlist("modules"):
                run('INSERT INTO "BusinessModule" ("businessId", "moduleId") VALUES (%s, %s) ON CONFLICT DO NOTHING', b["id"], mid)
            if f.get("ownerEmail"):
                add_member(b["id"], clean_email(f.get("ownerEmail")), clean_name(f.get("ownerName")), system_role("owner")["id"],
                           f.get("ownerPassword") or None, "Management")
        audit("CREATE", "admin", f"Created business {name}", business=b, entity_type="Business", entity_id=b["id"])
        flash("Business created", "success")
        return redirect(url_for("admin_business", bid=b["id"]))
    rows = q('''SELECT b.*, p.name AS plan, (SELECT count(*) FROM "BusinessMember" m WHERE m."businessId" = b.id) AS members,
                (SELECT count(*) FROM "BusinessModule" bm WHERE bm."businessId" = b.id AND bm."isEnabled") AS modules
                FROM "Business" b LEFT JOIN "Plan" p ON p.id = b."planId" ORDER BY b."createdAt" DESC''')
    return render_template("admin/businesses.html", rows=rows, plans=q('SELECT * FROM "Plan" WHERE "isActive" ORDER BY "priceCents"'),
                           modules=q('SELECT * FROM "Module" WHERE NOT "isMandatory" ORDER BY "sortOrder"'))


@app.get("/admin/businesses/<bid>")
@admin_required
def admin_business(bid):
    b = q1('SELECT b.*, p.name AS plan FROM "Business" b LEFT JOIN "Plan" p ON p.id = b."planId" WHERE b.id = %s', bid)
    if not b:
        abort(404)
    modules = q('''SELECT m.*, bm."isEnabled" AS biz_enabled, bm."customName", bm."customDescription" FROM "Module" m
                   LEFT JOIN "BusinessModule" bm ON bm."moduleId" = m.id AND bm."businessId" = %s ORDER BY m."sortOrder"''', bid)
    members = q('''SELECT m.*, u.name, u.email, u."lastLoginAt", r.name AS role FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId"
                   JOIN "Role" r ON r.id = m."roleId" WHERE m."businessId" = %s ORDER BY m."joinedAt"''', bid)
    counts = q1('''SELECT (SELECT count(*) FROM "CustomRecord" WHERE "businessId" = %s) AS records, (SELECT count(*) FROM "Attendance" WHERE "businessId" = %s) AS attendance,
                   (SELECT count(*) FROM "Schedule" WHERE "businessId" = %s) AS schedules''', bid, bid, bid)
    logs = q('SELECT * FROM "ActivityLog" WHERE "businessId" = %s ORDER BY "createdAt" DESC LIMIT 15', bid)
    return render_template("admin/business.html", b=b, modules=modules, members=members, counts=counts, logs=logs,
                           plans=q('SELECT * FROM "Plan" ORDER BY "priceCents"'), roles=roles_for_business(bid), tab=request.args.get("tab", "overview"))


@app.post("/admin/businesses/<bid>/update")
@admin_required
def admin_business_update(bid):
    f = request.form
    b = update("Business", bid, name=clean_name(f.get("name"), "Business name", 2, 120), email=f.get("email") or None, phone=f.get("phone") or None,
               address=f.get("address") or None, industry=f.get("industry") or None, timezone=valid_tz(f.get("timezone")),
               workStartTime=f.get("workStartTime") or "09:00", lateGraceMinutes=f.get("lateGraceMinutes", 10, type=int),
               halfDayMinutes=f.get("halfDayMinutes", 240, type=int), workDays=[int(x) for x in f.getlist("workDays")],
               planId=f.get("planId") or None, planExpiresAt=dt.datetime.fromisoformat(f["planExpiresAt"]) if f.get("planExpiresAt") else None)
    audit("UPDATE", "admin", f"Updated business {b['name']}", business=b, entity_type="Business", entity_id=bid)
    flash("Saved", "success")
    return redirect(url_for("admin_business", bid=bid))


@app.post("/admin/businesses/<bid>/status")
@admin_required
def admin_business_status(bid):
    b = q1('SELECT * FROM "Business" WHERE id = %s', bid) or abort(404)
    status = "SUSPENDED" if b["status"] == "ACTIVE" else "ACTIVE"
    update("Business", bid, status=status)
    if status == "SUSPENDED":
        run('DELETE FROM "Session" s USING "User" u WHERE s."userId" = u.id AND s."businessId" = %s AND NOT u."isSuperAdmin"', bid)
    audit("DEACTIVATE" if status == "SUSPENDED" else "ACTIVATE", "admin", f"{'Suspended' if status == 'SUSPENDED' else 'Reactivated'} {b['name']}",
          business=b, entity_type="Business", entity_id=bid)
    flash("Business suspended" if status == "SUSPENDED" else "Business reactivated", "success")
    return redirect(url_for("admin_business", bid=bid))


@app.post("/admin/businesses/<bid>/delete")
@admin_required
def admin_business_delete(bid):
    b = q1('SELECT * FROM "Business" WHERE id = %s', bid) or abort(404)
    if request.form.get("confirm") != b["name"]:
        raise UserError("Type the business name exactly to confirm deletion")
    run('DELETE FROM "Business" WHERE id = %s', bid)
    audit("DELETE", "admin", f"Permanently deleted business {b['name']}", business=b, entity_type="Business", entity_id=bid)
    flash("Business deleted", "success")
    return redirect(url_for("admin_businesses"))


@app.post("/admin/businesses/<bid>/modules")
@admin_required
def admin_business_modules(bid):
    b = q1('SELECT * FROM "Business" WHERE id = %s', bid) or abort(404)
    enabled = set(request.form.getlist("enabled"))
    with tx():
        for m in q('SELECT id, key FROM "Module" WHERE NOT "isMandatory"'):
            run('''INSERT INTO "BusinessModule" ("businessId", "moduleId", "isEnabled", "customName", "customDescription") VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT ("businessId", "moduleId") DO UPDATE SET "isEnabled" = EXCLUDED."isEnabled", "customName" = EXCLUDED."customName",
                   "customDescription" = EXCLUDED."customDescription"''',
                bid, m["id"], m["id"] in enabled, (request.form.get(f"name_{m['id']}") or "").strip()[:80] or None,
                (request.form.get(f"desc_{m['id']}") or "").strip()[:300] or None)
    audit("PERMISSION_CHANGE", "admin", f"Updated modules for {b['name']}", business=b, entity_type="Business", entity_id=bid, metadata={"enabled": list(enabled)})
    flash("Properties updated", "success")
    return redirect(url_for("admin_business", bid=bid, tab="properties"))


@app.post("/admin/businesses/<bid>/members")
@admin_required
def admin_business_add_member(bid):
    f = request.form
    b = q1('SELECT * FROM "Business" WHERE id = %s', bid) or abort(404)
    u = add_member(bid, clean_email(f.get("email")), clean_name(f.get("name")), f.get("roleId"), f.get("password") or None, f.get("department"))
    audit("CREATE", "employees", f"Assigned {u['email']} to {b['name']}", business=b, entity_type="BusinessMember", entity_id=u["id"])
    flash("Member added", "success")
    return redirect(url_for("admin_business", bid=bid, tab="members"))


@app.post("/admin/members/<mid>")
@admin_required
def admin_member_update(mid):
    m = q1('SELECT m.*, u.email FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId" WHERE m.id = %s', mid) or abort(404)
    b = q1('SELECT * FROM "Business" WHERE id = %s', m["businessId"])
    if request.form.get("delete"):
        run('DELETE FROM "BusinessMember" WHERE id = %s', mid)
        run('UPDATE "Session" SET "businessId" = NULL WHERE "userId" = %s AND "businessId" = %s', m["userId"], m["businessId"])
        audit("DELETE", "employees", f"Removed {m['email']} from {b['name']}", business=b, entity_type="BusinessMember", entity_id=mid)
        flash("Member removed", "success")
    else:
        changes = {}
        if request.form.get("roleId"):
            role_usable(m["businessId"], request.form["roleId"])
            changes["roleId"] = request.form["roleId"]
        if "isActive" in request.form:
            changes["isActive"] = request.form["isActive"] == "1"
            if not changes["isActive"]:
                run('UPDATE "Session" SET "businessId" = NULL WHERE "userId" = %s AND "businessId" = %s', m["userId"], m["businessId"])
        update("BusinessMember", mid, **changes)
        audit("PERMISSION_CHANGE" if "roleId" in changes else "UPDATE", "employees", f"Updated membership of {m['email']}", business=b,
              entity_type="BusinessMember", entity_id=mid)
        flash("Membership updated", "success")
    return redirect(url_for("admin_business", bid=m["businessId"], tab="members"))


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    if request.method == "POST":
        f = request.form
        name, email, pw = clean_name(f.get("name")), clean_email(f.get("email")), f.get("password") or ""
        check_password_rules(pw)
        if f.get("businessId"):
            user = add_member(f["businessId"], email, name, f.get("roleId"), pw, f.get("department"))
        else:
            if q1('SELECT 1 FROM "User" WHERE email = %s', email):
                raise UserError("A user with this email already exists", 409)
            user = insert("User", name=name, email=email, passwordHash=hash_password(pw), emailVerifiedAt=now())
        if f.get("isSuperAdmin"):
            update("User", user["id"], isSuperAdmin=True)
        audit("CREATE", "admin", f"Created user {email}", entity_type="User", entity_id=user["id"])
        flash("User created", "success")
        return redirect(url_for("admin_users"))
    flt, search = request.args.get("filter", "all"), (request.args.get("q") or "").strip()
    where, params = ["TRUE"], []
    if flt == "admins":
        where.append('u."isSuperAdmin"')
    elif flt == "inactive":
        where.append('NOT u."isActive"')
    elif flt == "unassigned":
        where.append('NOT EXISTS (SELECT 1 FROM "BusinessMember" m WHERE m."userId" = u.id)')
    if search:
        where.append("(u.name ILIKE %s OR u.email ILIKE %s)")
        params += [f"%{search}%", f"%{search}%"]
    users = q(f'''SELECT u.*, COALESCE((SELECT json_agg(json_build_object('business', b.name, 'role', r.name, 'active', m."isActive"))
                  FROM "BusinessMember" m JOIN "Business" b ON b.id = m."businessId" JOIN "Role" r ON r.id = m."roleId" WHERE m."userId" = u.id), '[]') AS memberships
                  FROM "User" u WHERE {" AND ".join(where)} ORDER BY u."createdAt" DESC LIMIT 500''', *params)
    return render_template("admin/users.html", users=users, businesses=q('SELECT id, name FROM "Business" ORDER BY name'),
                           roles=q('SELECT id, name, "businessId" FROM "Role" ORDER BY rank DESC'), flt=flt, search=search)


@app.post("/admin/users/<uid>")
@admin_required
def admin_user_update(uid):
    f = request.form
    user = q1('SELECT * FROM "User" WHERE id = %s', uid) or abort(404)
    me = g.sess["userId"]
    act = f.get("action")
    if act == "delete":
        if uid == me:
            raise UserError("You can't delete your own account")
        run('DELETE FROM "User" WHERE id = %s', uid)
        audit("DELETE", "admin", f"Deleted user {user['email']}", entity_type="User", entity_id=uid)
        flash("User deleted", "success")
    elif act == "toggle_active":
        if uid == me:
            raise UserError("You can't deactivate your own account")
        update("User", uid, isActive=not user["isActive"])
        if user["isActive"]:
            run('DELETE FROM "Session" WHERE "userId" = %s', uid)
        audit("DEACTIVATE" if user["isActive"] else "ACTIVATE", "admin", f"{'Deactivated' if user['isActive'] else 'Activated'} {user['email']}", entity_type="User", entity_id=uid)
        flash("Updated", "success")
    elif act == "toggle_admin":
        if uid == me:
            raise UserError("You can't change your own super admin access")
        update("User", uid, isSuperAdmin=not user["isSuperAdmin"])
        audit("PERMISSION_CHANGE", "admin", f"Changed super admin flag for {user['email']}", entity_type="User", entity_id=uid)
        flash("Updated", "success")
    elif act == "password":
        check_password_rules(f.get("newPassword") or "")
        update("User", uid, passwordHash=hash_password(f["newPassword"]))
        run('DELETE FROM "Session" WHERE "userId" = %s', uid)
        audit("PASSWORD_RESET", "admin", f"Reset password for {user['email']}", entity_type="User", entity_id=uid)
        flash("Password reset; the user was signed out everywhere", "success")
    elif act == "reset_email":
        token = issue_token(uid, "PASSWORD_RESET", 60 * 24)
        send_mail(user["email"], "Reset your password", f"An administrator requested a password reset.\n\n{APP_URL}/reset-password?token={token}")
        audit("PASSWORD_RESET", "admin", f"Sent reset link to {user['email']}", entity_type="User", entity_id=uid)
        flash("Reset link sent", "success")
    elif act == "rename":
        update("User", uid, name=clean_name(f.get("name")))
        flash("Saved", "success")
    return redirect(url_for("admin_users", **request.args))


@app.route("/admin/modules", methods=["GET", "POST"])
@admin_required
def admin_modules():
    if request.method == "POST":
        f = request.form
        key = (f.get("key") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", key):
            raise UserError("Keys use lowercase letters, numbers and underscores")
        if q1('SELECT 1 FROM "Module" WHERE key = %s', key):
            raise UserError("A module with this key already exists", 409)
        with tx():
            m = insert("Module", key=key, name=clean_name(f.get("name"), "Name", 2, 80), description=f.get("description") or None,
                       icon=f.get("icon") if f.get("icon") in ICONS else "Box", kind="CUSTOM")
            provision_permissions(m["id"], key, CUSTOM_ACTIONS)
        audit("CREATE", "admin", f"Created module {m['name']}", entity_type="Module", entity_id=m["id"])
        flash("Module created — now design its form", "success")
        return redirect(url_for("admin_module", mid=m["id"]))
    rows = q('''SELECT m.*, (SELECT count(*) FROM "ModuleField" f WHERE f."moduleId" = m.id) AS fields,
                (SELECT count(*) FROM "CustomRecord" r WHERE r."moduleId" = m.id) AS records,
                (SELECT count(*) FROM "BusinessModule" bm WHERE bm."moduleId" = m.id AND bm."isEnabled") AS businesses
                FROM "Module" m ORDER BY m."sortOrder", m.name''')
    return render_template("admin/modules.html", rows=rows)


@app.post("/admin/modules/<mid>/toggle")
@admin_required
def admin_module_toggle(mid):
    field = request.form.get("field")
    if field not in ("isEnabled", "isMandatory", "isDefault"):
        abort(400)
    m = q1('SELECT * FROM "Module" WHERE id = %s', mid) or abort(404)
    update("Module", mid, **{field: not m[field]})
    audit("UPDATE", "admin", f"Toggled {field} on {m['name']}", entity_type="Module", entity_id=mid)
    flash(f"{m['name']} updated", "success")
    return redirect(request.referrer or url_for("admin_modules"))


@app.route("/admin/modules/<mid>", methods=["GET", "POST"])
@admin_required
def admin_module(mid):
    m = q1('SELECT * FROM "Module" WHERE id = %s', mid) or abort(404)
    if request.method == "POST":
        f = request.form
        update("Module", mid, name=clean_name(f.get("name"), "Name", 2, 80), description=f.get("description") or None,
               icon=f.get("icon") if f.get("icon") in ICONS else m["icon"], sortOrder=f.get("sortOrder", 100, type=int),
               isEnabled=bool(f.get("isEnabled")), isMandatory=bool(f.get("isMandatory")), isDefault=bool(f.get("isDefault")))
        audit("UPDATE", "admin", f"Updated module {m['name']}", entity_type="Module", entity_id=mid)
        flash("Settings saved", "success")
        return redirect(url_for("admin_module", mid=mid))
    fields = q('SELECT * FROM "ModuleField" WHERE "moduleId" = %s ORDER BY "sortOrder"', mid)
    assigned = {r["businessId"] for r in q('SELECT "businessId" FROM "BusinessModule" WHERE "moduleId" = %s AND "isEnabled"', mid)}
    return render_template("admin/module.html", m=m, fields=fields, assigned=assigned,
                           businesses=q('SELECT id, name, status FROM "Business" ORDER BY name'))


@app.post("/admin/modules/<mid>/fields")
@admin_required
def admin_module_fields(mid):
    m = q1('SELECT * FROM "Module" WHERE id = %s', mid) or abort(404)
    if m["kind"] != "CUSTOM":
        raise UserError("Only custom modules have editable forms")
    try:
        fields = json.loads(request.form.get("fields_json") or "[]")
    except ValueError:
        raise UserError("Invalid form definition")
    keys, clean = [], []
    for fd in fields[:60]:
        key = (fd.get("key") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", key):
            raise UserError(f"Invalid field key '{key}': use lowercase letters, numbers and underscores")
        if key in keys:
            raise UserError("Field keys must be unique")
        if fd.get("type") not in FIELD_TYPES:
            raise UserError("Invalid field type")
        label = (fd.get("label") or "").strip()[:80]
        if not label:
            raise UserError("Every field needs a label")
        keys.append(key)
        clean.append(dict(key=key, label=label, type=fd["type"], required=bool(fd.get("required")), showInTable=bool(fd.get("showInTable", True)),
                          options=[o.strip()[:80] for o in (fd.get("options") or []) if o.strip()][:100], helpText=(fd.get("helpText") or "")[:200] or None))
    with tx():
        if keys:
            run('DELETE FROM "ModuleField" WHERE "moduleId" = %s AND NOT (key = ANY(%s))', mid, keys)
        else:
            run('DELETE FROM "ModuleField" WHERE "moduleId" = %s', mid)
        for i, fd in enumerate(clean):
            run('''INSERT INTO "ModuleField" (id, "moduleId", key, label, type, required, options, "helpText", "showInTable", "sortOrder", "updatedAt")
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT ("moduleId", key) DO UPDATE SET label = EXCLUDED.label, type = EXCLUDED.type, required = EXCLUDED.required,
                   options = EXCLUDED.options, "helpText" = EXCLUDED."helpText", "showInTable" = EXCLUDED."showInTable", "sortOrder" = EXCLUDED."sortOrder", "updatedAt" = EXCLUDED."updatedAt"''',
                new_id(), mid, fd["key"], fd["label"], fd["type"], fd["required"], fd["options"], fd["helpText"], fd["showInTable"], i, now())
    audit("UPDATE", "admin", f"Updated form fields of {m['name']}", entity_type="Module", entity_id=mid, metadata={"fields": keys})
    flash("Form saved", "success")
    return redirect(url_for("admin_module", mid=mid))


@app.post("/admin/modules/<mid>/assign")
@admin_required
def admin_module_assign(mid):
    m = q1('SELECT * FROM "Module" WHERE id = %s', mid) or abort(404)
    ids = request.form.getlist("businesses")
    with tx():
        run('UPDATE "BusinessModule" SET "isEnabled" = false WHERE "moduleId" = %s', mid)
        for bid in ids:
            run('''INSERT INTO "BusinessModule" ("businessId", "moduleId", "isEnabled") VALUES (%s, %s, true)
                   ON CONFLICT ("businessId", "moduleId") DO UPDATE SET "isEnabled" = true''', bid, mid)
    audit("PERMISSION_CHANGE", "admin", f"Assigned {m['name']} to {len(ids)} business(es)", entity_type="Module", entity_id=mid)
    flash("Assignments saved", "success")
    return redirect(url_for("admin_module", mid=mid))


@app.post("/admin/modules/<mid>/delete")
@admin_required
def admin_module_delete(mid):
    m = q1('SELECT * FROM "Module" WHERE id = %s', mid) or abort(404)
    if m["isCore"]:
        raise UserError("Built-in modules can't be deleted. Disable them instead.")
    run('DELETE FROM "Module" WHERE id = %s', mid)
    audit("DELETE", "admin", f"Deleted module {m['name']} and all its records", entity_type="Module", entity_id=mid)
    flash("Module deleted", "success")
    return redirect(url_for("admin_modules"))


@app.route("/admin/roles", methods=["GET", "POST"])
@admin_required
def admin_roles():
    if request.method == "POST":
        f = request.form
        name = clean_name(f.get("name"), "Role name", 2, 60)
        rank = max(1, min(99, f.get("rank", 20, type=int)))
        with tx():
            r = insert("Role", key=f"custom-{slugify(name)}-{secrets.token_hex(2)}", name=name, description=f.get("description") or None,
                       businessId=f.get("businessId") or None, rank=rank)
            if f.get("copyFrom"):
                run('''INSERT INTO "RolePermission" ("roleId", "permissionId", scope) SELECT %s, "permissionId", scope FROM "RolePermission" WHERE "roleId" = %s''', r["id"], f["copyFrom"])
        audit("CREATE", "admin", f"Created role {name}", entity_type="Role", entity_id=r["id"])
        flash("Role created", "success")
        return redirect(url_for("admin_roles", role=r["id"]))
    roles = q('''SELECT r.*, b.name AS business, (SELECT count(*) FROM "BusinessMember" m WHERE m."roleId" = r.id) AS members
                 FROM "Role" r LEFT JOIN "Business" b ON b.id = r."businessId" ORDER BY r.rank DESC, r.name''')
    sel = request.args.get("role") or next((r["id"] for r in roles if r["key"] == "manager"), roles[0]["id"] if roles else None)
    role = next((r for r in roles if r["id"] == sel), None)
    modules = q('SELECT * FROM "Module" ORDER BY "sortOrder"')
    perms = q('SELECT * FROM "Permission"')
    grants = {r["permissionId"]: r["scope"] for r in q('SELECT * FROM "RolePermission" WHERE "roleId" = %s', sel)} if sel else {}
    matrix = [{"m": m, "cells": {p["action"]: p for p in perms if p["moduleId"] == m["id"]}} for m in modules]
    return render_template("admin/roles.html", roles=roles, role=role, matrix=matrix, grants=grants,
                           businesses=q('SELECT id, name FROM "Business" ORDER BY name'))


@app.post("/admin/roles/<rid>/permissions")
@admin_required
def admin_role_permissions(rid):
    r = q1('SELECT * FROM "Role" WHERE id = %s', rid) or abort(404)
    if r["fullAccess"]:
        raise UserError("The owner role always has full access")
    grants = [(k[5:], v) for k, v in request.form.items() if k.startswith("perm_") and v in ("OWN", "ALL")]
    with tx():
        run('DELETE FROM "RolePermission" WHERE "roleId" = %s', rid)
        for pid, scope in grants:
            run('INSERT INTO "RolePermission" ("roleId", "permissionId", scope) VALUES (%s, %s, %s)', rid, pid, scope)
    audit("PERMISSION_CHANGE", "admin", f"Updated permissions for role {r['name']} ({len(grants)} grants)", entity_type="Role", entity_id=rid)
    flash("Permissions saved", "success")
    return redirect(url_for("admin_roles", role=rid))


@app.post("/admin/roles/<rid>/delete")
@admin_required
def admin_role_delete(rid):
    r = q1('SELECT r.*, (SELECT count(*) FROM "BusinessMember" m WHERE m."roleId" = r.id) AS members FROM "Role" r WHERE id = %s', rid) or abort(404)
    if r["isSystem"]:
        raise UserError("System roles can't be deleted")
    if r["members"]:
        raise UserError(f"Reassign the {r['members']} member(s) using this role first")
    run('DELETE FROM "Role" WHERE id = %s', rid)
    audit("DELETE", "admin", f"Deleted role {r['name']}", entity_type="Role", entity_id=rid)
    flash("Role deleted", "success")
    return redirect(url_for("admin_roles"))


@app.route("/admin/plans", methods=["GET", "POST"])
@admin_required
def admin_plans():
    if request.method == "POST":
        f = request.form
        data = dict(name=clean_name(f.get("name"), "Plan name", 2, 40), description=f.get("description") or None,
                    priceCents=round(float(f.get("price") or 0) * 100), maxUsers=max(1, f.get("maxUsers", 10, type=int)), isActive=bool(f.get("isActive")))
        if f.get("id"):
            p = update("Plan", f["id"], **data)
            audit("UPDATE", "admin", f"Updated plan {p['name']}", entity_type="Plan", entity_id=p["id"])
        else:
            p = insert("Plan", **data)
            audit("CREATE", "admin", f"Created plan {p['name']}", entity_type="Plan", entity_id=p["id"])
        flash("Plan saved", "success")
        return redirect(url_for("admin_plans"))
    rows = q('SELECT p.*, (SELECT count(*) FROM "Business" b WHERE b."planId" = p.id) AS businesses FROM "Plan" p ORDER BY "priceCents"')
    return render_template("admin/plans.html", rows=rows)


def log_query(base_where: list, base_params: list):
    a = request.args
    where, params = list(base_where), list(base_params)
    for col, key in (('"userId"', "userId"), ('"businessId"', "businessId"), ("action", "action"), ("module", "module")):
        if a.get(key):
            where.append(f"{col} = %s")
            params.append(a[key])
    if a.get("q"):
        where.append('(description ILIKE %s OR "userName" ILIKE %s)')
        params += [f"%{a['q']}%"] * 2
    if a.get("from"):
        where.append('"createdAt" >= %s')
        params.append(parse_day(a["from"]))
    if a.get("to"):
        where.append('"createdAt" < %s')
        params.append(parse_day(a["to"]) + dt.timedelta(days=1))
    return " AND ".join(where) or "TRUE", params


def log_page(template_kwargs: dict, where: str, params: list, can_export=True):
    if request.args.get("format") == "csv":
        if not can_export:
            abort(403)
        rows = q(f'SELECT * FROM "ActivityLog" WHERE {where} ORDER BY "createdAt" DESC LIMIT 50000', *params)
        return to_csv_response(rows, [("createdAt", "Timestamp (UTC)"), ("businessName", "Business"), ("userName", "User"), ("userEmail", "Email"),
                                      ("action", "Action"), ("module", "Module"), ("description", "Description"), ("entityType", "Record type"),
                                      ("entityId", "Record ID"), ("ip", "IP")], "activity-logs.csv")
    total = q1(f'SELECT count(*) AS n FROM "ActivityLog" WHERE {where}', *params)["n"]
    page, size, pages = paginate(total, 25)
    rows = q(f'SELECT * FROM "ActivityLog" WHERE {where} ORDER BY "createdAt" DESC LIMIT %s OFFSET %s', *params, size, page * size)
    return render_template("logs.html", rows=rows, total=total, page=page, pages=pages, can_export=can_export,
                           actions=["LOGIN", "LOGOUT", "LOGIN_FAILED", "CREATE", "UPDATE", "DELETE", "CHECK_IN", "CHECK_OUT", "APPROVE", "REJECT",
                                    "PERMISSION_CHANGE", "DEACTIVATE", "ACTIVATE", "EXPORT", "ANNOUNCEMENT", "PASSWORD_RESET"], **template_kwargs)


@app.get("/admin/logs")
@admin_required
def admin_logs():
    where, params = log_query([], [])
    return log_page(dict(title="Audit logs", subtitle="Append-only record of every important action on the platform. Entries cannot be edited or deleted.",
                         users=q('SELECT id, name FROM "User" ORDER BY name LIMIT 1000'), businesses=q('SELECT id, name FROM "Business" ORDER BY name'),
                         modules=q('SELECT key, name FROM "Module" ORDER BY "sortOrder"'), endpoint="admin_logs"), where, params)


@app.route("/admin/announcements", methods=["GET", "POST"])
@admin_required
def admin_announcements():
    if request.method == "POST":
        f = request.form
        title = clean_name(f.get("title"), "Title", 2, 140)
        where, params = ['m."isActive"'], []
        if f.get("businessId"):
            where.append('m."businessId" = %s')
            params.append(f["businessId"])
        if f.get("roleKey"):
            where.append("r.key = %s")
            params.append(f["roleKey"])
        rows = q(f'''SELECT DISTINCT m."userId", m."businessId" FROM "BusinessMember" m JOIN "Role" r ON r.id = m."roleId" WHERE {" AND ".join(where)}''', *params)
        for r in rows:
            insert("Notification", userId=r["userId"], businessId=r["businessId"], type="ANNOUNCEMENT", title=title,
                   body=(f.get("body") or "")[:2000] or None, link="/notifications")
        audit("ANNOUNCEMENT", "admin", f'Sent announcement "{title}" to {len(rows)} recipient(s)')
        flash(f"Sent to {len(rows)} recipient(s)", "success")
        return redirect(url_for("admin_announcements"))
    return render_template("admin/announcements.html", businesses=q('SELECT id, name FROM "Business" ORDER BY name'),
                           roles=q('SELECT key, name FROM "Role" WHERE "businessId" IS NULL ORDER BY rank DESC'),
                           recent=q('SELECT * FROM "ActivityLog" WHERE action = %s ORDER BY "createdAt" DESC LIMIT 10', "ANNOUNCEMENT"))


# ════════════════════════════════════════════════════════════════════════════
# BUSINESS WORKSPACE — dashboard
# ════════════════════════════════════════════════════════════════════════════

def attendance_report(b: dict, user_ids: list, start: dt.date, end: dt.date):
    """Per user per day: record status, or LEAVE / WEEKEND / ABSENT / UPCOMING / NOT_JOINED when there is no record."""
    if not user_ids:
        return []
    recs = q('SELECT * FROM "Attendance" WHERE "businessId" = %s AND "userId" = ANY(%s) AND date BETWEEN %s AND %s', b["id"], user_ids, start, end)
    leaves = q('''SELECT * FROM "LeaveRequest" WHERE "businessId" = %s AND "userId" = ANY(%s) AND status = 'APPROVED' AND "startDate" <= %s AND "endDate" >= %s''',
               b["id"], user_ids, end, start)
    members = {m["userId"]: m for m in q('''SELECT m."userId", m."joinedAt", u.name, u.email FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId"
                                             WHERE m."businessId" = %s AND m."userId" = ANY(%s)''', b["id"], user_ids)}
    today = business_day(now(), b["timezone"])
    by_key = {(r["userId"], r["date"]): r for r in recs}
    out = []
    for uid in user_ids:
        m = members.get(uid)
        if not m:
            continue
        joined = business_day(m["joinedAt"], b["timezone"])
        totals = dict(present=0, absent=0, half=0, leave=0, late=0, minutes=0)
        cells = []
        for d in each_day(start, end):
            r = by_key.get((uid, d))
            on_leave = any(l["userId"] == uid and l["startDate"] <= d <= l["endDate"] for l in leaves)
            if r and r["checkIn"] and not r["checkOut"] and d == today:
                st = "IN_PROGRESS"
            elif r:
                st = r["status"]
            elif on_leave:
                st = "LEAVE"
            elif d.isoweekday() % 7 not in (b["workDays"] or []):
                st = "WEEKEND"
            elif d >= today:
                st = "UPCOMING"
            elif d < joined:
                st = "NOT_JOINED"
            else:
                st = "ABSENT"
            totals["present"] += st in ("PRESENT", "IN_PROGRESS")
            totals["absent"] += st == "ABSENT"
            totals["half"] += st == "HALF_DAY"
            totals["leave"] += st == "LEAVE"
            totals["late"] += bool(r and r["isLate"])
            totals["minutes"] += (r and r["workMinutes"]) or 0
            cells.append({"date": d, "status": st, "rec": r})
        out.append({"userId": uid, "name": m["name"], "email": m["email"], "cells": cells, "totals": totals})
    return sorted(out, key=lambda x: x["name"].lower())


def visible_schedules_sql(ctx: Ctx, scope: str):
    """WHERE clause (and params) for schedules the current user may see."""
    base = '"businessId" = %s'
    params = [ctx.business["id"]]
    mine = '("createdById" = %s OR EXISTS (SELECT 1 FROM "ScheduleAssignee" a WHERE a."scheduleId" = "Schedule".id AND a."userId" = %s))'
    params_mine = [ctx.user["id"], ctx.user["id"]]
    if scope == "ALL":
        return f"{base} AND ({mine} OR visibility = 'BUSINESS')", params + params_mine
    dept = ' OR (visibility = \'BUSINESS\' AND department = %s)' if ctx.department else ""
    company = " OR (visibility = 'BUSINESS' AND department IS NULL AND NOT EXISTS (SELECT 1 FROM \"ScheduleAssignee\" a WHERE a.\"scheduleId\" = \"Schedule\".id))"
    return f"{base} AND ({mine}{dept}{company})", params + params_mine + ([ctx.department] if ctx.department else [])


@app.get("/app")
@tenant()
def dashboard():
    ctx = g.ctx
    b, tz = ctx.business, ctx.business["timezone"]
    today = business_day(now(), tz)
    data = {"today": today}
    if ctx.can("employees", "VIEW"):
        data["members"] = q1('SELECT count(*) AS n FROM "BusinessMember" WHERE "businessId" = %s AND "isActive"', b["id"])["n"]
    if ctx.can("attendance", "CREATE"):
        data["my_today"] = q1('SELECT * FROM "Attendance" WHERE "businessId" = %s AND "userId" = %s AND date = %s', b["id"], ctx.user["id"], today)
    if ctx.can("attendance", "VIEW") == "ALL":
        uids = [r["userId"] for r in q('SELECT "userId" FROM "BusinessMember" WHERE "businessId" = %s AND "isActive"', b["id"])]
        week = attendance_report(b, uids, today - dt.timedelta(days=6), today)
        last = [r["cells"][-1] for r in week]
        data["present"] = sum(c["status"] in ("PRESENT", "IN_PROGRESS", "HALF_DAY") for c in last)
        data["late"] = sum(bool(c["rec"] and c["rec"]["isLate"]) for c in last)
        data["team"] = len(week)
        if week:
            labels = [c["date"].strftime("%a") for c in week[0]["cells"]]
            series = {k: [sum(r["cells"][i]["status"] in v for r in week) for i in range(len(labels))]
                      for k, v in {"Present": ("PRESENT", "IN_PROGRESS"), "Half day": ("HALF_DAY",), "Leave": ("LEAVE",), "Absent": ("ABSENT",)}.items()}
            data["chart"] = {"labels": labels, "series": series}
    if ctx.can("leave", "APPROVE") == "ALL":
        data["pending"] = q1('SELECT count(*) AS n FROM "LeaveRequest" WHERE "businessId" = %s AND status = %s AND "userId" <> %s', b["id"], "PENDING", ctx.user["id"])["n"]
        data["pending_label"] = "Leave awaiting approval"
    elif ctx.can("leave", "VIEW"):
        data["pending"] = q1('SELECT count(*) AS n FROM "LeaveRequest" WHERE "businessId" = %s AND status = %s AND "userId" = %s', b["id"], "PENDING", ctx.user["id"])["n"]
        data["pending_label"] = "My pending leave"
    if ctx.can("attendance", "APPROVE") == "ALL":
        data["pending_corr"] = q1('SELECT count(*) AS n FROM "AttendanceCorrection" WHERE "businessId" = %s AND status = %s AND "userId" <> %s', b["id"], "PENDING", ctx.user["id"])["n"]
    if ctx.can("scheduling", "VIEW"):
        where, params = visible_schedules_sql(ctx, "OWN")
        horizon = now() + dt.timedelta(days=7)
        rows = q(f'SELECT * FROM "Schedule" WHERE {where} AND NOT "isCancelled" AND "startAt" <= %s LIMIT 200', *params, horizon)
        data["upcoming"] = sorted(({**s, "occ": o[0]} for s in rows for o in occurrences(s, now(), horizon)), key=lambda x: x["occ"])[:6]
    if ctx.can("activity_logs", "VIEW"):
        own = ctx.can("activity_logs", "VIEW") == "OWN"
        own_sql = 'AND "userId" = %s' if own else ""
        data["logs"] = q(f'SELECT * FROM "ActivityLog" WHERE "businessId" = %s {own_sql} ORDER BY "createdAt" DESC LIMIT 8',
                         *([b["id"], ctx.user["id"]] if own else [b["id"]]))
    custom = [m for m in ctx.modules if m["kind"] == "CUSTOM"]
    if custom:
        conds, params = [], []
        for m in custom:
            if ctx.can(m["key"], "VIEW") == "OWN":
                conds.append('(r."moduleId" = %s AND r."createdById" = %s)')
                params += [m["id"], ctx.user["id"]]
            else:
                conds.append('r."moduleId" = %s')
                params.append(m["id"])
        data["records"] = q(f'''SELECT r.*, u.name AS creator FROM "CustomRecord" r LEFT JOIN "User" u ON u.id = r."createdById"
                                WHERE r."businessId" = %s AND ({" OR ".join(conds)}) ORDER BY r."createdAt" DESC LIMIT 6''', b["id"], *params)
        data["mods_by_id"] = {m["id"]: m for m in custom}
        if any(m["key"] == "tasks" for m in custom):
            own = ctx.can("tasks", "VIEW") == "OWN"
            own_sql = 'AND r."createdById" = %s' if own else ""
            data["open_tasks"] = q1(f'''SELECT count(*) AS n FROM "CustomRecord" r JOIN "Module" m ON m.id = r."moduleId" WHERE r."businessId" = %s AND m.key = 'tasks'
                                        AND COALESCE(r.data->>'status', '') <> 'Done' {own_sql}''',
                                     *([b["id"], ctx.user["id"]] if own else [b["id"]]))["n"]
    data["notifications"] = q('SELECT * FROM "Notification" WHERE "userId" = %s AND ("businessId" = %s OR "businessId" IS NULL) ORDER BY "createdAt" DESC LIMIT 5', ctx.user["id"], b["id"])
    hour = to_local(now(), tz).hour
    data["greeting"] = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
    return render_template("dashboard.html", ctx=ctx, d=data, custom=custom)


# ════════════════════════════════════════════════════════════════════════════
# Employees
# ════════════════════════════════════════════════════════════════════════════

@app.route("/app/employees", methods=["GET", "POST"])
@tenant("employees", "VIEW")
def employees():
    ctx = g.ctx
    bid = ctx.business["id"]
    if request.method == "POST":
        if require("employees", "CREATE") != "ALL":
            raise UserError("You don't have permission to do that", 403)
        f = request.form
        role = role_usable(bid, f.get("roleId"))
        if role["rank"] >= ctx.rank:
            raise UserError("You can only assign roles below your own", 403)
        u = add_member(bid, clean_email(f.get("email")), clean_name(f.get("name")), role["id"], f.get("password") or None, f.get("department"), f.get("jobTitle"))
        audit("CREATE", "employees", f"Added {u['name']} ({u['email']}) as {role['name']}", entity_type="BusinessMember", entity_id=u["id"])
        flash("Team member added", "success")
        return redirect(url_for("employees"))
    dept, search = request.args.get("dept", ""), (request.args.get("q") or "").strip()
    where, params = ['m."businessId" = %s'], [bid]
    if dept:
        where.append("m.department = %s")
        params.append(dept)
    if search:
        where.append("(u.name ILIKE %s OR u.email ILIKE %s OR m.department ILIKE %s OR m.\"jobTitle\" ILIKE %s)")
        params += [f"%{search}%"] * 4
    rows = q(f'''SELECT m.*, u.name, u.email, u."lastLoginAt", r.name AS role, r.rank FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId"
                 JOIN "Role" r ON r.id = m."roleId" WHERE {" AND ".join(where)} ORDER BY u.name''', *params)
    if request.args.get("format") == "csv":
        require("employees", "EXPORT")
        audit("EXPORT", "employees", f"Exported {len(rows)} employees")
        return to_csv_response([{**r, "status": "Active" if r["isActive"] else "Inactive", "joined": r["joinedAt"].date().isoformat()} for r in rows],
                               [("name", "Name"), ("email", "Email"), ("role", "Role"), ("department", "Department"), ("jobTitle", "Job title"),
                                ("status", "Status"), ("joined", "Joined")], "employees.csv")
    departments = [r["department"] for r in q('SELECT DISTINCT department FROM "BusinessMember" WHERE "businessId" = %s AND department IS NOT NULL ORDER BY 1', bid)]
    return render_template("employees.html", ctx=ctx, rows=rows, roles=[r for r in roles_for_business(bid) if r["rank"] < ctx.rank],
                           departments=departments, dept=dept, search=search)


@app.post("/app/employees/<mid>")
@tenant("employees", "VIEW")
def employee_update(mid):
    ctx = g.ctx
    m = q1('SELECT m.*, u.name, r.rank FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId" JOIN "Role" r ON r.id = m."roleId" WHERE m.id = %s', mid)
    if not m or m["businessId"] != ctx.business["id"]:
        abort(404)
    if m["userId"] == ctx.user["id"]:
        raise UserError("You can't change your own membership here")
    if m["rank"] >= ctx.rank:
        raise UserError("You can't manage someone at or above your level", 403)
    f = request.form
    if f.get("action") == "delete":
        if require("employees", "DELETE") != "ALL":
            raise UserError("You don't have permission to do that", 403)
        run('DELETE FROM "BusinessMember" WHERE id = %s', mid)
        run('UPDATE "Session" SET "businessId" = NULL WHERE "userId" = %s AND "businessId" = %s', m["userId"], m["businessId"])
        audit("DELETE", "employees", f"Removed {m['name']} from the team", entity_type="BusinessMember", entity_id=mid)
        flash("Removed", "success")
        return redirect(url_for("employees"))
    if require("employees", "UPDATE") != "ALL":
        raise UserError("You don't have permission to do that", 403)
    if f.get("action") == "toggle":
        update("BusinessMember", mid, isActive=not m["isActive"])
        if m["isActive"]:
            run('UPDATE "Session" SET "businessId" = NULL WHERE "userId" = %s AND "businessId" = %s', m["userId"], m["businessId"])
        audit("DEACTIVATE" if m["isActive"] else "ACTIVATE", "employees", f"{'Deactivated' if m['isActive'] else 'Reactivated'} {m['name']}", entity_type="BusinessMember", entity_id=mid)
    else:
        changes = {"department": f.get("department") or None, "jobTitle": f.get("jobTitle") or None}
        if f.get("roleId") and f["roleId"] != m["roleId"]:
            role = role_usable(m["businessId"], f["roleId"])
            if role["rank"] >= ctx.rank:
                raise UserError("You can only assign roles below your own", 403)
            changes["roleId"] = role["id"]
            notify([m["userId"]], "ROLE_CHANGED", f"Your role is now {role['name']}", link="/app", business_id=m["businessId"])
        update("BusinessMember", mid, **changes)
        audit("PERMISSION_CHANGE" if "roleId" in changes else "UPDATE", "employees", f"Updated {m['name']}", entity_type="BusinessMember", entity_id=mid)
    flash("Saved", "success")
    return redirect(url_for("employees"))


# ════════════════════════════════════════════════════════════════════════════
# Attendance
# ════════════════════════════════════════════════════════════════════════════

def compute_day(b: dict, check_in, check_out):
    late, late_min, work, status = False, 0, 0, "PRESENT"
    if check_in:
        diff = local_minutes(check_in, b["timezone"]) - hhmm(b["workStartTime"])
        if diff > b["lateGraceMinutes"]:
            late, late_min = True, diff
    if check_in and check_out:
        work = max(0, round((check_out - check_in).total_seconds() / 60))
        if work < b["halfDayMinutes"]:
            status = "HALF_DAY"
    return dict(isLate=late, lateMinutes=late_min, workMinutes=work, status=status)


def month_range(ym: str, today: dt.date):
    try:
        y, m = map(int, ym.split("-"))
        start = dt.date(y, m, 1)
    except (ValueError, AttributeError):
        start = today.replace(day=1)
    last = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    return start, min(last, today) if start <= today else start, last


@app.get("/app/attendance")
@tenant("attendance", "VIEW")
def attendance():
    ctx = g.ctx
    b = ctx.business
    today = business_day(now(), b["timezone"])
    team = g.scope == "ALL"
    tab = request.args.get("tab") or ("today" if team else "history")
    my_today = q1('SELECT * FROM "Attendance" WHERE "businessId" = %s AND "userId" = %s AND date = %s', b["id"], ctx.user["id"], today)
    members = q('''SELECT u.id, u.name, m.department FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId" WHERE m."businessId" = %s AND m."isActive" ORDER BY u.name''', b["id"]) if team else []
    departments = sorted({m["department"] for m in members if m["department"]})
    dept, uid = request.args.get("dept", ""), request.args.get("user", "")
    ym = request.args.get("month") or today.strftime("%Y-%m")
    view = {}
    if tab in ("today", "report") and team:
        ids = [m["id"] for m in members if (not dept or m["department"] == dept) and (not uid or m["id"] == uid)]
        if tab == "today":
            view["rows"] = attendance_report(b, ids, today, today)
        else:
            start, end, last = month_range(ym, today)
            view["rows"] = attendance_report(b, ids, start, end)
            view["days"] = list(each_day(start, last))
    elif tab == "history":
        start, end, last = month_range(ym, today)
        rows = attendance_report(b, [ctx.user["id"]], start, end)
        view["me"] = rows[0] if rows else None
    elif tab == "corrections":
        can_approve = ctx.can("attendance", "APPROVE") == "ALL"
        view["corrections"] = q(f'''SELECT c.*, u.name, rv.name AS reviewer FROM "AttendanceCorrection" c JOIN "User" u ON u.id = c."userId"
                                    LEFT JOIN "User" rv ON rv.id = c."reviewerId" WHERE c."businessId" = %s {"" if can_approve else 'AND c."userId" = %s'}
                                    ORDER BY c."createdAt" DESC LIMIT 200''', *([b["id"]] if can_approve else [b["id"], ctx.user["id"]]))
    return render_template("attendance.html", ctx=ctx, tab=tab, team=team, today=today, my_today=my_today, members=members, departments=departments,
                           dept=dept, uid=uid, ym=ym, view=view, can_edit=ctx.can("attendance", "UPDATE") == "ALL",
                           can_approve=ctx.can("attendance", "APPROVE") == "ALL", can_export=bool(ctx.can("attendance", "EXPORT")),
                           can_check=bool(ctx.can("attendance", "CREATE")))


@app.post("/app/attendance/check")
@tenant("attendance", "CREATE")
def attendance_check():
    ctx = g.ctx
    b, t = ctx.business, now()
    day = business_day(t, b["timezone"])
    rec = q1('SELECT * FROM "Attendance" WHERE "businessId" = %s AND "userId" = %s AND date = %s', b["id"], ctx.user["id"], day)
    if request.form.get("type") == "IN":
        if rec and rec["checkIn"]:
            raise UserError("You've already checked in today", 409)
        if rec and rec["status"] == "LEAVE":
            raise UserError("You're on approved leave today")
        c = compute_day(b, t, None)
        if rec:
            rec = update("Attendance", rec["id"], checkIn=t, isLate=c["isLate"], lateMinutes=c["lateMinutes"], status="PRESENT")
        else:
            rec = insert("Attendance", businessId=b["id"], userId=ctx.user["id"], date=day, checkIn=t, isLate=c["isLate"], lateMinutes=c["lateMinutes"])
        audit("CHECK_IN", "attendance", f"{ctx.user['name']} checked in" + (f" ({c['lateMinutes']} min late)" if c["isLate"] else ""), entity_type="Attendance", entity_id=rec["id"])
        msg = "Checked in"
    else:
        if not rec or not rec["checkIn"]:
            raise UserError("You haven't checked in today")
        if rec["checkOut"]:
            raise UserError("You've already checked out today", 409)
        c = compute_day(b, rec["checkIn"], t)
        rec = update("Attendance", rec["id"], checkOut=t, workMinutes=c["workMinutes"], status=c["status"])
        audit("CHECK_OUT", "attendance", f"{ctx.user['name']} checked out after {fmt_minutes(c['workMinutes'])}", entity_type="Attendance", entity_id=rec["id"])
        msg = "Checked out"
    if is_api():
        return jsonify(record={k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in rec.items()})
    flash(msg, "success")
    return redirect(request.referrer or url_for("attendance"))


@app.post("/app/attendance/edit")
@tenant("attendance", "UPDATE")
def attendance_edit():
    ctx = g.ctx
    if g.scope != "ALL":
        abort(403)
    f, b = request.form, ctx.business
    member = q1('SELECT m.*, u.name FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId" WHERE m."businessId" = %s AND m."userId" = %s', b["id"], f.get("userId"))
    if not member:
        abort(403)
    day = parse_day(f.get("date"))
    cin = zoned_to_utc(day, hhmm(f["checkIn"]), b["timezone"]) if f.get("checkIn") else None
    cout = zoned_to_utc(day, hhmm(f["checkOut"]), b["timezone"]) if f.get("checkOut") else None
    if cin and cout and cout <= cin:
        raise UserError("Check-out must be after check-in")
    if cout and not cin:
        raise UserError("A check-out needs a check-in")
    c = compute_day(b, cin, cout)
    status = f.get("status") or (c["status"] if cin else "ABSENT")
    run('''INSERT INTO "Attendance" (id, "businessId", "userId", date, "checkIn", "checkOut", "isLate", "lateMinutes", "workMinutes", status, note, "updatedAt")
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT ("businessId", "userId", date) DO UPDATE SET "checkIn" = EXCLUDED."checkIn",
           "checkOut" = EXCLUDED."checkOut", "isLate" = EXCLUDED."isLate", "lateMinutes" = EXCLUDED."lateMinutes", "workMinutes" = EXCLUDED."workMinutes",
           status = EXCLUDED.status, note = EXCLUDED.note, "updatedAt" = EXCLUDED."updatedAt"''',
        new_id(), b["id"], member["userId"], day, cin, cout, c["isLate"], c["lateMinutes"], c["workMinutes"], status, (f.get("note") or "")[:300] or None, now())
    audit("UPDATE", "attendance", f"Set {member['name']}'s attendance on {day} to {status}", entity_type="Attendance")
    if member["userId"] != ctx.user["id"]:
        notify([member["userId"]], "ATTENDANCE_UPDATED", f"Your attendance for {day} was updated", f"Marked {status.replace('_', ' ').lower()} by {ctx.user['name']}",
               "/app/attendance", b["id"])
    flash("Attendance updated", "success")
    return redirect(request.referrer or url_for("attendance"))


@app.get("/app/attendance/export")
@tenant("attendance", "EXPORT")
def attendance_export():
    ctx = g.ctx
    b = ctx.business
    today = business_day(now(), b["timezone"])
    start, end, _ = month_range(request.args.get("month") or today.strftime("%Y-%m"), today)
    if ctx.can("attendance", "VIEW") == "ALL":
        ids = [m["userId"] for m in q('SELECT "userId" FROM "BusinessMember" WHERE "businessId" = %s AND "isActive"', b["id"])
               if not request.args.get("user") or m["userId"] == request.args["user"]]
    else:
        ids = [ctx.user["id"]]
    rows = []
    t = lambda d: to_local(d, b["timezone"]).strftime("%H:%M") if d else ""
    for r in attendance_report(b, ids, start, end):
        for c in r["cells"]:
            if c["status"] in ("UPCOMING", "NOT_JOINED"):
                continue
            rec = c["rec"] or {}
            rows.append({"date": c["date"].isoformat(), "name": r["name"], "email": r["email"], "status": c["status"], "in": t(rec.get("checkIn")),
                         "out": t(rec.get("checkOut")), "late": f"{rec['lateMinutes']} min" if rec.get("isLate") else "",
                         "worked": fmt_minutes(rec["workMinutes"]) if rec.get("workMinutes") else ""})
    audit("EXPORT", "attendance", f"Exported attendance {start} → {end}")
    return to_csv_response(rows, [("date", "Date"), ("name", "Employee"), ("email", "Email"), ("status", "Status"), ("in", "Check in"),
                                  ("out", "Check out"), ("late", "Late by"), ("worked", "Worked")], "attendance.csv")


@app.post("/app/attendance/corrections")
@tenant("attendance", "CREATE")
def correction_create():
    ctx = g.ctx
    f, b = request.form, ctx.business
    day = parse_day(f.get("date"))
    if day > business_day(now(), b["timezone"]):
        raise UserError("You can't request corrections for future dates")
    if not f.get("checkIn") and not f.get("checkOut"):
        raise UserError("Provide a check-in and/or check-out time")
    reason = (f.get("reason") or "").strip()
    if len(reason) < 5:
        raise UserError("Please explain the correction")
    cin = zoned_to_utc(day, hhmm(f["checkIn"]), b["timezone"]) if f.get("checkIn") else None
    cout = zoned_to_utc(day, hhmm(f["checkOut"]), b["timezone"]) if f.get("checkOut") else None
    if cin and cout and cout <= cin:
        raise UserError("Check-out must be after check-in")
    if q1('SELECT 1 FROM "AttendanceCorrection" WHERE "businessId" = %s AND "userId" = %s AND date = %s AND status = %s', b["id"], ctx.user["id"], day, "PENDING"):
        raise UserError("You already have a pending correction for that day")
    c = insert("AttendanceCorrection", businessId=b["id"], userId=ctx.user["id"], date=day, requestedCheckIn=cin, requestedCheckOut=cout, reason=reason[:500])
    audit("CREATE", "attendance", f"{ctx.user['name']} requested an attendance correction for {day}", entity_type="AttendanceCorrection", entity_id=c["id"])
    notify([u for u in users_with_permission(b["id"], "attendance", "APPROVE") if u != ctx.user["id"]], "CORRECTION_REQUESTED",
           f"{ctx.user['name']} requested an attendance correction", f"{day}: {reason}", "/app/attendance?tab=corrections", b["id"])
    flash("Request sent for approval", "success")
    return redirect(url_for("attendance", tab="corrections"))


@app.post("/app/attendance/corrections/<cid>")
@tenant("attendance", "VIEW")
def correction_review(cid):
    ctx = g.ctx
    b = ctx.business
    c = q1('SELECT c.*, u.name FROM "AttendanceCorrection" c JOIN "User" u ON u.id = c."userId" WHERE c.id = %s', cid)
    if not c or c["businessId"] != b["id"]:
        abort(404)
    if c["status"] != "PENDING":
        raise UserError("This request has already been handled")
    action, note = request.form.get("action"), (request.form.get("note") or "")[:300] or None
    if action == "CANCEL":
        if c["userId"] != ctx.user["id"]:
            abort(403)
        run('UPDATE "AttendanceCorrection" SET status = %s WHERE id = %s', "CANCELLED", cid)
        flash("Request cancelled", "success")
        return redirect(url_for("attendance", tab="corrections"))
    if ctx.can("attendance", "APPROVE") != "ALL":
        abort(403)
    if c["userId"] == ctx.user["id"] and not ctx.full:
        raise UserError("You can't approve your own request", 403)
    with tx():
        run('UPDATE "AttendanceCorrection" SET status = %s, "reviewerId" = %s, "reviewNote" = %s, "reviewedAt" = %s WHERE id = %s',
            "APPROVED" if action == "APPROVE" else "REJECTED", ctx.user["id"], note, now(), cid)
        if action == "APPROVE":
            ex = q1('SELECT * FROM "Attendance" WHERE "businessId" = %s AND "userId" = %s AND date = %s', b["id"], c["userId"], c["date"])
            cin = c["requestedCheckIn"] or (ex and ex["checkIn"])
            cout = c["requestedCheckOut"] or (ex and ex["checkOut"])
            d = compute_day(b, cin, cout)
            run('''INSERT INTO "Attendance" (id, "businessId", "userId", date, "checkIn", "checkOut", "isLate", "lateMinutes", "workMinutes", status, note, "updatedAt")
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT ("businessId", "userId", date) DO UPDATE SET "checkIn" = EXCLUDED."checkIn",
                   "checkOut" = EXCLUDED."checkOut", "isLate" = EXCLUDED."isLate", "lateMinutes" = EXCLUDED."lateMinutes", "workMinutes" = EXCLUDED."workMinutes",
                   status = EXCLUDED.status, note = EXCLUDED.note, "updatedAt" = EXCLUDED."updatedAt"''',
                new_id(), b["id"], c["userId"], c["date"], cin, cout, d["isLate"], d["lateMinutes"], d["workMinutes"], d["status"] if cin else "ABSENT",
                f"Corrected: {c['reason']}"[:300], now())
    verb = "approved" if action == "APPROVE" else "rejected"
    audit("APPROVE" if action == "APPROVE" else "REJECT", "attendance", f"{verb.capitalize()} {c['name']}'s correction for {c['date']}", entity_type="AttendanceCorrection", entity_id=cid)
    notify([c["userId"]], f"CORRECTION_{verb.upper()}", f"Attendance correction {verb}", f"{c['date']}" + (f" — {note}" if note else ""), "/app/attendance", b["id"])
    flash(f"Correction {verb}", "success")
    return redirect(url_for("attendance", tab="corrections"))


# ════════════════════════════════════════════════════════════════════════════
# Leave
# ════════════════════════════════════════════════════════════════════════════

@app.route("/app/leave", methods=["GET", "POST"])
@tenant("leave", "VIEW")
def leave():
    ctx = g.ctx
    b = ctx.business
    if request.method == "POST":
        require("leave", "CREATE")
        f = request.form
        if f.get("type") not in LEAVE_TYPES:
            raise UserError("Choose a leave type")
        half = bool(f.get("halfDay"))
        start = parse_day(f.get("startDate"))
        end = start if half else parse_day(f.get("endDate"))
        if end < start:
            raise UserError("End date must be on or after the start date")
        reason = (f.get("reason") or "").strip()
        if len(reason) < 3:
            raise UserError("Please give a reason")
        if q1('''SELECT 1 FROM "LeaveRequest" WHERE "businessId" = %s AND "userId" = %s AND status IN ('PENDING', 'APPROVED') AND "startDate" <= %s AND "endDate" >= %s''',
              b["id"], ctx.user["id"], end, start):
            raise UserError("This overlaps with another leave request")
        l = insert("LeaveRequest", businessId=b["id"], userId=ctx.user["id"], type=f["type"], startDate=start, endDate=end, halfDay=half, reason=reason[:1000])
        audit("CREATE", "leave", f"{ctx.user['name']} requested {f['type']} leave {start} → {end}", entity_type="LeaveRequest", entity_id=l["id"])
        notify([u for u in users_with_permission(b["id"], "leave", "APPROVE") if u != ctx.user["id"]], "LEAVE_REQUESTED",
               f"{ctx.user['name']} requested leave", f"{f['type']}: {start} → {end}", "/app/leave", b["id"])
        flash("Leave requested", "success")
        return redirect(url_for("leave"))
    can_approve = ctx.can("leave", "APPROVE") == "ALL"
    status = request.args.get("status", "PENDING" if can_approve else "")
    mine = g.scope == "OWN" or request.args.get("mine") == "1"
    where, params = ['l."businessId" = %s'], [b["id"]]
    if mine:
        where.append('l."userId" = %s')
        params.append(ctx.user["id"])
    if status:
        where.append("l.status = %s")
        params.append(status)
    rows = q(f'''SELECT l.*, u.name, rv.name AS reviewer FROM "LeaveRequest" l JOIN "User" u ON u.id = l."userId" LEFT JOIN "User" rv ON rv.id = l."reviewerId"
                 WHERE {" AND ".join(where)} ORDER BY l."createdAt" DESC LIMIT 500''', *params)
    if request.args.get("format") == "csv":
        require("leave", "EXPORT")
        return to_csv_response([{**r, "start": r["startDate"].isoformat(), "end": r["endDate"].isoformat(), "half": "Yes" if r["halfDay"] else ""} for r in rows],
                               [("name", "Employee"), ("type", "Type"), ("start", "From"), ("end", "To"), ("half", "Half day"), ("status", "Status"),
                                ("reason", "Reason"), ("reviewer", "Reviewed by")], "leave.csv")
    return render_template("leave.html", ctx=ctx, rows=rows, status=status, mine=mine, can_approve=can_approve, team=g.scope == "ALL",
                           can_create=bool(ctx.can("leave", "CREATE")), can_export=bool(ctx.can("leave", "EXPORT")))


@app.post("/app/leave/<lid>")
@tenant("leave", "VIEW")
def leave_review(lid):
    ctx = g.ctx
    b = ctx.business
    l = q1('SELECT l.*, u.name FROM "LeaveRequest" l JOIN "User" u ON u.id = l."userId" WHERE l.id = %s', lid)
    if not l or l["businessId"] != b["id"]:
        abort(404)
    action, note = request.form.get("action"), (request.form.get("note") or "")[:300] or None
    if action == "CANCEL":
        if l["userId"] != ctx.user["id"]:
            abort(403)
        if l["status"] != "PENDING":
            raise UserError("Only pending requests can be cancelled")
        run('UPDATE "LeaveRequest" SET status = %s WHERE id = %s', "CANCELLED", lid)
        audit("UPDATE", "leave", f"{ctx.user['name']} cancelled a leave request", entity_type="LeaveRequest", entity_id=lid)
        flash("Request cancelled", "success")
        return redirect(url_for("leave"))
    if ctx.can("leave", "APPROVE") != "ALL":
        abort(403)
    if l["status"] != "PENDING":
        raise UserError("This request has already been handled")
    if l["userId"] == ctx.user["id"] and not ctx.full:
        raise UserError("You can't approve your own leave", 403)
    with tx():
        run('UPDATE "LeaveRequest" SET status = %s, "reviewerId" = %s, "reviewNote" = %s, "reviewedAt" = %s WHERE id = %s',
            "APPROVED" if action == "APPROVE" else "REJECTED", ctx.user["id"], note, now(), lid)
        if action == "APPROVE" and not l["halfDay"]:
            for d in each_day(l["startDate"], l["endDate"]):
                if d.isoweekday() % 7 not in (b["workDays"] or []):
                    continue
                ex = q1('SELECT * FROM "Attendance" WHERE "businessId" = %s AND "userId" = %s AND date = %s', b["id"], l["userId"], d)
                if ex and ex["checkIn"]:
                    continue
                if ex:
                    update("Attendance", ex["id"], status="LEAVE", note=f"{l['type']} leave")
                else:
                    insert("Attendance", businessId=b["id"], userId=l["userId"], date=d, status="LEAVE", note=f"{l['type']} leave")
    verb = "approved" if action == "APPROVE" else "rejected"
    audit("APPROVE" if action == "APPROVE" else "REJECT", "leave", f"{verb.capitalize()} {l['name']}'s {l['type']} leave ({l['startDate']} → {l['endDate']})", entity_type="LeaveRequest", entity_id=lid)
    notify([l["userId"]], f"LEAVE_{verb.upper()}", f"Your leave request was {verb}", f"{l['type']}: {l['startDate']} → {l['endDate']}" + (f" — {note}" if note else ""), "/app/leave", b["id"])
    flash(f"Leave {verb}", "success")
    return redirect(url_for("leave"))


# ════════════════════════════════════════════════════════════════════════════
# Scheduling (page + JSON API used by the calendar)
# ════════════════════════════════════════════════════════════════════════════

@app.get("/app/schedule")
@tenant("scheduling", "VIEW")
def schedule():
    ctx = g.ctx
    members = q('''SELECT u.id, u.name, m.department FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId" WHERE m."businessId" = %s AND m."isActive" ORDER BY u.name''', ctx.business["id"])
    return render_template("schedule.html", ctx=ctx, members=members, departments=sorted({m["department"] for m in members if m["department"]}),
                           perms={"view": g.scope, "create": bool(ctx.can("scheduling", "CREATE")), "update": ctx.can("scheduling", "UPDATE"), "delete": ctx.can("scheduling", "DELETE")})


def _iso(d):
    return d.replace(tzinfo=dt.timezone.utc).isoformat() if d else None


@app.get("/api/schedules")
@tenant("scheduling", "VIEW")
def api_schedules():
    ctx = g.ctx
    try:
        start = dt.datetime.fromisoformat(request.args["from"].replace("Z", "+00:00")).astimezone(dt.timezone.utc).replace(tzinfo=None)
        end = dt.datetime.fromisoformat(request.args["to"].replace("Z", "+00:00")).astimezone(dt.timezone.utc).replace(tzinfo=None)
    except (KeyError, ValueError):
        raise UserError("Invalid range")
    if (end - start).days > 400:
        raise UserError("Range too large")
    only_mine = request.args.get("mine") == "1"
    where, params = visible_schedules_sql(ctx, "OWN" if only_mine else g.scope)
    if only_mine:
        where += ' AND ("createdById" = %s OR EXISTS (SELECT 1 FROM "ScheduleAssignee" a WHERE a."scheduleId" = "Schedule".id AND a."userId" = %s))'
        params += [ctx.user["id"], ctx.user["id"]]
    rows = q(f'''SELECT s.*, u.name AS creator FROM "Schedule" s LEFT JOIN "User" u ON u.id = s."createdById"
                 WHERE {where.replace('"Schedule".id', 's.id').replace('"businessId"', 's."businessId"').replace('"createdById"', 's."createdById"')}
                 AND NOT s."isCancelled" AND s."startAt" <= %s AND (s.recurrence <> 'NONE' OR s."endAt" >= %s)
                 AND (s."recurrenceUntil" IS NULL OR s."recurrenceUntil" >= %s) LIMIT 2000''', *params, end, start, start)
    assignees = {}
    if rows:
        for a in q('SELECT a."scheduleId", u.id, u.name FROM "ScheduleAssignee" a JOIN "User" u ON u.id = a."userId" WHERE a."scheduleId" = ANY(%s)', [r["id"] for r in rows]):
            assignees.setdefault(a["scheduleId"], []).append({"id": a["id"], "name": a["name"]})
    events = []
    for s in rows:
        for o_start, o_end in occurrences(s, start, end):
            events.append({
                "id": f"{s['id']}:{o_start.isoformat()}", "scheduleId": s["id"], "title": s["title"], "start": _iso(o_start), "end": _iso(o_end),
                "allDay": s["allDay"], "color": s["color"], "category": s["category"], "location": s["location"], "description": s["description"],
                "recurrence": s["recurrence"], "recurrenceUntil": _iso(s["recurrenceUntil"]), "visibility": s["visibility"], "department": s["department"],
                "reminderMinutes": s["reminderMinutes"], "seriesStart": _iso(s["startAt"]), "seriesEnd": _iso(s["endAt"]),
                "createdBy": {"id": s["createdById"], "name": s["creator"]}, "assignees": assignees.get(s["id"], []),
                "canEdit": ctx.can("scheduling", "UPDATE") == "ALL" or (ctx.can("scheduling", "UPDATE") == "OWN" and s["createdById"] == ctx.user["id"]),
                "canDelete": ctx.can("scheduling", "DELETE") == "ALL" or (ctx.can("scheduling", "DELETE") == "OWN" and s["createdById"] == ctx.user["id"]),
            })
    return jsonify(events=events)


def _schedule_input(data: dict, tz: str):
    title = (data.get("title") or "").strip()
    if not title or len(title) > 140:
        raise UserError("Title is required (max 140 characters)", 422)
    try:
        start = dt.datetime.fromisoformat(data["startAt"].replace("Z", "+00:00")).astimezone(dt.timezone.utc).replace(tzinfo=None)
        end = dt.datetime.fromisoformat(data["endAt"].replace("Z", "+00:00")).astimezone(dt.timezone.utc).replace(tzinfo=None)
        until = dt.datetime.fromisoformat(data["recurrenceUntil"].replace("Z", "+00:00")).astimezone(dt.timezone.utc).replace(tzinfo=None) if data.get("recurrenceUntil") else None
    except (KeyError, ValueError, AttributeError):
        raise UserError("Invalid date/time", 422)
    if end < start:
        raise UserError("End must be after start", 422)
    color = data.get("color") if re.fullmatch(r"#[0-9a-fA-F]{6}", data.get("color") or "") else "#6366f1"
    return dict(title=title, description=(data.get("description") or "")[:2000] or None, location=(data.get("location") or "")[:200] or None,
                category=(data.get("category") or "Meeting")[:40], color=color, startAt=start, endAt=end, allDay=bool(data.get("allDay")),
                recurrence=data.get("recurrence") if data.get("recurrence") in ("NONE", "DAILY", "WEEKLY", "MONTHLY") else "NONE",
                recurrenceUntil=until, visibility="PERSONAL" if data.get("visibility") == "PERSONAL" else "BUSINESS",
                department=(data.get("department") or None), reminderMinutes=int(data["reminderMinutes"]) if str(data.get("reminderMinutes") or "").isdigit() else None)


def _valid_assignees(bid, ids):
    if not ids:
        return []
    return [r["userId"] for r in q('SELECT "userId" FROM "BusinessMember" WHERE "businessId" = %s AND "isActive" AND "userId" = ANY(%s)', bid, list(ids))]


def _dept_members(bid, dept):
    if not dept:
        return []
    return [r["userId"] for r in q('SELECT "userId" FROM "BusinessMember" WHERE "businessId" = %s AND department = %s AND "isActive"', bid, dept)]


@app.post("/api/schedules")
@tenant("scheduling", "CREATE")
def api_schedule_create():
    ctx = g.ctx
    data = request.get_json(silent=True) or {}
    s_in = _schedule_input(data, ctx.business["timezone"])
    bid = ctx.business["id"]
    assignees = _valid_assignees(bid, data.get("assigneeIds") or [])
    with tx():
        s = insert("Schedule", businessId=bid, createdById=ctx.user["id"], **s_in)
        for uid in assignees:
            insert("ScheduleAssignee", scheduleId=s["id"], userId=uid)
    audit("CREATE", "scheduling", f'Created schedule "{s["title"]}"', entity_type="Schedule", entity_id=s["id"], metadata={"assignees": len(assignees), "department": s["department"]})
    when = to_local(s["startAt"], ctx.business["timezone"]).strftime("%d %b %Y" + ("" if s["allDay"] else ", %H:%M"))
    notify([u for u in assignees + _dept_members(bid, s["department"]) if u != ctx.user["id"]], "SCHEDULE_ASSIGNED", f"New schedule: {s['title']}",
           when + (f" · {s['location']}" if s["location"] else ""), "/app/schedule", bid)
    return jsonify(id=s["id"]), 201


@app.route("/api/schedules/<sid>", methods=["POST", "DELETE"])
@tenant("scheduling", "VIEW")
def api_schedule_change(sid):
    ctx = g.ctx
    bid = ctx.business["id"]
    s = q1('SELECT * FROM "Schedule" WHERE id = %s', sid)
    if not s or s["businessId"] != bid:
        abort(404)
    old_assignees = [r["userId"] for r in q('SELECT "userId" FROM "ScheduleAssignee" WHERE "scheduleId" = %s', sid)]
    if request.method == "DELETE":
        scope = require("scheduling", "DELETE")
        if scope == "OWN" and s["createdById"] != ctx.user["id"]:
            abort(404)
        run('DELETE FROM "Schedule" WHERE id = %s', sid)
        audit("DELETE", "scheduling", f'Deleted schedule "{s["title"]}"', entity_type="Schedule", entity_id=sid)
        notify([u for u in old_assignees if u != ctx.user["id"]], "SCHEDULE_CANCELLED", f"Removed: {s['title']}", link="/app/schedule", business_id=bid)
        return jsonify(ok=True)
    scope = require("scheduling", "UPDATE")
    if scope == "OWN" and s["createdById"] != ctx.user["id"]:
        abort(404)
    data = request.get_json(silent=True) or {}
    if data.get("cancel"):
        update("Schedule", sid, isCancelled=True)
        audit("CANCEL", "scheduling", f'Cancelled schedule "{s["title"]}"', entity_type="Schedule", entity_id=sid)
        notify([u for u in old_assignees + _dept_members(bid, s["department"]) if u != ctx.user["id"]], "SCHEDULE_CANCELLED", f"Cancelled: {s['title']}", link="/app/schedule", business_id=bid)
        return jsonify(ok=True)
    s_in = _schedule_input(data, ctx.business["timezone"])
    assignees = _valid_assignees(bid, data.get("assigneeIds") or [])
    with tx():
        update("Schedule", sid, **s_in)
        run('DELETE FROM "ScheduleAssignee" WHERE "scheduleId" = %s', sid)
        for uid in assignees:
            insert("ScheduleAssignee", scheduleId=sid, userId=uid)
    audit("UPDATE", "scheduling", f'Updated schedule "{s_in["title"]}"', entity_type="Schedule", entity_id=sid)
    notify([u for u in set(assignees + old_assignees + _dept_members(bid, s_in["department"])) if u != ctx.user["id"]], "SCHEDULE_UPDATED",
           f"Updated: {s_in['title']}", link="/app/schedule", business_id=bid)
    return jsonify(ok=True)


@app.get("/api/schedules/availability")
@tenant("scheduling", "VIEW")
def api_availability():
    ctx = g.ctx
    if g.scope != "ALL":
        abort(403)
    b = ctx.business
    day = parse_day(request.args.get("date") or business_day(now(), b["timezone"]).isoformat())
    start = zoned_to_utc(day, 0, b["timezone"])
    end = start + dt.timedelta(days=1)
    members = q('''SELECT u.id, u.name, m.department FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId" WHERE m."businessId" = %s AND m."isActive" ORDER BY u.name''', b["id"])
    scheds = q('SELECT * FROM "Schedule" WHERE "businessId" = %s AND NOT "isCancelled" AND "startAt" <= %s', b["id"], end)
    links = {}
    for a in q('SELECT "scheduleId", "userId" FROM "ScheduleAssignee" a JOIN "Schedule" s ON s.id = a."scheduleId" WHERE s."businessId" = %s', b["id"]):
        links.setdefault(a["scheduleId"], set()).add(a["userId"])
    leaves = {l["userId"]: l["type"] for l in q('''SELECT "userId", type FROM "LeaveRequest" WHERE "businessId" = %s AND status = 'APPROVED' AND "startDate" <= %s AND "endDate" >= %s''', b["id"], day, day)}
    people = []
    for m in members:
        ev = []
        for s in scheds:
            if m["id"] in links.get(s["id"], set()) or (s["department"] and s["department"] == m["department"]):
                for o_start, _ in occurrences(s, start, end - dt.timedelta(seconds=1)):
                    ev.append({"title": s["title"], "time": "All day" if s["allDay"] else to_local(o_start, b["timezone"]).strftime("%H:%M"), "color": s["color"]})
        people.append({"id": m["id"], "name": m["name"], "onLeave": leaves.get(m["id"]), "events": ev})
    return jsonify(people=people)


# ════════════════════════════════════════════════════════════════════════════
# Dynamic modules (custom records)
# ════════════════════════════════════════════════════════════════════════════

def load_custom(key: str):
    ctx = load_ctx()
    m = q1('SELECT * FROM "Module" WHERE key = %s', key)
    if not m or m["kind"] != "CUSTOM" or not ctx.has(key):
        abort(404)
    m["name"] = ctx.name(key, m["name"])
    m["description"] = (ctx.module(key) or {}).get("description") or m["description"]
    fields = q('SELECT * FROM "ModuleField" WHERE "moduleId" = %s ORDER BY "sortOrder"', m["id"])
    return m, fields


def validate_record(ctx: Ctx, fields, form, files, existing: dict | None = None):
    """Server-side validation driven by the module's field definitions. Unknown inputs are ignored."""
    data, errors, uploads = {}, {}, []
    for f in fields:
        k, t, label = f["key"], f["type"], f["label"]
        if t == "CHECKBOX":
            data[k] = form.get(k) in ("on", "1", "true")
            continue
        if t == "FILE":
            keep = set(form.getlist(f"{k}__keep"))
            prev = [x for x in ((existing or {}).get(k) or []) if x["id"] in keep]
            new = [fs for fs in files.getlist(k) if fs and fs.filename]
            for fs in new:
                if not ALLOWED_MIME.match(fs.mimetype or ""):
                    errors[k] = f"{fs.filename}: file type not allowed"
            if f["required"] and not prev and not new:
                errors[k] = f"{label} is required"
            if len(prev) + len(new) > 10:
                errors[k] = "Up to 10 files"
            data[k] = prev
            uploads += [(k, fs) for fs in new]
            continue
        v = (form.get(k) or "").strip()
        if not v:
            if f["required"]:
                errors[k] = f"{label} is required"
            continue
        if t == "NUMBER":
            try:
                n = float(v)
                data[k] = int(n) if n.is_integer() else n
            except ValueError:
                errors[k] = "Must be a number"
        elif t == "DATE" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            errors[k] = "Invalid date"
        elif t == "TIME" and not re.fullmatch(r"\d{2}:\d{2}", v):
            errors[k] = "Invalid time"
        elif t == "DATETIME" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}.*", v):
            errors[k] = "Invalid date/time"
        elif t == "DROPDOWN" and f["options"] and v not in f["options"]:
            errors[k] = "Choose an option"
        elif t == "EMAIL" and not EMAIL_RE.match(v):
            errors[k] = "Invalid email"
        elif t == "PHONE" and not re.fullmatch(r"[+()\d\s.\-]{5,25}", v):
            errors[k] = "Invalid phone number"
        elif len(v) > (10000 if t == "TEXTAREA" else 1000):
            errors[k] = "Too long"
        else:
            data[k] = v
    if errors:
        raise UserError("; ".join(f"{next(x['label'] for x in fields if x['key'] == k)}: {e}" if not e.startswith(next(x['label'] for x in fields if x['key'] == k)) else e for k, e in errors.items()), 422)
    for k, fs in uploads:
        data[k].append(save_upload(ctx.business["id"], fs))
    return data


def record_filters(ctx: Ctx, m: dict, scope: str):
    a = request.args
    where, params = ['r."businessId" = %s', 'r."moduleId" = %s'], [ctx.business["id"], m["id"]]
    if scope == "OWN":
        where.append('r."createdById" = %s')
        params.append(ctx.user["id"])
    if a.get("q"):
        where.append("r.data::text ILIKE %s")
        params.append(f"%{a['q']}%")
    if a.get("field") and a.get("value") and re.fullmatch(r"[a-z][a-z0-9_]*", a["field"]):
        where.append("r.data->>%s = %s")
        params += [a["field"], a["value"]]
    if a.get("createdBy"):
        where.append('r."createdById" = %s')
        params.append(a["createdBy"])
    if a.get("from"):
        where.append('r."createdAt" >= %s')
        params.append(parse_day(a["from"]))
    if a.get("to"):
        where.append('r."createdAt" < %s')
        params.append(parse_day(a["to"]) + dt.timedelta(days=1))
    return " AND ".join(where), params


@app.route("/app/m/<key>", methods=["GET", "POST"])
@tenant()
def records(key):
    ctx = g.ctx
    m, fields = load_custom(key)
    if request.method == "POST":
        require(key, "CREATE")
        data = validate_record(ctx, fields, request.form, request.files)
        with tx():
            r = insert("CustomRecord", businessId=ctx.business["id"], moduleId=m["id"], data=Jsonb(data), createdById=ctx.user["id"], updatedById=ctx.user["id"])
            ids = [x["id"] for f in fields if f["type"] == "FILE" for x in data.get(f["key"], [])]
            if ids:
                run('UPDATE "FileAttachment" SET "recordId" = %s WHERE id = ANY(%s) AND "businessId" = %s', r["id"], ids, ctx.business["id"])
        audit("CREATE", key, f"Added a {m['name']} entry", entity_type=m["name"], entity_id=r["id"])
        flash("Entry added", "success")
        return redirect(url_for("records", key=key))
    scope = ctx.can(key, "VIEW")
    if not scope:
        abort(404)
    where, params = record_filters(ctx, m, scope)
    if request.args.get("format") == "csv":
        require(key, "EXPORT")
        rows = q(f'SELECT r.*, u.name AS creator FROM "CustomRecord" r LEFT JOIN "User" u ON u.id = r."createdById" WHERE {where} ORDER BY r."createdAt" DESC LIMIT 20000', *params)
        out = [{**{f["key"]: _display(r["data"].get(f["key"]), f["type"]) for f in fields}, "_by": r["creator"], "_at": r["createdAt"].isoformat()} for r in rows]
        audit("EXPORT", key, f"Exported {len(out)} {m['name']} entries")
        return to_csv_response(out, [(f["key"], f["label"]) for f in fields] + [("_by", "Created by"), ("_at", "Created at (UTC)")], f"{key}.csv")
    total = q1(f'SELECT count(*) AS n FROM "CustomRecord" r WHERE {where}', *params)["n"]
    page, size, pages = paginate(total, 20)
    rows = q(f'''SELECT r.*, u.name AS creator, uu.name AS updater FROM "CustomRecord" r LEFT JOIN "User" u ON u.id = r."createdById"
                 LEFT JOIN "User" uu ON uu.id = r."updatedById" WHERE {where} ORDER BY r."createdAt" DESC LIMIT %s OFFSET %s''', *params, size, page * size)
    members = q('SELECT u.id, u.name FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId" WHERE m."businessId" = %s ORDER BY u.name', ctx.business["id"]) if scope == "ALL" else []
    return render_template("records.html", ctx=ctx, m=m, fields=fields, rows=rows, total=total, page=page, pages=pages, members=members, scope=scope,
                           perms={"create": bool(ctx.can(key, "CREATE")), "update": ctx.can(key, "UPDATE"), "delete": ctx.can(key, "DELETE"), "export": bool(ctx.can(key, "EXPORT"))})


def _own_record(ctx, key, m, rid, action):
    r = q1('SELECT r.*, u.name AS creator, uu.name AS updater FROM "CustomRecord" r LEFT JOIN "User" u ON u.id = r."createdById" LEFT JOIN "User" uu ON uu.id = r."updatedById" WHERE r.id = %s', rid)
    if not r or r["businessId"] != ctx.business["id"] or r["moduleId"] != m["id"]:
        abort(404)
    scope = require(key, action)
    if scope == "OWN" and r["createdById"] != ctx.user["id"]:
        abort(404)
    return r


@app.route("/app/m/<key>/<rid>", methods=["GET", "POST"])
@tenant()
def record_detail(key, rid):
    ctx = g.ctx
    m, fields = load_custom(key)
    if request.method == "GET":
        r = _own_record(ctx, key, m, rid, "VIEW")
        upd = ctx.can(key, "UPDATE")
        dele = ctx.can(key, "DELETE")
        mine = r["createdById"] == ctx.user["id"]
        return render_template("record.html", ctx=ctx, m=m, fields=fields, r=r, edit=request.args.get("edit") == "1",
                               can_update=upd == "ALL" or (upd == "OWN" and mine), can_delete=dele == "ALL" or (dele == "OWN" and mine))
    if request.form.get("action") == "delete":
        r = _own_record(ctx, key, m, rid, "DELETE")
        files = q('SELECT "storageKey" FROM "FileAttachment" WHERE "recordId" = %s', rid)
        run('DELETE FROM "CustomRecord" WHERE id = %s', rid)
        delete_stored([f["storageKey"] for f in files])
        audit("DELETE", key, f"Deleted a {m['name']} entry", entity_type=m["name"], entity_id=rid, metadata={"data": r["data"]})
        flash("Entry deleted", "success")
        return redirect(url_for("records", key=key))
    r = _own_record(ctx, key, m, rid, "UPDATE")
    data = validate_record(ctx, fields, request.form, request.files, existing=r["data"])
    keep_ids = [x["id"] for f in fields if f["type"] == "FILE" for x in data.get(f["key"], [])]
    removed = q('SELECT id, "storageKey" FROM "FileAttachment" WHERE "recordId" = %s AND NOT (id = ANY(%s))', rid, keep_ids or [""])
    with tx():
        update("CustomRecord", rid, data=Jsonb(data), updatedById=ctx.user["id"])
        if keep_ids:
            run('UPDATE "FileAttachment" SET "recordId" = %s WHERE id = ANY(%s) AND "businessId" = %s', rid, keep_ids, ctx.business["id"])
        if removed:
            run('DELETE FROM "FileAttachment" WHERE id = ANY(%s)', [x["id"] for x in removed])
    delete_stored([x["storageKey"] for x in removed])
    audit("UPDATE", key, f"Updated a {m['name']} entry", entity_type=m["name"], entity_id=rid)
    if r["createdById"] and r["createdById"] != ctx.user["id"]:
        notify([r["createdById"]], "RECORD_UPDATED", f"{ctx.user['name']} updated your {m['name']} entry", link=url_for("record_detail", key=key, rid=rid), business_id=ctx.business["id"])
    flash("Entry updated", "success")
    return redirect(url_for("record_detail", key=key, rid=rid))


@app.get("/files/<fid>")
@tenant()
def file_download(fid):
    ctx = g.ctx
    f = q1('SELECT f.*, r."createdById" AS rec_owner, m.key AS module_key FROM "FileAttachment" f LEFT JOIN "CustomRecord" r ON r.id = f."recordId" LEFT JOIN "Module" m ON m.id = r."moduleId" WHERE f.id = %s', fid)
    if not f or f["businessId"] != ctx.business["id"]:  # other tenants' files look like missing files
        abort(404)
    if f["recordId"]:
        scope = ctx.can(f["module_key"], "VIEW")
        if not scope or (scope == "OWN" and f["rec_owner"] != ctx.user["id"]):
            abort(403)
    elif f["uploadedById"] != ctx.user["id"]:
        abort(403)
    path = (UPLOAD_DIR / f["storageKey"]).resolve()
    if not str(path).startswith(str(UPLOAD_DIR.resolve())) or not path.exists():
        abort(404)
    inline = f["mimeType"].startswith("image/") or f["mimeType"] == "application/pdf"
    resp = send_file(path, mimetype=f["mimeType"], as_attachment=not inline, download_name=f["filename"], max_age=300)
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    return resp


# ════════════════════════════════════════════════════════════════════════════
# Activity logs, search, settings
# ════════════════════════════════════════════════════════════════════════════

@app.get("/app/logs")
@tenant("activity_logs", "VIEW")
def tenant_logs():
    ctx = g.ctx
    base, params = ['"businessId" = %s'], [ctx.business["id"]]
    if g.scope == "OWN":
        base.append('"userId" = %s')
        params.append(ctx.user["id"])
    args = request.args.to_dict()
    args.pop("businessId", None)
    where, params = log_query(base, params)
    users = q('SELECT u.id, u.name FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId" WHERE m."businessId" = %s ORDER BY u.name', ctx.business["id"]) if g.scope == "ALL" else None
    return log_page(dict(title=ctx.name("activity_logs", "Activity logs"), subtitle="Everything that happened in this workspace. Logs are read-only." if g.scope == "ALL" else "Your own activity in this workspace.",
                         users=users, businesses=None, modules=[{"key": m["key"], "name": m["name"]} for m in ctx.modules], endpoint="tenant_logs"),
                    where, params, can_export=bool(ctx.can("activity_logs", "EXPORT")))


@app.get("/api/search")
@tenant()
def api_search():
    ctx = g.ctx
    term = (request.args.get("q") or "").strip()
    if len(term) < 2:
        return jsonify(results=[])
    like, out, bid = f"%{term}%", [], ctx.business["id"]
    if ctx.can("employees", "VIEW"):
        for p in q('''SELECT u.name, u.email, m.department FROM "BusinessMember" m JOIN "User" u ON u.id = m."userId" WHERE m."businessId" = %s AND (u.name ILIKE %s OR u.email ILIKE %s) LIMIT 5''', bid, like, like):
            out.append({"type": "Person", "label": p["name"], "sub": p["department"] or p["email"], "href": url_for("employees", q=p["name"])})
    for m in ctx.modules:
        if m["kind"] != "CUSTOM":
            continue
        scope = ctx.can(m["key"], "VIEW")
        rows = q(f'''SELECT id, data FROM "CustomRecord" WHERE "businessId" = %s AND "moduleId" = %s {'AND "createdById" = %s' if scope == "OWN" else ""}
                     AND data::text ILIKE %s ORDER BY "createdAt" DESC LIMIT 4''', *([bid, m["id"], ctx.user["id"], like] if scope == "OWN" else [bid, m["id"], like]))
        for r in rows:
            label = next((v for v in r["data"].values() if isinstance(v, str) and v), "Entry")
            out.append({"type": m["name"][:14], "label": label, "href": url_for("record_detail", key=m["key"], rid=r["id"])})
    if ctx.can("scheduling", "VIEW") == "ALL":
        for e in q('''SELECT title, "startAt" FROM "Schedule" WHERE "businessId" = %s AND visibility = 'BUSINESS' AND title ILIKE %s ORDER BY "startAt" DESC LIMIT 4''', bid, like):
            out.append({"type": "Event", "label": e["title"], "sub": e["startAt"].strftime("%d %b %Y"), "href": url_for("schedule")})
    return jsonify(results=out)


@app.route("/app/settings", methods=["GET", "POST"])
@tenant()
def settings():
    ctx = g.ctx
    if not ctx.full:
        abort(404)
    b = ctx.business
    if request.method == "POST":
        f = request.form
        tz = f.get("timezone") or "UTC"
        try:
            ZoneInfo(tz)
        except Exception:
            raise UserError("Unknown timezone")
        nb = update("Business", b["id"], name=clean_name(f.get("name"), "Business name", 2, 120), email=f.get("email") or None, phone=f.get("phone") or None,
                    address=f.get("address") or None, industry=f.get("industry") or None, timezone=tz, workStartTime=f.get("workStartTime") or "09:00",
                    lateGraceMinutes=f.get("lateGraceMinutes", 10, type=int), halfDayMinutes=f.get("halfDayMinutes", 240, type=int),
                    workDays=[int(x) for x in f.getlist("workDays")])
        audit("UPDATE", "business", "Updated business settings", business=nb, entity_type="Business", entity_id=b["id"])
        flash("Settings saved", "success")
        return redirect(url_for("settings"))
    plan = q1('SELECT * FROM "Plan" WHERE id = %s', b["planId"]) if b["planId"] else None
    seats = q1('SELECT count(*) AS n FROM "BusinessMember" WHERE "businessId" = %s AND "isActive"', b["id"])["n"]
    return render_template("settings.html", ctx=ctx, b=b, plan=plan, seats=seats)


# ════════════════════════════════════════════════════════════════════════════
# CLI: migrations & seed
# ════════════════════════════════════════════════════════════════════════════

@app.cli.command("migrate")
def migrate_cmd():
    """Apply SQL files in ./migrations that haven't run yet."""
    with app.app_context():
        run('CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT now())')
        applied = {r["name"] for r in q("SELECT name FROM schema_migrations")}
        if not applied and q1("SELECT to_regclass('public._prisma_migrations') AS t")["t"]:
            # Database created by the earlier Prisma version: mark its migrations as done.
            done = {r["migration_name"].split("_", 1)[1] for r in q("SELECT migration_name FROM _prisma_migrations WHERE finished_at IS NOT NULL")}
            for p in sorted((BASE_DIR / "migrations").glob("*.sql")):
                if p.stem.split("_", 1)[1] in done:
                    run("INSERT INTO schema_migrations (name) VALUES (%s) ON CONFLICT DO NOTHING", p.name)
                    applied.add(p.name)
        for p in sorted((BASE_DIR / "migrations").glob("*.sql")):
            if p.name in applied:
                continue
            with tx():
                conn().execute(p.read_text(encoding="utf-8"))
                run("INSERT INTO schema_migrations (name) VALUES (%s)", p.name)
            click.echo(f"applied {p.name}")
        click.echo("Database is up to date.")


@app.cli.command("seed")
def seed_cmd():
    """Idempotent seed: super admin, plans, roles, modules, (demo businesses unless SEED_DEMO=false)."""
    with app.app_context():
        email = os.environ.get("SEED_SUPERADMIN_EMAIL", "admin@orbit.local").lower()
        if not q1('SELECT 1 FROM "User" WHERE email = %s', email):
            insert("User", email=email, name="Platform Admin", isSuperAdmin=True, emailVerifiedAt=now(),
                   passwordHash=hash_password(os.environ.get("SEED_SUPERADMIN_PASSWORD", "Admin@12345")))
        for name, desc, price, seats in (("Free", "Up to 5 users", 0, 5), ("Growth", "Up to 50 users", 4900, 50), ("Enterprise", "Up to 1000 users", 19900, 1000)):
            if not q1('SELECT 1 FROM "Plan" WHERE name = %s', name):
                insert("Plan", name=name, description=desc, priceCents=price, maxUsers=seats)
        for key, name, desc, full, rank in ROLE_TEMPLATES:
            if not q1('SELECT 1 FROM "Role" WHERE "businessId" IS NULL AND key = %s', key):
                insert("Role", key=key, name=name, description=desc, fullAccess=full, rank=rank, isSystem=True)
        for key, name, desc, icon, order, actions in BUILTIN_MODULES:
            m = q1('SELECT * FROM "Module" WHERE key = %s', key) or insert(
                "Module", key=key, name=name, description=desc, icon=icon, sortOrder=order, kind="BUILTIN", isCore=True,
                isMandatory=key == "employees", isDefault=key in DEFAULT_MODULE_KEYS)
            provision_permissions(m["id"], key, actions, custom=False)
        for i, (key, name, desc, icon, fields) in enumerate(CUSTOM_TEMPLATES):
            m = q1('SELECT * FROM "Module" WHERE key = %s', key) or insert(
                "Module", key=key, name=name, description=desc, icon=icon, sortOrder=100 + i, kind="CUSTOM", isDefault=key in DEFAULT_MODULE_KEYS)
            provision_permissions(m["id"], key, CUSTOM_ACTIONS)
            if not q1('SELECT 1 FROM "ModuleField" WHERE "moduleId" = %s', m["id"]):
                for j, (fk, label, ftype, req, opts) in enumerate(fields):
                    insert("ModuleField", moduleId=m["id"], key=fk, label=label, type=ftype, required=req, options=opts, sortOrder=j)
        if os.environ.get("SEED_DEMO", "true").lower() != "false":
            pw = hash_password("Demo@12345")
            growth = q1('SELECT id FROM "Plan" WHERE name = %s', "Growth")
            demos = [
                ("acme-logistics", "Acme Logistics", "Logistics", "Asia/Kolkata", ["attendance", "activity_logs", "scheduling", "leave", "daily_work_report", "visitor_entry"],
                 [("owner@acme.test", "Riya Kapoor", "owner", "Management"), ("manager@acme.test", "Sam Okafor", "manager", "Operations"), ("employee@acme.test", "Lee Chen", "employee", "Operations")]),
                ("bloom-retail", "Bloom Retail", "Retail", "America/New_York", ["inventory", "work_reports", "tasks"],
                 [("owner@bloom.test", "Alex Moreno", "owner", "Management"), ("staff@bloom.test", "Jordan Patel", "employee", "Store")]),
            ]
            for slug, name, industry, tz, mods, people in demos:
                b = q1('SELECT * FROM "Business" WHERE slug = %s', slug) or insert(
                    "Business", slug=slug, name=name, industry=industry, timezone=tz, planId=growth and growth["id"], email=people[0][0])
                for mk in mods:
                    run('''INSERT INTO "BusinessModule" ("businessId", "moduleId") SELECT %s, id FROM "Module" WHERE key = %s ON CONFLICT DO NOTHING''', b["id"], mk)
                for em, nm, rk, dept in people:
                    u = q1('SELECT * FROM "User" WHERE email = %s', em) or insert("User", email=em, name=nm, passwordHash=pw, emailVerifiedAt=now())
                    if not q1('SELECT 1 FROM "BusinessMember" WHERE "businessId" = %s AND "userId" = %s', b["id"], u["id"]):
                        insert("BusinessMember", businessId=b["id"], userId=u["id"], roleId=system_role(rk)["id"], department=dept)
            click.echo("Demo workspaces: owner@acme.test / owner@bloom.test (password Demo@12345)")
        click.echo(f"Seed complete. Super admin: {email}")


if __name__ == "__main__":
    app.run(debug=not IS_PROD)
