import os
from dataclasses import dataclass, field


def _bool(v: str | None, default=False) -> bool:
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _days(v: str) -> list[int]:
    out = sorted({int(x) for x in v.split(",") if x.strip()}, reverse=True)
    return out or [30, 7, 0]


@dataclass
class Settings:
    brand_name: str = "Marg Reminder"
    app_url: str = ""
    secret_key: str = "dev-secret-change-me"
    database_url: str = "sqlite:///./marg_reminder.db"
    admin_email: str = ""
    admin_password: str = ""
    timezone: str = "Asia/Kolkata"
    country_code: str = "91"
    cookie_secure: bool = False
    reminder_days: list[int] = field(default_factory=lambda: [30, 15, 7, 1, 0, -7])
    reminder_hour: int = 10
    enable_scheduler: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    wa_token: str = ""
    wa_phone_id: str = ""
    wa_template: str = "marg_renewal_reminder"
    wa_lang: str = "en"
    sms_provider: str = ""
    twilio_sid: str = ""
    twilio_token: str = ""
    twilio_from: str = ""
    msg91_authkey: str = ""
    msg91_template_id: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            brand_name=e("BRAND_NAME", cls.brand_name),
            app_url=e("APP_URL", "").rstrip("/"),
            secret_key=e("SECRET_KEY", cls.secret_key),
            database_url=e("DATABASE_URL", cls.database_url),
            admin_email=e("ADMIN_EMAIL", ""),
            admin_password=e("ADMIN_PASSWORD", ""),
            timezone=e("TIMEZONE", cls.timezone),
            country_code=e("DEFAULT_COUNTRY_CODE", cls.country_code),
            cookie_secure=_bool(e("COOKIE_SECURE")),
            reminder_days=_days(e("REMINDER_DAYS", "30,15,7,1,0,-7")),
            reminder_hour=int(e("REMINDER_HOUR", "10")),
            enable_scheduler=_bool(e("ENABLE_SCHEDULER"), True),
            smtp_host=e("SMTP_HOST", ""),
            smtp_port=int(e("SMTP_PORT", "587")),
            smtp_user=e("SMTP_USER", ""),
            smtp_password=e("SMTP_PASSWORD", ""),
            smtp_from=e("SMTP_FROM", ""),
            wa_token=e("WHATSAPP_TOKEN", ""),
            wa_phone_id=e("WHATSAPP_PHONE_ID", ""),
            wa_template=e("WHATSAPP_TEMPLATE", cls.wa_template),
            wa_lang=e("WHATSAPP_LANG", "en"),
            sms_provider=e("SMS_PROVIDER", "").lower(),
            twilio_sid=e("TWILIO_SID", ""),
            twilio_token=e("TWILIO_TOKEN", ""),
            twilio_from=e("TWILIO_FROM", ""),
            msg91_authkey=e("MSG91_AUTHKEY", ""),
            msg91_template_id=e("MSG91_TEMPLATE_ID", ""),
        )
