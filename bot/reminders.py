from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from bot.database import SessionFactory
from bot.keyboards.inline import expiry_reminder_keyboard
from bot.models import (
    SubscriptionReminderPreference,
    User,
    UserSubscription,
    UserStatus,
)
from bot.time_utils import format_datetime_dual, to_persian_digits


logger = logging.getLogger(__name__)


async def send_expiry_reminders(bot: Bot, session_factory: SessionFactory) -> None:
    now = datetime.now(timezone.utc)
    today = now.date()
    async with session_factory() as session:
        rows = list(
            await session.execute(
                select(UserSubscription, User, SubscriptionReminderPreference)
                .join(User, User.telegram_id == UserSubscription.user_id)
                .outerjoin(
                    SubscriptionReminderPreference,
                    SubscriptionReminderPreference.user_id == UserSubscription.user_id,
                )
                .where(
                    User.status == UserStatus.ACTIVE,
                    UserSubscription.expires_at > now,
                    UserSubscription.expires_at <= now + timedelta(days=3),
                )
            )
        )
        changed = False
        for subscription, user, preference in rows:
            if preference is not None and not preference.enabled:
                continue
            if preference is not None and preference.last_notified_at is not None:
                notified_at = preference.last_notified_at
                if notified_at.tzinfo is None:
                    notified_at = notified_at.replace(tzinfo=timezone.utc)
                if notified_at.date() == today:
                    continue
            expires_at = subscription.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            seconds = (expires_at - now).total_seconds()
            days_left = max(1, min(3, int((seconds + 86399) // 86400)))
            try:
                await bot.send_message(
                    user.telegram_id,
                    "یادآوری پایان اشتراک ⏳\n\n"
                    f"تنها <b>{to_persian_digits(days_left)} روز</b> از اشتراک "
                    f"<b>{html.escape(subscription.plan_name)}</b> شما باقی مانده است. "
                    "با تمدید زودتر، مدت جدید به اعتبار فعلی شما اضافه می‌شود.\n\n"
                    f"تاریخ پایان:\n{format_datetime_dual(expires_at)}",
                    reply_markup=expiry_reminder_keyboard(),
                )
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not send expiry reminder to %s: %s", user.telegram_id, exc
                )
                continue
            if preference is None:
                preference = SubscriptionReminderPreference(user_id=user.telegram_id)
                session.add(preference)
            preference.last_notified_at = now
            changed = True
        if changed:
            await session.commit()
