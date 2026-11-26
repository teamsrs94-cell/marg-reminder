from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """Admin (software owner) or distributor. Distributors receive reminder digests."""

    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="distributor")  # admin | distributor
    name: Mapped[str] = mapped_column(String(120))
    company: Mapped[str] = mapped_column(String(160), default="")
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(20), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    customers: Mapped[list["Customer"]] = relationship(back_populates="distributor")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Customer(Base):
    """The buyer of Marg ERP."""

    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    contact_person: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(160), default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    notify: Mapped[bool] = mapped_column(Boolean, default=True)  # buyer consent / opt-out
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    distributor: Mapped[User] = relationship(back_populates="customers")
    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    product: Mapped[str] = mapped_column(String(80), default="Marg ERP")
    license_no: Mapped[str] = mapped_column(String(80), default="")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    customer: Mapped[Customer] = relationship(back_populates="subscriptions")
    logs: Mapped[list["ReminderLog"]] = relationship(back_populates="subscription", cascade="all, delete-orphan")


class ReminderLog(Base):
    __tablename__ = "reminder_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    expiry_date: Mapped[date] = mapped_column(Date)  # the cycle this reminder belongs to
    stage: Mapped[int] = mapped_column(Integer)  # reminder offset (days before expiry)
    recipient: Mapped[str] = mapped_column(String(12))  # buyer | distributor
    channel: Mapped[str] = mapped_column(String(10))  # whatsapp | sms | email
    to_address: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(10))  # sent | failed
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

    subscription: Mapped[Subscription] = relationship(back_populates="logs")

    __table_args__ = (Index("ix_log_dedupe", "subscription_id", "expiry_date", "stage", "recipient", "channel"),)


class RunLog(Base):
    """One row per automatic reminder run, so the owner can see the automation is alive."""

    __tablename__ = "run_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    trigger: Mapped[str] = mapped_column(String(10), default="auto")  # auto | manual
    due: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")


class AppSetting(Base):
    """Super-admin editable configuration (secret values are stored encrypted)."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class UserSetting(Base):
    """Per-dealer sender credentials (own Gmail / Twilio). Secret values are stored encrypted."""

    __tablename__ = "user_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
