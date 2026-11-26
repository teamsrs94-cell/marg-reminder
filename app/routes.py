import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from .models import Customer, ReminderLog, RunLog, Subscription, User, UserSetting
from .security import hash_password, new_csrf, verify_password
from .services import appsettings, importer
from .services.util import add_one_year, fmt_date, normalize_phone, parse_date, valid_email

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["dmy"] = fmt_date
PAGE = 25


class LoginRequired(Exception):
    pass


def setup_errors(app):
    @app.exception_handler(LoginRequired)
    async def _login(request, exc):
        return RedirectResponse("/login", 303)


def get_db(request: Request):
    with request.app.state.Session() as db:
        yield db


def today(request: Request) -> date:
    return datetime.now(ZoneInfo(request.app.state.settings.timezone)).date()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.active:
        request.session.clear()
        raise LoginRequired()
    return user


def admin_only(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admins only")
    return user


async def csrf(request: Request):
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "x")):
        raise HTTPException(400, "Invalid form token, please reload the page")


def render(request: Request, name: str, user: User | None = None, **ctx):
    if "csrf" not in request.session:
        request.session["csrf"] = new_csrf()
    ctx.update(request=request, user=user, csrf=request.session["csrf"], flash=request.session.pop("flash", None))
    return templates.TemplateResponse(request, name, ctx)


def flash(request: Request, msg: str):
    request.session["flash"] = msg


def go(url: str):
    return RedirectResponse(url, 303)


def owned_customers(user: User):
    q = select(Customer)
    return q if user.is_admin else q.where(Customer.distributor_id == user.id)


def get_customer(db, user, cid) -> Customer:
    c = db.get(Customer, cid)
    if not c or not (user.is_admin or c.distributor_id == user.id):
        raise HTTPException(404)
    return c


def get_sub(db, user, sid) -> Subscription:
    s = db.get(Subscription, sid, options=[joinedload(Subscription.customer)])
    if not s or not (user.is_admin or s.customer.distributor_id == user.id):
        raise HTTPException(404)
    return s


def status_of(expiry: date, t: date) -> str:
    d = (expiry - t).days
    return "expired" if d < 0 else "soon" if d <= 30 else "active"


templates.env.globals["status_of"] = status_of


# ---------------------------------------------------------------- auth
@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"ok": True}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(csrf)])
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    lim, key = request.app.state.limiter, f"{email.lower()}|{request.client.host if request.client else ''}"
    if lim.blocked(key):
        return render(request, "login.html", error="Too many attempts. Try again in 10 minutes.")
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not user.active or not verify_password(password, user.password_hash):
        lim.fail(key)
        return render(request, "login.html", error="Wrong email or password.")
    lim.reset(key)
    request.session.clear()
    request.session["uid"] = user.id
    return go("/")


@router.post("/logout", dependencies=[Depends(csrf)])
def logout(request: Request):
    request.session.clear()
    return go("/login")


# ---------------------------------------------------------------- dashboard
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = today(request)
    base = select(Subscription).join(Customer)
    if not user.is_admin:
        base = base.where(Customer.distributor_id == user.id)

    def count(*cond):
        return db.scalar(select(func.count()).select_from(base.where(*cond).subquery()))

    stats = {
        "expired": count(Subscription.expiry_date < t),
        "week": count(Subscription.expiry_date >= t, Subscription.expiry_date <= t + timedelta(days=7)),
        "month": count(Subscription.expiry_date >= t, Subscription.expiry_date <= t + timedelta(days=30)),
        "active": count(Subscription.expiry_date >= t),
        "customers": db.scalar(select(func.count()).select_from(owned_customers(user).subquery())),
    }
    attention = db.scalars(
        base.options(joinedload(Subscription.customer))
        .where(Subscription.expiry_date <= t + timedelta(days=30), Subscription.expiry_date >= t - timedelta(days=60))
        .order_by(Subscription.expiry_date).limit(15)
    ).unique().all()
    return render(request, "dashboard.html", user, stats=stats, attention=attention, t=t)


# ---------------------------------------------------------------- subscriptions list
@router.get("/subscriptions", response_class=HTMLResponse)
def subscriptions(request: Request, q: str = "", status: str = "", page: int = 1,
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = today(request)
    base = select(Subscription).join(Customer)
    if not user.is_admin:
        base = base.where(Customer.distributor_id == user.id)
    if q.strip():
        like = f"%{q.strip()}%"
        base = base.where(or_(Customer.name.ilike(like), Customer.phone.ilike(like),
                              Subscription.license_no.ilike(like), Customer.city.ilike(like)))
    if status == "expired":
        base = base.where(Subscription.expiry_date < t)
    elif status == "soon":
        base = base.where(Subscription.expiry_date >= t, Subscription.expiry_date <= t + timedelta(days=30))
    elif status == "active":
        base = base.where(Subscription.expiry_date > t + timedelta(days=30))
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    page = max(page, 1)
    rows = db.scalars(base.options(joinedload(Subscription.customer)).order_by(Subscription.expiry_date)
                      .limit(PAGE).offset((page - 1) * PAGE)).unique().all()
    return render(request, "subscriptions.html", user, rows=rows, q=q, status=status, page=page,
                  pages=max(1, -(-total // PAGE)), total=total, t=t)


# ---------------------------------------------------------------- customers
@router.get("/customers", response_class=HTMLResponse)
def customers(request: Request, q: str = "", page: int = 1, user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    base = owned_customers(user)
    if q.strip():
        like = f"%{q.strip()}%"
        base = base.where(or_(Customer.name.ilike(like), Customer.phone.ilike(like), Customer.city.ilike(like)))
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    page = max(page, 1)
    rows = db.scalars(base.order_by(Customer.name).limit(PAGE).offset((page - 1) * PAGE)).all()
    return render(request, "customers.html", user, rows=rows, q=q, page=page,
                  pages=max(1, -(-total // PAGE)), total=total)


@router.get("/customers/new", response_class=HTMLResponse)
def customer_new(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    dists = db.scalars(select(User).where(User.role == "distributor", User.active).order_by(User.name)).all() if user.is_admin else []
    return render(request, "customer_form.html", user, c=None, dists=dists, error=None)


def _customer_fields(request, name, contact_person, phone, email, city):
    s = request.app.state.settings
    p = normalize_phone(phone, s.country_code)
    if phone.strip() and not p:
        return None, "Enter a valid mobile number (10 digits, or with country code)."
    if email.strip() and not valid_email(email.strip()):
        return None, "Enter a valid email address."
    return dict(name=name.strip(), contact_person=contact_person.strip(), phone=p, email=email.strip(),
                city=city.strip()), None


@router.post("/customers/new", dependencies=[Depends(csrf)])
def customer_create(request: Request, name: str = Form(...), contact_person: str = Form(""), phone: str = Form(""),
                    email: str = Form(""), city: str = Form(""), notify: str = Form(""),
                    distributor_id: int = Form(0), user: User = Depends(current_user), db: Session = Depends(get_db)):
    f, err = _customer_fields(request, name, contact_person, phone, email, city)
    dist_id = distributor_id if user.is_admin else user.id
    if not err and not db.get(User, dist_id):
        err = "Choose a distributor."
    if err:
        dists = db.scalars(select(User).where(User.role == "distributor", User.active)).all() if user.is_admin else []
        return render(request, "customer_form.html", user, c=None, dists=dists, error=err, form=dict(name=name, contact_person=contact_person, phone=phone, email=email, city=city))
    c = Customer(distributor_id=dist_id, notify=bool(notify), **f)
    db.add(c)
    db.commit()
    flash(request, "Customer added. Now add the subscription.")
    return go(f"/customers/{c.id}/subscriptions/new")


@router.get("/customers/{cid}", response_class=HTMLResponse)
def customer_detail(cid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = get_customer(db, user, cid)
    subs = sorted(c.subscriptions, key=lambda s: s.expiry_date)
    return render(request, "customer_detail.html", user, c=c, subs=subs, t=today(request))


@router.get("/customers/{cid}/edit", response_class=HTMLResponse)
def customer_edit(cid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "customer_form.html", user, c=get_customer(db, user, cid), dists=[], error=None)


@router.post("/customers/{cid}/edit", dependencies=[Depends(csrf)])
def customer_update(cid: int, request: Request, name: str = Form(...), contact_person: str = Form(""),
                    phone: str = Form(""), email: str = Form(""), city: str = Form(""), notify: str = Form(""),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = get_customer(db, user, cid)
    f, err = _customer_fields(request, name, contact_person, phone, email, city)
    if err:
        return render(request, "customer_form.html", user, c=c, dists=[], error=err)
    for k, v in f.items():
        setattr(c, k, v)
    c.notify = bool(notify)
    db.commit()
    flash(request, "Customer saved.")
    return go(f"/customers/{c.id}")


@router.post("/customers/{cid}/delete", dependencies=[Depends(csrf)])
def customer_delete(cid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(get_customer(db, user, cid))
    db.commit()
    flash(request, "Customer deleted.")
    return go("/customers")


# ---------------------------------------------------------------- subscription CRUD
def _sub_fields(product, license_no, start_date, expiry_date, amount, notes):
    expiry = parse_date(expiry_date)
    if not expiry:
        return None, "Enter a valid expiry date."
    try:
        amt = float(amount.replace(",", "")) if amount.strip() else None
    except ValueError:
        return None, "Amount must be a number."
    start = parse_date(start_date)
    if start and start > expiry:
        return None, "Start date is after the expiry date."
    return dict(product=product.strip() or "Marg ERP", license_no=license_no.strip(), start_date=start,
                expiry_date=expiry, amount=amt, notes=notes.strip()), None


@router.get("/customers/{cid}/subscriptions/new", response_class=HTMLResponse)
def sub_new(cid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "sub_form.html", user, c=get_customer(db, user, cid), s=None, error=None)


@router.post("/customers/{cid}/subscriptions/new", dependencies=[Depends(csrf)])
def sub_create(cid: int, request: Request, product: str = Form("Marg ERP"), license_no: str = Form(""),
               start_date: str = Form(""), expiry_date: str = Form(""), amount: str = Form(""), notes: str = Form(""),
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = get_customer(db, user, cid)
    f, err = _sub_fields(product, license_no, start_date, expiry_date, amount, notes)
    if err:
        return render(request, "sub_form.html", user, c=c, s=None, error=err)
    db.add(Subscription(customer_id=c.id, **f))
    db.commit()
    flash(request, "Subscription added. Reminders are now scheduled.")
    return go(f"/customers/{c.id}")


@router.get("/subscriptions/{sid}/edit", response_class=HTMLResponse)
def sub_edit(sid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = get_sub(db, user, sid)
    return render(request, "sub_form.html", user, c=s.customer, s=s, error=None)


@router.post("/subscriptions/{sid}/edit", dependencies=[Depends(csrf)])
def sub_update(sid: int, request: Request, product: str = Form("Marg ERP"), license_no: str = Form(""),
               start_date: str = Form(""), expiry_date: str = Form(""), amount: str = Form(""), notes: str = Form(""),
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = get_sub(db, user, sid)
    f, err = _sub_fields(product, license_no, start_date, expiry_date, amount, notes)
    if err:
        return render(request, "sub_form.html", user, c=s.customer, s=s, error=err)
    for k, v in f.items():
        setattr(s, k, v)
    db.commit()
    flash(request, "Subscription saved.")
    return go(f"/customers/{s.customer_id}")


@router.post("/subscriptions/{sid}/renew", dependencies=[Depends(csrf)])
def sub_renew(sid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """One tap: start the next year from the old expiry (or today if long expired)."""
    s = get_sub(db, user, sid)
    t = today(request)
    s.start_date = max(s.expiry_date + timedelta(days=1), t) if s.expiry_date < t else s.expiry_date + timedelta(days=1)
    s.expiry_date = add_one_year(s.start_date) - timedelta(days=1)
    db.commit()
    flash(request, f"Renewed until {fmt_date(s.expiry_date)}.")
    return go(request.headers.get("referer") or f"/customers/{s.customer_id}")


@router.post("/subscriptions/{sid}/delete", dependencies=[Depends(csrf)])
def sub_delete(sid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = get_sub(db, user, sid)
    cid = s.customer_id
    db.delete(s)
    db.commit()
    flash(request, "Subscription deleted.")
    return go(f"/customers/{cid}")


# ---------------------------------------------------------------- import
@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, user: User = Depends(current_user)):
    return render(request, "import.html", user, result=None, columns=importer.COLUMNS)


@router.get("/import/sample.csv")
def import_sample(user: User = Depends(current_user)):
    return Response(importer.SAMPLE, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=marg_import_sample.csv"})


@router.post("/import", dependencies=[Depends(csrf)])
async def import_run(request: Request, file: UploadFile = File(...), user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    data = await file.read()
    if len(data) > 5_000_000:
        return render(request, "import.html", user, result=None, columns=importer.COLUMNS, error="File too large (max 5 MB).")
    try:
        rows = importer.read_rows(file.filename or "", data)
    except Exception:
        return render(request, "import.html", user, result=None, columns=importer.COLUMNS,
                      error="Could not read that file. Use the CSV sample or an .xlsx file.")
    res = importer.import_rows(db, rows, user, request.app.state.settings.country_code)
    return render(request, "import.html", user, result=res, columns=importer.COLUMNS)


# ---------------------------------------------------------------- reminders
@router.get("/reminders", response_class=HTMLResponse)
def reminder_log(request: Request, page: int = 1, user: User = Depends(current_user), db: Session = Depends(get_db)):
    base = select(ReminderLog).join(Subscription).join(Customer)
    if not user.is_admin:
        base = base.where(Customer.distributor_id == user.id)
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    page = max(page, 1)
    rows = db.scalars(base.options(joinedload(ReminderLog.subscription).joinedload(Subscription.customer))
                      .order_by(ReminderLog.id.desc()).limit(PAGE).offset((page - 1) * PAGE)).unique().all()
    s = request.app.state.settings
    n = request.app.state.notifier
    channels = {c: n.configured(c) for c in ("whatsapp", "sms", "email")}
    runs = db.scalars(select(RunLog).order_by(RunLog.id.desc()).limit(5)).all() if user.is_admin else []
    return render(request, "reminders.html", user, rows=rows, page=page, pages=max(1, -(-total // PAGE)),
                  channels=channels, schedule=s.reminder_days, hour=s.reminder_hour, runs=runs)


@router.post("/reminders/run", dependencies=[Depends(csrf)])
def reminders_run(request: Request, user: User = Depends(admin_only)):
    from .main import run_daily

    flash(request, "Run complete: " + run_daily(request.app, "manual").as_text())
    return go("/reminders")


# ---------------------------------------------------------------- distributors (admin)
@router.get("/distributors", response_class=HTMLResponse)
def distributors(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).where(User.role == "distributor").order_by(User.name)).all()
    counts = dict(db.execute(select(Customer.distributor_id, func.count()).group_by(Customer.distributor_id)).all())
    return render(request, "distributors.html", user, rows=rows, counts=counts, error=None)


@router.post("/distributors", dependencies=[Depends(csrf)])
def distributor_create(request: Request, name: str = Form(...), company: str = Form(""), email: str = Form(...),
                       phone: str = Form(""), password: str = Form(...), user: User = Depends(admin_only),
                       db: Session = Depends(get_db)):
    email = email.strip().lower()
    cc = request.app.state.settings.country_code
    err = None
    if not valid_email(email):
        err = "Enter a valid email."
    elif len(password) < 8:
        err = "Password must be at least 8 characters."
    elif phone.strip() and not normalize_phone(phone, cc):
        err = "Enter a valid mobile number."
    elif db.scalar(select(User).where(User.email == email)):
        err = "That email is already registered."
    if err:
        rows = db.scalars(select(User).where(User.role == "distributor").order_by(User.name)).all()
        return render(request, "distributors.html", user, rows=rows, counts={}, error=err)
    db.add(User(role="distributor", name=name.strip(), company=company.strip(), email=email,
                phone=normalize_phone(phone, cc), password_hash=hash_password(password)))
    db.commit()
    flash(request, "Distributor created.")
    return go("/distributors")


@router.post("/distributors/{uid}/toggle", dependencies=[Depends(csrf)])
def distributor_toggle(uid: int, request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    d = db.get(User, uid)
    if not d or d.is_admin:
        raise HTTPException(404)
    d.active = not d.active
    db.commit()
    return go("/distributors")


# ---------------------------------------------------------------- account
def _account_ctx(request, db, user):
    st = request.app.state.settings
    v = appsettings.dealer_values(st, db, user.id) if not user.is_admin else {}
    return dict(ds=v, ds_secret={"smtp_password": bool(v.get("smtp_password")), "twilio_token": bool(v.get("twilio_token"))},
                brand=st.brand_name)


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "account.html", user, error=None, **_account_ctx(request, db, user))


@router.post("/account", dependencies=[Depends(csrf)])
def account_save(request: Request, name: str = Form(...), company: str = Form(""), phone: str = Form(""),
                 current_password: str = Form(""), new_password: str = Form(""),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    cc = request.app.state.settings.country_code
    if phone.strip() and not normalize_phone(phone, cc):
        return render(request, "account.html", user, error="Enter a valid mobile number.", **_account_ctx(request, db, user))
    if new_password:
        if not verify_password(current_password, user.password_hash):
            return render(request, "account.html", user, error="Current password is wrong.", **_account_ctx(request, db, user))
        if len(new_password) < 8:
            return render(request, "account.html", user, error="New password must be at least 8 characters.", **_account_ctx(request, db, user))
        user.password_hash = hash_password(new_password)
    user.name, user.company, user.phone = name.strip(), company.strip(), normalize_phone(phone, cc)
    db.add(user)
    db.commit()
    flash(request, "Saved.")
    return go("/account")


# ---------------------------------------------------------------- super-admin settings
def _settings_ctx(request, **extra):
    s = request.app.state.settings
    n = request.app.state.notifier
    secrets_set = {k: bool(getattr(s, k)) for k, (_, sec) in appsettings.FIELDS.items() if sec}
    return dict(s=s, days=",".join(map(str, s.reminder_days)), secrets_set=secrets_set,
                ready={c: n.configured(c) for c in ("email", "sms", "whatsapp")}, **extra)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(admin_only)):
    return render(request, "settings.html", user, error=None, **_settings_ctx(request))


@router.post("/settings", dependencies=[Depends(csrf)])
async def settings_save(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    form = {k: str(v) for k, v in (await request.form()).items() if k != "csrf"}
    try:
        appsettings.save(request.app.state.settings, db, form)
    except ValueError as e:
        db.rollback()
        return render(request, "settings.html", user, error=str(e) or "Invalid value", **_settings_ctx(request))
    flash(request, "Settings saved. They apply immediately.")
    return go("/settings")


@router.post("/settings/test", dependencies=[Depends(csrf)])
def settings_test(request: Request, channel: str = Form(...), to: str = Form(...), user: User = Depends(admin_only)):
    s, n = request.app.state.settings, request.app.state.notifier
    if channel not in ("email", "sms"):
        raise HTTPException(400)
    addr = to.strip() if channel == "email" else normalize_phone(to, s.country_code)
    if not addr or (channel == "email" and not valid_email(addr)):
        flash(request, "Test failed: enter a valid " + ("email address." if channel == "email" else "mobile number."))
        return go("/settings")
    if not n.configured(channel):
        flash(request, f"Test failed: {channel} is not configured yet. Save the settings first.")
        return go("/settings")
    ok, detail = n.send(channel, addr, "Test message from Marg Reminder. Your setup works!", ["Test", "Marg ERP", "-", "-", "Marg Reminder"],
                        "Marg Reminder test")
    flash(request, f"Test {channel} sent to {addr}." if ok else f"Test {channel} FAILED: {detail}")
    return go("/settings")


# ---------------------------------------------------------------- dealer's own sender (optional override)
@router.post("/account/sender", dependencies=[Depends(csrf)])
async def sender_save(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.is_admin:
        raise HTTPException(403)
    form = {k: str(v) for k, v in (await request.form()).items() if k != "csrf"}
    if form.get("clear"):
        db.query(UserSetting).filter(UserSetting.user_id == user.id).delete()
        db.commit()
        flash(request, "Your own sender was removed. Reminders now use the default sender.")
        return go("/account")
    try:
        appsettings.dealer_save(request.app.state.settings, db, user.id, form)
    except ValueError as e:
        db.rollback()
        return render(request, "account.html", user, error=str(e), **_account_ctx(request, db, user))
    flash(request, "Sender settings saved.")
    return go("/account")


@router.post("/account/sender/test", dependencies=[Depends(csrf)])
def sender_test(request: Request, channel: str = Form(...), to: str = Form(...), user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    st, n = request.app.state.settings, request.app.state.notifier
    if channel not in ("email", "sms") or user.is_admin:
        raise HTTPException(400)
    eff, own = appsettings.dealer_sender(st, db, user.id)
    addr = to.strip() if channel == "email" else normalize_phone(to, st.country_code)
    if not addr or (channel == "email" and not valid_email(addr)):
        flash(request, "Enter a valid " + ("email address." if channel == "email" else "mobile number."))
        return go("/account")
    if not own[channel]:
        flash(request, f"No complete {channel} sender saved yet, so reminders use the default sender.")
        return go("/account")
    ok, detail = n.with_settings(eff).send(channel, addr, "Test message from your own sender. It works!",
                                           ["Test", "Marg ERP", "-", "-", user.company or user.name], "Test from your sender",
                                           reply_to=user.email, from_name=user.company or user.name)
    flash(request, f"Test {channel} sent to {addr}." if ok else f"Test {channel} FAILED: {detail}")
    return go("/account")
