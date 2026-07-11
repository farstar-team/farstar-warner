from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.config import Settings
from bot.models import Base


SessionFactory = async_sessionmaker[AsyncSession]


def create_database(settings: Settings) -> tuple[AsyncEngine, SessionFactory]:
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_recycle=1800,
    )
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    return engine, session_factory


async def initialize_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # Version 5.1.0 is OSINT-only: erase legacy encrypted Meta credentials.
        await connection.execute(text("DELETE FROM instagram_monitoring_accounts"))
        if connection.dialect.name == "postgresql":
            migrations = (
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "admin_report_copy BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "admin_report_categories VARCHAR(200) NOT NULL DEFAULT ''",
                "ALTER TABLE target_pages ADD COLUMN IF NOT EXISTS "
                "last_successful_check_at TIMESTAMPTZ NULL",
                "ALTER TABLE target_pages ADD COLUMN IF NOT EXISTS "
                "last_status_changed_at TIMESTAMPTZ NULL",
                "ALTER TABLE target_pages ADD COLUMN IF NOT EXISTS "
                "last_check_outcome VARCHAR(32) NULL",
                "ALTER TABLE target_pages ADD COLUMN IF NOT EXISTS "
                "last_http_status INTEGER NULL",
                "ALTER TABLE target_pages ADD COLUMN IF NOT EXISTS "
                "last_evidence_source VARCHAR(64) NULL",
                "ALTER TABLE target_pages ADD COLUMN IF NOT EXISTS "
                "last_evidence_at TIMESTAMPTZ NULL",
                "ALTER TABLE target_pages ADD COLUMN IF NOT EXISTS "
                "last_deactivation_evidence_at TIMESTAMPTZ NULL",
                "ALTER TABLE target_pages ADD COLUMN IF NOT EXISTS "
                "status_confirmed BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE target_pages ADD COLUMN IF NOT EXISTS "
                "consecutive_active_checks INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE target_pages ADD COLUMN IF NOT EXISTS "
                "consecutive_deactivated_checks INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE target_pages ADD COLUMN IF NOT EXISTS "
                "consecutive_inconclusive_checks INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS "
                "notify_follower_change BOOLEAN NOT NULL DEFAULT TRUE",
                "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS "
                "follower_report_mode VARCHAR(16) NOT NULL DEFAULT 'threshold'",
                "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS "
                "follower_change_threshold INTEGER NOT NULL DEFAULT 100",
                "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS "
                "follower_report_baseline BIGINT NULL",
                "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS "
                "last_follower_report_at TIMESTAMPTZ NULL",
                "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS "
                "notify_verification_change BOOLEAN NOT NULL DEFAULT TRUE",
                "ALTER TABLE page_snapshots ADD COLUMN IF NOT EXISTS "
                "external_link VARCHAR(2000) NULL",
                "ALTER TABLE page_snapshots ADD COLUMN IF NOT EXISTS "
                "external_link_initialized BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE page_snapshots ADD COLUMN IF NOT EXISTS "
                "account_type VARCHAR(32) NULL",
                "ALTER TABLE page_snapshots ADD COLUMN IF NOT EXISTS "
                "account_type_initialized BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE page_snapshots ADD COLUMN IF NOT EXISTS "
                "category_name VARCHAR(255) NULL",
                "ALTER TABLE page_snapshots ADD COLUMN IF NOT EXISTS "
                "guest_searchable BOOLEAN NULL",
                "ALTER TABLE page_snapshots ADD COLUMN IF NOT EXISTS "
                "guest_searchable_initialized BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS "
                "price_currency VARCHAR(8) NOT NULL DEFAULT 'TOMAN'",
                "ALTER TABLE subscription_plans ADD COLUMN IF NOT EXISTS "
                "price_currency VARCHAR(8) NOT NULL DEFAULT 'TOMAN'",
                "ALTER TABLE payment_config ADD COLUMN IF NOT EXISTS "
                "zarinpal_merchant_id VARCHAR(100) NULL",
                "ALTER TABLE payment_config ADD COLUMN IF NOT EXISTS "
                "zarinpal_callback_url VARCHAR(1000) NULL",
                "ALTER TABLE payment_config ADD COLUMN IF NOT EXISTS "
                "zarinpal_enabled BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE payment_invoices ADD COLUMN IF NOT EXISTS "
                "zarinpal_merchant_id VARCHAR(100) NULL",
            )
            for statement in migrations:
                await connection.execute(text(statement))
        elif connection.dialect.name == "sqlite":
            existing_columns = {
                row[1]
                for row in (
                    await connection.execute(
                        text("PRAGMA table_info(notification_settings)")
                    )
                ).all()
            }
            sqlite_migrations = {
                "notify_follower_change": (
                    "ALTER TABLE notification_settings ADD COLUMN "
                    "notify_follower_change BOOLEAN NOT NULL DEFAULT 1"
                ),
                "follower_report_mode": (
                    "ALTER TABLE notification_settings ADD COLUMN "
                    "follower_report_mode VARCHAR(16) NOT NULL DEFAULT 'threshold'"
                ),
                "follower_change_threshold": (
                    "ALTER TABLE notification_settings ADD COLUMN "
                    "follower_change_threshold INTEGER NOT NULL DEFAULT 100"
                ),
                "follower_report_baseline": (
                    "ALTER TABLE notification_settings ADD COLUMN "
                    "follower_report_baseline BIGINT NULL"
                ),
                "last_follower_report_at": (
                    "ALTER TABLE notification_settings ADD COLUMN "
                    "last_follower_report_at DATETIME NULL"
                ),
                "notify_verification_change": (
                    "ALTER TABLE notification_settings ADD COLUMN "
                    "notify_verification_change BOOLEAN NOT NULL DEFAULT 1"
                ),
            }
            for column, statement in sqlite_migrations.items():
                if column not in existing_columns:
                    await connection.execute(text(statement))
            target_columns = {
                row[1]
                for row in (
                    await connection.execute(text("PRAGMA table_info(target_pages)"))
                ).all()
            }
            target_migrations = {
                "last_successful_check_at": (
                    "ALTER TABLE target_pages ADD COLUMN "
                    "last_successful_check_at DATETIME NULL"
                ),
                "last_status_changed_at": (
                    "ALTER TABLE target_pages ADD COLUMN "
                    "last_status_changed_at DATETIME NULL"
                ),
                "last_check_outcome": (
                    "ALTER TABLE target_pages ADD COLUMN "
                    "last_check_outcome VARCHAR(32) NULL"
                ),
                "last_http_status": (
                    "ALTER TABLE target_pages ADD COLUMN last_http_status INTEGER NULL"
                ),
                "last_evidence_source": (
                    "ALTER TABLE target_pages ADD COLUMN "
                    "last_evidence_source VARCHAR(64) NULL"
                ),
                "last_evidence_at": (
                    "ALTER TABLE target_pages ADD COLUMN last_evidence_at DATETIME NULL"
                ),
                "last_deactivation_evidence_at": (
                    "ALTER TABLE target_pages ADD COLUMN "
                    "last_deactivation_evidence_at DATETIME NULL"
                ),
                "status_confirmed": (
                    "ALTER TABLE target_pages ADD COLUMN "
                    "status_confirmed BOOLEAN NOT NULL DEFAULT 0"
                ),
                "consecutive_active_checks": (
                    "ALTER TABLE target_pages ADD COLUMN "
                    "consecutive_active_checks INTEGER NOT NULL DEFAULT 0"
                ),
                "consecutive_deactivated_checks": (
                    "ALTER TABLE target_pages ADD COLUMN "
                    "consecutive_deactivated_checks INTEGER NOT NULL DEFAULT 0"
                ),
                "consecutive_inconclusive_checks": (
                    "ALTER TABLE target_pages ADD COLUMN "
                    "consecutive_inconclusive_checks INTEGER NOT NULL DEFAULT 0"
                ),
            }
            for column, statement in target_migrations.items():
                if column not in target_columns:
                    await connection.execute(text(statement))
            snapshot_columns = {
                row[1]
                for row in (
                    await connection.execute(text("PRAGMA table_info(page_snapshots)"))
                ).all()
            }
            snapshot_migrations = {
                "external_link": (
                    "ALTER TABLE page_snapshots ADD COLUMN external_link "
                    "VARCHAR(2000) NULL"
                ),
                "external_link_initialized": (
                    "ALTER TABLE page_snapshots ADD COLUMN external_link_initialized "
                    "BOOLEAN NOT NULL DEFAULT 0"
                ),
                "account_type": (
                    "ALTER TABLE page_snapshots ADD COLUMN account_type VARCHAR(32) NULL"
                ),
                "account_type_initialized": (
                    "ALTER TABLE page_snapshots ADD COLUMN account_type_initialized "
                    "BOOLEAN NOT NULL DEFAULT 0"
                ),
                "category_name": (
                    "ALTER TABLE page_snapshots ADD COLUMN category_name "
                    "VARCHAR(255) NULL"
                ),
                "guest_searchable": (
                    "ALTER TABLE page_snapshots ADD COLUMN guest_searchable BOOLEAN NULL"
                ),
                "guest_searchable_initialized": (
                    "ALTER TABLE page_snapshots ADD COLUMN guest_searchable_initialized "
                    "BOOLEAN NOT NULL DEFAULT 0"
                ),
            }
            for column, statement in snapshot_migrations.items():
                if column not in snapshot_columns:
                    await connection.execute(text(statement))
            user_columns = {
                row[1]
                for row in (
                    await connection.execute(text("PRAGMA table_info(users)"))
                ).all()
            }
            if "admin_report_copy" not in user_columns:
                await connection.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN admin_report_copy "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if "admin_report_categories" not in user_columns:
                await connection.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN admin_report_categories "
                        "VARCHAR(200) NOT NULL DEFAULT ''"
                    )
                )
            store_columns = {
                row[1]
                for row in (
                    await connection.execute(text("PRAGMA table_info(store_products)"))
                ).all()
            }
            if "price_currency" not in store_columns:
                await connection.execute(
                    text(
                        "ALTER TABLE store_products ADD COLUMN price_currency "
                        "VARCHAR(8) NOT NULL DEFAULT 'TOMAN'"
                    )
                )
            plan_columns = {
                row[1]
                for row in (
                    await connection.execute(
                        text("PRAGMA table_info(subscription_plans)")
                    )
                ).all()
            }
            if "price_currency" not in plan_columns:
                await connection.execute(
                    text(
                        "ALTER TABLE subscription_plans ADD COLUMN price_currency "
                        "VARCHAR(8) NOT NULL DEFAULT 'TOMAN'"
                    )
                )
            payment_columns = {
                row[1]
                for row in (
                    await connection.execute(text("PRAGMA table_info(payment_config)"))
                ).all()
            }
            payment_migrations = {
                "zarinpal_merchant_id": (
                    "ALTER TABLE payment_config ADD COLUMN "
                    "zarinpal_merchant_id VARCHAR(100) NULL"
                ),
                "zarinpal_callback_url": (
                    "ALTER TABLE payment_config ADD COLUMN "
                    "zarinpal_callback_url VARCHAR(1000) NULL"
                ),
                "zarinpal_enabled": (
                    "ALTER TABLE payment_config ADD COLUMN "
                    "zarinpal_enabled BOOLEAN NOT NULL DEFAULT 0"
                ),
            }
            for column, statement in payment_migrations.items():
                if column not in payment_columns:
                    await connection.execute(text(statement))
            invoice_columns = {
                row[1]
                for row in (
                    await connection.execute(
                        text("PRAGMA table_info(payment_invoices)")
                    )
                ).all()
            }
            if "zarinpal_merchant_id" not in invoice_columns:
                await connection.execute(
                    text(
                        "ALTER TABLE payment_invoices ADD COLUMN "
                        "zarinpal_merchant_id VARCHAR(100) NULL"
                    )
                )


@asynccontextmanager
async def session_scope(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
