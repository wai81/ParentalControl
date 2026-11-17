"""Database tables."""

from datetime import date, datetime

from sqlalchemy import JSON, Date, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pc_server.db.base import Base, TimestampMixin, UTCDateTime, utcnow


class Setting(Base):
    """Server-wide values, e.g. the parent's password hash."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class PairingCode(Base):
    """One-time code from the web panel; the agent exchanges it for a device token."""

    __tablename__ = "pairing_codes"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Device(TimestampMixin, Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    status: Mapped[dict | None] = mapped_column(JSON, default=None)  # last DeviceStatus
    rules: Mapped[dict | None] = mapped_column(JSON, default=None)  # pc_shared.rules.Rules
    rules_version: Mapped[int] = mapped_column(Integer, default=0)


class Command(Base):
    """Parent's command waiting for (or delivered to) the agent."""

    __tablename__ = "commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(16))
    minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    ok: Mapped[bool | None] = mapped_column(default=None)
    result: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (Index("ix_commands_device_pending", "device_id", "done_at"),)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    local_id: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    kind: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        UniqueConstraint("device_id", "local_id", name="uq_events_device_local"),
        Index("ix_events_device_ts", "device_id", "ts"),
    )


class DayUsage(Base):
    __tablename__ = "day_usage"

    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    used_minutes: Mapped[float] = mapped_column(Float, default=0)
    bonus_minutes: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[bool] = mapped_column(default=False)


class AppUsage(Base):
    __tablename__ = "app_usage"

    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    app: Mapped[str] = mapped_column(String(100), primary_key=True)
    minutes: Mapped[float] = mapped_column(Float, default=0)
