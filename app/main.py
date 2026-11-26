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
from .models import User
from .security import LoginLimiter, hash_password
from .services.notify import Notifier
from .services.reminders import run_reminders

log = logging.getLogger("marg")
BASE = Path(__file__).parent


def bootstrap_admin(Session, settings: Settings):
    with Session() as db:
        if settings.admin_email and not db.scalar(select(User).where(User.role == "admin")):
            db.add(User(role="admin", name="Admin", email=settings.admin_email.lower(),
                        password_hash=hash_password(settings.admin_password or "admin")))
            db.commit()
            log.warning("Created admin user %s", settings.admin_email)


def run_daily(app: FastAPI) -> str:
    s: Settings = app.state.settings
    today = datetime.now(ZoneInfo(s.timezone)).date()
    with app.state.Session() as db:
        return run_reminders(db, s, app.state.notifier, today).as_text()


async def scheduler(app: FastAPI):
    """Check every 10 min; run once per day after REMINDER_HOUR. Re-running is safe (dedupe in DB)."""
    s: Settings = app.state.settings
    last = None
    while True:
        now = datetime.now(ZoneInfo(s.timezone))
        if now.hour >= s.reminder_hour and last != now.date():
            try:
                log.info("Reminder run: %s", await asyncio.to_thread(run_daily, app))
                last = now.date()
            except Exception:
                log.exception("Reminder run failed; will retry")
        await asyncio.sleep(600)


def create_app(settings: Settings | None = None, notifier: Notifier | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    Session = make_session_factory(engine)
    bootstrap_admin(Session, settings)

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
