from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import secrets
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from urllib.parse import quote, urlsplit

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup
from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import LockError, RedisError
from sqlalchemy import and_, case, func, select

from bot.config import Settings
from bot.database import SessionFactory
from bot.diagnostics import DiagnosticStore
from bot.models import (
    NotificationSettings,
    PageEvent,
    PageSnapshot,
    PageStatus,
    PlanTier,
    TargetPage,
    User,
    UserSubscription,
    UserStatus,
)
from bot.report_cards import AlertCardData, ProfileCardData, ReportCardRenderer
from bot.reporting import ADMIN_REPORT_KEYS, parse_admin_report_categories


logger = logging.getLogger(__name__)

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
WEB_PROFILE_APP_ID = "936619743392459"
INSTAGRAM_REQUIRED_HEADERS = {
    "User-Agent": USER_AGENTS[0],
    "X-IG-App-ID": WEB_PROFILE_APP_ID,
}
INSTAGRAM_BROWSER_HEADERS = {
    **INSTAGRAM_REQUIRED_HEADERS,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.instagram.com/",
}
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


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
    full_name: str | None = None
    biography: str | None = None
    profile_picture_url: str | None = None
    follower_count: int | None = None
    following_count: int | None = None
    post_count: int | None = None
    is_private: bool | None = None
    is_verified: bool = False
    retry_after: int | None = None
    http_status: int | None = None
    from_cache: bool = False


@dataclass(slots=True, frozen=True)
class CurlResponse:
    status_code: int | None
    body: bytes
    error: str | None = None
    return_code: int = 0
    transport: str = "unknown"
    elapsed_ms: int | None = None


@dataclass(slots=True, frozen=True)
class NotificationPayload:
    message: str
    username: str
    title: str
    category: str
    primary_label: str
    primary_value: str
    secondary_label: str | None = None
    secondary_value: str | None = None
    accent: str = "gold"
    reply_markup: InlineKeyboardMarkup | None = None


class InstagramChecker:
    LOCK_KEY = "farstar:checker:lock"
    STATUS_COOLDOWN_KEY = "farstar:checker:status-cooldown"
    DEACTIVATION_STREAK_PREFIX = "farstar:checker:deactivation-streak:"
    PROXY_FAILURE_STREAK_KEY = "farstar:checker:proxy-failure-streak"
    PROXY_ALERT_KEY = "farstar:checker:proxy-alert-sent"
    HEALTH_LOCK_KEY = "farstar:checker:health-monitor-lock"
    HEALTH_STATE_KEY = "farstar:checker:health-state"
    HEALTH_ALERT_KEY = "farstar:checker:health-alert"
    REFERENCE_ALERT_KEY = "farstar:checker:reference-alert"
    LOCK_TIMEOUT_SECONDS = 120
    MAX_RESPONSE_BYTES = 8_000_000
    CURL_EXECUTABLE = "curl"
    CURL_ATTEMPTS = 3
    PROFILE_CACHE_PREFIX = "farstar:instagram:profile:fresh:"
    PROFILE_STALE_PREFIX = "farstar:instagram:profile:stale:"
    PROFILE_LOCK_PREFIX = "farstar:instagram:profile-lock:"
    FRESH_CACHE_SECONDS = 60
    STALE_CACHE_SECONDS = 86400

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
        self.diagnostics = DiagnosticStore(redis)
        self._rate_limited = asyncio.Event()
        self._lock_lost = asyncio.Event()
        self._http = httpx.AsyncClient(
            http2=True,
            follow_redirects=False,
            proxy=settings.instagram_proxy_url,
            trust_env=False,
            timeout=httpx.Timeout(settings.instagram_request_timeout_seconds),
            headers=INSTAGRAM_BROWSER_HEADERS,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._direct_http = httpx.AsyncClient(
            http2=True,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(settings.instagram_request_timeout_seconds),
            headers=INSTAGRAM_BROWSER_HEADERS,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def set_browser_probe(self, probe: object) -> None:
        """Retain startup compatibility; monitoring uses the curl transport only."""
        del probe

    async def close(self) -> None:
        await self._http.aclose()
        await self._direct_http.aclose()

    async def fetch_profile(
        self,
        username: str,
        *,
        allow_stale: bool = True,
        force_refresh: bool = False,
    ) -> ProfileResult:
        normalized_username = username.strip().lower()
        if not force_refresh:
            cached = await self._read_cached_profile(
                f"{self.PROFILE_CACHE_PREFIX}{normalized_username}"
            )
            if cached is not None:
                return replace(cached, from_cache=True)

        profile_lock = self.redis.lock(
            f"{self.PROFILE_LOCK_PREFIX}{normalized_username}",
            timeout=60,
            blocking_timeout=30,
        )
        acquired = await profile_lock.acquire()
        if not acquired:
            if force_refresh:
                response = await self._execute_profile_request(normalized_username)
                result = await self._profile_result_from_response(
                    response,
                    normalized_username,
                )
                if result.outcome == CheckOutcome.ACTIVE:
                    await self._cache_profile(result, normalized_username)
                elif result.outcome == CheckOutcome.DEACTIVATED:
                    await self._cache_deactivated(result, normalized_username)
                return result
            return await self._stale_or_unknown(normalized_username, allow_stale)
        try:
            if not force_refresh:
                cached = await self._read_cached_profile(
                    f"{self.PROFILE_CACHE_PREFIX}{normalized_username}"
                )
                if cached is not None:
                    return replace(cached, from_cache=True)
            response = await self._execute_profile_request(normalized_username)
            result = await self._profile_result_from_response(
                response,
                normalized_username,
            )
            if result.outcome == CheckOutcome.ACTIVE:
                await self._cache_profile(result, normalized_username)
                return result
            if result.outcome == CheckOutcome.DEACTIVATED:
                await self._cache_deactivated(result, normalized_username)
                return result
            if result.outcome in {CheckOutcome.UNKNOWN, CheckOutcome.RATE_LIMITED}:
                stale = await self._stale_or_unknown(
                    normalized_username,
                    allow_stale,
                    fallback=result,
                )
                return stale
            return result
        finally:
            with suppress(LockError):
                await profile_lock.release()

    async def _profile_result_from_response(
        self,
        response: CurlResponse,
        username: str,
    ) -> ProfileResult:
        if response.status_code is None and not response.body:
            logger.warning(
                "Instagram profile request failed for %s via %s (code %s): %s",
                username,
                response.transport,
                response.return_code,
                response.error or "unknown error",
            )
            await self._record_final_failure(username, response)
            return ProfileResult(CheckOutcome.UNKNOWN)

        result = self._parse_profile_response(
            response.body,
            username,
            response.status_code,
        )
        if result is not None:
            return result

        status_code = response.status_code
        if status_code == 404 or self._body_indicates_deactivated(response.body):
            return ProfileResult(CheckOutcome.DEACTIVATED, http_status=status_code)
        if status_code in {403, 429}:
            self._rate_limited.set()
            await self._record_final_failure(username, response)
            return ProfileResult(CheckOutcome.UNKNOWN, http_status=status_code)
        if status_code != 200:
            logger.info(
                "Instagram Web Profile API returned HTTP %s for %s",
                status_code,
                username,
            )
            await self._record_final_failure(username, response)
            return ProfileResult(CheckOutcome.UNKNOWN, http_status=status_code)
        await self._record_final_failure(username, response)
        return ProfileResult(CheckOutcome.UNKNOWN, http_status=status_code)

    async def _cache_profile(
        self,
        result: ProfileResult,
        requested_username: str,
    ) -> None:
        if not result.canonical_username:
            return
        payload = asdict(replace(result, from_cache=False))
        payload["outcome"] = result.outcome.value
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        usernames = {result.canonical_username.lower(), requested_username.lower()}
        for username in usernames:
            await self.redis.set(
                f"{self.PROFILE_CACHE_PREFIX}{username}",
                encoded,
                ex=self.FRESH_CACHE_SECONDS,
            )
            await self.redis.set(
                f"{self.PROFILE_STALE_PREFIX}{username}",
                encoded,
                ex=self.STALE_CACHE_SECONDS,
            )

    async def _read_cached_profile(self, key: str) -> ProfileResult | None:
        raw = await self.redis.get(key)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            payload["outcome"] = CheckOutcome(payload["outcome"])
            return ProfileResult(**payload)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            await self.redis.delete(key)
            return None

    async def _cache_deactivated(
        self,
        result: ProfileResult,
        username: str,
    ) -> None:
        payload = asdict(replace(result, from_cache=False))
        payload["outcome"] = result.outcome.value
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        await self.redis.set(
            f"{self.PROFILE_CACHE_PREFIX}{username}",
            encoded,
            ex=self.FRESH_CACHE_SECONDS,
        )
        await self.redis.delete(f"{self.PROFILE_STALE_PREFIX}{username}")

    async def _stale_or_unknown(
        self,
        username: str,
        allow_stale: bool,
        fallback: ProfileResult | None = None,
    ) -> ProfileResult:
        if allow_stale:
            stale = await self._read_cached_profile(
                f"{self.PROFILE_STALE_PREFIX}{username}"
            )
            if stale is not None:
                return replace(stale, from_cache=True)
        return fallback or ProfileResult(CheckOutcome.UNKNOWN)

    async def _execute_profile_request(self, username: str) -> CurlResponse:
        trace_id = secrets.token_hex(4)
        await self.diagnostics.add(
            level="INFO",
            event="profile_request_started",
            message="بررسی زنده پیج آغاز شد.",
            trace_id=trace_id,
            username=username,
        )
        last_response: CurlResponse | None = None
        for attempt in range(1, self.CURL_ATTEMPTS + 1):
            http_response = await self._execute_http2_once(username)
            await self._record_transport_attempt(
                trace_id, username, attempt, http_response
            )
            if self._response_is_authoritative(http_response, username):
                await self._record_authoritative(trace_id, username, http_response)
                return http_response
            last_response = http_response

            if self.settings.instagram_proxy_url:
                direct_http_response = await self._execute_http2_once(
                    username,
                    use_proxy=False,
                )
                await self._record_transport_attempt(
                    trace_id, username, attempt, direct_http_response
                )
                if self._response_is_authoritative(direct_http_response, username):
                    await self._record_authoritative(
                        trace_id, username, direct_http_response
                    )
                    return direct_http_response
                last_response = direct_http_response

                direct_curl_response = await self._execute_curl_once(
                    username,
                    force_http2=False,
                    use_proxy=False,
                )
                await self._record_transport_attempt(
                    trace_id, username, attempt, direct_curl_response
                )
                if self._response_is_authoritative(direct_curl_response, username):
                    await self._record_authoritative(
                        trace_id, username, direct_curl_response
                    )
                    return direct_curl_response
                last_response = direct_curl_response

            curl_http2_response = await self._execute_curl_once(
                username,
                force_http2=True,
            )
            await self._record_transport_attempt(
                trace_id, username, attempt, curl_http2_response
            )
            if self._response_is_authoritative(curl_http2_response, username):
                await self._record_authoritative(
                    trace_id, username, curl_http2_response
                )
                return curl_http2_response
            if (
                curl_http2_response.return_code != 2
                or "the installed libcurl version doesn't support"
                not in (curl_http2_response.error or "").lower()
            ):
                last_response = curl_http2_response

            curl_response = await self._execute_curl_once(
                username,
                force_http2=False,
            )
            await self._record_transport_attempt(
                trace_id, username, attempt, curl_response
            )
            if self._response_is_authoritative(curl_response, username):
                await self._record_authoritative(trace_id, username, curl_response)
                return curl_response
            last_response = curl_response

            if attempt < self.CURL_ATTEMPTS:
                ceiling = min(45.0, 15.0 * (2**attempt))
                await asyncio.sleep(random.uniform(15.0, ceiling))

        final_response = last_response or CurlResponse(
            None,
            b"",
            "Instagram request transports did not run",
            1,
            "none",
        )
        await self.diagnostics.add(
            level="ERROR",
            event="profile_request_failed",
            message="هیچ مسیر اتصال پاسخ قطعی و معتبر دریافت نکرد.",
            trace_id=trace_id,
            username=username,
            transport=final_response.transport,
            http_status=final_response.status_code,
            return_code=final_response.return_code,
            elapsed_ms=final_response.elapsed_ms,
            response_bytes=len(final_response.body),
            detail=self._response_diagnostic(final_response),
        )
        return final_response

    async def _execute_http2_once(
        self,
        username: str,
        *,
        use_proxy: bool = True,
    ) -> CurlResponse:
        safe_username = quote(username.strip(), safe="")
        url = (
            f"{self.settings.instagram_base_url}"
            f"/api/v1/users/web_profile_info/?username={safe_username}"
        )
        started_at = time.perf_counter()
        try:
            client = self._http if use_proxy else self._direct_http
            response = await client.get(url)
        except httpx.HTTPError as exc:
            return CurlResponse(
                None,
                b"",
                str(exc),
                1,
                "httpx-http2-proxy" if use_proxy else "httpx-http2-direct",
                int((time.perf_counter() - started_at) * 1000),
            )
        if len(response.content) > self.MAX_RESPONSE_BYTES:
            return CurlResponse(
                response.status_code,
                b"",
                "response exceeded safety limit",
                63,
                f"httpx-{response.http_version}-{'proxy' if use_proxy else 'direct'}",
                int((time.perf_counter() - started_at) * 1000),
            )
        return CurlResponse(
            response.status_code,
            response.content,
            f"httpx {response.http_version}",
            0,
            f"httpx-{response.http_version}-{'proxy' if use_proxy else 'direct'}",
            int((time.perf_counter() - started_at) * 1000),
        )

    def _response_is_authoritative(
        self,
        response: CurlResponse,
        username: str,
    ) -> bool:
        if response.status_code == 404:
            return True
        if response.status_code != 200:
            return False
        return self._parse_profile_response(
            response.body, username, 200
        ) is not None or self._body_indicates_deactivated(response.body)

    async def _execute_curl(self, username: str) -> CurlResponse:
        last_response: CurlResponse | None = None
        for attempt in range(1, self.CURL_ATTEMPTS + 1):
            response = await self._execute_curl_once(username, force_http2=False)
            if response.status_code in {200, 404} and response.body:
                return response
            if response.status_code == 404:
                return response
            last_response = response
            if attempt < self.CURL_ATTEMPTS:
                await asyncio.sleep(0.4 * attempt)
        return last_response or CurlResponse(None, b"", "curl did not run", 1)

    async def _execute_curl_once(
        self,
        username: str,
        *,
        force_http2: bool,
        use_proxy: bool = True,
    ) -> CurlResponse:
        safe_username = quote(username.strip(), safe="")
        url = (
            f"{self.settings.instagram_base_url}"
            f"/api/v1/users/web_profile_info/?username={safe_username}"
        )
        request_timeout = self.settings.instagram_request_timeout_seconds
        status_marker = "\n__FARSTAR_HTTP_STATUS__:%{http_code}"
        command = (
            self.CURL_EXECUTABLE,
            "-s",
            "--http2",
            "-A",
            USER_AGENTS[0],
            "-H",
            f"X-IG-App-ID: {WEB_PROFILE_APP_ID}",
            "--write-out",
            status_marker,
            url,
        )
        if use_proxy and self.settings.instagram_proxy_url:
            proxy_url = self.settings.instagram_proxy_url
            if proxy_url.startswith("socks5://"):
                proxy_url = proxy_url.replace("socks5://", "socks5h://", 1)
            command = (*command[:-1], "--proxy", proxy_url, command[-1])
        if not force_http2:
            command = tuple(part for part in command if part != "--http2")
        transport = "curl-http2" if force_http2 else "curl"
        transport += (
            "-proxy" if use_proxy and self.settings.instagram_proxy_url else "-direct"
        )
        started_at = time.perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            return CurlResponse(
                None,
                b"",
                str(exc),
                127,
                transport,
                int((time.perf_counter() - started_at) * 1000),
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=request_timeout + 5,
            )
        except TimeoutError:
            process.kill()
            with suppress(Exception):
                await process.communicate()
            return CurlResponse(
                None,
                b"",
                "curl execution timed out",
                28,
                transport,
                int((time.perf_counter() - started_at) * 1000),
            )

        error = stderr.decode("utf-8", errors="replace").strip() or None
        if process.returncode != 0:
            return CurlResponse(
                None,
                b"",
                error,
                process.returncode or 1,
                transport,
                int((time.perf_counter() - started_at) * 1000),
            )
        if len(stdout) > self.MAX_RESPONSE_BYTES:
            return CurlResponse(
                None,
                b"",
                "response exceeded safety limit",
                63,
                transport,
                int((time.perf_counter() - started_at) * 1000),
            )

        marker = b"\n__FARSTAR_HTTP_STATUS__:"
        body, separator, status_bytes = stdout.rpartition(marker)
        status_bytes = status_bytes.strip()
        if not separator or not status_bytes.isdigit():
            return CurlResponse(
                None,
                b"",
                "curl did not return an HTTP status",
                1,
                transport,
                int((time.perf_counter() - started_at) * 1000),
            )
        return CurlResponse(
            int(status_bytes),
            body,
            error,
            process.returncode or 0,
            transport,
            int((time.perf_counter() - started_at) * 1000),
        )

    @classmethod
    def _parse_profile_response(
        cls,
        body: bytes,
        requested_username: str,
        status_code: int,
    ) -> ProfileResult | None:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        user_data = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user_data, dict):
            return None

        raw_profile_id = user_data.get("id")
        raw_username = user_data.get("username")
        profile_id = str(raw_profile_id).strip() if raw_profile_id else ""
        canonical_username = (
            str(raw_username).strip().lower()
            if raw_username
            else requested_username.lower()
        )
        if not profile_id.isdigit() or not canonical_username:
            return None

        return ProfileResult(
            outcome=CheckOutcome.ACTIVE,
            canonical_username=canonical_username,
            profile_id=profile_id,
            full_name=cls._optional_text(user_data.get("full_name")),
            biography=cls._optional_text(user_data.get("biography")),
            profile_picture_url=cls._optional_text(
                user_data.get("profile_pic_url_hd") or user_data.get("profile_pic_url")
            ),
            follower_count=cls._edge_count(user_data, "edge_followed_by"),
            following_count=cls._edge_count(user_data, "edge_follow"),
            post_count=cls._edge_count(
                user_data,
                "edge_owner_to_timeline_media",
            ),
            is_private=(
                user_data.get("is_private")
                if isinstance(user_data.get("is_private"), bool)
                else None
            ),
            is_verified=bool(user_data.get("is_verified", False)),
            http_status=status_code,
        )

    @staticmethod
    def _body_indicates_deactivated(body: bytes) -> bool:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        data = payload.get("data")
        if isinstance(data, dict) and data.get("user") is None:
            return True
        message = str(payload.get("message") or "").lower()
        status = str(payload.get("status") or "").lower()
        return status == "fail" and any(
            phrase in message
            for phrase in (
                "not found",
                "not available",
                "doesn't exist",
                "user not found",
            )
        )

    async def _record_transport_attempt(
        self,
        trace_id: str,
        username: str,
        attempt: int,
        response: CurlResponse,
    ) -> None:
        authoritative = self._response_is_authoritative(response, username)
        await self.diagnostics.add(
            level="INFO" if authoritative else "WARNING",
            event="transport_attempt",
            message=(
                f"تلاش شماره {attempt} پاسخ معتبر داد."
                if authoritative
                else f"تلاش شماره {attempt} پاسخ معتبر نداد و مسیر بعدی اجرا می‌شود."
            ),
            trace_id=trace_id,
            username=username,
            transport=response.transport,
            http_status=response.status_code,
            return_code=response.return_code,
            elapsed_ms=response.elapsed_ms,
            response_bytes=len(response.body),
            detail=self._response_diagnostic(response),
        )

    async def _record_authoritative(
        self,
        trace_id: str,
        username: str,
        response: CurlResponse,
    ) -> None:
        parsed = self._parse_profile_response(
            response.body,
            username,
            response.status_code or 0,
        )
        if parsed is not None:
            message = "اطلاعات معتبر پیج از data.user استخراج شد."
        else:
            message = "پاسخ قطعی نبودن پیج دریافت شد."
        await self.diagnostics.add(
            level="INFO",
            event="profile_request_succeeded",
            message=message,
            trace_id=trace_id,
            username=username,
            transport=response.transport,
            http_status=response.status_code,
            return_code=response.return_code,
            elapsed_ms=response.elapsed_ms,
            response_bytes=len(response.body),
            detail=self._response_diagnostic(response),
        )

    async def _record_final_failure(
        self,
        username: str,
        response: CurlResponse,
    ) -> None:
        await self.diagnostics.add(
            level="ERROR",
            event="profile_result_unknown",
            message=(
                "نتیجه قطعی نبود؛ برای جلوگیری از هشدار اشتباه، وضعیت ذخیره‌شده تغییر نکرد."
            ),
            username=username,
            transport=response.transport,
            http_status=response.status_code,
            return_code=response.return_code,
            elapsed_ms=response.elapsed_ms,
            response_bytes=len(response.body),
            detail=self._response_diagnostic(response),
        )

    @classmethod
    def _response_diagnostic(cls, response: CurlResponse) -> str:
        if response.status_code is None:
            return response.error or "هیچ پاسخ HTTP دریافت نشد."
        status_reasons = {
            301: "اینستاگرام درخواست را به آدرس دیگری منتقل کرد.",
            302: "اینستاگرام درخواست را ریدایرکت کرد؛ احتمال انتقال به صفحه ورود وجود دارد.",
            400: "ساختار درخواست از سمت اینستاگرام نامعتبر تشخیص داده شد.",
            401: "اینستاگرام درخواست بدون نشست را نپذیرفت (Unauthorized).",
            403: "دسترسی این IP یا اثرانگشت درخواست از سمت اینستاگرام رد شد.",
            404: "اینستاگرام پیج را پیدا نکرد؛ پاسخ قطعی غیرفعال/تغییرنام محسوب می‌شود.",
            429: "اینستاگرام تعداد درخواست‌ها را محدود کرده است.",
        }
        if response.status_code in status_reasons:
            reason = status_reasons[response.status_code]
        elif response.status_code >= 500:
            reason = "سرویس اینستاگرام خطای سمت سرور برگرداند."
        elif response.status_code != 200:
            reason = f"پاسخ HTTP {response.status_code} قطعی و قابل پردازش نبود."
        else:
            reason = "پاسخ HTTP 200 دریافت شد."

        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            data = payload.get("data")
            user = data.get("user") if isinstance(data, dict) else None
            if isinstance(user, dict):
                profile_id = str(user.get("id") or "")
                username = str(user.get("username") or "")
                if profile_id.isdigit() and username:
                    return f"{reason} ساختار data.user و شناسه عددی معتبر است."
                return f"{reason} data.user وجود دارد اما id یا username معتبر نیست."
            message = str(payload.get("message") or "").strip()
            status = str(payload.get("status") or "").strip()
            suffix = "; ".join(
                part
                for part in (
                    f"status={status}" if status else "",
                    f"message={message}" if message else "",
                )
                if part
            )
            return (
                f"{reason} data.user معتبر وجود ندارد.{f' {suffix}' if suffix else ''}"
            )

        lowered = response.body[:2000].decode("utf-8", errors="ignore").lower()
        if "accounts/login" in lowered or "log in" in lowered:
            return f"{reason} محتوای پاسخ نشانه صفحه ورود اینستاگرام دارد."
        if response.status_code == 200:
            return f"{reason} بدنه پاسخ JSON معتبر نبود یا data.user نداشت."
        return reason

    async def proxy_preflight(self) -> bool:
        proxy_url = self.settings.instagram_proxy_url
        if not proxy_url:
            await self.diagnostics.add(
                level="WARNING",
                event="proxy_disabled",
                message="پراکسی وارپ تنظیم نشده است؛ بررسی با مسیر مستقیم ادامه می‌یابد.",
            )
            return True

        started_at = time.perf_counter()
        try:
            trace_response = await self._http.get(
                self.settings.proxy_health_url,
                headers={"User-Agent": USER_AGENTS[0]},
            )
        except httpx.HTTPError as exc:
            return await self._register_proxy_failure(
                f"اتصال به health endpoint وارپ ناموفق بود: {type(exc).__name__}: {exc}"
            )

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        trace_body = trace_response.text.lower()
        if trace_response.status_code != 200 or not any(
            marker in trace_body for marker in ("warp=on", "warp=plus")
        ):
            return await self._register_proxy_failure(
                "health endpoint تأیید نکرد که ترافیک از WARP عبور می‌کند.",
                http_status=trace_response.status_code,
                elapsed_ms=elapsed_ms,
            )

        instagram_response = await self._instagram_preflight_response()
        if not self._response_is_authoritative(instagram_response, "instagram"):
            return await self._register_proxy_failure(
                self._response_diagnostic(instagram_response),
                http_status=instagram_response.status_code,
                elapsed_ms=instagram_response.elapsed_ms,
            )

        await self.redis.delete(self.PROXY_FAILURE_STREAK_KEY, self.PROXY_ALERT_KEY)
        await self.diagnostics.add(
            level="INFO",
            event="proxy_preflight_succeeded",
            message="سلامت WARP و دسترسی Web Profile اینستاگرام تأیید شد.",
            transport=instagram_response.transport,
            http_status=instagram_response.status_code,
            elapsed_ms=instagram_response.elapsed_ms,
            response_bytes=len(instagram_response.body),
            detail=f"proxy={proxy_url}; warp=on",
        )
        return True

    async def _instagram_preflight_response(self) -> CurlResponse:
        responses = [await self._execute_http2_once("instagram")]
        if not self._response_is_authoritative(responses[-1], "instagram"):
            responses.append(
                await self._execute_curl_once("instagram", force_http2=True)
            )
        if not self._response_is_authoritative(responses[-1], "instagram"):
            responses.append(
                await self._execute_curl_once("instagram", force_http2=False)
            )
        return responses[-1]

    async def _register_proxy_failure(
        self,
        detail: str,
        *,
        http_status: int | None = None,
        elapsed_ms: int | None = None,
    ) -> bool:
        streak = await self.redis.incr(self.PROXY_FAILURE_STREAK_KEY)
        if streak == 1:
            await self.redis.expire(self.PROXY_FAILURE_STREAK_KEY, 86400)
        await self.diagnostics.add(
            level="ERROR",
            event="proxy_preflight_failed",
            message=(
                "مسیر WARP پاسخ سالم نداد؛ تصمیم ادامه پایش با تست مرجع اینستاگرام انجام می‌شود."
            ),
            http_status=http_status,
            elapsed_ms=elapsed_ms,
            detail=f"failure_streak={streak}; {detail}",
        )
        if streak >= 3:
            should_alert = await self.redis.set(
                self.PROXY_ALERT_KEY,
                "1",
                ex=21600,
                nx=True,
            )
            if should_alert:
                await self._notify(
                    self.settings.admin_telegram_id,
                    "⚠️ سیستم وارپ یا مسیر دسترسی اینستاگرام با اختلال مواجه شده است!\n\n"
                    "سه چرخه متوالی پاسخ سالم دریافت نشد و پایش برای جلوگیری از "
                    "اعلان اشتباه متوقف ماند. جزئیات کامل در بخش «لاگ کامل و "
                    "عیب‌یابی» پنل مدیریت ثبت شده است.",
                )
        return False

    async def reference_profile_preflight(self) -> bool:
        result = await self.fetch_profile(
            "instagram",
            allow_stale=False,
            force_refresh=True,
        )
        if result.outcome == CheckOutcome.ACTIVE and result.profile_id:
            await self.redis.delete(self.REFERENCE_ALERT_KEY)
            await self.diagnostics.add(
                level="INFO",
                event="reference_profile_succeeded",
                message=(
                    "پیج مرجع @instagram با پاسخ زنده و معتبر دیده شد؛ "
                    "چرخه پایش مجاز به بررسی هدف‌ها است."
                ),
                username="instagram",
                http_status=result.http_status,
                detail=f"profile_id={result.profile_id}; from_cache={result.from_cache}",
            )
            return True

        await self.diagnostics.add(
            level="ERROR",
            event="reference_profile_failed",
            message=(
                "پیج مرجع @instagram پاسخ زنده معتبر نداد؛ برای جلوگیری از هشدار اشتباه "
                "وضعیت هیچ پیجی تغییر نمی‌کند."
            ),
            username="instagram",
            http_status=result.http_status,
            detail=f"outcome={result.outcome.value}; from_cache={result.from_cache}",
        )
        should_alert = await self.redis.set(
            self.REFERENCE_ALERT_KEY,
            "1",
            ex=900,
            nx=True,
        )
        if should_alert:
            await self._notify(
                self.settings.admin_telegram_id,
                "تست مرجع اینستاگرام ناموفق بود. ⚠️\n\n"
                "ربات نتوانست پیج ثابت <b>@instagram</b> را با پاسخ زنده ببیند؛ "
                "بنابراین این چرخه بدون تغییر وضعیت پیج‌های کاربران متوقف شد. "
                "جزئیات در بخش لاگ کامل ثبت شده است.",
            )
        return False

    async def health_monitor(self) -> None:
        lock = self.redis.lock(
            self.HEALTH_LOCK_KEY,
            timeout=240,
            blocking_timeout=0,
        )
        if not await lock.acquire(blocking=False):
            return
        try:
            failure_reason: str | None = None
            proxy_ok = await self.proxy_preflight()
            reference_ok = await self.reference_profile_preflight()
            if not reference_ok:
                latest = await self.diagnostics.latest(1)
                failure_reason = (
                    latest[0].detail
                    if latest and latest[0].detail
                    else "پیج مرجع @instagram پاسخ زنده معتبر نداد."
                )
            else:
                result = await self.fetch_profile(
                    "instagram",
                    allow_stale=False,
                    force_refresh=True,
                )
                if result.outcome != CheckOutcome.ACTIVE:
                    failure_reason = (
                        "Web Profile API برای پیج آزمایشی پاسخ معتبر نداد؛ "
                        f"outcome={result.outcome.value}; http={result.http_status}"
                    )
                else:
                    try:
                        await asyncio.to_thread(
                            ReportCardRenderer.render_profile,
                            ProfileCardData(
                                username=result.canonical_username or "instagram",
                                full_name=result.full_name,
                                biography=result.biography,
                                follower_count=result.follower_count,
                                following_count=result.following_count,
                                post_count=result.post_count,
                                is_private=result.is_private,
                                is_verified=result.is_verified,
                            ),
                        )
                    except Exception as exc:
                        failure_reason = (
                            "موتور گزارش تصویری نتوانست کارت آزمایشی بسازد؛ "
                            f"{type(exc).__name__}: {exc}"
                        )

            previous_state = await self.redis.get(self.HEALTH_STATE_KEY)
            if failure_reason is None:
                await self.redis.set(self.HEALTH_STATE_KEY, "healthy", ex=86400)
                await self.redis.delete(self.HEALTH_ALERT_KEY)
                await self.diagnostics.add(
                    level="INFO",
                    event="health_monitor_succeeded",
                    message=(
                        "تست پنج‌دقیقه‌ای API مرجع و موتور گزارش تصویری موفق بود؛ "
                        f"وضعیت WARP={'سالم' if proxy_ok else 'در حالت failover مستقیم'}."
                    ),
                )
                if previous_state == "failed":
                    await self._notify(
                        self.settings.admin_telegram_id,
                        "سیستم پایش دوباره سالم شد. ✅\n\n"
                        "پیج مرجع اینستاگرام و تولید گزارش تصویری با موفقیت "
                        "آزمایش شدند؛ مسیر اتصال نیز آماده پایش است.",
                    )
                return

            await self.redis.set(self.HEALTH_STATE_KEY, "failed", ex=86400)
            await self.diagnostics.add(
                level="ERROR",
                event="health_monitor_failed",
                message="تست پنج‌دقیقه‌ای سلامت سامانه ناموفق بود.",
                detail=failure_reason,
            )
            should_alert = await self.redis.set(
                self.HEALTH_ALERT_KEY,
                "1",
                ex=900,
                nx=True,
            )
            if should_alert:
                await self._notify(
                    self.settings.admin_telegram_id,
                    "ربات در تهیه گزارش آزمایشی با مشکل روبه‌رو شد. ⚠️\n\n"
                    f"علت تشخیصی: <code>{html.escape(failure_reason[:700])}</code>\n\n"
                    "جزئیات بیشتر در بخش «لاگ کامل و عیب‌یابی» ثبت شده است.",
                )
        except Exception as exc:
            logger.exception("Health monitor failed unexpectedly")
            await self.diagnostics.add(
                level="CRITICAL",
                event="health_monitor_exception",
                message="اجرای مانیتور سلامت با خطای داخلی متوقف شد.",
                detail=f"{type(exc).__name__}: {exc}",
            )
            with suppress(Exception):
                should_alert = await self.redis.set(
                    self.HEALTH_ALERT_KEY,
                    "1",
                    ex=900,
                    nx=True,
                )
                if should_alert:
                    await self._notify(
                        self.settings.admin_telegram_id,
                        "مانیتور سلامت ربات با خطای داخلی متوقف شد. ⚠️\n\n"
                        f"نوع خطا: <code>{html.escape(type(exc).__name__)}</code>\n"
                        "جزئیات کامل در لاگ عیب‌یابی ثبت شده است.",
                    )
        finally:
            with suppress(LockError, RedisError):
                await lock.release()

    async def run(self) -> None:
        if await self.redis.exists(self.STATUS_COOLDOWN_KEY):
            await self.redis.delete(self.STATUS_COOLDOWN_KEY)
            logger.info("Removed legacy Instagram cooldown key before checker cycle")

        lock = self.redis.lock(
            self.LOCK_KEY,
            timeout=self.LOCK_TIMEOUT_SECONDS,
            blocking_timeout=0,
        )
        acquired = await lock.acquire(blocking=False)
        if not acquired:
            logger.info("Checker skipped because another instance holds the lock")
            await self.diagnostics.add(
                level="INFO",
                event="checker_cycle_skipped",
                message="چرخه اجرا نشد؛ نمونه دیگری از چکر قفل اجرا را در اختیار دارد.",
            )
            return

        self._lock_lost.clear()
        renewal_task = asyncio.create_task(self._renew_lock(lock))
        try:
            self._rate_limited.clear()
            proxy_ok = await self.proxy_preflight()
            if not await self.reference_profile_preflight():
                return
            if not proxy_ok:
                await self.diagnostics.add(
                    level="WARNING",
                    event="checker_direct_fallback_enabled",
                    message=(
                        "WARP سالم نبود اما پیج مرجع از مسیر جایگزین دیده شد؛ "
                        "چرخه با failover مستقیم ادامه می‌یابد."
                    ),
                )
            target_ids = await self._eligible_target_ids()
            await self.diagnostics.add(
                level="INFO",
                event="checker_cycle_started",
                message=f"چرخه پایش برای {len(target_ids)} پیج آغاز شد.",
            )
            if not target_ids:
                return

            queue: asyncio.Queue[int | None] = asyncio.Queue()
            for target_id in target_ids:
                queue.put_nowait(target_id)

            worker_count = min(
                self.settings.check_concurrency,
                len(target_ids),
            )
            for _ in range(worker_count):
                queue.put_nowait(None)
            workers = [
                asyncio.create_task(self._worker(queue)) for _ in range(worker_count)
            ]
            await asyncio.gather(*workers)
            await self.diagnostics.add(
                level="INFO",
                event="checker_cycle_completed",
                message=f"چرخه پایش {len(target_ids)} پیج پایان یافت.",
            )
        except Exception as exc:
            logger.exception("Unexpected checker cycle failure")
            await self.diagnostics.add(
                level="ERROR",
                event="checker_cycle_failed",
                message="چرخه پایش با خطای داخلی متوقف شد.",
                detail=f"{type(exc).__name__}: {exc}",
            )
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
            except Exception as exc:
                logger.exception("Failed to process target %s", target_id)
                await self.diagnostics.add(
                    level="ERROR",
                    event="target_processing_failed",
                    message=f"پردازش هدف داخلی {target_id} با خطا متوقف شد.",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            finally:
                queue.task_done()
            if target_id is not None and not (
                self._rate_limited.is_set() or self._lock_lost.is_set()
            ):
                await asyncio.sleep(
                    random.uniform(
                        self.settings.page_check_delay_min_seconds,
                        self.settings.page_check_delay_max_seconds,
                    )
                )

    async def _eligible_target_ids(self) -> list[int]:
        now = datetime.now(timezone.utc)
        effective_limit = case(
            (
                and_(
                    UserSubscription.expires_at.is_not(None),
                    UserSubscription.expires_at > now,
                ),
                UserSubscription.target_limit,
            ),
            (
                and_(
                    User.plan_tier == PlanTier.VIP,
                    User.subscription_expiry > now,
                ),
                PlanTier.VIP.target_limit,
            ),
            (
                and_(
                    User.plan_tier == PlanTier.PREMIUM,
                    User.subscription_expiry > now,
                ),
                PlanTier.PREMIUM.target_limit,
            ),
            else_=PlanTier.FREE.target_limit,
        )
        ranked_targets = (
            select(
                TargetPage.id.label("target_id"),
                func.row_number()
                .over(
                    partition_by=TargetPage.user_id,
                    order_by=TargetPage.id,
                )
                .label("target_rank"),
                effective_limit.label("target_limit"),
            )
            .join(User, User.telegram_id == TargetPage.user_id)
            .outerjoin(
                UserSubscription,
                UserSubscription.user_id == User.telegram_id,
            )
            .where(User.status == UserStatus.ACTIVE)
            .subquery()
        )
        async with self.session_factory() as session:
            result = await session.scalars(
                select(ranked_targets.c.target_id)
                .where(ranked_targets.c.target_rank <= ranked_targets.c.target_limit)
                .order_by(ranked_targets.c.target_id)
            )
            return list(result)

    async def check_target_now(self, target_id: int) -> ProfileResult | None:
        self._rate_limited.clear()
        if not await self.reference_profile_preflight():
            return None
        return await self._check_target(target_id)

    async def _record_inconclusive_check(
        self,
        target_id: int,
        result: ProfileResult,
        *,
        outcome: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            target = await session.scalar(
                select(TargetPage).where(TargetPage.id == target_id).with_for_update()
            )
            if target is None:
                return
            target.last_checked_at = datetime.now(timezone.utc)
            target.last_check_outcome = outcome or result.outcome.value
            target.last_http_status = result.http_status
            await session.commit()

    async def _check_target(self, target_id: int) -> ProfileResult | None:
        async with self.session_factory() as session:
            snapshot = await session.get(TargetPage, target_id)
            if snapshot is None:
                return None
            username = snapshot.instagram_username
            known_status = snapshot.last_known_status

        # Background state transitions must only use a live Instagram response.
        # Stale metadata is reserved for user-facing profile previews.
        result = await self.fetch_profile(
            username,
            allow_stale=False,
            force_refresh=True,
        )
        await self.diagnostics.add(
            level=(
                "INFO"
                if result.outcome in {CheckOutcome.ACTIVE, CheckOutcome.DEACTIVATED}
                else "WARNING"
            ),
            event="target_result",
            message=f"نتیجه نهایی پایش: {result.outcome.value}",
            username=username,
            http_status=result.http_status,
            detail=(
                f"target_id={target_id}; profile_id={result.profile_id or 'none'}; "
                f"from_cache={result.from_cache}"
            ),
        )
        if result.outcome == CheckOutcome.UNKNOWN:
            await self._record_inconclusive_check(target_id, result)
            return result
        if result.outcome == CheckOutcome.RATE_LIMITED:
            logger.warning(
                "Ignoring non-authoritative rate-limit result for %s", username
            )
            await self._record_inconclusive_check(target_id, result)
            return result

        streak_key = f"{self.DEACTIVATION_STREAK_PREFIX}{target_id}"
        await self.redis.delete(streak_key)
        if (
            result.outcome == CheckOutcome.DEACTIVATED
            and known_status != PageStatus.DEACTIVATED
        ):
            confirmations = 1
            while confirmations < self.settings.deactivation_confirmations:
                await asyncio.sleep(random.uniform(2.0, 5.0))
                confirmation = await self.fetch_profile(
                    username,
                    allow_stale=False,
                    force_refresh=True,
                )
                if confirmation.outcome != CheckOutcome.DEACTIVATED:
                    await self._record_inconclusive_check(
                        target_id,
                        confirmation,
                        outcome="UnconfirmedDeactivation",
                    )
                    await self.diagnostics.add(
                        level="WARNING",
                        event="deactivation_confirmation_rejected",
                        message=(
                            "پاسخ غیرفعال در تأیید فوری تکرار نشد؛ وضعیت ذخیره‌شده "
                            "بدون تغییر باقی ماند."
                        ),
                        username=username,
                        http_status=confirmation.http_status,
                        detail=(
                            f"target_id={target_id}; first=deactivated; "
                            f"confirmation={confirmation.outcome.value}"
                        ),
                    )
                    return confirmation
                confirmations += 1
            await self.diagnostics.add(
                level="INFO",
                event="deactivation_confirmed_immediately",
                message=("غیرفعال‌شدن پیج با چند پاسخ قطعی در همان چرخه تأیید شد."),
                username=username,
                http_status=result.http_status,
                detail=(f"target_id={target_id}; confirmations={confirmations}"),
            )

        notifications: list[NotificationPayload] = []
        recipient_id: int | None = None
        admin_report_categories: set[str] = set()
        async with self.session_factory() as session:
            target = await session.scalar(
                select(TargetPage).where(TargetPage.id == target_id).with_for_update()
            )
            if target is None:
                return None
            owner = await session.get(User, target.user_id)
            if owner is not None:
                admin_report_categories = parse_admin_report_categories(
                    owner.admin_report_categories
                )
                if owner.admin_report_copy and not admin_report_categories:
                    admin_report_categories = set(ADMIN_REPORT_KEYS)

            notification_settings = await session.get(
                NotificationSettings,
                (target.user_id, target.id),
            )
            if notification_settings is None:
                notification_settings = NotificationSettings(
                    user_id=target.user_id,
                    target_page_id=target.id,
                )
                session.add(notification_settings)
                await session.flush()

            previous_status = target.last_known_status
            previous_username = target.instagram_username
            previous_profile_id = target.last_known_id
            new_status = (
                PageStatus.ACTIVE
                if result.outcome == CheckOutcome.ACTIVE
                else PageStatus.DEACTIVATED
            )

            target.last_known_status = new_status
            checked_at = datetime.now(timezone.utc)
            target.last_checked_at = checked_at
            target.last_successful_check_at = checked_at
            target.last_check_outcome = result.outcome.value
            target.last_http_status = result.http_status
            target.status_confirmed = True
            if new_status == PageStatus.ACTIVE:
                target.consecutive_active_checks += 1
                target.consecutive_deactivated_checks = 0
            else:
                target.consecutive_deactivated_checks = max(
                    self.settings.deactivation_confirmations,
                    target.consecutive_deactivated_checks + 1,
                )
                target.consecutive_active_checks = 0
            if previous_status != new_status or target.last_status_changed_at is None:
                target.last_status_changed_at = checked_at
            username_changed = False
            identity_changed = False

            if result.outcome == CheckOutcome.ACTIVE and result.profile_id:
                canonical_username = (
                    result.canonical_username or previous_username
                ).lower()
                same_identity = bool(
                    previous_profile_id and previous_profile_id == result.profile_id
                )
                identity_changed = bool(
                    previous_profile_id and previous_profile_id != result.profile_id
                )
                if canonical_username != previous_username.lower() and (
                    same_identity or not previous_profile_id
                ):
                    conflict = await session.scalar(
                        select(TargetPage.id).where(
                            TargetPage.user_id == target.user_id,
                            TargetPage.instagram_username == canonical_username,
                            TargetPage.id != target.id,
                        )
                    )
                    if conflict is None:
                        target.instagram_username = canonical_username
                        username_changed = same_identity
                    else:
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

                snapshot = await session.get(PageSnapshot, target.id)
                picture_key = self._profile_picture_key(result.profile_picture_url)
                if snapshot is None:
                    snapshot = PageSnapshot(
                        target_page_id=target.id,
                        user_id=target.user_id,
                    )
                    session.add(snapshot)
                else:
                    escaped_page = html.escape(target.instagram_username)
                    if snapshot.is_verified != result.is_verified:
                        verification_event = (
                            "verification_added"
                            if result.is_verified
                            else "verification_removed"
                        )
                        verification_text = (
                            "تیک آبی پیج دریافت شد."
                            if result.is_verified
                            else "تیک آبی پیج حذف شد."
                        )
                        session.add(
                            PageEvent(
                                target_page_id=target.id,
                                user_id=target.user_id,
                                event_type=verification_event,
                                description=verification_text,
                            )
                        )
                        if notification_settings.notify_verification_change:
                            notifications.append(
                                NotificationPayload(
                                    message=(
                                        f"{verification_text} {'✅' if result.is_verified else '⚠️'}\n\n"
                                        f"پیج: <b>@{escaped_page}</b>"
                                    ),
                                    username=target.instagram_username,
                                    title=(
                                        "پیج تیک آبی گرفت"
                                        if result.is_verified
                                        else "تیک آبی پیج حذف شد"
                                    ),
                                    category="VERIFICATION",
                                    primary_label="وضعیت تأیید",
                                    primary_value=(
                                        "دارای تیک آبی"
                                        if result.is_verified
                                        else "بدون تیک آبی"
                                    ),
                                    accent="blue" if result.is_verified else "red",
                                )
                            )
                    if (
                        picture_key
                        and snapshot.profile_picture_key
                        and picture_key != snapshot.profile_picture_key
                    ):
                        session.add(
                            PageEvent(
                                target_page_id=target.id,
                                user_id=target.user_id,
                                event_type="profile_picture_changed",
                                description="تصویر پروفایل پیج تغییر کرد.",
                            )
                        )
                        notifications.append(
                            NotificationPayload(
                                message=(
                                    "عکس پروفایل پیج تغییر کرد! 🖼️\n\n"
                                    f"پیج: <b>@{escaped_page}</b>"
                                ),
                                username=target.instagram_username,
                                title="عکس پروفایل تغییر کرد",
                                category="PROFILE",
                                primary_label="رویداد",
                                primary_value="تصویر جدید شناسایی شد",
                                accent="blue",
                            )
                        )
                    if (
                        snapshot.post_count is not None
                        and result.post_count is not None
                        and snapshot.post_count != result.post_count
                    ):
                        previous_posts = snapshot.post_count
                        session.add(
                            PageEvent(
                                target_page_id=target.id,
                                user_id=target.user_id,
                                event_type="post_count_changed",
                                description=(
                                    f"تعداد پست‌ها از {previous_posts} به "
                                    f"{result.post_count} تغییر کرد."
                                ),
                            )
                        )
                        notifications.append(
                            NotificationPayload(
                                message=(
                                    "تعداد پست‌های پیج تغییر کرد 🗂️\n\n"
                                    f"پیج: <b>@{escaped_page}</b>\n"
                                    f"مقدار قبلی: <code>{self._format_count(previous_posts)}</code>\n"
                                    f"مقدار جدید: <code>{self._format_count(result.post_count)}</code>"
                                ),
                                username=target.instagram_username,
                                title="تعداد پست‌ها تغییر کرد",
                                category="CONTENT",
                                primary_label="تعداد فعلی",
                                primary_value=self._format_count(result.post_count),
                                secondary_label="مقدار قبلی",
                                secondary_value=self._format_count(previous_posts),
                                accent="gold",
                            )
                        )
                    if (
                        snapshot.full_name is not None
                        and result.full_name is not None
                        and snapshot.full_name != result.full_name
                    ):
                        session.add(
                            PageEvent(
                                target_page_id=target.id,
                                user_id=target.user_id,
                                event_type="full_name_changed",
                                description="نام نمایشی اصلی پیج تغییر کرد.",
                            )
                        )
                        notifications.append(
                            NotificationPayload(
                                message=(
                                    "نام اصلی پیج عوض شد 🔄\n\n"
                                    f"پیج: <b>@{escaped_page}</b>\n"
                                    f"نام قبلی: <b>{html.escape(snapshot.full_name)}</b>\n"
                                    f"نام جدید: <b>{html.escape(result.full_name)}</b>"
                                ),
                                username=target.instagram_username,
                                title="نام اصلی پیج عوض شد",
                                category="PROFILE",
                                primary_label="نام جدید",
                                primary_value=result.full_name,
                                secondary_label="نام قبلی",
                                secondary_value=snapshot.full_name,
                                accent="blue",
                            )
                        )
                    if (
                        snapshot.biography is not None
                        and snapshot.biography != result.biography
                    ):
                        session.add(
                            PageEvent(
                                target_page_id=target.id,
                                user_id=target.user_id,
                                event_type="biography_changed",
                                description="متن بیوگرافی پیج تغییر کرد.",
                            )
                        )
                        notifications.append(
                            NotificationPayload(
                                message=(
                                    "بیوگرافی پیج تغییر کرد 📝\n\n"
                                    f"پیج: <b>@{escaped_page}</b>\n"
                                    "نسخه جدید در بخش اطلاعات زنده پیج قابل مشاهده است."
                                ),
                                username=target.instagram_username,
                                title="بیوگرافی پیج تغییر کرد",
                                category="PROFILE",
                                primary_label="وضعیت",
                                primary_value="متن جدید ثبت شد",
                                accent="blue",
                            )
                        )
                if result.follower_count is not None:
                    follower_now = datetime.now(timezone.utc)
                    follower_baseline = notification_settings.follower_report_baseline
                    if (
                        follower_baseline is None
                        or not notification_settings.notify_follower_change
                    ):
                        notification_settings.follower_report_baseline = (
                            result.follower_count
                        )
                        notification_settings.last_follower_report_at = follower_now
                    else:
                        follower_delta = result.follower_count - follower_baseline
                        follower_mode = notification_settings.follower_report_mode
                        last_report_at = notification_settings.last_follower_report_at
                        if last_report_at and last_report_at.tzinfo is None:
                            last_report_at = last_report_at.replace(tzinfo=timezone.utc)
                        hourly_due = follower_mode == "hourly" and (
                            last_report_at is None
                            or follower_now - last_report_at >= timedelta(hours=1)
                        )
                        threshold = max(
                            1,
                            notification_settings.follower_change_threshold,
                        )
                        threshold_due = (
                            follower_mode != "hourly"
                            and abs(follower_delta) >= threshold
                        )
                        if hourly_due or threshold_due:
                            event_type = (
                                "follower_hourly_report"
                                if hourly_due
                                else "follower_threshold_report"
                            )
                            session.add(
                                PageEvent(
                                    target_page_id=target.id,
                                    user_id=target.user_id,
                                    event_type=event_type,
                                    description=(
                                        f"گزارش فالوور از {follower_baseline} به "
                                        f"{result.follower_count}؛ تغییر "
                                        f"{follower_delta}."
                                    ),
                                )
                            )
                            report_title = (
                                "گزارش ساعتی فالوورها 🕐"
                                if hourly_due
                                else "تعداد فالوورها به آستانه رسید 📈"
                            )
                            notifications.append(
                                NotificationPayload(
                                    message=(
                                        f"{report_title}\n\n"
                                        f"پیج: <b>@{html.escape(target.instagram_username)}</b>\n"
                                        f"مقدار مبنا: <code>{self._format_count(follower_baseline)}</code>\n"
                                        f"مقدار فعلی: <code>{self._format_count(result.follower_count)}</code>\n"
                                        f"میزان تغییر: <code>{self._format_count(follower_delta, signed=True)}</code>"
                                    ),
                                    username=target.instagram_username,
                                    title=report_title.rstrip(" 🕐📈"),
                                    category="FOLLOWERS",
                                    primary_label="تعداد فعلی",
                                    primary_value=self._format_count(
                                        result.follower_count
                                    ),
                                    secondary_label="میزان تغییر",
                                    secondary_value=self._format_count(
                                        follower_delta,
                                        signed=True,
                                    ),
                                    accent=("green" if follower_delta >= 0 else "red"),
                                )
                            )
                            notification_settings.follower_report_baseline = (
                                result.follower_count
                            )
                            notification_settings.last_follower_report_at = follower_now
                if picture_key:
                    snapshot.profile_picture_key = picture_key
                    snapshot.profile_picture_url = result.profile_picture_url
                snapshot.full_name = self._truncate(result.full_name, 255)
                snapshot.biography = self._truncate(result.biography, 2000)
                snapshot.follower_count = result.follower_count
                snapshot.following_count = result.following_count
                snapshot.post_count = result.post_count
                snapshot.is_private = result.is_private
                snapshot.is_verified = result.is_verified

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
                if notification_settings.notify_activation:
                    notifications.append(
                        NotificationPayload(
                            message=(
                                f"پیج فعال شد! 🎉\n\nپیج: <b>@{escaped_current}</b>"
                            ),
                            username=target.instagram_username,
                            title="پیج دوباره فعال شد",
                            category="ACTIVATED",
                            primary_label="وضعیت جدید",
                            primary_value="فعال و در دسترس",
                            accent="green",
                        )
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
                if notification_settings.notify_deactivation:
                    notifications.append(
                        NotificationPayload(
                            message=(
                                f"پیج دی‌اکتیو شد! ⚠️\n\nپیج: <b>@{escaped_current}</b>"
                            ),
                            username=target.instagram_username,
                            title="پیج دی‌اکتیو شد",
                            category="DEACTIVATED",
                            primary_label="وضعیت جدید",
                            primary_value="غیرفعال یا خارج از دسترس",
                            accent="red",
                        )
                    )

            if username_changed:
                session.add(
                    PageEvent(
                        target_page_id=target.id,
                        user_id=target.user_id,
                        event_type="username_changed",
                        description=(
                            f"نام کاربری از @{previous_username} "
                            f"به @{target.instagram_username} تغییر کرد."
                        ),
                    )
                )
                if notification_settings.notify_username_change:
                    notifications.append(
                        NotificationPayload(
                            message=(
                                "نام کاربری پیج تغییر کرد! 🔄\n\n"
                                f"نام قبلی: <b>@{escaped_previous}</b>\n"
                                f"نام جدید: <b>@{escaped_current}</b>"
                            ),
                            username=target.instagram_username,
                            title="نام کاربری تغییر کرد",
                            category="IDENTITY",
                            primary_label="نام جدید",
                            primary_value=f"@{target.instagram_username}",
                            secondary_label="نام قبلی",
                            secondary_value=f"@{previous_username}",
                            accent="blue",
                        )
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
                if notification_settings.notify_username_change:
                    notifications.append(
                        NotificationPayload(
                            message=(
                                "هویت پیج تغییر کرده است! ⚠️\n\n"
                                f"پیج: <b>@{escaped_current}</b>\n"
                                "شناسه یکتای اینستاگرام با مقدار قبلی مطابقت ندارد."
                            ),
                            username=target.instagram_username,
                            title="هویت عددی پیج تغییر کرد",
                            category="CRITICAL",
                            primary_label="شناسه جدید",
                            primary_value=result.profile_id or "نامشخص",
                            secondary_label="شناسه قبلی",
                            secondary_value=previous_profile_id or "ثبت نشده",
                            accent="red",
                        )
                    )

            recipient_id = target.user_id
            await session.commit()

        await self.diagnostics.add(
            level="INFO",
            event="target_state_synchronized",
            message=(
                f"وضعیت پیج ذخیره شد؛ {len(notifications)} اعلان برای مالک ساخته شد."
            ),
            username=username,
            http_status=result.http_status,
            detail=(
                f"target_id={target_id}; owner_id={recipient_id}; "
                f"admin_categories={','.join(sorted(admin_report_categories)) or 'none'}"
            ),
        )

        if recipient_id is not None:
            for notification in notifications:
                await self._notify(recipient_id, notification)
            if (
                admin_report_categories
                and recipient_id != self.settings.admin_telegram_id
            ):
                for notification in notifications:
                    if notification.category not in admin_report_categories:
                        continue
                    await self._notify(
                        self.settings.admin_telegram_id,
                        replace(
                            notification,
                            message=(
                                "رونوشت گزارش کاربر "
                                f"<code>{recipient_id}</code> 📨\n\n"
                                f"{notification.message}"
                            ),
                            reply_markup=None,
                        ),
                    )
        return result

    async def _notify(
        self,
        telegram_id: int,
        notification: str | NotificationPayload,
    ) -> None:
        if isinstance(notification, str):
            payload = NotificationPayload(
                message=notification,
                username="system",
                title="هشدار زیرساخت پایش",
                category="SYSTEM",
                primary_label="وضعیت",
                primary_value="نیازمند بررسی مدیر",
                accent="red",
            )
        else:
            payload = notification
        try:
            card = await asyncio.to_thread(
                ReportCardRenderer.render_alert,
                AlertCardData(
                    title=payload.title,
                    username=payload.username,
                    category=payload.category,
                    primary_label=payload.primary_label,
                    primary_value=payload.primary_value,
                    secondary_label=payload.secondary_label,
                    secondary_value=payload.secondary_value,
                    accent=payload.accent,
                    occurred_at=datetime.now().astimezone(),
                ),
            )
            await self.bot.send_photo(
                telegram_id,
                BufferedInputFile(card, filename="farstar-security-report.jpg"),
                caption=payload.message,
                reply_markup=payload.reply_markup,
            )
            return
        except TelegramAPIError as exc:
            logger.warning(
                "Telegram photo notification failed for user %s: %s",
                telegram_id,
                exc,
            )
        except Exception as exc:
            logger.warning("Could not render notification card: %s", exc)
        try:
            await self.bot.send_message(
                telegram_id,
                payload.message,
                reply_markup=payload.reply_markup,
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Telegram notification failed for user %s: %s",
                telegram_id,
                exc,
            )

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _edge_count(user_data: dict[object, object], field: str) -> int | None:
        edge = user_data.get(field)
        if not isinstance(edge, dict):
            return None
        value = edge.get("count")
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    @staticmethod
    def _profile_picture_key(value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlsplit(value)
        return parsed.path or None

    @staticmethod
    def _truncate(value: str | None, limit: int) -> str | None:
        if value is None:
            return None
        return value[:limit]

    @staticmethod
    def _format_count(value: int, *, signed: bool = False) -> str:
        formatted = f"{value:+,}" if signed else f"{value:,}"
        return formatted.translate(PERSIAN_DIGITS)

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
            "Instagram rate limit detected; pausing checks for %s seconds",
            cooldown,
        )
        return cooldown
