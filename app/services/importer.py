"""Bulk import of customers + subscriptions from CSV / XLSX."""
import csv
import io
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Customer, Subscription, User
from .util import normalize_phone, parse_date, valid_email

COLUMNS = ["distributor_email", "customer", "contact_person", "phone", "email", "city",
           "product", "license_no", "start_date", "expiry_date", "amount"]
SAMPLE = (
    ",".join(COLUMNS) + "\n"
    "dist@example.com,Sharma Medical Store,Rajesh Sharma,9876543210,rajesh@example.com,Jaipur,"
    "Marg ERP 9+ Silver,MRG-10001,2025-04-01,2026-03-31,12000\n"
)
ALIASES = {"customer_name": "customer", "name": "customer", "mobile": "phone", "licence_no": "license_no",
           "license": "license_no", "licence": "license_no", "expiry": "expiry_date", "end_date": "expiry_date",
           "start": "start_date", "distributor": "distributor_email"}


@dataclass
class ImportResult:
    created_customers: int = 0
    created_subscriptions: int = 0
    updated_subscriptions: int = 0
    errors: list[str] = field(default_factory=list)


def read_rows(filename: str, data: bytes) -> list[dict]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        rows = list(ws.iter_rows(values_only=True))
    else:
        text = data.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [ALIASES.get(k, k) for k in (str(h or "").strip().lower().replace(" ", "_") for h in rows[0])]
    return [dict(zip(head, r)) for r in rows[1:] if any(c not in (None, "") for c in r)]


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def import_rows(db: Session, rows: list[dict], actor: User, country_code: str = "91") -> ImportResult:
    res = ImportResult()
    dists = {u.email.lower(): u for u in db.scalars(select(User).where(User.active))}
    for i, r in enumerate(rows, start=2):
        dist = actor
        if actor.is_admin:
            dist = dists.get(_s(r.get("distributor_email")).lower())
            if not dist:
                res.errors.append(f"Row {i}: unknown distributor_email '{_s(r.get('distributor_email'))}'")
                continue
        name, expiry = _s(r.get("customer")), parse_date(r.get("expiry_date"))
        if not name or not expiry:
            res.errors.append(f"Row {i}: customer and a valid expiry_date are required")
            continue
        phone = normalize_phone(_s(r.get("phone")), country_code)
        email = _s(r.get("email"))
        if _s(r.get("phone")) and not phone:
            res.errors.append(f"Row {i}: invalid phone '{_s(r.get('phone'))}' (saved without phone)")
        if email and not valid_email(email):
            res.errors.append(f"Row {i}: invalid email '{email}' (saved without email)")
            email = ""
        cust = db.scalar(select(Customer).where(Customer.distributor_id == dist.id,
                                                func.lower(Customer.name) == name.lower(),
                                                Customer.phone == phone))
        if not cust:
            cust = Customer(distributor_id=dist.id, name=name, contact_person=_s(r.get("contact_person")),
                            phone=phone, email=email, city=_s(r.get("city")))
            db.add(cust)
            db.flush()
            res.created_customers += 1
        lic = _s(r.get("license_no"))
        sub = db.scalar(select(Subscription).where(Subscription.customer_id == cust.id,
                                                   Subscription.license_no == lic)) if lic else None
        try:
            amount = float(_s(r.get("amount")).replace(",", "")) if _s(r.get("amount")) else None
        except ValueError:
            amount = None
        if sub:
            sub.expiry_date, sub.start_date = expiry, parse_date(r.get("start_date")) or sub.start_date
            sub.product = _s(r.get("product")) or sub.product
            sub.amount = amount if amount is not None else sub.amount
            res.updated_subscriptions += 1
        else:
            db.add(Subscription(customer_id=cust.id, product=_s(r.get("product")) or "Marg ERP", license_no=lic,
                                start_date=parse_date(r.get("start_date")), expiry_date=expiry, amount=amount))
            res.created_subscriptions += 1
    db.commit()
    return res
