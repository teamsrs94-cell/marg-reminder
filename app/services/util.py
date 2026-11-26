import re
from datetime import date, datetime

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_phone(raw: str, country_code: str = "91") -> str:
    """Return digits-only international number (e.g. 919876543210) or '' if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if len(digits) == 10:
        digits = country_code + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = country_code + digits[1:]
    return digits if 11 <= len(digits) <= 15 else ""


def valid_email(v: str) -> bool:
    return bool(EMAIL_RE.match(v or ""))


_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%d/%m/%y")


def parse_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for f in _FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def fmt_date(d: date | None) -> str:
    return d.strftime("%d-%m-%Y") if d else ""


def add_one_year(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + 1, day=28)


def timing_text(days_left: int) -> str:
    if days_left > 1:
        return f"expires in {days_left} days"
    if days_left == 1:
        return "expires tomorrow"
    if days_left == 0:
        return "expires today"
    if days_left == -1:
        return "expired yesterday"
    return f"expired {-days_left} days ago"
