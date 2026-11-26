"""Daily reminder engine.

For every subscription inside the reminder window we compute its current *stage* (the smallest
configured offset >= days_left). Each (subscription, expiry cycle, stage, recipient, channel) is sent
once; failures are retried on the next run. Renewing changes expiry_date, which starts a fresh cycle.
Buyers get one message per subscription; a distributor gets one digest per channel per run.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..config import Settings
from ..models import Customer, ReminderLog, Subscription
from .notify import NotConfigured, Notifier
from .util import fmt_date, normalize_phone, timing_text, valid_email

CHANNELS = ("whatsapp", "sms", "email")
OVERDUE_GRACE_DAYS = 14  # keep catching up this long past the last configured offset


@dataclass
class RunSummary:
    sent: int = 0
    failed: int = 0
    skipped_unconfigured: set = field(default_factory=set)
    subscriptions: int = 0

    def as_text(self) -> str:
        t = f"{self.subscriptions} subscription(s) due, {self.sent} sent, {self.failed} failed"
        if self.skipped_unconfigured:
            t += f"; channels not configured: {', '.join(sorted(self.skipped_unconfigured))}"
        return t


def stage_for(days_left: int, offsets: list[int]) -> int | None:
    """Smallest offset >= days_left, or None if outside the window."""
    if days_left < min(offsets) - OVERDUE_GRACE_DAYS:
        return None
    cands = [o for o in offsets if o >= days_left]
    return min(cands) if cands else None


def buyer_text(sub: Subscription, days_left: int) -> tuple[str, list[str], str]:
    c, d = sub.customer, sub.customer.distributor
    when = timing_text(days_left)
    contact = f"{d.company or d.name}" + (f", {d.phone}" if d.phone else "")
    text = (
        f"Dear {c.contact_person or c.name}, your {sub.product} subscription"
        f"{' (licence ' + sub.license_no + ')' if sub.license_no else ''} {when} on {fmt_date(sub.expiry_date)}. "
        f"Please renew to avoid interruption. Contact: {contact}."
    )
    params = [c.contact_person or c.name, sub.product, fmt_date(sub.expiry_date), when, d.company or d.name]
    return text, params, f"{sub.product} renewal: {when}"


def digest_text(items: list[tuple[Subscription, int]]) -> tuple[str, list[str], str]:
    lines = [
        f"{i}. {s.customer.name} - {s.product} - {timing_text(dl)} ({fmt_date(s.expiry_date)})"
        + (f" - {s.customer.phone}" if s.customer.phone else "")
        for i, (s, dl) in enumerate(items, 1)
    ]
    head = f"Marg renewals needing attention ({len(items)}):"
    shown = lines[:10]
    more = f"\n...and {len(lines) - 10} more (see dashboard)" if len(lines) > 10 else ""
    text = head + "\n" + "\n".join(shown) + more
    # template params for WhatsApp/SMS digests: count + one-line summary
    params = [str(len(items)), "; ".join(l.split(" - ")[0] for l in shown)[:300], "", "", ""]
    return text, params, f"{len(items)} Marg renewal(s) due"


def run_reminders(db: Session, settings: Settings, notifier: Notifier, today: date) -> RunSummary:
    offsets = settings.reminder_days
    out = RunSummary()
    q = (
        select(Subscription)
        .options(joinedload(Subscription.customer).joinedload(Customer.distributor))
        .where(
            Subscription.expiry_date <= today + timedelta(days=max(offsets)),
            Subscription.expiry_date >= today - timedelta(days=OVERDUE_GRACE_DAYS - min(offsets)),
        )
    )
    subs = db.scalars(q).unique().all()

    sent_keys = {
        (r.subscription_id, r.expiry_date, r.stage, r.recipient, r.channel)
        for r in db.execute(
            select(ReminderLog.subscription_id, ReminderLog.expiry_date, ReminderLog.stage,
                   ReminderLog.recipient, ReminderLog.channel)
            .where(ReminderLog.status == "sent", ReminderLog.expiry_date >= today - timedelta(days=60))
        )
    }

    def log(sub, stage, recipient, channel, to, ok, detail):
        db.add(ReminderLog(subscription_id=sub.id, expiry_date=sub.expiry_date, stage=stage, recipient=recipient,
                           channel=channel, to_address=to, status="sent" if ok else "failed", detail=detail))
        out.sent += ok
        out.failed += not ok

    def address(channel, phone, email):
        if channel == "email":
            return email if valid_email(email) else ""
        return normalize_phone(phone, settings.country_code)

    digests: dict[int, list[tuple[Subscription, int, int]]] = {}
    for sub in subs:
        days_left = (sub.expiry_date - today).days
        stage = stage_for(days_left, offsets)
        if stage is None:
            continue
        out.subscriptions += 1
        cust, dist = sub.customer, sub.customer.distributor
        if cust.notify:
            text, params, subject = buyer_text(sub, days_left)
            for ch in CHANNELS:
                to = address(ch, cust.phone, cust.email)
                if not to or (sub.id, sub.expiry_date, stage, "buyer", ch) in sent_keys:
                    continue
                try:
                    ok, detail = notifier.send(ch, to, text, params, subject)
                except NotConfigured:
                    out.skipped_unconfigured.add(ch)
                    continue
                log(sub, stage, "buyer", ch, to, ok, detail)
        if dist.active:
            digests.setdefault(dist.id, []).append((sub, days_left, stage))

    for items in digests.values():
        dist = items[0][0].customer.distributor
        text, params, subject = digest_text([(s, dl) for s, dl, _ in items])
        for ch in CHANNELS:
            to = address(ch, dist.phone, dist.email)
            pending = [(s, st) for s, _, st in items if (s.id, s.expiry_date, st, "distributor", ch) not in sent_keys]
            if not to or not pending:
                continue
            if len(pending) != len(items):  # rebuild digest with only unsent items
                keep = {s.id for s, _ in pending}
                text, params, subject = digest_text([(s, dl) for s, dl, _ in items if s.id in keep])
            try:
                ok, detail = notifier.send(ch, to, text, params, subject)
            except NotConfigured:
                out.skipped_unconfigured.add(ch)
                continue
            for s, st in pending:
                log(s, st, "distributor", ch, to, ok, detail)
    db.commit()
    return out
