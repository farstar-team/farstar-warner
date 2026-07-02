from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from urllib.parse import quote, urlsplit

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import LockError
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


class InstagramChecker:
    LOCK_KEY = "farstar:checker:lock"
    STATUS_COOLDOWN_KEY = "farstar:checker:status-cooldown"
    DEACTIVATION_STREAK_PREFIX = "farstar:checker:deactivation-streak:"
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
            timeout=httpx.Timeout(settings.instagram_request_timeout_seconds),
            headers=INSTAGRAM_BROWSER_HEADERS,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def set_browser_probe(self, probe: object) -> None:
        """Retain startup compatibility; monitoring uses the curl transport only."""
        del probe

    async def close(self) -> None:
        await self._http.aclose()

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
        if status_code == 429:
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
                or "the installed libcurl version doesn't support" not in (
                    curl_http2_response.error or ""
                ).lower()
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
                await asyncio.sleep(0.45 * attempt)

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

    async def _execute_http2_once(self, username: str) -> CurlResponse:
        safe_username = quote(username.strip(), safe="")
        url = (
            f"{self.settings.instagram_base_url}"
            f"/api/v1/users/web_profile_info/?username={safe_username}"
        )
        started_at = time.perf_counter()
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as exc:
            return CurlResponse(
                None,
                b"",
                str(exc),
                1,
                "httpx-http2",
                int((time.perf_counter() - started_at) * 1000),
            )
        if len(response.content) > self.MAX_RESPONSE_BYTES:
            return CurlResponse(
                response.status_code,
                b"",
                "response exceeded safety limit",
                63,
                f"httpx-{response.http_version}",
                int((time.perf_counter() - started_at) * 1000),
            )
        return CurlResponse(
            response.status_code,
            response.content,
            f"httpx {response.http_version}",
            0,
            f"httpx-{response.http_version}",
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
        return (
            self._parse_profile_response(response.body, username, 200) is not None
            or self._body_indicates_deactivated(response.body)
        )

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
        if not force_http2:
            command = tuple(part for part in command if part != "--http2")
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
                "curl",
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
                "curl-http2" if force_http2 else "curl",
                int((time.perf_counter() - started_at) * 1000),
            )

        error = stderr.decode("utf-8", errors="replace").strip() or None
        if process.returncode != 0:
            return CurlResponse(
                None,
                b"",
                error,
                process.returncode or 1,
                "curl-http2" if force_http2 else "curl",
                int((time.perf_counter() - started_at) * 1000),
            )
        if len(stdout) > self.MAX_RESPONSE_BYTES:
            return CurlResponse(
                None,
                b"",
                "response exceeded safety limit",
                63,
                "curl-http2" if force_http2 else "curl",
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
                "curl-http2" if force_http2 else "curl",
                int((time.perf_counter() - started_at) * 1000),
            )
        return CurlResponse(
            int(status_bytes),
            body,
            error,
            process.returncode or 0,
            "curl-http2" if force_http2 else "curl",
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
                part for part in (f"status={status}" if status else "", f"message={message}" if message else "") if part
            )
            return f"{reason} data.user معتبر وجود ندارد.{f' {suffix}' if suffix else ''}"

        lowered = response.body[:2000].decode("utf-8", errors="ignore").lower()
        if "accounts/login" in lowered or "log in" in lowered:
            return f"{reason} محتوای پاسخ نشانه صفحه ورود اینستاگرام دارد."
        if response.status_code == 200:
            return f"{reason} بدنه پاسخ JSON معتبر نبود یا data.user نداشت."
        return reason

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

            # A single scheduler worker prevents queued background requests from
            # starving interactive profile requests.
            worker_count = 1
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

    async def _check_target(self, target_id: int) -> None:
        async with self.session_factory() as session:
            snapshot = await session.get(TargetPage, target_id)
            if snapshot is None:
                return
            username = snapshot.instagram_username

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
            return
        if result.outcome == CheckOutcome.RATE_LIMITED:
            logger.warning("Ignoring non-authoritative rate-limit result for %s", username)
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
                await self.diagnostics.add(
                    level="INFO",
                    event="deactivation_confirmation_pending",
                    message=(
                        "پاسخ غیرفعال ثبت شد اما برای جلوگیری از هشدار اشتباه "
                        "منتظر تأیید بعدی است."
                    ),
                    username=username,
                    http_status=result.http_status,
                    detail=(
                        f"target_id={target_id}; confirmation={streak}/"
                        f"{self.settings.deactivation_confirmations}"
                    ),
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
            target.last_checked_at = datetime.now(timezone.utc)
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
                elif (
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
                        "عکس پروفایل پیج تغییر کرد! 🖼️\n\n"
                        f"پیج: <b>@{html.escape(target.instagram_username)}</b>"
                    )
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
                if notification_settings.notify_deactivation:
                    notifications.append(
                        f"پیج دی‌اکتیو شد! ⚠️\n\nپیج: <b>@{escaped_current}</b>"
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
                if notification_settings.notify_username_change:
                    notifications.append(
                        "هویت پیج تغییر کرده است! ⚠️\n\n"
                        f"پیج: <b>@{escaped_current}</b>\n"
                        "شناسه یکتای اینستاگرام با مقدار قبلی مطابقت ندارد."
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
                "admin_copy=false"
            ),
        )

        if recipient_id is not None:
            for notification in notifications:
                await self._notify(recipient_id, notification)

    async def _notify(self, telegram_id: int, message: str) -> None:
        try:
            await self.bot.send_message(telegram_id, message)
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
