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
            PlanTier.PREMIUM: 100,
            PlanTier.VIP: 500,
        }[self]


class PageStatus(str, enum.Enum):
    ACTIVE = "Active"
    DEACTIVATED = "Deactivated"


class ReceiptStatus(str, enum.Enum):
    PENDING = "Pending"
    APPROVED = "Approved"
    REJECTED = "Rejected"


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
    events: Mapped[list[PageEvent]] = relationship(
        back_populates="target_page",
        cascade="all, delete-orphan",
        passive_deletes=True,
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


class PageEvent(Base):
    __tablename__ = "page_events"
    __table_args__ = (
        Index("ix_page_events_target_created", "target_page_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_page_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("target_pages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    target_page: Mapped[TargetPage] = relationship(back_populates="events")


class PageSnapshot(Base):
    __tablename__ = "page_snapshots"

    target_page_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("target_pages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    profile_picture_key: Mapped[str | None] = mapped_column(String(1000))
    profile_picture_url: Mapped[str | None] = mapped_column(String(2000))
    full_name: Mapped[str | None] = mapped_column(String(255))
    biography: Mapped[str | None] = mapped_column(String(2000))
    follower_count: Mapped[int | None] = mapped_column(BigInteger)
    following_count: Mapped[int | None] = mapped_column(BigInteger)
    post_count: Mapped[int | None] = mapped_column(Integer)
    is_private: Mapped[bool | None] = mapped_column(Boolean)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class RequiredChannel(Base):
    __tablename__ = "required_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_identifier: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    join_url: Mapped[str] = mapped_column(String(500), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        primary_key=True,
    )
    plan_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("subscription_plans.id", ondelete="SET NULL"),
        nullable=True,
    )
    plan_name: Mapped[str] = mapped_column(String(80), nullable=False)
    target_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PaymentConfig(Base):
    __tablename__ = "payment_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    support_username: Mapped[str | None] = mapped_column(String(64))
    card_number: Mapped[str | None] = mapped_column(String(32))
    card_holder: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PaymentReceipt(Base):
    __tablename__ = "payment_receipts"
    __table_args__ = (
        Index("ix_payment_receipts_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("subscription_plans.id", ondelete="SET NULL"),
        nullable=True,
    )
    plan_name: Mapped[str] = mapped_column(String(80), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    target_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_id: Mapped[str] = mapped_column(String(512), nullable=False)
    file_unique_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[ReceiptStatus] = mapped_column(
        Enum(
            ReceiptStatus,
            name="receipt_status",
            values_callable=lambda e: [item.value for item in e],
        ),
        nullable=False,
        default=ReceiptStatus.PENDING,
        index=True,
    )
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class SubscriptionReminderPreference(Base):
    __tablename__ = "subscription_reminder_preferences"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DiscountCode(Base):
    __tablename__ = "discount_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    percent: Mapped[int] = mapped_column(Integer, nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReceiptDiscount(Base):
    __tablename__ = "receipt_discounts"
    __table_args__ = (
        UniqueConstraint("discount_code_id", "user_id", name="uq_discount_user"),
    )

    receipt_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("payment_receipts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    discount_code_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("discount_codes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    original_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discount_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)


class StoreConfig(Base):
    __tablename__ = "store_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class StoreProduct(Base):
    __tablename__ = "store_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purchase_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
