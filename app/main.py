import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings
from .db import make_engine, make_session_factory
from .models import RunLog, User
from .security import LoginLimiter, hash_password
from .services import appsettings
from .services.notify import Notifier
from .services.reminders import RunSummary, run_reminders

log = logging.getLogger("marg")
BASE = Path(__file__).parent


def bootstrap_admin(Session, settings: Settings):
    with Session() as db:
        if settings.admin_email and not db.scalar(select(User).where(User.role == "admin")):
            db.add(User(role="admin", name="Admin", email=settings.admin_email.lower(),
                        password_hash=hash_password(settings.admin_password or "admin")))
            db.commit()
            log.warning("Created admin user %s", settings.admin_email)


def alert_admin(app: FastAPI, subject: str, text: str):
    s: Settings = app.state.settings
    if s.admin_email and app.state.notifier.configured("email"):
        try:
            app.state.notifier.send("email", s.admin_email, text, [], subject)
        except Exception:
            log.exception("Could not send admin alert")


def run_daily(app: FastAPI, trigger: str = "auto") -> RunSummary:
    """Run all due reminders, record the run, and email the admin if anything failed."""
    s: Settings = app.state.settings
    today = datetime.now(ZoneInfo(s.timezone)).date()
    with app.state.Session() as db:
        try:
            r = run_reminders(db, s, app.state.notifier, today)
            db.add(RunLog(trigger=trigger, due=r.subscriptions, sent=r.sent, failed=r.failed, summary=r.as_text()))
            db.commit()
        except Exception as e:
            db.rollback()
            db.add(RunLog(trigger=trigger, error=f"{type(e).__name__}: {e}"[:500], summary="Run crashed"))
            db.commit()
            alert_admin(app, "Marg Reminder: run crashed", f"The reminder run crashed: {e}")
            raise
    if r.failed:
        alert_admin(app, f"Marg Reminder: {r.failed} reminder(s) failed",
                    f"{r.as_text()}\nOpen the Reminders page for details. Failed sends are retried automatically.")
    return r


async def scheduler(app: FastAPI):
    """Fully automatic. Ticks every 5 min; runs once a day after REMINDER_HOUR, then retries
    any failures hourly (max 3 extra tries). Re-running is safe: every reminder is de-duplicated in the DB."""
    s: Settings = app.state.settings
    last_date, last_at, retries, last_failed = None, None, 0, False
    while True:
        now = datetime.now(ZoneInfo(s.timezone))
        due = now.hour >= s.reminder_hour and (
            last_date != now.date()
            or (last_failed and retries < 3 and last_at and (now - last_at).total_seconds() >= 3600)
        )
        if due:
            retries = retries + 1 if last_date == now.date() else 0
            try:
                r = await asyncio.to_thread(run_daily, app)
                log.info("Reminder run: %s", r.as_text())
                last_failed = r.failed > 0
            except Exception:
                log.exception("Reminder run failed; will retry")
                last_failed = True
            last_date, last_at = now.date(), now
        await asyncio.sleep(300)


def create_app(settings: Settings | None = None, notifier: Notifier | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    Session = make_session_factory(engine)
    bootstrap_admin(Session, settings)
    with Session() as db:
        appsettings.load_into(settings, db)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(scheduler(app)) if settings.enable_scheduler else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Marg Reminder", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings, app.state.Session = settings, Session
    app.state.notifier = notifier or Notifier(settings)
    app.state.limiter = LoginLimiter()
    app.state.reset_limiter = LoginLimiter(limit=5, window=900)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=settings.cookie_secure,
                       same_site="lax", max_age=60 * 60 * 24 * 14)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    from .routes import router, setup_errors
    app.include_router(router)
    setup_errors(app)
    return app


def app_factory() -> FastAPI:  # uvicorn app.main:app_factory --factory
    logging.basicConfig(level=logging.INFO)
    return create_app()
