from __future__ import annotations

import enum
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserStatus(str, enum.Enum):
    ACTIVE = "Active"
    BANNED = "Banned"


class PlanTier(str, enum.Enum):
    FREE = "Free"
    PREMIUM = "Premium"
    VIP = "VIP"

    @property
    def target_limit(self) -> int:
        return {
            PlanTier.FREE: 1,
            PlanTier.PREMIUM: 10,
            PlanTier.VIP: 50,
        }[self]


class PageStatus(str, enum.Enum):
    ACTIVE = "Active"
    DEACTIVATED = "Deactivated"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subscription_expiry: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: utc_now() + timedelta(days=7),
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(
            UserStatus,
            name="user_status",
            values_callable=lambda e: [item.value for item in e],
        ),
        nullable=False,
        default=UserStatus.ACTIVE,
        index=True,
    )
    plan_tier: Mapped[PlanTier] = mapped_column(
        Enum(
            PlanTier,
            name="plan_tier",
            values_callable=lambda e: [item.value for item in e],
        ),
        nullable=False,
        default=PlanTier.FREE,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    targets: Mapped[list[TargetPage]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    notification_settings: Mapped[list[NotificationSettings]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class TargetPage(Base):
    __tablename__ = "target_pages"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "instagram_username", name="uq_target_owner_username"
        ),
        Index("ix_target_pages_user_status", "user_id", "last_known_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instagram_username: Mapped[str] = mapped_column(String(30), nullable=False)
    last_known_status: Mapped[PageStatus | None] = mapped_column(
        Enum(
            PageStatus,
            name="page_status",
            values_callable=lambda e: [item.value for item in e],
        ),
        nullable=True,
        index=True,
    )
    last_known_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    owner: Mapped[User] = relationship(back_populates="targets")
    notification_settings: Mapped[NotificationSettings] = relationship(
        back_populates="target_page",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )


class NotificationSettings(Base):
    __tablename__ = "notification_settings"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        primary_key=True,
    )
    target_page_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("target_pages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    notify_activation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    notify_deactivation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    notify_username_change: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    user: Mapped[User] = relationship(back_populates="notification_settings")
    target_page: Mapped[TargetPage] = relationship(
        back_populates="notification_settings"
    )
