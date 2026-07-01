from __future__ import annotations

import asyncio
import html
import logging
import random
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import LockError
from sqlalchemy import select

from bot.config import Settings
from bot.database import SessionFactory
from bot.models import (
    NotificationSettings,
    PageEvent,
    PageStatus,
    TargetPage,
    User,
    UserStatus,
)


logger = logging.getLogger(__name__)

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)

WEB_PROFILE_APP_ID = "936619743392459"


class CheckOutcome(str, Enum):
    ACTIVE = "active"
    DEACTIVATED = "deactivated"
    UNKNOWN = "unknown"
    RATE_LIMITED = "rate_limited"


@dataclass(slots=True, frozen=True)
class ProfileResult:
    outcome: CheckOutcome
    canonical_username: str | None = None
    profile_id: str | None = None
    retry_after: int | None = None
    http_status: int | None = None


class InstagramChecker:
    LOCK_KEY = "farstar:checker:lock"
    STATUS_COOLDOWN_KEY = "farstar:checker:status-cooldown"
    ACCESS_ALERT_KEY = "farstar:checker:access-alert"
    DEACTIVATION_STREAK_PREFIX = "farstar:checker:deactivation-streak:"
    LOCK_TIMEOUT_SECONDS = 120

    def __init__(
        self,
        bot: Bot,
        session_factory: SessionFactory,
        redis: Redis,
        settings: Settings,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.instagram_request_timeout_seconds),
            limits=httpx.Limits(
                max_connections=max(settings.check_concurrency * 2, 10),
                max_keepalive_connections=max(settings.check_concurrency, 5),
                keepalive_expiry=30.0,
            ),
            follow_redirects=False,
            verify=True,
        )
        self._rate_limited = asyncio.Event()
        self._lock_lost = asyncio.Event()

    def set_browser_probe(
        self,
        probe: object,
    ) -> None:
        """Keep startup compatibility; status monitoring intentionally uses the API."""
        del probe

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_profile(self, username: str) -> ProfileResult:
        await asyncio.sleep(
            random.uniform(
                self.settings.check_jitter_min_seconds,
                self.settings.check_jitter_max_seconds,
            )
        )
        headers = {
            "User-Agent": USER_AGENTS[0],
            "X-IG-App-ID": WEB_PROFILE_APP_ID,
        }
        url = f"{self.settings.instagram_base_url}/api/v1/users/web_profile_info/"

        try:
            response = await self._client.get(
                url,
                params={"username": username},
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.warning("Instagram request failed for %s: %s", username, exc)
            await self._alert_access_issue(username, None)
            return ProfileResult(CheckOutcome.UNKNOWN)

        status_code = response.status_code
        if status_code == 429:
            return ProfileResult(
                CheckOutcome.RATE_LIMITED,
                retry_after=self._parse_retry_after(
                    response.headers.get("Retry-After")
                ),
                http_status=status_code,
            )
        if status_code == 404:
            return ProfileResult(CheckOutcome.DEACTIVATED, http_status=status_code)
        if status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                logger.warning("Instagram returned invalid JSON for %s", username)
                await self._alert_access_issue(username, status_code)
                return ProfileResult(CheckOutcome.UNKNOWN, http_status=status_code)

            if not isinstance(payload, dict):
                await self._alert_access_issue(username, status_code)
                return ProfileResult(CheckOutcome.UNKNOWN, http_status=status_code)
            data = payload.get("data")
            user_data = data.get("user") if isinstance(data, dict) else None
            if not isinstance(user_data, dict):
                logger.warning(
                    "Instagram JSON did not contain data.user for %s", username
                )
                await self._alert_access_issue(username, status_code)
                return ProfileResult(CheckOutcome.UNKNOWN, http_status=status_code)

            raw_profile_id = user_data.get("id")
            raw_username = user_data.get("username")
            profile_id = str(raw_profile_id).strip() if raw_profile_id else ""
            canonical_username = (
                str(raw_username).strip().lower() if raw_username else username.lower()
            )
            if not profile_id.isdigit() or not canonical_username:
                logger.warning("Instagram JSON identity was invalid for %s", username)
                await self._alert_access_issue(username, status_code)
                return ProfileResult(CheckOutcome.UNKNOWN, http_status=status_code)

            return ProfileResult(
                CheckOutcome.ACTIVE,
                canonical_username=canonical_username,
                profile_id=profile_id,
                http_status=status_code,
            )

        logger.info(
            "Instagram Web Profile API returned HTTP %s for %s",
            status_code,
            username,
        )
        await self._alert_access_issue(username, status_code)
        return ProfileResult(CheckOutcome.UNKNOWN, http_status=status_code)

    async def _alert_access_issue(
        self,
        username: str,
        http_status: int | None,
    ) -> None:
        should_alert = await self.redis.set(
            self.ACCESS_ALERT_KEY,
            "1",
            ex=3600,
            nx=True,
        )
        if not should_alert:
            return
        status_text = str(http_status) if http_status is not None else "بدون پاسخ"
        await self._notify(
            self.settings.admin_telegram_id,
            "دسترسی عمومی اینستاگرام پاسخ قطعی نداد. ⚠️\n\n"
            f"نمونه پیج: <b>@{html.escape(username)}</b>\n"
            f"کد HTTP: <code>{status_text}</code>\n"
            "وضعیت پیج‌ها تغییر نکرد. لطفاً بخش «وضعیت اتصال اینستاگرام» را بررسی کنید.",
        )

    async def run(self) -> None:
        if await self.redis.exists(self.STATUS_COOLDOWN_KEY):
            logger.info("Checker skipped because the Instagram cooldown is active")
            return

        lock = self.redis.lock(
            self.LOCK_KEY,
            timeout=self.LOCK_TIMEOUT_SECONDS,
            blocking_timeout=0,
        )
        acquired = await lock.acquire(blocking=False)
        if not acquired:
            logger.info("Checker skipped because another instance holds the lock")
            return

        self._lock_lost.clear()
        renewal_task = asyncio.create_task(self._renew_lock(lock))
        try:
            self._rate_limited.clear()
            target_ids = await self._eligible_target_ids()
            if not target_ids:
                return

            queue: asyncio.Queue[int | None] = asyncio.Queue()
            for target_id in target_ids:
                queue.put_nowait(target_id)

            worker_count = min(self.settings.check_concurrency, len(target_ids))
            for _ in range(worker_count):
                queue.put_nowait(None)
            workers = [
                asyncio.create_task(self._worker(queue)) for _ in range(worker_count)
            ]
            await asyncio.gather(*workers)
        except Exception:
            logger.exception("Unexpected checker cycle failure")
        finally:
            renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await renewal_task
            try:
                await lock.release()
            except LockError:
                logger.warning("Checker lock expired before it could be released")

    async def _renew_lock(self, lock: Lock) -> None:
        while True:
            await asyncio.sleep(self.LOCK_TIMEOUT_SECONDS / 3)
            try:
                await lock.extend(self.LOCK_TIMEOUT_SECONDS, replace_ttl=True)
            except LockError:
                self._lock_lost.set()
                logger.error("Checker lost its distributed lock; stopping this cycle")
                return
            except Exception:
                self._lock_lost.set()
                logger.exception("Checker could not renew its distributed lock")
                return

    async def _worker(self, queue: asyncio.Queue[int | None]) -> None:
        while True:
            target_id = await queue.get()
            try:
                if target_id is None:
                    return
                if self._rate_limited.is_set() or self._lock_lost.is_set():
                    continue
                await self._check_target(target_id)
            except Exception:
                logger.exception("Failed to process target %s", target_id)
            finally:
                queue.task_done()

    async def _eligible_target_ids(self) -> list[int]:
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            result = await session.scalars(
                select(TargetPage.id)
                .join(User, User.telegram_id == TargetPage.user_id)
                .where(
                    User.status == UserStatus.ACTIVE,
                    User.subscription_expiry > now,
                )
                .order_by(TargetPage.id)
            )
            return list(result)

    async def _check_target(self, target_id: int) -> None:
        async with self.session_factory() as session:
            snapshot = await session.get(TargetPage, target_id)
            if snapshot is None:
                return
            username = snapshot.instagram_username

        result = await self.fetch_profile(username)
        if result.outcome == CheckOutcome.UNKNOWN:
            return
        if result.outcome == CheckOutcome.RATE_LIMITED:
            self._rate_limited.set()
            await self._activate_cooldown(result.retry_after)
            return

        streak_key = f"{self.DEACTIVATION_STREAK_PREFIX}{target_id}"
        if result.outcome == CheckOutcome.DEACTIVATED:
            streak = await self.redis.incr(streak_key)
            if streak == 1:
                await self.redis.expire(streak_key, 86400)
            if streak < self.settings.deactivation_confirmations:
                logger.info(
                    "Waiting for deactivation confirmation %s/%s for target %s",
                    streak,
                    self.settings.deactivation_confirmations,
                    target_id,
                )
                return
            await self.redis.delete(streak_key)
        else:
            await self.redis.delete(streak_key)

        notifications: list[str] = []
        recipient_id: int | None = None
        async with self.session_factory() as session:
            target = await session.scalar(
                select(TargetPage).where(TargetPage.id == target_id).with_for_update()
            )
            if target is None:
                return

            settings = await session.get(
                NotificationSettings, (target.user_id, target.id)
            )
            if settings is None:
                settings = NotificationSettings(
                    user_id=target.user_id, target_page_id=target.id
                )
                session.add(settings)
                await session.flush()

            previous_status = target.last_known_status
            previous_username = target.instagram_username
            previous_profile_id = target.last_known_id
            new_status = (
                PageStatus.ACTIVE
                if result.outcome == CheckOutcome.ACTIVE
                else PageStatus.DEACTIVATED
            )
            canonical_username = result.canonical_username or previous_username
            canonical_username = canonical_username.lower()
            username_differs = canonical_username != previous_username.lower()
            identity_matches = bool(
                previous_profile_id
                and result.profile_id
                and previous_profile_id == result.profile_id
            )
            identity_changed = bool(
                previous_profile_id
                and result.profile_id
                and previous_profile_id != result.profile_id
            )
            username_changed = username_differs and identity_matches

            target.last_known_status = new_status
            target.last_checked_at = datetime.now(timezone.utc)
            if result.outcome == CheckOutcome.ACTIVE and result.profile_id:
                if username_differs and (identity_matches or not previous_profile_id):
                    conflicting_target = await session.scalar(
                        select(TargetPage.id).where(
                            TargetPage.user_id == target.user_id,
                            TargetPage.instagram_username == canonical_username,
                            TargetPage.id != target.id,
                        )
                    )
                    if conflicting_target is None:
                        target.instagram_username = canonical_username
                    else:
                        username_changed = False
                        session.add(
                            PageEvent(
                                target_page_id=target.id,
                                user_id=target.user_id,
                                event_type="username_conflict",
                                description=(
                                    f"نام کاربری جدید @{canonical_username} از قبل "
                                    "در فهرست کاربر ثبت شده بود."
                                ),
                            )
                        )
                target.last_known_id = result.profile_id

            escaped_current = html.escape(target.instagram_username)
            escaped_previous = html.escape(previous_username)
            if (
                previous_status == PageStatus.DEACTIVATED
                and new_status == PageStatus.ACTIVE
            ):
                session.add(
                    PageEvent(
                        target_page_id=target.id,
                        user_id=target.user_id,
                        event_type="activated",
                        description="پیج از وضعیت غیرفعال به فعال تغییر کرد.",
                    )
                )
                if settings.notify_activation:
                    notifications.append(
                        f"پیج فعال شد! 🎉\n\nپیج: <b>@{escaped_current}</b>"
                    )
            elif (
                previous_status == PageStatus.ACTIVE
                and new_status == PageStatus.DEACTIVATED
            ):
                session.add(
                    PageEvent(
                        target_page_id=target.id,
                        user_id=target.user_id,
                        event_type="deactivated",
                        description="پیج از وضعیت فعال به غیرفعال تغییر کرد.",
                    )
                )
                if settings.notify_deactivation:
                    notifications.append(
                        f"پیج دی‌اکتیو شد! ⚠️\n\nپیج: <b>@{escaped_current}</b>"
                    )

            if username_changed:
                session.add(
                    PageEvent(
                        target_page_id=target.id,
                        user_id=target.user_id,
                        event_type="username_changed",
                        description=f"نام کاربری از @{previous_username} به @{canonical_username} تغییر کرد.",
                    )
                )
                if settings.notify_username_change:
                    notifications.append(
                        "نام کاربری پیج تغییر کرد! 🔄\n\n"
                        f"نام قبلی: <b>@{escaped_previous}</b>\n"
                        f"نام جدید: <b>@{escaped_current}</b>"
                    )

            if identity_changed:
                session.add(
                    PageEvent(
                        target_page_id=target.id,
                        user_id=target.user_id,
                        event_type="identity_changed",
                        description=(
                            f"شناسه یکتای پیج از {previous_profile_id} "
                            f"به {result.profile_id} تغییر کرد."
                        ),
                    )
                )
                if settings.notify_username_change:
                    notifications.append(
                        "هویت پیج تغییر کرده است! ⚠️\n\n"
                        f"پیج: <b>@{escaped_current}</b>\n"
                        "شناسه یکتای اینستاگرام با مقدار قبلی مطابقت ندارد."
                    )

            recipient_id = target.user_id
            await session.commit()

        if recipient_id is not None:
            for message in notifications:
                await self._notify(recipient_id, message)

    async def _notify(self, telegram_id: int, message: str) -> None:
        try:
            await self.bot.send_message(telegram_id, message)
        except TelegramAPIError as exc:
            logger.warning(
                "Telegram notification failed for user %s: %s", telegram_id, exc
            )

    @staticmethod
    def _parse_retry_after(value: str | None) -> int | None:
        if not value:
            return None
        if value.isdigit():
            return int(value)
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0, int((retry_at - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None

    async def _activate_cooldown(self, requested_seconds: int | None) -> int:
        cooldown = requested_seconds or self.settings.rate_limit_cooldown_seconds
        cooldown = max(60, min(cooldown, 86400))
        await self.redis.set(self.STATUS_COOLDOWN_KEY, str(cooldown), ex=cooldown)
        logger.warning(
            "Instagram rate limit detected; pausing checks for %s seconds", cooldown
        )
        return cooldown
