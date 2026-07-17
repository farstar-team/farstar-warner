from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from redis.asyncio import Redis
from redis.exceptions import LockError, RedisError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from bot.config import Settings
from bot.database import SessionFactory
from bot.models import BroadcastCampaign, BroadcastDelivery, User, UserStatus


logger = logging.getLogger(__name__)

ACTIVE_CAMPAIGN_STATES = ("Preparing", "Queued", "Delivering")
TERMINAL_CAMPAIGN_STATES = ("Completed", "Canceled")
DELIVERABLE_STATES = ("Pending", "Retry")
CREATE_LOCK_KEY = "farstar:broadcast:create-lock"
ENQUEUE_LOCK_PREFIX = "farstar:broadcast:enqueue-lock:"
DISPATCH_LOCK_KEY = "farstar:broadcast:dispatch-lock"
RECONCILE_LOCK_KEY = "farstar:broadcast:reconcile-lock"
TELEGRAM_COOLDOWN_KEY = "farstar:broadcast:telegram-cooldown"
PROGRESS_CACHE_PREFIX = "farstar:broadcast:progress:"


@dataclass(slots=True, frozen=True)
class BroadcastSendResult:
    delivered: bool
    error: str | None = None
    retry_after: int | None = None
    terminal: bool = False


@dataclass(slots=True, frozen=True)
class BroadcastView:
    campaign_id: int
    status: str
    total: int
    sent: int
    failed: int
    skipped: int
    pending: int
    created_at: datetime
    completed_at: datetime | None
    progress_chat_id: int | None
    progress_message_id: int | None


def _digits(value: object) -> str:
    return str(value).translate(str.maketrans("0123456789,", "۰۱۲۳۴۵۶۷۸۹٬"))


def _status_label(status: str) -> str:
    return {
        "Preparing": "در حال آماده‌سازی گیرندگان",
        "Queued": "در صف ارسال",
        "Delivering": "در حال ارسال",
        "Completed": "تکمیل‌شده",
        "Canceled": "لغوشده",
    }.get(status, status)


def validate_broadcast_content(plain_text: str, message_html: str) -> str | None:
    if not plain_text.strip():
        return "پیام همگانی باید یک پیام متنی باشد؛ دوباره ارسال کنید."
    if len(plain_text.strip()) > 4000:
        return "متن پیام بیش از حد طولانی است. حداکثر ۴۰۰۰ نویسه ارسال کنید."
    if not message_html.strip() or len(message_html.strip()) > 4000:
        return (
            "متن قالب‌بندی‌شده بیش از حد طولانی است. بخشی از قالب‌بندی یا متن را "
            "کم کنید و دوباره بفرستید."
        )
    return None


def broadcast_progress_text(view: BroadcastView) -> str:
    processed = view.sent + view.failed + view.skipped
    percent = 100 if view.total == 0 and view.status == "Completed" else int(
        min(100, (processed * 100) / max(1, view.total))
    )
    lines = [
        "پیام همگانی مدیر 📣",
        "",
        f"شناسه ارسال: <code>{_digits(view.campaign_id)}</code>",
        f"وضعیت: <b>{_status_label(view.status)}</b>",
        f"پیشرفت: <b>{_digits(percent)}٪</b>",
        "",
        f"کل گیرندگان: {_digits(view.total)}",
        f"ارسال موفق: {_digits(view.sent)} ✅",
        f"در صف/تلاش مجدد: {_digits(view.pending)} ⏳",
        f"ناموفق دائمی: {_digits(view.failed)} ❌",
    ]
    if view.skipped:
        lines.append(f"لغوشده پیش از ارسال: {_digits(view.skipped)} ⏹")
    if view.status == "Completed":
        lines.extend(
            [
                "",
                "ارسال همگانی پایان یافت. گیرنده‌هایی که ربات را مسدود کرده‌اند "
                "در بخش ناموفق دائمی شمرده می‌شوند.",
            ]
        )
    elif view.status == "Canceled":
        lines.extend(["", "ادامه ارسال این پیام متوقف شده است."])
    return "\n".join(lines)


def broadcast_status_keyboard(view: BroadcastView) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if view.status in ACTIVE_CAMPAIGN_STATES:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 تازه‌سازی", callback_data=f"admin:broadcast:view:{view.campaign_id}"
                ),
                InlineKeyboardButton(
                    text="⏹ توقف ارسال",
                    callback_data=f"admin:broadcast:cancel_prompt:{view.campaign_id}",
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 تازه‌سازی", callback_data=f"admin:broadcast:view:{view.campaign_id}"
                )
            ]
        )
    if view.failed:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔁 تلاش دوباره ناموفق‌ها",
                    callback_data=f"admin:broadcast:retry:{view.campaign_id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="↩️ پنل مدیریت", callback_data="admin:home")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید و شروع ارسال",
                    callback_data="admin:broadcast:confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ لغو پیام همگانی",
                    callback_data="admin:broadcast:draft_cancel",
                )
            ],
        ]
    )


async def _send_broadcast_message(
    bot: Bot,
    recipient_id: int,
    message_html: str,
) -> BroadcastSendResult:
    try:
        await bot.send_message(recipient_id, message_html)
        return BroadcastSendResult(True)
    except TelegramRetryAfter as exc:
        return BroadcastSendResult(
            False,
            error=f"TelegramRetryAfter: {exc}",
            retry_after=max(1, int(exc.retry_after)),
        )
    except TelegramForbiddenError as exc:
        return BroadcastSendResult(
            False,
            error=f"TelegramForbiddenError: {exc}",
            terminal=True,
        )
    except TelegramBadRequest as exc:
        return BroadcastSendResult(
            False,
            error=f"TelegramBadRequest: {exc}",
            terminal=True,
        )
    except TelegramAPIError as exc:
        return BroadcastSendResult(False, error=f"TelegramAPIError: {exc}")
    except (OSError, asyncio.TimeoutError) as exc:
        return BroadcastSendResult(False, error=f"{type(exc).__name__}: {exc}")


async def create_broadcast_campaign(
    session_factory: SessionFactory,
    redis: Redis,
    *,
    admin_id: int,
    campaign_key: str,
    message_html: str,
    progress_chat_id: int,
    progress_message_id: int,
) -> tuple[int, bool]:
    """Create one campaign, returning an active campaign on duplicate confirmation."""
    lock = redis.lock(CREATE_LOCK_KEY, timeout=30, blocking_timeout=3)
    acquired = await lock.acquire(blocking=True)
    if not acquired:
        raise RuntimeError("broadcast creation is currently busy")
    try:
        async with session_factory() as session:
            existing_key = await session.scalar(
                select(BroadcastCampaign).where(
                    BroadcastCampaign.campaign_key == campaign_key
                )
            )
            if existing_key is not None:
                return existing_key.id, False
            active = await session.scalar(
                select(BroadcastCampaign)
                .where(BroadcastCampaign.status.in_(ACTIVE_CAMPAIGN_STATES))
                .order_by(BroadcastCampaign.id.desc())
                .limit(1)
            )
            if active is not None:
                return active.id, False
            campaign = BroadcastCampaign(
                campaign_key=campaign_key,
                admin_id=admin_id,
                message_html=message_html,
                status="Preparing",
                progress_chat_id=progress_chat_id,
                progress_message_id=progress_message_id,
            )
            session.add(campaign)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                stored = await session.scalar(
                    select(BroadcastCampaign).where(
                        BroadcastCampaign.campaign_key == campaign_key
                    )
                )
                if stored is None:
                    raise
                return stored.id, False
            return campaign.id, True
    finally:
        with suppress(LockError, RedisError):
            await lock.release()


async def find_active_campaign(
    session_factory: SessionFactory,
) -> int | None:
    async with session_factory() as session:
        return await session.scalar(
            select(BroadcastCampaign.id)
            .where(BroadcastCampaign.status.in_(ACTIVE_CAMPAIGN_STATES))
            .order_by(BroadcastCampaign.id.desc())
            .limit(1)
        )


async def enqueue_broadcast_recipients(
    session_factory: SessionFactory,
    redis: Redis,
    campaign_id: int,
    *,
    batch_size: int = 1000,
) -> int:
    """Resume-safe recipient materialization; no Telegram request occurs here."""
    lock = redis.lock(
        f"{ENQUEUE_LOCK_PREFIX}{campaign_id}", timeout=300, blocking_timeout=0
    )
    if not await lock.acquire(blocking=False):
        return 0
    enqueued = 0
    try:
        while True:
            async with session_factory() as session:
                campaign = await session.get(BroadcastCampaign, campaign_id)
                if campaign is None or campaign.status != "Preparing":
                    return enqueued
                cursor = campaign.enqueue_cursor
                admin_id = campaign.admin_id

            async with session_factory() as session:
                recipient_ids = list(
                    await session.scalars(
                        select(User.telegram_id)
                        .where(
                            User.telegram_id > cursor,
                            User.telegram_id != admin_id,
                            User.status != UserStatus.BANNED,
                        )
                        .order_by(User.telegram_id)
                        .limit(max(1, min(batch_size, 5000)))
                    )
                )

            now = datetime.now(timezone.utc)
            async with session_factory() as session:
                campaign = await session.scalar(
                    select(BroadcastCampaign)
                    .where(BroadcastCampaign.id == campaign_id)
                    .with_for_update()
                )
                if campaign is None or campaign.status != "Preparing":
                    return enqueued
                if not recipient_ids:
                    campaign.status = "Queued" if campaign.total_count else "Completed"
                    campaign.started_at = campaign.started_at or now
                    if not campaign.total_count:
                        campaign.completed_at = now
                    await session.commit()
                    return enqueued
                if campaign.enqueue_cursor != cursor:
                    await session.rollback()
                    continue
                session.add_all(
                    BroadcastDelivery(
                        campaign_id=campaign_id,
                        recipient_id=recipient_id,
                        status="Pending",
                        next_attempt_at=now,
                    )
                    for recipient_id in recipient_ids
                )
                campaign.enqueue_cursor = recipient_ids[-1]
                campaign.total_count += len(recipient_ids)
                await session.commit()
                enqueued += len(recipient_ids)
    finally:
        with suppress(LockError, RedisError):
            await lock.release()


async def dispatch_broadcast_deliveries(
    bot: Bot,
    session_factory: SessionFactory,
    redis: Redis,
    settings: Settings,
    *,
    limit: int = 25,
) -> int:
    """Deliver one rate-safe batch with no DB session open during Telegram calls."""
    if await redis.ttl(TELEGRAM_COOLDOWN_KEY) > 0:
        return 0
    lock = redis.lock(DISPATCH_LOCK_KEY, timeout=180, blocking_timeout=0)
    if not await lock.acquire(blocking=False):
        return 0
    sent_count = 0
    try:
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(BroadcastDelivery, BroadcastCampaign)
                    .join(
                        BroadcastCampaign,
                        BroadcastCampaign.id == BroadcastDelivery.campaign_id,
                    )
                    .where(
                        BroadcastDelivery.status.in_(DELIVERABLE_STATES),
                        BroadcastDelivery.next_attempt_at <= now,
                        BroadcastCampaign.status.in_(("Queued", "Delivering")),
                    )
                    .order_by(
                        BroadcastDelivery.next_attempt_at,
                        BroadcastDelivery.id,
                    )
                    .limit(max(1, min(limit, 30)))
                )
            ).all()

        for delivery, campaign in rows:
            result = await _send_broadcast_message(
                bot, delivery.recipient_id, campaign.message_html
            )
            now = datetime.now(timezone.utc)
            async with session_factory() as session:
                stored = await session.scalar(
                    select(BroadcastDelivery)
                    .where(BroadcastDelivery.id == delivery.id)
                    .with_for_update()
                )
                if stored is None or stored.status not in DELIVERABLE_STATES:
                    continue
                stored_campaign = await session.get(
                    BroadcastCampaign, stored.campaign_id
                )
                if stored_campaign is None or stored_campaign.status == "Canceled":
                    stored.status = "Canceled"
                    await session.commit()
                    continue
                stored_campaign.status = "Delivering"
                stored_campaign.started_at = stored_campaign.started_at or now
                stored.attempt_count += 1
                if result.delivered:
                    stored.status = "Sent"
                    stored.sent_at = now
                    stored.last_error = None
                    sent_count += 1
                else:
                    stored.last_error = (result.error or "خطای نامشخص")[:2000]
                    exhausted = stored.attempt_count >= settings.outbox_max_attempts
                    if result.terminal or exhausted:
                        stored.status = "Dead"
                    else:
                        stored.status = "Retry"
                        delay = result.retry_after or min(
                            21600,
                            30 * (2 ** min(stored.attempt_count - 1, 10)),
                        )
                        stored.next_attempt_at = now + timedelta(
                            seconds=max(1, int(delay))
                        )
                await session.commit()

            if result.retry_after:
                await redis.set(
                    TELEGRAM_COOLDOWN_KEY,
                    "1",
                    ex=max(1, int(result.retry_after)),
                )
                break
            await asyncio.sleep(0.05)
        return sent_count
    finally:
        with suppress(LockError, RedisError):
            await lock.release()


async def refresh_broadcast_view(
    session_factory: SessionFactory,
    campaign_id: int,
) -> BroadcastView | None:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        counts = {
            str(status): int(count)
            for status, count in (
                await session.execute(
                    select(
                        BroadcastDelivery.status,
                        func.count(BroadcastDelivery.id),
                    )
                    .where(BroadcastDelivery.campaign_id == campaign_id)
                    .group_by(BroadcastDelivery.status)
                )
            ).all()
        }
        campaign = await session.scalar(
            select(BroadcastCampaign)
            .where(BroadcastCampaign.id == campaign_id)
            .with_for_update()
        )
        if campaign is None:
            return None
        sent = counts.get("Sent", 0)
        failed = counts.get("Dead", 0)
        skipped = counts.get("Canceled", 0)
        pending = counts.get("Pending", 0) + counts.get("Retry", 0)
        campaign.sent_count = sent
        campaign.failed_count = failed
        campaign.skipped_count = skipped
        if campaign.status not in ("Preparing", "Canceled") and pending == 0:
            campaign.status = "Completed"
            campaign.completed_at = campaign.completed_at or now
        elif campaign.status == "Queued" and (sent or failed):
            campaign.status = "Delivering"
        view = BroadcastView(
            campaign_id=campaign.id,
            status=campaign.status,
            total=campaign.total_count,
            sent=sent,
            failed=failed,
            skipped=skipped,
            pending=pending,
            created_at=campaign.created_at,
            completed_at=campaign.completed_at,
            progress_chat_id=campaign.progress_chat_id,
            progress_message_id=campaign.progress_message_id,
        )
        await session.commit()
        return view


async def reconcile_broadcast_campaigns(
    bot: Bot,
    session_factory: SessionFactory,
    redis: Redis,
) -> None:
    """Resume preparation and keep administrator progress/final summary current."""
    lock = redis.lock(RECONCILE_LOCK_KEY, timeout=240, blocking_timeout=0)
    if not await lock.acquire(blocking=False):
        return
    try:
        async with session_factory() as session:
            preparing_ids = list(
                await session.scalars(
                    select(BroadcastCampaign.id).where(
                        BroadcastCampaign.status == "Preparing"
                    )
                )
            )
        for campaign_id in preparing_ids:
            await enqueue_broadcast_recipients(session_factory, redis, campaign_id)

        async with session_factory() as session:
            campaign_ids = list(
                await session.scalars(
                    select(BroadcastCampaign.id)
                    .where(
                        (BroadcastCampaign.status.in_(ACTIVE_CAMPAIGN_STATES))
                        | (
                            BroadcastCampaign.status.in_(TERMINAL_CAMPAIGN_STATES)
                            & BroadcastCampaign.summary_notified_at.is_(None)
                        )
                    )
                    .order_by(BroadcastCampaign.id)
                )
            )

        for campaign_id in campaign_ids:
            view = await refresh_broadcast_view(session_factory, campaign_id)
            if (
                view is None
                or view.progress_chat_id is None
                or view.progress_message_id is None
            ):
                continue
            fingerprint = ":".join(
                map(
                    str,
                    (
                        view.status,
                        view.total,
                        view.sent,
                        view.failed,
                        view.skipped,
                        view.pending,
                    ),
                )
            )
            cache_key = f"{PROGRESS_CACHE_PREFIX}{campaign_id}"
            if view.status not in TERMINAL_CAMPAIGN_STATES:
                if await redis.get(cache_key) == fingerprint:
                    continue
            edit_succeeded = False
            try:
                await bot.edit_message_text(
                    chat_id=view.progress_chat_id,
                    message_id=view.progress_message_id,
                    text=broadcast_progress_text(view),
                    reply_markup=broadcast_status_keyboard(view),
                )
                edit_succeeded = True
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    edit_succeeded = True
                else:
                    logger.warning(
                        "Could not update broadcast %s progress: %s", campaign_id, exc
                    )
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not update broadcast %s progress: %s", campaign_id, exc
                )
            if not edit_succeeded:
                continue
            await redis.set(cache_key, fingerprint, ex=604800)
            if view.status in TERMINAL_CAMPAIGN_STATES:
                async with session_factory() as session:
                    campaign = await session.get(BroadcastCampaign, campaign_id)
                    if campaign is not None:
                        campaign.summary_notified_at = datetime.now(timezone.utc)
                        await session.commit()
    finally:
        with suppress(LockError, RedisError):
            await lock.release()


async def cancel_broadcast_campaign(
    session_factory: SessionFactory,
    campaign_id: int,
) -> BroadcastView | None:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        campaign = await session.scalar(
            select(BroadcastCampaign)
            .where(BroadcastCampaign.id == campaign_id)
            .with_for_update()
        )
        if campaign is None:
            return None
        if campaign.status in ACTIVE_CAMPAIGN_STATES:
            await session.execute(
                update(BroadcastDelivery)
                .where(
                    BroadcastDelivery.campaign_id == campaign_id,
                    BroadcastDelivery.status.in_(DELIVERABLE_STATES),
                )
                .values(status="Canceled", last_error="ارسال توسط مدیر متوقف شد")
            )
            campaign.status = "Canceled"
            campaign.completed_at = now
            campaign.summary_notified_at = None
            await session.commit()
    return await refresh_broadcast_view(session_factory, campaign_id)


async def retry_failed_broadcast_deliveries(
    session_factory: SessionFactory,
    campaign_id: int,
) -> tuple[BroadcastView | None, int]:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        campaign = await session.scalar(
            select(BroadcastCampaign)
            .where(BroadcastCampaign.id == campaign_id)
            .with_for_update()
        )
        if campaign is None:
            return None, 0
        result = await session.execute(
            update(BroadcastDelivery)
            .where(
                BroadcastDelivery.campaign_id == campaign_id,
                BroadcastDelivery.status == "Dead",
            )
            .values(
                status="Retry",
                attempt_count=0,
                next_attempt_at=now,
                last_error=None,
                sent_at=None,
            )
        )
        retried = int(result.rowcount or 0)
        if retried:
            campaign.status = "Delivering"
            campaign.completed_at = None
            campaign.summary_notified_at = None
        await session.commit()
    return await refresh_broadcast_view(session_factory, campaign_id), retried


async def cleanup_broadcast_campaigns(
    session_factory: SessionFactory,
    *,
    retention_days: int = 90,
) -> int:
    """Delete old terminal campaigns; delivery rows cascade with the campaign."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(7, min(retention_days, 3650))
    )
    async with session_factory() as session:
        result = await session.execute(
            delete(BroadcastCampaign).where(
                BroadcastCampaign.status.in_(TERMINAL_CAMPAIGN_STATES),
                BroadcastCampaign.completed_at.is_not(None),
                BroadcastCampaign.completed_at < cutoff,
            )
        )
        await session.commit()
    return int(result.rowcount or 0)
