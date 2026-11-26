"""Settings the Super Admin edits in the UI. Stored in the DB (secrets encrypted with a key derived from
SECRET_KEY) and layered over the .env defaults. Applied in place so the running scheduler/notifier see changes
immediately, with no restart."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from dataclasses import replace

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import AppSetting, UserSetting

# field -> (kind, is_secret)
FIELDS: dict[str, tuple[str, bool]] = {
    "brand_name": ("str", False),
    "reminder_days": ("days", False), "reminder_hour": ("int", False), "country_code": ("str", False),
    "smtp_host": ("str", False), "smtp_port": ("int", False), "smtp_user": ("str", False),
    "smtp_password": ("str", True), "smtp_from": ("str", False),
    "sms_provider": ("str", False), "twilio_sid": ("str", False), "twilio_token": ("str", True),
    "twilio_from": ("str", False), "msg91_authkey": ("str", True), "msg91_template_id": ("str", False),
    "wa_token": ("str", True), "wa_phone_id": ("str", False), "wa_template": ("str", False), "wa_lang": ("str", False),
}


def _fernet(secret_key: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest()))


def parse_days(v: str) -> list[int]:
    days = sorted({int(x) for x in v.replace(" ", "").split(",") if x}, reverse=True)
    if not days or any(d < -60 or d > 365 for d in days):
        raise ValueError("Reminder days must be numbers between -60 and 365, like 30,15,7,1,0,-7")
    return days


def _coerce(kind: str, raw: str):
    if kind == "days":
        return parse_days(raw)
    if kind == "int":
        return int(raw)
    return raw.strip()


def _store(kind: str, val) -> str:
    return ",".join(map(str, val)) if kind == "days" else str(val)


def load_into(settings: Settings, db: Session):
    f = _fernet(settings.secret_key)
    for row in db.query(AppSetting).all():
        if row.key not in FIELDS:
            continue
        kind, secret = FIELDS[row.key]
        try:
            raw = f.decrypt(row.value.encode()).decode() if secret else row.value
            setattr(settings, row.key, _coerce(kind, raw))
        except (InvalidToken, ValueError):
            continue  # unreadable (e.g. SECRET_KEY changed) -> fall back to .env value


def save(settings: Settings, db: Session, form: dict[str, str]) -> None:
    """Validate everything first, then write. Blank secret = keep the saved one."""
    new = {}
    for key, (kind, secret) in FIELDS.items():
        if key not in form:
            continue
        raw = (form[key] or "").strip()
        if secret and raw == "":
            continue
        if key == "reminder_hour" and not (0 <= int(raw or -1) <= 23):
            raise ValueError("Reminder hour must be 0-23")
        if key == "sms_provider" and raw.lower() not in ("", "twilio", "msg91"):
            raise ValueError("SMS provider must be twilio or msg91")
        if key in ("smtp_port", "country_code") and raw == "":
            continue
        new[key] = _coerce(kind, raw.lower() if key == "sms_provider" else raw)
    f = _fernet(settings.secret_key)
    for key, val in new.items():
        kind, secret = FIELDS[key]
        stored = _store(kind, val)
        if secret:
            stored = f.encrypt(stored.encode()).decode()
        row = db.get(AppSetting, key)
        if row:
            row.value = stored
        else:
            db.add(AppSetting(key=key, value=stored))
        setattr(settings, key, val)
    db.commit()


# ---------------------------------------------------------------- per-dealer sender (Option C)
DEALER_FIELDS = {"smtp_host": False, "smtp_port": False, "smtp_user": False, "smtp_password": True,
                 "smtp_from": False, "twilio_sid": False, "twilio_token": True, "twilio_from": False}


def dealer_values(settings: Settings, db: Session, uid: int) -> dict[str, str]:
    f = _fernet(settings.secret_key)
    out = {}
    for row in db.query(UserSetting).filter(UserSetting.user_id == uid):
        if row.key not in DEALER_FIELDS or not row.value:
            continue
        try:
            out[row.key] = f.decrypt(row.value.encode()).decode() if DEALER_FIELDS[row.key] else row.value
        except InvalidToken:
            continue
    return out


def dealer_save(settings: Settings, db: Session, uid: int, form: dict[str, str]) -> None:
    f = _fernet(settings.secret_key)
    port = (form.get("smtp_port") or "").strip()
    if port and not port.isdigit():
        raise ValueError("SMTP port must be a number")
    for key, secret in DEALER_FIELDS.items():
        if key not in form:
            continue
        raw = (form[key] or "").strip()
        if secret and raw == "":
            continue  # blank secret = keep saved one
        row = db.get(UserSetting, (uid, key))
        stored = f.encrypt(raw.encode()).decode() if (secret and raw) else raw
        if row:
            row.value = stored
        else:
            db.add(UserSetting(user_id=uid, key=key, value=stored))
    db.commit()


def dealer_sender(settings: Settings, db: Session, uid: int) -> tuple[Settings, dict[str, bool]]:
    """Global settings overlaid with the dealer's own email / SMS credentials (only when complete).
    Returns (effective settings, {"email": own?, "sms": own?})."""
    v = dealer_values(settings, db, uid)
    eff, own = replace(settings), {"email": False, "sms": False}
    if v.get("smtp_host") and v.get("smtp_from"):
        eff.smtp_host, eff.smtp_from = v["smtp_host"], v["smtp_from"]
        eff.smtp_port = int(v.get("smtp_port") or 587)
        eff.smtp_user, eff.smtp_password = v.get("smtp_user", ""), v.get("smtp_password", "")
        own["email"] = True
    if v.get("twilio_sid") and v.get("twilio_token") and v.get("twilio_from"):
        eff.sms_provider, eff.twilio_sid = "twilio", v["twilio_sid"]
        eff.twilio_token, eff.twilio_from = v["twilio_token"], v["twilio_from"]
        own["sms"] = True
    return eff, own
