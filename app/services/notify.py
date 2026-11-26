"""Channel senders. Each returns (ok, detail). A channel that isn't configured raises NotConfigured
so the reminder run skips it quietly instead of logging failures every day."""
import smtplib
from email.message import EmailMessage

import httpx

from ..config import Settings


class NotConfigured(Exception):
    pass


class Notifier:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.s = settings
        self.http = client or httpx.Client(timeout=15)

    def configured(self, channel: str) -> bool:
        s = self.s
        if channel == "email":
            return bool(s.smtp_host and s.smtp_from)
        if channel == "whatsapp":
            return bool(s.wa_token and s.wa_phone_id)
        if channel == "sms":
            if s.sms_provider == "twilio":
                return bool(s.twilio_sid and s.twilio_token and s.twilio_from)
            if s.sms_provider == "msg91":
                return bool(s.msg91_authkey and s.msg91_template_id)
        return False

    def send(self, channel: str, to: str, text: str, params: list[str], subject: str = "") -> tuple[bool, str]:
        if not self.configured(channel):
            raise NotConfigured(channel)
        try:
            return getattr(self, f"_{channel}")(to, text, params, subject)
        except Exception as e:  # network/provider errors must never kill the run
            return False, f"{type(e).__name__}: {e}"[:300]

    # -- email -------------------------------------------------------------
    def _email(self, to, text, params, subject):
        s = self.s
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject or "Marg ERP renewal reminder"
        msg.set_content(text)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            smtp.ehlo()
            if s.smtp_port != 25:
                smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
        return True, "ok"

    # -- whatsapp (Meta Cloud API, template message) -------------------------
    def _whatsapp(self, to, text, params, subject):
        s = self.s
        body = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": s.wa_template,
                "language": {"code": s.wa_lang},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": p or "-"} for p in params]}
                ],
            },
        }
        r = self.http.post(
            f"https://graph.facebook.com/v20.0/{s.wa_phone_id}/messages",
            headers={"Authorization": f"Bearer {s.wa_token}"},
            json=body,
        )
        return (r.is_success, "ok" if r.is_success else f"HTTP {r.status_code}: {r.text[:200]}")

    # -- sms ---------------------------------------------------------------
    def _sms(self, to, text, params, subject):
        s = self.s
        if s.sms_provider == "twilio":
            r = self.http.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_sid}/Messages.json",
                auth=(s.twilio_sid, s.twilio_token),
                data={"To": "+" + to, "From": s.twilio_from, "Body": text},
            )
        else:  # msg91 flow API (DLT-registered template with variables var1..varN)
            r = self.http.post(
                "https://control.msg91.com/api/v5/flow/",
                headers={"authkey": s.msg91_authkey},
                json={
                    "template_id": s.msg91_template_id,
                    "recipients": [{"mobiles": to, **{f"var{i+1}": p for i, p in enumerate(params)}}],
                },
            )
        return (r.is_success, "ok" if r.is_success else f"HTTP {r.status_code}: {r.text[:200]}")
