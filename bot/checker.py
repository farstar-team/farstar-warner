from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from urllib.parse import unquote

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import LockError
from sqlalchemy import select

from bot.config import Settings
from bot.database import SessionFactory
from bot.models import NotificationSettings, PageStatus, TargetPage, User, UserStatus


logger = logging.getLogger(__name__)

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Mobile Safari/537.36",
)

USERNAME_PATTERNS = (
    re.compile(
        r'<meta\s+(?:property|name)=["\']og:url["\']\s+content=["\'][^"\']*instagram\.com/([^/"\'?]+)',
        re.I,
    ),
    re.compile(r'"username"\s*:\s*"([A-Za-z0-9._]{1,30})"', re.I),
    re.compile(r"&quot;username&quot;\s*:\s*&quot;([A-Za-z0-9._]{1,30})&quot;", re.I),
)
PROFILE_ID_PATTERNS = (
    re.compile(r'"profile_id"\s*:\s*"?(\d{3,30})"?', re.I),
    re.compile(r'"user_id"\s*:\s*"?(\d{3,30})"?', re.I),
    re.compile(r"&quot;profile_id&quot;\s*:\s*&quot;?(\d{3,30})", re.I),
)


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
    COOLDOWN_KEY = "farstar:checker:cooldown"
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
            follow_redirects=True,
            verify=True,
        )
        self._rate_limited = asyncio.Event()
        self._lock_lost = asyncio.Event()

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
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": random.choice(
                ("en-US,en;q=0.9", "en-GB,en;q=0.8", "en;q=0.9")
            ),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }
        url = f"{self.settings.instagram_base_url}/{username}/"

        try:
            response = await self._client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.warning("Instagram request failed for %s: %s", username, exc)
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
        if status_code != 200:
            logger.info("Instagram returned HTTP %s for %s", status_code, username)
            return ProfileResult(CheckOutcome.UNKNOWN, http_status=status_code)

        final_path = response.url.path.lower()
        body_lower = response.text[:250_000].lower()
        if final_path.startswith("/accounts/login") or final_path.startswith(
            "/challenge"
        ):
            return ProfileResult(CheckOutcome.UNKNOWN, http_status=status_code)
        if (
            "sorry, this page isn't available" in body_lower
            or "page not found" in body_lower
        ):
            return ProfileResult(CheckOutcome.DEACTIVATED, http_status=status_code)

        canonical_username = self._extract_first(USERNAME_PATTERNS, response.text)
        profile_id = self._extract_first(PROFILE_ID_PATTERNS, response.text)
        return ProfileResult(
            CheckOutcome.ACTIVE,
            canonical_username=unquote(canonical_username)
            if canonical_username
            else username,
            profile_id=profile_id,
            http_status=status_code,
        )

    async def run(self) -> None:
        if await self.redis.exists(self.COOLDOWN_KEY):
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
            cooldown = result.retry_after or self.settings.rate_limit_cooldown_seconds
            cooldown = max(60, min(cooldown, 86400))
            self._rate_limited.set()
            await self.redis.set(self.COOLDOWN_KEY, str(cooldown), ex=cooldown)
            logger.warning(
                "Instagram rate limit detected; pausing checks for %s seconds", cooldown
            )
            return

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
            new_status = (
                PageStatus.ACTIVE
                if result.outcome == CheckOutcome.ACTIVE
                else PageStatus.DEACTIVATED
            )
            canonical_username = result.canonical_username or previous_username

            target.last_known_status = new_status
            target.last_checked_at = datetime.now(timezone.utc)
            if result.profile_id:
                target.last_known_id = result.profile_id
            if canonical_username.lower() != previous_username.lower():
                target.instagram_username = canonical_username

            escaped_current = html.escape(target.instagram_username)
            escaped_previous = html.escape(previous_username)
            if (
                previous_status == PageStatus.DEACTIVATED
                and new_status == PageStatus.ACTIVE
                and settings.notify_activation
            ):
                notifications.append(
                    f"پیج فعال شد! 🎉\n\nپیج: <b>@{escaped_current}</b>"
                )
            elif (
                previous_status == PageStatus.ACTIVE
                and new_status == PageStatus.DEACTIVATED
                and settings.notify_deactivation
            ):
                notifications.append(
                    f"پیج دی‌اکتیو شد! ⚠️\n\nپیج: <b>@{escaped_current}</b>"
                )

            if (
                canonical_username.lower() != previous_username.lower()
                and settings.notify_username_change
            ):
                notifications.append(
                    "نام کاربری پیج تغییر کرد! 🔄\n\n"
                    f"نام قبلی: <b>@{escaped_previous}</b>\n"
                    f"نام جدید: <b>@{escaped_current}</b>"
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
    def _extract_first(patterns: tuple[re.Pattern[str], ...], value: str) -> str | None:
        for pattern in patterns:
            match = pattern.search(value)
            if match:
                return match.group(1)
        return None

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
