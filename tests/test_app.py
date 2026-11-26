import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models import Customer, ReminderLog, Subscription, User
from app.services.notify import NotConfigured, Notifier
from app.services.reminders import run_reminders, stage_for
from app.services.util import normalize_phone


class FakeNotifier(Notifier):
    def __init__(self, settings, channels=("whatsapp", "sms", "email"), fail=()):
        super().__init__(settings)
        self.channels, self.fail, self.sent = channels, set(fail), []

    def configured(self, ch):
        return ch in self.channels

    def send(self, ch, to, text, params, subject=""):
        if ch not in self.channels:
            raise NotConfigured(ch)
        self.sent.append((ch, to, text))
        return (ch not in self.fail, "ok" if ch not in self.fail else "boom")


@pytest.fixture
def env(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db", admin_email="admin@x.com", admin_password="adminpass1",
                 enable_scheduler=False, secret_key="k")
    n = FakeNotifier(s)
    app = create_app(s, n)
    return app, TestClient(app), n, s


def csrf_of(client, path="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def login(client, email, pw):
    r = client.post("/login", data={"email": email, "password": pw, "csrf": csrf_of(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def post(client, path, data, page="/"):
    return client.post(path, data={**data, "csrf": csrf_of(client, page)}, follow_redirects=False)


def seed(app, expiry_offset=7, today=date(2026, 11, 26)):
    with app.state.Session() as db:
        d = User(role="distributor", name="Dev", company="Dev Traders", email="d@x.com", phone="9811111111", password_hash="x")
        db.add(d); db.flush()
        c = Customer(distributor_id=d.id, name="Sharma Store", phone="919876543210", email="b@x.com")
        db.add(c); db.flush()
        s = Subscription(customer_id=c.id, product="Marg Silver", expiry_date=today + timedelta(days=expiry_offset))
        db.add(s); db.commit()
        return d.id, c.id, s.id


def test_stage_for():
    o = [30, 15, 7, 1, 0, -7]
    assert stage_for(40, o) is None and stage_for(30, o) == 30 and stage_for(29, o) == 30 and stage_for(14, o) == 15
    assert stage_for(5, o) == 7 and stage_for(0, o) == 0 and stage_for(-3, o) == 0 and stage_for(-7, o) == -7
    assert stage_for(-30, o) is None


def test_phone():
    assert normalize_phone("98765 43210") == "919876543210"
    assert normalize_phone("+91-98765-43210") == "919876543210"
    assert normalize_phone("123") == ""


def test_reminders_send_once_and_digest(env):
    app, _, n, s = env
    seed(app, 7)
    today = date(2026, 11, 26)
    with app.state.Session() as db:
        r = run_reminders(db, s, n, today)
    # buyer: 3 channels, distributor digest: 3 channels
    assert r.sent == 6 and r.failed == 0
    assert sum(1 for c, to, t in n.sent if to == "919876543210") == 2  # whatsapp + sms
    with app.state.Session() as db:
        assert run_reminders(db, s, n, today).sent == 0  # idempotent same day
        assert run_reminders(db, s, n, today + timedelta(days=1)).sent == 0  # same stage (7), still deduped
        assert run_reminders(db, s, n, today + timedelta(days=6)).sent == 6  # stage 1 -> new reminders


def test_failed_is_retried_and_unconfigured_skipped(env):
    app, _, _, s = env
    seed(app, 1)
    n = FakeNotifier(s, channels=("email", "whatsapp"), fail=("whatsapp",))
    today = date(2026, 11, 26)
    with app.state.Session() as db:
        r = run_reminders(db, s, n, today)
        assert r.failed == 2 and r.sent == 2 and "sms" in r.skipped_unconfigured
        n.fail.clear()
        r2 = run_reminders(db, s, n, today)
        assert r2.sent == 2 and r2.failed == 0  # only the failed ones retried


def test_opt_out_and_renewal_new_cycle(env):
    app, _, n, s = env
    _, cid, sid = seed(app, 0)
    today = date(2026, 11, 26)
    with app.state.Session() as db:
        db.get(Customer, cid).notify = False
        db.commit()
        r = run_reminders(db, s, n, today)
        assert all(to != "919876543210" for _, to, _ in n.sent) and r.sent == 3  # distributor only
        sub = db.get(Subscription, sid)
        sub.expiry_date = today + timedelta(days=30)  # renewed -> new cycle, stage 30
        db.get(Customer, cid).notify = True
        db.commit()
        assert run_reminders(db, s, n, today).sent == 6


def test_auth_scoping_and_flow(env):
    app, c, n, s = env
    assert c.get("/", follow_redirects=False).status_code == 303
    login(c, "admin@x.com", "adminpass1")
    r = post(c, "/distributors", {"name": "A", "company": "Alpha", "email": "a@x.com", "phone": "9822222222", "password": "password1"}, "/distributors")
    assert r.status_code == 303
    r = post(c, "/distributors", {"name": "B", "company": "Beta", "email": "b@x.com", "phone": "", "password": "password1"}, "/distributors")
    c.post("/logout", data={"csrf": csrf_of(c)})

    login(c, "a@x.com", "password1")
    r = post(c, "/customers/new", {"name": "Alpha Cust", "phone": "9876500000", "notify": "1"}, "/customers/new")
    assert r.status_code == 303
    cid = int(r.headers["location"].split("/")[2])
    r = post(c, f"/customers/{cid}/subscriptions/new", {"product": "Marg", "expiry_date": (date.today() + timedelta(days=10)).isoformat()}, f"/customers/{cid}/subscriptions/new")
    assert r.status_code == 303
    assert "Alpha Cust" in c.get("/subscriptions").text and "Alpha Cust" in c.get("/").text
    r = post(c, f"/customers/{cid}/subscriptions/new", {"expiry_date": "bad"}, f"/customers/{cid}/subscriptions/new")
    assert r.status_code == 200 and "valid expiry" in r.text
    c.post("/logout", data={"csrf": csrf_of(c)})

    login(c, "b@x.com", "password1")  # other distributor can't see it
    assert c.get(f"/customers/{cid}").status_code == 404
    assert "Alpha Cust" not in c.get("/customers").text
    assert c.get("/distributors").status_code == 403
    assert c.post("/customers/new", data={"name": "x"}).status_code == 400  # CSRF enforced


def test_renew_and_import(env):
    app, c, n, s = env
    _, cid, sid = seed(app, -10)
    with app.state.Session() as db:
        d = db.get(Customer, cid).distributor
        d.password_hash = __import__("app.security", fromlist=["x"]).hash_password("password1")
        db.commit()
    login(c, "d@x.com", "password1")
    assert post(c, f"/subscriptions/{sid}/renew", {}, "/subscriptions").status_code == 303
    with app.state.Session() as db:
        assert db.get(Subscription, sid).expiry_date > date.today() - timedelta(days=1)
    csv = ("customer,phone,email,product,license_no,expiry_date,amount\n"
           "Gupta Pharma,98765 11111,g@x.com,Marg Gold,L1,31-03-2027,\"12,000\"\n"
           ",1,,,,,\nBad Date,9,,,L2,notadate,\n")
    r = c.post("/import", data={"csrf": csrf_of(c, "/import")}, files={"file": ("a.csv", csv, "text/csv")})
    assert r.status_code == 200 and "1 customers added" in r.text and "2 issue" in r.text.replace("(s)", "")
    with app.state.Session() as db:
        sub = db.scalar(select(Subscription).where(Subscription.license_no == "L1"))
        assert sub.expiry_date == date(2027, 3, 31) and float(sub.amount) == 12000


def test_run_daily_logs_and_alerts_admin(env):
    from app.main import run_daily
    from app.models import RunLog

    app, c, _, s = env
    assert c.get("/health").json() == {"ok": True}
    seed(app, 1, today=date.today())
    app.state.notifier = FakeNotifier(s, fail=("sms",))
    r = run_daily(app)
    assert r.failed >= 1
    with app.state.Session() as db:
        row = db.scalar(select(RunLog))
        assert row.failed == r.failed and row.trigger == "auto"
    assert any(to == "admin@x.com" and "failed" in t for _, to, t in app.state.notifier.sent)
