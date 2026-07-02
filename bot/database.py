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
        if connection.dialect.name == "postgresql":
            migrations = (
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "admin_report_copy BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "admin_report_categories VARCHAR(200) NOT NULL DEFAULT ''",
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
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS "
                "price_currency VARCHAR(8) NOT NULL DEFAULT 'TOMAN'",
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


@asynccontextmanager
async def session_scope(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
