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
    def __init__(self, settings, channels=("whatsapp", "sms", "email"), fail=(), fail_hosts=()):
        super().__init__(settings)
        self.channels, self.fail, self.fail_hosts, self.sent, self.meta = channels, set(fail), set(fail_hosts), [], []

    def configured(self, ch):
        return ch in self.channels

    def send(self, ch, to, text, params, subject="", reply_to="", from_name=""):
        if ch not in self.channels:
            raise NotConfigured(ch)
        self.sent.append((ch, to, text))
        self.meta.append(dict(ch=ch, to=to, smtp_from=self.s.smtp_from, twilio_from=self.s.twilio_from,
                              reply_to=reply_to, from_name=from_name))
        bad = ch in self.fail or (ch == "email" and self.s.smtp_host in self.fail_hosts)
        return (not bad, "boom" if bad else "ok")


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


def test_super_admin_settings(env):
    from app.models import AppSetting

    app, c, n, s = env
    app.state.notifier = Notifier(s)  # real notifier: configured() depends on settings
    login(c, "admin@x.com", "adminpass1")
    assert not app.state.notifier.configured("sms")
    r = post(c, "/settings", {"reminder_days": "14,7,0", "reminder_hour": "9", "smtp_host": "smtp.gmail.com", "smtp_port": "587",
                              "smtp_user": "me@gmail.com", "smtp_password": "app-pass", "smtp_from": "me@gmail.com",
                              "sms_provider": "twilio", "twilio_sid": "AC1", "twilio_token": "tok", "twilio_from": "+1555"}, "/settings")
    assert r.status_code == 303
    assert s.reminder_days == [14, 7, 0] and s.reminder_hour == 9
    assert app.state.notifier.configured("sms") and app.state.notifier.configured("email")
    with app.state.Session() as db:
        assert "app-pass" not in db.get(AppSetting, "smtp_password").value  # encrypted at rest
    page = c.get("/settings").text
    assert "app-pass" not in page and "saved" in page
    post(c, "/settings", {"smtp_password": "", "smtp_host": "smtp2.x.com"}, "/settings")  # blank keeps secret
    assert s.smtp_password == "app-pass" and s.smtp_host == "smtp2.x.com"
    assert post(c, "/settings", {"reminder_hour": "99"}, "/settings").status_code == 200  # validation error
    assert post(c, "/settings", {"reminder_days": "abc"}, "/settings").status_code == 200
    # persists across restart
    s2 = Settings(database_url=s.database_url, secret_key="k", enable_scheduler=False, admin_email="admin@x.com")
    app2 = create_app(s2, n)
    assert s2.twilio_token == "tok" and s2.reminder_days == [14, 7, 0]
    # distributors cannot open it
    c.post("/logout", data={"csrf": csrf_of(c)})
    assert TestClient(app).get("/settings", follow_redirects=False).status_code == 303


def _set_dealer_sender(app, s, did, **vals):
    from app.services import appsettings

    with app.state.Session() as db:
        appsettings.dealer_save(s, db, did, vals)


def test_buyer_from_dealer_and_digest_from_default(env):
    app, _, n, s = env
    s.smtp_host, s.smtp_from, s.brand_name = "smtp.default", "srs@default.com", "SRS Engineering"
    did, _, _ = seed(app, 7)
    today = date(2026, 11, 26)
    # 1) no own sender: buyer mail via default, branded "Dev Traders via SRS Engineering", reply-to dealer
    with app.state.Session() as db:
        run_reminders(db, s, n, today)
    buyer = next(m for m in n.meta if m["to"] == "b@x.com")
    assert buyer["smtp_from"] == "srs@default.com" and buyer["reply_to"] == "d@x.com"
    assert buyer["from_name"] == "Dev Traders via SRS Engineering"
    digest = next(m for m in n.meta if m["to"] == "d@x.com" and m["ch"] == "email")
    assert digest["smtp_from"] == "srs@default.com" and digest["from_name"] == "SRS Engineering"
    # 2) dealer adds own Gmail + Twilio: buyer now gets it FROM the dealer; digest still from default
    n.meta.clear(); n.sent.clear()
    _set_dealer_sender(app, s, did, smtp_host="smtp.gmail.com", smtp_port="587", smtp_user="dev@gmail.com",
                       smtp_password="pw", smtp_from="dev@gmail.com", twilio_sid="ACdev", twilio_token="t", twilio_from="+1999")
    with app.state.Session() as db:
        run_reminders(db, s, n, today + timedelta(days=6))  # next stage -> sends again
    buyer = next(m for m in n.meta if m["to"] == "b@x.com")
    assert buyer["smtp_from"] == "dev@gmail.com" and buyer["from_name"] == "Dev Traders"
    assert next(m for m in n.meta if m["to"] == "919876543210" and m["ch"] == "sms")["twilio_from"] == "+1999"
    digest = next(m for m in n.meta if m["to"] == "d@x.com" and m["ch"] == "email")
    assert digest["smtp_from"] == "srs@default.com"


def test_dealer_sender_failure_falls_back_to_default(env):
    app, _, _, s = env
    s.smtp_host, s.smtp_from = "smtp.default", "srs@default.com"
    did, _, _ = seed(app, 1)
    _set_dealer_sender(app, s, did, smtp_host="smtp.broken", smtp_user="u", smtp_password="p", smtp_from="dev@broken.com")
    n = FakeNotifier(s, fail_hosts=("smtp.broken",))
    with app.state.Session() as db:
        r = run_reminders(db, s, n, date(2026, 11, 26))
        assert r.failed == 0
        log = db.scalar(select(ReminderLog).where(ReminderLog.to_address == "b@x.com", ReminderLog.channel == "email"))
        assert log.status == "sent" and "sent via default" in log.detail
    assert any(m["to"] == "b@x.com" and m["smtp_from"] == "srs@default.com" for m in n.meta)


def test_dealer_sender_ui_encrypted(env):
    from app.models import UserSetting
    from app.security import hash_password

    app, c, _, s = env
    did, _, _ = seed(app, 7)
    with app.state.Session() as db:
        db.get(User, did).password_hash = hash_password("password1"); db.commit()
    login(c, "d@x.com", "password1")
    r = post(c, "/account/sender", {"smtp_host": "smtp.gmail.com", "smtp_user": "dev@gmail.com", "smtp_password": "secret-pw",
                                    "smtp_from": "dev@gmail.com"}, "/account")
    assert r.status_code == 303
    with app.state.Session() as db:
        assert "secret-pw" not in db.get(UserSetting, (did, "smtp_password")).value
    assert "secret-pw" not in c.get("/account").text
    assert c.get("/settings", follow_redirects=False).status_code == 403
    post(c, "/account/sender", {"clear": "1"}, "/account")
    with app.state.Session() as db:
        assert db.query(UserSetting).count() == 0


def _reset_link(n):
    import re as _re
    texts = [t for ch, to, t in n.sent if ch == "email" and "/reset/" in t]
    return _re.search(r"(https?://\S+)/reset/(\S+)", texts[-1]).group(2) if texts else None


def test_forgot_and_reset_password(env):
    app, c, n, s = env
    s.app_url = "https://reminder.example.com"
    # unknown email: same answer, nothing sent
    r = post(c, "/forgot", {"email": "nobody@x.com"}, "/forgot")
    assert r.status_code == 200 and "If that email is registered" in r.text and _reset_link(n) is None
    # known email: link emailed, uses configured public address
    r = post(c, "/forgot", {"email": "admin@x.com"}, "/forgot")
    assert "If that email is registered" in r.text
    assert "https://reminder.example.com/reset/" in n.sent[-1][2]
    tok = _reset_link(n)
    assert "Set a new password" in c.get(f"/reset/{tok}").text
    # mismatch / short rejected
    assert "do not match" in post(c, f"/reset/{tok}", {"password": "newpass123", "confirm": "different1"}, f"/reset/{tok}").text
    assert "at least 8" in post(c, f"/reset/{tok}", {"password": "short", "confirm": "short"}, f"/reset/{tok}").text
    # success
    r = post(c, f"/reset/{tok}", {"password": "brandnew99", "confirm": "brandnew99"}, f"/reset/{tok}")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    login(c, "admin@x.com", "brandnew99")
    c.post("/logout", data={"csrf": csrf_of(c)})
    # link is single-use
    assert "Link expired" in c.get(f"/reset/{tok}").text
    assert "Link expired" in post(c, f"/reset/{tok}", {"password": "another123", "confirm": "another123"}, "/login").text
    # tampered link
    assert "Link expired" in c.get(f"/reset/{tok}x").text


def test_reset_link_expires_and_rate_limit(env, monkeypatch):
    from app import security

    app, c, n, s = env
    post(c, "/forgot", {"email": "admin@x.com"}, "/forgot")
    tok = _reset_link(n)
    monkeypatch.setattr(security, "RESET_MAX_AGE", -1)  # pretend an hour has passed
    assert "Link expired" in c.get(f"/reset/{tok}").text
    monkeypatch.undo()
    for _ in range(6):
        r = post(c, "/forgot", {"email": "admin@x.com"}, "/forgot")
    assert "Too many requests" in r.text


def test_admin_resets_distributor_password_and_eye_toggle(env):
    from app.security import hash_password

    app, c, n, s = env
    did, _, _ = seed(app, 7)
    with app.state.Session() as db:
        db.get(User, did).password_hash = hash_password("oldpassw0rd"); db.commit()
    login(c, "admin@x.com", "adminpass1")
    assert "data-pw-toggle" in c.get("/distributors").text  # show/hide password control on every page
    assert post(c, f"/distributors/{did}/password", {"password": "short"}, "/distributors").status_code == 303
    post(c, f"/distributors/{did}/password", {"password": "freshpass99"}, "/distributors")
    c.post("/logout", data={"csrf": csrf_of(c)})
    login(c, "d@x.com", "freshpass99")
    assert "data-pw-toggle" in TestClient(app).get("/login").text
