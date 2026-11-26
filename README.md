# Marg Reminder

Subscription/renewal tracker for **Marg ERP** distributors. Distributors log in (mobile-friendly web app), record
their buyers and each buyer's Marg subscription expiry, and the system automatically sends reminders by
**WhatsApp, SMS and email** to the **buyer** (one message per subscription) and to the **distributor**
(one daily digest of everything needing attention).

## Features
- Distributor logins (admin creates distributors); each distributor sees only their own customers.
- Customers, subscriptions (edition, licence no., start/expiry, amount), one-tap **Renewed ✓**.
- Bulk import from Excel/CSV (re-import updates by licence no.).
- Daily scheduler, default at 30/15/7/1 days before, on the day, and 7 days after expiry (`REMINDER_DAYS`).
  Each reminder is sent once per renewal cycle; failures retry next run; a missed day catches up.
- Per-customer opt-out, reminder log with delivery status, dashboard (expired / 7 days / 30 days).
- Search + pagination, indexed queries, SQLite WAL (or any SQLAlchemy DB via `DATABASE_URL`).

## Run
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit, then: export $(grep -v '^#' .env | xargs)
uvicorn app.main:app_factory --factory --port 8000
```
Log in with `ADMIN_EMAIL` / `ADMIN_PASSWORD` (created on first start), add distributors, then they add customers.
Docker: `docker build -t marg-reminder . && docker run -p 8000:8000 -v mrdata:/data --env-file .env marg-reminder`.
Run **one worker** (it hosts the scheduler). Put it behind HTTPS and set `COOKIE_SECURE=true`.

## Super Admin settings (no .env editing needed)
Log in as the admin → **Settings**: set Gmail/SMTP, Twilio (or MSG91) SMS, the reminder days and daily send hour, and
send test email/SMS from the page. Values apply instantly (no restart), secrets are stored encrypted in the database,
and `.env` values are only the starting defaults.

## Fully automatic
Nobody has to press anything. After `REMINDER_HOUR` every day the app finds due subscriptions and sends the buyer and
distributor reminders, retries failures hourly (3 times), catches up after downtime, and emails `ADMIN_EMAIL` if a run
fails. Docker `restart: unless-stopped` + healthcheck keep it alive; the Reminders page (admin) shows every run.

## Deploy next to Enquira (VPS with Docker + nginx)
```bash
git clone https://github.com/teamsrs94-cell/marg-reminder /opt/marg-reminder && cd /opt/marg-reminder
bash deploy/install.sh reminder.srs-engineering.in you@example.com   # 1st run creates .env; edit it, run again
```
Add a DNS `A` record for `reminder` first. Runs on port 3200 (Enquira keeps 3100). Update later: `bash deploy/update.sh`.

## Channels (configure in `.env`; unconfigured channels are skipped)
| Channel | Provider | Notes |
|---|---|---|
| WhatsApp | Meta WhatsApp Cloud API | Business-initiated messages need an **approved template** with 5 body variables: `{{1}}` customer, `{{2}}` product, `{{3}}` expiry date, `{{4}}` timing ("expires in 7 days"), `{{5}}` distributor. Distributor digests reuse the same template, so create a second template if you want a different digest wording. |
| SMS | Twilio or MSG91 (`SMS_PROVIDER`) | In India SMS needs a **DLT-registered** template (MSG91 flow: variables `var1..var5` in the same order). |
| Email | any SMTP server | |

Only message customers who have agreed to receive renewal notices; the opt-out checkbox on each customer is honoured.

## Tests
`python -m pytest -q`

## Not included yet
Marg ERP has no public API, so expiry dates are entered/imported. Possible next steps: payment links, per-distributor
templates, renewal history/revenue reports, multi-process scheduler lock.
