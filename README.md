# Orbit — multi-tenant business management (Flask + HTML)

- `app.py` — the complete Flask backend (auth, super admin, RBAC, modules/form builder, attendance, leave, scheduling, audit logs, notifications, CSV export).
- `templates/` — the HTML pages (Jinja). CSS and JS are inline in `templates/base.html`.
- `migrations/` — PostgreSQL schema (applied by `flask --app app migrate`).

## Run

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
npm run db:start            # optional local Postgres on :5433 (or point DATABASE_URL at your own)
flask --app app migrate
flask --app app seed
flask --app app run --debug # http://127.0.0.1:5000
```

Logins: `admin@orbit.local` / `Admin@12345` (super admin); `owner@acme.test`, `manager@acme.test`, `employee@acme.test`, `owner@bloom.test` / `Demo@12345`.

Emails print to the console unless `SMTP_HOST` is set. Uploaded files go to `./uploads` and are only served through a permission check.
