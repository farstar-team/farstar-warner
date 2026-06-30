from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeChat
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.checker import InstagramChecker
from bot.config import Settings, get_settings
from bot.database import SessionFactory, create_database, initialize_database
from bot.handlers import admin, user
from bot.models import PlanTier, User, UserStatus
from bot.profile_preview import ProfilePreviewService


logger = logging.getLogger(__name__)


async def wait_for_dependencies(
    engine: AsyncEngine,
    redis: Redis,
    attempts: int = 30,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            await redis.ping()
            return
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Dependencies are not ready (attempt %s/%s): %s", attempt, attempts, exc
            )
            await asyncio.sleep(min(attempt, 5))
    raise RuntimeError("Database or Redis did not become ready") from last_error


async def configure_commands(bot: Bot, settings: Settings) -> None:
    public_commands = [
        BotCommand(command="start", description="شروع کار با ربات"),
        BotCommand(command="help", description="راهنمای استفاده"),
    ]
    await bot.set_my_commands(public_commands)
    await bot.set_my_commands(
        [
            *public_commands,
            BotCommand(command="admin", description="پنل مدیریت"),
        ],
        scope=BotCommandScopeChat(chat_id=settings.admin_telegram_id),
    )


async def ensure_primary_admin(
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    minimum_expiry = datetime.now(timezone.utc) + timedelta(days=3650)
    async with session_factory() as session:
        admin_user = await session.get(User, settings.admin_telegram_id)
        if admin_user is None:
            admin_user = User(
                telegram_id=settings.admin_telegram_id,
                username=None,
                subscription_expiry=minimum_expiry,
                status=UserStatus.ACTIVE,
                plan_tier=PlanTier.VIP,
            )
            session.add(admin_user)
        else:
            admin_user.status = UserStatus.ACTIVE
            admin_user.plan_tier = PlanTier.VIP
            if admin_user.subscription_expiry < minimum_expiry:
                admin_user.subscription_expiry = minimum_expiry
        await session.commit()


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    engine, session_factory = create_database(settings)
    redis: Redis = Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        health_check_interval=30,
        socket_keepalive=True,
    )
    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = RedisStorage(redis=redis)
    dispatcher = Dispatcher(storage=storage)
    scheduler = AsyncIOScheduler(
        timezone=timezone.utc,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 60,
        },
    )
    checker = InstagramChecker(bot, session_factory, redis, settings)
    profile_preview = ProfilePreviewService(redis, settings)

    try:
        await wait_for_dependencies(engine, redis)
        await initialize_database(engine)
        await ensure_primary_admin(session_factory, settings)
        await bot.delete_webhook(drop_pending_updates=False)
        await configure_commands(bot, settings)

        stored_interval = await redis.get("farstar:checker:interval")
        try:
            check_interval = int(stored_interval or settings.check_interval_seconds)
        except (TypeError, ValueError):
            check_interval = settings.check_interval_seconds
        if not 30 <= check_interval <= 86400:
            check_interval = settings.check_interval_seconds
            await redis.set("farstar:checker:interval", str(check_interval))

        scheduler.add_job(
            checker.run,
            trigger=IntervalTrigger(seconds=check_interval, timezone=timezone.utc),
            id="instagram-checker",
            name="Instagram public profile checker",
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
        )
        scheduler.start()

        user.register_middlewares(session_factory, settings)
        dispatcher.include_router(admin.router)
        dispatcher.include_router(user.router)

        logger.info(
            "Farstar Warner started with a %s-second check interval", check_interval
        )
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            tasks_concurrency_limit=100,
            session_factory=session_factory,
            settings=settings,
            redis=redis,
            scheduler=scheduler,
            checker=checker,
            profile_preview=profile_preview,
        )
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await profile_preview.close()
        await checker.close()
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
