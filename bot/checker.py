from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import random
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup
from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import LockError, RedisError
from sqlalchemy import and_, case, delete, func, select, update

from bot.config import Settings
from bot.credential_store import CredentialStore, CredentialStoreError
from bot.database import SessionFactory
from bot.diagnostics import DiagnosticStore
from bot.models import (
    NotificationSettings,
    InstagramMonitoringAccount,
    NotificationOutbox,
    PageEvent,
    PageSnapshot,
    PageSnapshotHistory,
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
    source: str = "public_web_profile"
    metadata_complete: bool = False
    canonical_username: str | None = None
    profile_id: str | None = None
    full_name: str | None = None
    biography: str | None = None
    profile_picture_url: str | None = None
    follower_count: int | None = None
    following_count: int | None = None
    post_count: int | None = None
    is_private: bool | None = None
    is_verified: bool | None = None
    external_link: str | None = None
    external_link_observed: bool = False
    account_type: str | None = None
    account_type_observed: bool = False
    category_name: str | None = None
    guest_searchable: bool | None = None
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


@dataclass(slots=True, frozen=True)
class DeliveryResult:
    delivered: bool
    error: str | None = None
    retry_after: int | None = None
    terminal: bool = False


@dataclass(slots=True, frozen=True)
class ProfileTimelineEntry:
    observed_at: datetime
    status: PageStatus
    username: str
    follower_count: int | None
    following_count: int | None
    post_count: int | None
    external_link: str | None
    account_type: str | None
    guest_searchable: bool | None
    evidence_source: str | None


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
    PREFLIGHT_CACHE_KEY = "farstar:checker:preflight-result"
    PREFLIGHT_LOCK_KEY = "farstar:checker:preflight-lock"
    HEALTH_INCIDENT_KEY = "farstar:checker:health-incident"
    HEALTH_FAILURE_STREAK_KEY = "farstar:checker:health-failure-streak"
    HEALTH_MUTE_KEY = "farstar:checker:health-muted"
    RECOVERY_LOCK_KEY = "farstar:checker:activation-recovery-lock"
    RECOVERY_DUE_KEY = "farstar:checker:activation-recovery-due"
    OUTBOX_LOCK_KEY = "farstar:notification-outbox:lock"
    MANUAL_CHECK_LOCK_PREFIX = "farstar:checker:manual-lock:"
    REGISTRATION_LOCK_PREFIX = "farstar:checker:registration-lock:"
    REGISTRATION_RETRY_PREFIX = "farstar:checker:registration-retry:"
    REGISTRATION_RECOVERY_GATE_KEY = "farstar:checker:registration-recovery-gate"
    GUEST_AUDIT_PREFIX = "farstar:checker:guest-audit:"
    GRAPHQL_CIRCUIT_KEY = "farstar:checker:graphql-circuit"
    CYCLE_METRICS_KEY = "farstar:checker:last-cycle"
    LOCK_TIMEOUT_SECONDS = 120
    MAX_RESPONSE_BYTES = 8_000_000
    CURL_EXECUTABLE = "curl"
    PROFILE_CACHE_PREFIX = "farstar:instagram:profile:fresh:"
    PROFILE_STALE_PREFIX = "farstar:instagram:profile:stale:"
    PROFILE_LOCK_PREFIX = "farstar:instagram:profile-lock:"
    FRESH_CACHE_SECONDS = 60
    STALE_CACHE_SECONDS = 86400
    GRAPH_HEALTH_PREFIX = "farstar:graph-account-health:"
    GRAPH_HEALTH_SECONDS = 300
    REGISTRATION_RETRY_SECONDS = 30
    REGISTRATION_RECOVERY_GATE_SECONDS = 10

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
        self.credentials = CredentialStore(settings)
        self._rate_limited = asyncio.Event()
        self._lock_lost = asyncio.Event()
        self._official_ready = False
        self._cycle_fetch_tasks: dict[str, asyncio.Task[ProfileResult]] = {}
        self._cycle_confirmation_tasks: dict[
            tuple[str, int], asyncio.Task[ProfileResult]
        ] = {}
        self._cycle_guest_audit_tasks: dict[str, asyncio.Task[bool | None]] = {}
        self._cycle_blocked_count = 0
        self._browser_probe: Callable[[str], Awaitable[ProfileResult]] | None = None
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
        self._graph_http = httpx.AsyncClient(
            http2=True,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(settings.instagram_request_timeout_seconds),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={"Accept": "application/json"},
        )

    def set_browser_probe(
        self,
        probe: Callable[[str], Awaitable[ProfileResult]],
    ) -> None:
        self._browser_probe = probe

    async def close(self) -> None:
        await self._http.aclose()
        await self._direct_http.aclose()
        await self._graph_http.aclose()

    async def fetch_profile(
        self,
        username: str,
        *,
        allow_stale: bool = True,
        force_refresh: bool = False,
        expected_profile_id: str | None = None,
        bypass_cooldown: bool = False,
        activate_circuit: bool = True,
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
            timeout=180,
            blocking_timeout=30,
        )
        acquired = await profile_lock.acquire()
        if not acquired:
            return await self._stale_or_unknown(normalized_username, allow_stale)
        try:
            if not force_refresh:
                cached = await self._read_cached_profile(
                    f"{self.PROFILE_CACHE_PREFIX}{normalized_username}"
                )
                if cached is not None:
                    return replace(cached, from_cache=True)
            cooldown_ttl = await self.redis.ttl(self.STATUS_COOLDOWN_KEY)
            if cooldown_ttl > 0 and not bypass_cooldown:
                if allow_stale:
                    stale = await self._read_cached_profile(
                        f"{self.PROFILE_STALE_PREFIX}{normalized_username}"
                    )
                    if stale is not None:
                        return replace(stale, from_cache=True)
                return ProfileResult(
                    CheckOutcome.RATE_LIMITED,
                    retry_after=cooldown_ttl,
                )
            response = await self._execute_profile_request(normalized_username)
            result = await self._profile_result_from_response(
                response,
                normalized_username,
                activate_circuit=activate_circuit,
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

    async def fetch_registration_profile(
        self,
        username: str,
        *,
        requester_id: int,
        retry: bool = False,
    ) -> ProfileResult:
        """Resolve a page for registration without trusting negative recovery probes.

        Registration retries are intentionally rate controlled.  During a global
        Instagram cooldown we perform a small positive-only recovery probe: an
        ACTIVE result may be used, while 404/unknown evidence remains
        inconclusive.  This prevents a blocked route from registering a live page
        as deactivated.
        """
        normalized_username = username.strip().lower()
        retry_key = (
            f"{self.REGISTRATION_RETRY_PREFIX}{requester_id}:"
            f"{normalized_username}"
        )
        if retry:
            accepted = await self.redis.set(
                retry_key,
                "1",
                ex=self.REGISTRATION_RETRY_SECONDS,
                nx=True,
            )
            if not accepted:
                retry_ttl = await self.redis.ttl(retry_key)
                return ProfileResult(
                    CheckOutcome.RATE_LIMITED,
                    source="registration_retry_cooldown",
                    retry_after=max(1, retry_ttl),
                )

        registration_lock = self.redis.lock(
            f"{self.REGISTRATION_LOCK_PREFIX}{normalized_username}",
            timeout=120,
            blocking_timeout=1,
        )
        acquired = await registration_lock.acquire()
        if not acquired:
            return ProfileResult(
                CheckOutcome.RATE_LIMITED,
                source="registration_singleflight_busy",
                retry_after=5,
            )

        try:
            cooldown_ttl = await self.redis.ttl(self.STATUS_COOLDOWN_KEY)
            if cooldown_ttl > 0:
                recovery_slot = await self.redis.set(
                    self.REGISTRATION_RECOVERY_GATE_KEY,
                    normalized_username,
                    ex=self.REGISTRATION_RECOVERY_GATE_SECONDS,
                    nx=True,
                )
                if not recovery_slot:
                    gate_ttl = await self.redis.ttl(
                        self.REGISTRATION_RECOVERY_GATE_KEY
                    )
                    return ProfileResult(
                        CheckOutcome.RATE_LIMITED,
                        source="registration_recovery_gate_busy",
                        retry_after=max(1, gate_ttl),
                    )

                recovery = await self._fetch_positive_recovery_profile(
                    normalized_username
                )
                if recovery.outcome == CheckOutcome.ACTIVE:
                    return recovery

                # Absence through a route already known to be unhealthy is not
                # authoritative.  Keep the pending registration undecided and
                # expose a bounded retry delay to the UI.
                return ProfileResult(
                    CheckOutcome.RATE_LIMITED,
                    source="registration_positive_only_inconclusive",
                    retry_after=min(
                        max(1, cooldown_ttl),
                        self.REGISTRATION_RETRY_SECONDS,
                    ),
                    http_status=recovery.http_status,
                )

            return await self.fetch_profile(
                normalized_username,
                allow_stale=False,
                force_refresh=True,
                # Repeated blocked registration requests must participate in
                # the same global circuit breaker as background checks.  This
                # prevents a burst of add-page attempts from amplifying an
                # Instagram throttle while controlled positive retries remain
                # available during the cooldown.
                activate_circuit=True,
            )
        finally:
            with suppress(LockError):
                await registration_lock.release()

    async def monitoring_accounts(self) -> list[InstagramMonitoringAccount]:
        async with self.session_factory() as session:
            return list(
                await session.scalars(
                    select(InstagramMonitoringAccount).order_by(
                        InstagramMonitoringAccount.is_active.desc(),
                        InstagramMonitoringAccount.id,
                    )
                )
            )

    async def official_provider_preflight(self, *, force: bool = False) -> bool:
        self._official_ready = False
        return False

    async def validate_monitoring_account(
        self,
        instagram_user_id: str,
        access_token: str,
        *,
        account_id: int | None = None,
        force: bool = True,
    ) -> tuple[bool, str, str | None]:
        return (
            False,
            "دریافت و استفاده از توکن Meta در نسخه ۵.۱.۰ غیرفعال است.",
            None,
        )

        if account_id is not None and not force:
            cached = await self.redis.get(f"{self.GRAPH_HEALTH_PREFIX}{account_id}")
            if cached == "healthy":
                return True, "اتصال رسمی سالم است.", None
        url = (
            f"{self.settings.meta_graph_base_url}/"
            f"{self.settings.meta_graph_api_version}/{quote(instagram_user_id, safe='')}"
        )
        started_at = time.perf_counter()
        try:
            response = await self._graph_http.get(
                url,
                params={"fields": "id,username"},
                headers={"Authorization": f"Bearer {access_token.strip()}"},
            )
        except httpx.HTTPError as exc:
            message = f"خطای شبکه Graph API: {type(exc).__name__}"
            if account_id is not None:
                await self._set_monitoring_account_health(
                    account_id, healthy=False, error=message
                )
            return False, message, None

        payload = self._json_object(response.content)
        username = self._optional_text(payload.get("username")) if payload else None
        returned_id = str(payload.get("id") or "") if payload else ""
        healthy = response.status_code == 200 and returned_id == instagram_user_id
        error = (
            None if healthy else self._graph_error_text(payload, response.status_code)
        )
        if account_id is not None:
            await self._set_monitoring_account_health(
                account_id,
                healthy=healthy,
                error=error,
            )
            if healthy:
                await self.redis.set(
                    f"{self.GRAPH_HEALTH_PREFIX}{account_id}",
                    "healthy",
                    ex=self.GRAPH_HEALTH_SECONDS,
                )
            else:
                await self.redis.delete(f"{self.GRAPH_HEALTH_PREFIX}{account_id}")
        await self.diagnostics.add(
            level="INFO" if healthy else "ERROR",
            event="official_account_health",
            message=(
                "اتصال رسمی حساب مانیتورینگ Meta سالم است."
                if healthy
                else "اتصال رسمی حساب مانیتورینگ Meta معتبر نیست."
            ),
            transport="meta-graph-api",
            http_status=response.status_code,
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            response_bytes=len(response.content),
            detail=(
                f"account_id={account_id or 0}; ig_user_id={instagram_user_id}; "
                f"error={error or 'none'}"
            ),
        )
        return healthy, error or "اتصال رسمی سالم است.", username

    async def _fetch_profile_official(
        self,
        username: str,
        *,
        expected_profile_id: str | None,
    ) -> ProfileResult | None:
        # Kept as a compatibility stub for older admin code. Version 5.1.0 is
        # strictly OSINT-only and never reads or transmits stored credentials.
        return None

        if not self.credentials.available:
            return None
        accounts = [
            account for account in await self.monitoring_accounts() if account.is_active
        ]
        if not accounts:
            return None

        last_unknown: ProfileResult | None = None
        for account in accounts:
            try:
                token = self.credentials.decrypt(account.access_token_encrypted)
            except CredentialStoreError as exc:
                await self._set_monitoring_account_health(
                    account.id, healthy=False, error=str(exc)
                )
                continue
            healthy, _, _ = await self.validate_monitoring_account(
                account.instagram_user_id,
                token,
                account_id=account.id,
                force=False,
            )
            if not healthy:
                continue

            url = (
                f"{self.settings.meta_graph_base_url}/"
                f"{self.settings.meta_graph_api_version}/"
                f"{quote(account.instagram_user_id, safe='')}"
            )
            fields = (
                "business_discovery.username("
                f"{username}"
                "){id,username,name,biography,followers_count,follows_count,"
                "media_count,profile_picture_url}"
            )
            started_at = time.perf_counter()
            try:
                response = await self._graph_http.get(
                    url,
                    params={"fields": fields},
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                last_unknown = ProfileResult(CheckOutcome.UNKNOWN)
                await self.diagnostics.add(
                    level="WARNING",
                    event="official_profile_network_error",
                    message="Graph API برای پیج هدف پاسخ شبکه‌ای معتبر نداد.",
                    username=username,
                    transport="meta-graph-api",
                    detail=f"account_id={account.id}; error={type(exc).__name__}",
                )
                continue

            payload = self._json_object(response.content)
            minimal_fields_used = False
            if response.status_code != 200 and self._graph_error_code(payload) == 100:
                minimal_fields = (
                    "business_discovery.username("
                    f"{username}"
                    "){id,username,followers_count,media_count}"
                )
                try:
                    minimal_response = await self._graph_http.get(
                        url,
                        params={"fields": minimal_fields},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                except httpx.HTTPError:
                    minimal_response = None
                if minimal_response is not None:
                    response = minimal_response
                    payload = self._json_object(response.content)
                    minimal_fields_used = True
            discovery = payload.get("business_discovery") if payload else None
            if response.status_code == 200 and isinstance(discovery, dict):
                result = self._parse_business_discovery(discovery, username)
                if result is not None:
                    result = replace(
                        result,
                        metadata_complete=not minimal_fields_used,
                    )
                    await self.diagnostics.add(
                        level="INFO",
                        event="official_profile_succeeded",
                        message="اطلاعات پیج از Business Discovery رسمی Meta دریافت شد.",
                        username=username,
                        transport="meta-graph-api",
                        http_status=response.status_code,
                        elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                        response_bytes=len(response.content),
                        detail=f"monitoring_account_id={account.id}",
                    )
                    return result

            error_text = self._graph_error_text(payload, response.status_code)
            await self.diagnostics.add(
                level="WARNING",
                event="official_profile_unavailable",
                message=(
                    "Business Discovery پیج هدف را برنگرداند؛ مسیر عمومی به‌عنوان fallback بررسی می‌شود."
                ),
                username=username,
                transport="meta-graph-api",
                http_status=response.status_code,
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                response_bytes=len(response.content),
                detail=f"monitoring_account_id={account.id}; error={error_text}",
            )
            error_code = self._graph_error_code(payload)
            if expected_profile_id and error_code in {100, 803}:
                return ProfileResult(
                    CheckOutcome.DEACTIVATED,
                    source="meta_business_discovery",
                    profile_id=expected_profile_id,
                    http_status=404,
                )
            last_unknown = ProfileResult(
                CheckOutcome.UNKNOWN,
                http_status=response.status_code,
            )
        return last_unknown

    async def _set_monitoring_account_health(
        self,
        account_id: int,
        *,
        healthy: bool,
        error: str | None,
    ) -> None:
        async with self.session_factory() as session:
            account = await session.get(InstagramMonitoringAccount, account_id)
            if account is None:
                return
            account.last_health_status = "healthy" if healthy else "failed"
            account.last_error = self._truncate(error, 500)
            account.last_checked_at = datetime.now(timezone.utc)
            await session.commit()

    @classmethod
    def _parse_business_discovery(
        cls,
        data: dict[object, object],
        requested_username: str,
    ) -> ProfileResult | None:
        profile_id = str(data.get("id") or "").strip()
        canonical_username = (
            str(data.get("username") or requested_username).strip().lower()
        )
        if not profile_id.isdigit() or not canonical_username:
            return None

        def count(name: str) -> int | None:
            value = data.get(name)
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else None
            )

        return ProfileResult(
            outcome=CheckOutcome.ACTIVE,
            source="meta_business_discovery",
            metadata_complete=False,
            canonical_username=canonical_username,
            profile_id=profile_id,
            full_name=cls._optional_text(data.get("name")),
            biography=cls._optional_text(data.get("biography")),
            profile_picture_url=cls._optional_text(data.get("profile_picture_url")),
            follower_count=count("followers_count"),
            following_count=count("follows_count"),
            post_count=count("media_count"),
            is_private=None,
            is_verified=None,
            http_status=200,
        )

    @staticmethod
    def _json_object(body: bytes) -> dict[str, object] | None:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _graph_error_code(payload: dict[str, object] | None) -> int | None:
        error = payload.get("error") if payload else None
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, int) and not isinstance(code, bool) else None

    @classmethod
    def _graph_error_text(
        cls,
        payload: dict[str, object] | None,
        status_code: int,
    ) -> str:
        error = payload.get("error") if payload else None
        if isinstance(error, dict):
            code = error.get("code")
            message = str(error.get("message") or "").strip()
            return f"HTTP {status_code}; code={code}; {message[:300]}"
        return f"HTTP {status_code}; پاسخ JSON خطای معتبر نداشت"

    async def _profile_result_from_response(
        self,
        response: CurlResponse,
        username: str,
        *,
        activate_circuit: bool = True,
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
        # A blocked response can still contain a JSON-shaped ``data.user=null``
        # body.  HTTP 401/403/429 is access-control evidence, never proof that a
        # target disappeared.  Only an explicit 404 or a successful (HTTP 200)
        # profile response with an explicit-null user is negative evidence.
        if status_code == 404 or (
            status_code == 200 and self._body_indicates_deactivated(response.body)
        ):
            return ProfileResult(
                CheckOutcome.DEACTIVATED,
                source="web_profile_explicit_absence",
                http_status=status_code,
            )

        discovery_result = await self._search_profile_via_graphql(username)
        # Guest search is a useful *positive* discovery source, but its result
        # list is not exhaustive.  Absence there may mean search throttling or a
        # private/low-ranked account, so it must never drive deactivation.
        if (
            discovery_result is not None
            and discovery_result.outcome == CheckOutcome.ACTIVE
        ):
            return discovery_result

        if self._browser_probe is not None:
            try:
                browser_result = await self._browser_probe(username)
            except Exception as exc:
                await self.diagnostics.add(
                    level="ERROR",
                    event="browser_fallback_exception",
                    message="شاهد کمکی مرورگر با خطای داخلی متوقف شد.",
                    username=username,
                    transport="playwright-public-embed",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            else:
                await self.diagnostics.add(
                    level=(
                        "INFO"
                        if browser_result.outcome
                        in {CheckOutcome.ACTIVE, CheckOutcome.DEACTIVATED}
                        else "WARNING"
                    ),
                    event="browser_fallback_result",
                    message=(
                        "مرورگر عمومی شاهد قطعی ارائه کرد."
                        if browser_result.outcome
                        in {CheckOutcome.ACTIVE, CheckOutcome.DEACTIVATED}
                        else "مرورگر عمومی نیز پاسخ قطعی ارائه نکرد."
                    ),
                    username=username,
                    transport="playwright-public-embed",
                    http_status=browser_result.http_status,
                    detail=(
                        f"outcome={browser_result.outcome.value}; "
                        f"source={browser_result.source}"
                    ),
                )
                if browser_result.outcome in {
                    CheckOutcome.ACTIVE,
                    CheckOutcome.DEACTIVATED,
                }:
                    return browser_result

        if self._response_is_access_blocked(response):
            cooldown = self.settings.rate_limit_cooldown_seconds
            if activate_circuit and not self._official_ready:
                self._cycle_blocked_count += 1
                threshold = max(
                    3,
                    min(int(getattr(self.settings, "check_concurrency", 4)), 5),
                )
                if self._cycle_blocked_count >= threshold:
                    self._rate_limited.set()
                    cooldown = await self._activate_cooldown(None)
            await self._record_final_failure(username, response)
            return ProfileResult(
                CheckOutcome.RATE_LIMITED,
                retry_after=cooldown,
                http_status=status_code,
            )
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

    async def _search_profile_via_graphql(
        self,
        username: str,
    ) -> ProfileResult | None:
        if await self.redis.ttl(self.GRAPHQL_CIRCUIT_KEY) > 0:
            return None
        routes = (
            [True, False]
            if getattr(self.settings, "instagram_proxy_url", None)
            else [False]
        )
        absence_result: ProfileResult | None = None
        absence_count = 0
        for use_proxy in routes:
            response = await self._execute_graphql_search_once(
                username,
                use_proxy=use_proxy,
            )
            result = self._parse_graphql_search_response(
                response.body,
                username,
                response.status_code,
            )
            positive = bool(
                result is not None and result.outcome == CheckOutcome.ACTIVE
            )
            await self.diagnostics.add(
                level=("INFO" if positive else "WARNING"),
                event="graphql_discovery_attempt",
                message=(
                    "جست‌وجوی مستقل نام کاربری شاهد مثبت داد."
                    if positive
                    else "جست‌وجوی مستقل نام کاربری شاهد مثبت قطعی نداد."
                ),
                username=username,
                transport=response.transport,
                http_status=response.status_code,
                return_code=response.return_code,
                elapsed_ms=response.elapsed_ms,
                response_bytes=len(response.body),
                detail=(
                    f"outcome={result.outcome.value}; source={result.source}"
                    if result is not None
                    else self._response_diagnostic(response)
                ),
            )
            if positive:
                return result
            if (
                result is not None
                and result.source == "graphql_username_search_absence"
                and result.guest_searchable is False
            ):
                absence_result = result
                absence_count += 1
                continue
            if response.status_code in {401, 403, 429}:
                await self.redis.set(
                    self.GRAPHQL_CIRCUIT_KEY,
                    str(response.status_code),
                    ex=self.settings.guest_search_audit_seconds,
                )
                break
        # Mark guest-search visibility false only if every configured route
        # independently returned a valid search payload without the target.
        # This result remains UNKNOWN for profile status evaluation.
        if absence_result is not None and absence_count == len(routes):
            return absence_result
        return None

    async def _audit_guest_searchability(self, username: str) -> bool | None:
        """Return only authoritative guest-search visibility; blocks stay unknown."""
        if not hasattr(self, "_direct_http"):
            return None
        cache_key = f"{self.GUEST_AUDIT_PREFIX}{username.lower()}"
        cached = await self.redis.get(cache_key)
        if cached in {"true", "false", "unknown"}:
            return {"true": True, "false": False, "unknown": None}[cached]
        discovery = await self._search_profile_via_graphql(username)
        if discovery is None:
            await self.redis.set(
                cache_key,
                "unknown",
                ex=self.settings.guest_search_audit_seconds,
            )
            return None
        if discovery.outcome == CheckOutcome.ACTIVE:
            await self.redis.set(
                cache_key,
                "true",
                ex=self.settings.guest_search_audit_seconds,
            )
            return True
        if (
            discovery.source == "graphql_username_search_absence"
            and discovery.guest_searchable is False
        ):
            await self.redis.set(
                cache_key,
                "false",
                ex=self.settings.guest_search_audit_seconds,
            )
            return False
        return None

    async def _execute_graphql_search_once(
        self,
        username: str,
        *,
        use_proxy: bool,
    ) -> CurlResponse:
        url = f"{self.settings.instagram_base_url}/graphql/query"
        variables = json.dumps(
            {"hasQuery": True, "query": username},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        started_at = time.perf_counter()
        client = self._http if use_proxy else self._direct_http
        transport = (
            "graphql-search-http2-proxy" if use_proxy else "graphql-search-http2-direct"
        )
        try:
            response = await client.post(
                url,
                data={
                    "variables": variables,
                    "doc_id": self.settings.instagram_search_doc_id,
                    "server_timestamps": "true",
                },
                headers={
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.8",
                    "Referer": "https://www.instagram.com/",
                },
            )
        except httpx.HTTPError as exc:
            return CurlResponse(
                None,
                b"",
                str(exc),
                1,
                transport,
                int((time.perf_counter() - started_at) * 1000),
            )
        if len(response.content) > self.MAX_RESPONSE_BYTES:
            return CurlResponse(
                response.status_code,
                b"",
                "response exceeded safety limit",
                63,
                transport,
                int((time.perf_counter() - started_at) * 1000),
            )
        return CurlResponse(
            response.status_code,
            response.content,
            f"httpx {response.http_version}",
            0,
            transport,
            int((time.perf_counter() - started_at) * 1000),
        )

    @classmethod
    def _parse_graphql_search_response(
        cls,
        body: bytes,
        requested_username: str,
        status_code: int | None,
    ) -> ProfileResult | None:
        if status_code != 200:
            return None
        payload = cls._json_object(body)
        if payload is None:
            return None
        payload_status = payload.get("status")
        if payload_status is not None and payload_status != "ok":
            return None
        data = payload.get("data") if payload else None
        search = (
            data.get("xdt_api__v1__fbsearch__non_profiled_serp")
            if isinstance(data, dict)
            else None
        )
        users = search.get("users") if isinstance(search, dict) else None
        if not isinstance(users, list):
            return None

        normalized = requested_username.strip().lower()
        for entry in users:
            if not isinstance(entry, dict):
                continue
            user_data = (
                entry.get("user") if isinstance(entry.get("user"), dict) else entry
            )
            raw_username = user_data.get("username")
            if not isinstance(raw_username, str) or raw_username.lower() != normalized:
                continue
            raw_id = user_data.get("id") or user_data.get("pk")
            profile_id = str(raw_id or "").strip()
            if not profile_id.isdigit():
                return None
            verified = user_data.get("is_verified")
            return ProfileResult(
                outcome=CheckOutcome.ACTIVE,
                source="graphql_username_search",
                metadata_complete=False,
                canonical_username=raw_username.lower(),
                profile_id=profile_id,
                full_name=cls._optional_text(user_data.get("full_name")),
                biography=cls._optional_text(user_data.get("biography")),
                profile_picture_url=cls._optional_text(
                    user_data.get("profile_pic_url_hd")
                    or user_data.get("profile_pic_url")
                ),
                follower_count=cls._plain_count(user_data.get("follower_count")),
                following_count=cls._plain_count(user_data.get("following_count")),
                post_count=cls._plain_count(user_data.get("media_count")),
                is_private=(
                    user_data.get("is_private")
                    if isinstance(user_data.get("is_private"), bool)
                    else None
                ),
                is_verified=verified if isinstance(verified, bool) else None,
                external_link=cls._extract_external_link(user_data),
                external_link_observed=any(
                    key in user_data
                    for key in ("external_url", "website_url", "bio_links")
                ),
                account_type=cls._extract_account_type(user_data),
                account_type_observed=any(
                    key in user_data
                    for key in (
                        "is_business_account",
                        "is_professional_account",
                        "account_type",
                    )
                ),
                category_name=cls._optional_text(
                    user_data.get("category_name")
                    or user_data.get("business_category_name")
                ),
                guest_searchable=True,
                http_status=200,
            )

        # Search results are ranked and non-exhaustive.  Missing from this list
        # is useful for the searchability audit, but is not deactivation proof.
        return ProfileResult(
            outcome=CheckOutcome.UNKNOWN,
            source="graphql_username_search_absence",
            metadata_complete=False,
            canonical_username=normalized,
            guest_searchable=False,
            http_status=200,
        )

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
        blocked_responses: list[CurlResponse] = []
        absence_responses: dict[str, CurlResponse] = {}
        attempt = 0

        # Curl is deliberately attempted first. The production diagnostics showed
        # that Instagram sometimes accepts this exact browser-shaped request while
        # rejecting an otherwise equivalent HTTPX TLS fingerprint with HTTP 401.
        transports: list[Callable[[], Awaitable[CurlResponse]]] = []
        if self.settings.instagram_proxy_url:
            transports.append(
                lambda: self._execute_curl_once(
                    username,
                    force_http2=False,
                    use_proxy=True,
                )
            )
        transports.append(
            lambda: self._execute_curl_once(
                username,
                force_http2=False,
                use_proxy=False,
            )
        )
        if self.settings.instagram_proxy_url:
            transports.append(
                lambda: self._execute_http2_once(username, use_proxy=True)
            )
        transports.append(
            lambda: self._execute_http2_once(username, use_proxy=False)
        )

        for execute in transports:
            attempt += 1
            response = await execute()
            await self._record_transport_attempt(
                trace_id,
                username,
                attempt,
                response,
            )
            if self._response_is_authoritative(response, username):
                await self._record_authoritative(trace_id, username, response)
                return response
            if self._response_indicates_absence(response):
                absence_responses[response.transport.lower()] = response
                continue
            if self._response_is_access_blocked(response):
                blocked_responses.append(response)
            else:
                last_response = response

        # A 404 or explicit "user is null" from one outbound route can be a
        # route-specific Instagram edge failure.  Proxy/direct agreement is
        # preferred; when only the direct route works, Curl and HTTP/2 must agree.
        # Any valid 200 above always wins, regardless of which route answered first.
        absence_routes = {
            self._transport_route(response)
            for response in absence_responses.values()
        }
        absence_confirmed = len(absence_routes) >= 2 or (
            absence_routes == {"direct"} and len(absence_responses) >= 2
        )
        if absence_confirmed:
            confirmed_absence = next(iter(absence_responses.values()))
            await self._record_authoritative(
                trace_id,
                username,
                confirmed_absence,
            )
            return confirmed_absence
        if absence_responses:
            unconfirmed_absence = next(iter(absence_responses.values()))
            await self.diagnostics.add(
                level="WARNING",
                event="profile_absence_unconfirmed",
                message=(
                    "نبودن پیج فقط از یک مسیر دیده شد؛ برای جلوگیری از تشخیص "
                    "غلط، نتیجه نامشخص نگه داشته شد."
                ),
                trace_id=trace_id,
                username=username,
                transport=unconfirmed_absence.transport,
                http_status=unconfirmed_absence.status_code,
                detail=(
                    f"independent_transports={len(absence_responses)}; "
                    f"routes={','.join(sorted(absence_routes))}; "
                    f"{self._response_diagnostic(unconfirmed_absence)}"
                ),
            )
            if last_response is None:
                last_response = CurlResponse(
                    0,
                    b"",
                    "profile absence was not confirmed by an independent route",
                    1,
                    "absence-consensus",
                )

        if blocked_responses:
            last_response = blocked_responses[0]
            await self.diagnostics.add(
                level="WARNING",
                event="web_profile_routes_blocked",
                message=(
                    "همه مسیرهای Web Profile رد شدند؛ شاهدهای عمومی مستقل "
                    "پیش از اعلام نتیجه نامشخص بررسی می‌شوند."
                ),
                trace_id=trace_id,
                username=username,
                transport=last_response.transport,
                http_status=last_response.status_code,
                detail=(
                    f"blocked_routes={len(blocked_responses)}; "
                    f"{self._response_diagnostic(last_response)}"
                ),
            )

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
        if response.status_code != 200:
            return False
        return self._parse_profile_response(response.body, username, 200) is not None

    @classmethod
    def _response_indicates_absence(cls, response: CurlResponse) -> bool:
        return response.status_code == 404 or (
            response.status_code == 200
            and cls._body_indicates_deactivated(response.body)
        )

    @staticmethod
    def _transport_route(response: CurlResponse) -> str:
        transport = response.transport.lower()
        if "proxy" in transport:
            return "proxy"
        if "direct" in transport:
            return "direct"
        return transport

    @staticmethod
    def _response_is_access_blocked(response: CurlResponse) -> bool:
        if response.status_code in {401, 403, 429}:
            return True
        lowered = response.body[:4000].decode("utf-8", errors="ignore").lower()
        return any(
            marker in lowered
            for marker in (
                "please wait a few minutes",
                "rate limit",
                "too many requests",
                "challenge_required",
            )
        )

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
        if status_code != 200:
            return None
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
            metadata_complete=True,
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
            external_link=cls._extract_external_link(user_data),
            external_link_observed=True,
            account_type=cls._extract_account_type(user_data),
            account_type_observed=any(
                key in user_data
                for key in (
                    "is_business_account",
                    "is_professional_account",
                    "account_type",
                )
            ),
            category_name=cls._optional_text(
                user_data.get("category_name")
                or user_data.get("business_category_name")
            ),
            http_status=status_code,
        )

    @classmethod
    def _extract_external_link(cls, user_data: dict[object, object]) -> str | None:
        candidates: list[object] = [
            user_data.get("external_url"),
            user_data.get("website_url"),
        ]
        bio_links = user_data.get("bio_links")
        if isinstance(bio_links, list):
            for entry in bio_links:
                if isinstance(entry, dict):
                    candidates.extend(
                        (entry.get("url"), entry.get("lynx_url"), entry.get("link_url"))
                    )
        for candidate in candidates:
            normalized = cls._normalize_external_link(candidate)
            if normalized:
                return normalized
        return None

    @classmethod
    def _normalize_external_link(cls, value: object) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if normalized.startswith("//"):
            normalized = f"https:{normalized}"
        parsed = urlsplit(normalized)
        if parsed.hostname and parsed.hostname.lower() == "l.instagram.com":
            redirected = parse_qs(parsed.query).get("u", [])
            if redirected:
                normalized = unquote(redirected[0]).strip()
                parsed = urlsplit(normalized)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.hostname.lower().rstrip(".") in {
            "instagram.com",
            "www.instagram.com",
        }:
            return None
        return cls._truncate(normalized, 2000)

    @staticmethod
    def _extract_account_type(user_data: dict[object, object]) -> str | None:
        raw_type = str(user_data.get("account_type") or "").strip().lower()
        if raw_type in {"business", "creator", "professional", "personal"}:
            return raw_type
        business = user_data.get("is_business_account")
        professional = user_data.get("is_professional_account")
        if business is True:
            return "business"
        if professional is True:
            return "professional"
        if business is False and professional is False:
            return "personal"
        return None

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
        access_blocked = self._response_is_access_blocked(response)
        await self.diagnostics.add(
            level="WARNING" if access_blocked else "ERROR",
            event=(
                "instagram_access_denied"
                if access_blocked
                else "profile_result_unknown"
            ),
            message=(
                "اینستاگرام دسترسی را موقتاً رد کرد؛ مدار محافظ فعال شد و retry متوقف شد."
                if access_blocked
                else "نتیجه قطعی نبود؛ برای جلوگیری از هشدار اشتباه، وضعیت ذخیره‌شده تغییر نکرد."
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
            401: "اینستاگرام دسترسی این درخواست را موقتاً رد کرد (Unauthorized).",
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

        await self.redis.delete(self.PROXY_FAILURE_STREAK_KEY, self.PROXY_ALERT_KEY)
        await self.diagnostics.add(
            level="INFO",
            event="proxy_preflight_succeeded",
            message=("تونل WARP مستقل از پاسخ اینستاگرام سالم است و warp=on تأیید شد."),
            transport="httpx-warp-trace",
            http_status=trace_response.status_code,
            elapsed_ms=elapsed_ms,
            response_bytes=len(trace_response.content),
            detail=f"proxy={proxy_url}; warp=on; instagram_not_tested_here=true",
        )
        return True

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
                "خود تونل WARP در تست Cloudflare سالم نبود؛ این خطا مستقل از پاسخ اینستاگرام است."
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
                    "⚠️ خود تونل WARP در سه تست متوالی سالم نبود!\n\n"
                    "Cloudflare Trace مقدار warp=on را تأیید نکرد. supervisor داخل "
                    "کانتینر تلاش می‌کند تونل را reconnect کند و مسیر مستقیم تا "
                    "بازیابی WARP در دسترس می‌ماند. جزئیات در لاگ ثبت شده است.",
                )
        return False

    async def reference_profile_preflight(self, *, force: bool = False) -> bool:
        """Run one shared, cached baseline probe instead of a three-way storm."""
        if not force:
            cached = await self.redis.get(self.PREFLIGHT_CACHE_KEY)
            if cached:
                try:
                    payload = json.loads(cached)
                except (TypeError, json.JSONDecodeError):
                    await self.redis.delete(self.PREFLIGHT_CACHE_KEY)
                else:
                    if isinstance(payload, dict) and isinstance(payload.get("ok"), bool):
                        return bool(payload["ok"])

        lock = self.redis.lock(
            self.PREFLIGHT_LOCK_KEY,
            timeout=max(60, int(self.settings.instagram_request_timeout_seconds * 8)),
            blocking_timeout=5,
        )
        if not await lock.acquire():
            cached = await self.redis.get(self.PREFLIGHT_CACHE_KEY)
            if cached:
                with suppress(TypeError, json.JSONDecodeError):
                    return bool(json.loads(cached).get("ok"))
            return False

        try:
            if not force:
                cached = await self.redis.get(self.PREFLIGHT_CACHE_KEY)
                if cached:
                    with suppress(TypeError, json.JSONDecodeError):
                        return bool(json.loads(cached).get("ok"))

            baseline_details: list[str] = []
            healthy = False
            for username in self.settings.baseline_usernames:
                try:
                    result = await self.fetch_profile(
                        username,
                        allow_stale=False,
                        force_refresh=True,
                        bypass_cooldown=True,
                        activate_circuit=False,
                    )
                except Exception as exc:
                    baseline_details.append(
                        f"@{username}=exception:{type(exc).__name__}"
                    )
                    continue
                baseline_details.append(
                    f"@{username}={result.outcome.value}:{result.source}:"
                    f"http={result.http_status or 0}"
                )
                if (
                    result.outcome == CheckOutcome.ACTIVE
                    and (result.canonical_username or "").lower() == username
                ):
                    healthy = True
                    break
                # An explicit access rejection is route-wide evidence. Trying two
                # more profiles immediately only amplifies the same block.
                if result.outcome == CheckOutcome.RATE_LIMITED or (
                    result.http_status in {401, 403, 429}
                ):
                    break

            detail = "; ".join(baseline_details) or "no_baseline_result"
            encoded = json.dumps(
                {
                    "ok": healthy,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "detail": detail,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await self.redis.set(
                self.PREFLIGHT_CACHE_KEY,
                encoded,
                ex=self.settings.preflight_cache_seconds,
            )

            if healthy:
                await self.redis.delete(self.STATUS_COOLDOWN_KEY)
                self._rate_limited.clear()
                await self.diagnostics.add(
                    level="INFO",
                    event="baseline_consensus_succeeded",
                    message="شاهد مرجع زنده دریافت شد؛ چرخه کامل پایش مجاز است.",
                    detail=detail,
                )
                await self._update_health_incident(healthy=True, detail=detail)
                return True

            await self._activate_cooldown(None)
            await self.diagnostics.add(
                level="ERROR",
                event="baseline_consensus_failed",
                message=(
                    "شاهد مرجع معتبر دریافت نشد؛ تغییر منفی وضعیت کاربران "
                    "متوقف و فقط مسیر بازیابی فعال‌شدن اجرا می‌شود."
                ),
                detail=detail,
            )
            await self._update_health_incident(healthy=False, detail=detail)
            return False
        finally:
            with suppress(LockError, RedisError):
                await lock.release()

    async def _update_health_incident(self, *, healthy: bool, detail: str) -> None:
        """Send one alert per incident, one recovery, and sparse reminders."""
        now = datetime.now(timezone.utc)
        raw_state = await self.redis.get(self.HEALTH_INCIDENT_KEY)
        state: dict[str, object] = {}
        if raw_state:
            with suppress(TypeError, json.JSONDecodeError):
                parsed = json.loads(raw_state)
                if isinstance(parsed, dict):
                    state = parsed

        previous_status = str(state.get("status") or "unknown")
        if healthy:
            await self.redis.delete(self.HEALTH_FAILURE_STREAK_KEY)
            recovered = previous_status == "failed"
            state = {
                "status": "healthy",
                "updated_at": now.isoformat(),
                "detail": detail[:700],
                "last_alert_at": state.get("last_alert_at"),
            }
            await self.redis.set(
                self.HEALTH_INCIDENT_KEY,
                json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                ex=604800,
            )
            if recovered:
                await self._notify(
                    self.settings.admin_telegram_id,
                    "دسترسی پایش اینستاگرام دوباره پایدار شد. ✅\n\n"
                    "صف بررسی کاربران از همین چرخه ادامه پیدا می‌کند و اعلان‌های "
                    "معوق نیز دوباره ارسال می‌شوند.",
                )
            return

        failure_streak = await self.redis.incr(self.HEALTH_FAILURE_STREAK_KEY)
        if failure_streak == 1:
            await self.redis.expire(self.HEALTH_FAILURE_STREAK_KEY, 86400)
        threshold = self.settings.health_failure_alert_threshold
        status = "failed" if failure_streak >= threshold else "degraded"
        should_alert = False
        last_alert_raw = state.get("last_alert_at")
        last_alert_at: datetime | None = None
        if isinstance(last_alert_raw, str):
            with suppress(ValueError):
                last_alert_at = datetime.fromisoformat(last_alert_raw)
        if status == "failed":
            should_alert = previous_status != "failed" or last_alert_at is None
            if (
                not should_alert
                and last_alert_at is not None
                and (now - last_alert_at).total_seconds()
                >= self.settings.health_alert_reminder_seconds
            ):
                should_alert = True
        muted = await self.redis.ttl(self.HEALTH_MUTE_KEY) > 0
        if should_alert and not muted:
            state["last_alert_at"] = now.isoformat()
        state.update(
            {
                "status": status,
                "updated_at": now.isoformat(),
                "failure_streak": failure_streak,
                "detail": detail[:700],
            }
        )
        await self.redis.set(
            self.HEALTH_INCIDENT_KEY,
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            ex=604800,
        )
        if should_alert and not muted:
            delivered = await self._notify(
                self.settings.admin_telegram_id,
                "دسترسی عمومی اینستاگرام دچار اختلال پایدار شده است. ⚠️\n\n"
                f"تعداد شکست متوالی: <code>{failure_streak}</code>\n"
                f"خلاصه فنی: <code>{html.escape(detail[:600])}</code>\n\n"
                "برای جلوگیری از گزارش اشتباه، تغییرهای منفی متوقف شده‌اند؛ "
                "اما بازیابی پیج‌های غیرفعال و صف اعلان ادامه دارد. تا زمان "
                "تغییر وضعیت، پیام تکراری ارسال نمی‌شود.",
            )
            if not delivered:
                state.pop("last_alert_at", None)
                await self.redis.set(
                    self.HEALTH_INCIDENT_KEY,
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                    ex=604800,
                )

    async def health_monitor(self) -> None:
        """Observe shared health state; never launch a competing probe storm."""
        lock = self.redis.lock(
            self.HEALTH_LOCK_KEY,
            timeout=240,
            blocking_timeout=0,
        )
        if not await lock.acquire(blocking=False):
            return
        try:
            proxy_ok = await self.proxy_preflight()
            reference_ok = await self.reference_profile_preflight(force=False)
            renderer_ok = True
            renderer_error: str | None = None
            try:
                await asyncio.to_thread(
                    ReportCardRenderer.render_profile,
                    ProfileCardData(
                        username="farstar_health",
                        full_name="Farstar Warner",
                        biography="Local renderer self-test",
                        follower_count=1,
                        following_count=1,
                        post_count=1,
                        is_private=False,
                        is_verified=False,
                    ),
                )
            except Exception as exc:
                renderer_ok = False
                renderer_error = f"{type(exc).__name__}: {exc}"
            state = "healthy" if reference_ok and renderer_ok else "degraded"
            await self.redis.set(self.HEALTH_STATE_KEY, state, ex=86400)
            await self.diagnostics.add(
                level="INFO" if state == "healthy" else "WARNING",
                event="health_monitor_snapshot",
                message=(
                    "نمونه سلامت اشتراکی سامانه ثبت شد؛ این job درخواست "
                    "اینستاگرام تکراری ایجاد نکرد."
                ),
                detail=(
                    f"reference_ok={reference_ok}; warp_ok={proxy_ok}; "
                    f"renderer_ok={renderer_ok}; renderer_error="
                    f"{renderer_error or 'none'}"
                ),
            )
        except Exception as exc:
            logger.exception("Health monitor failed unexpectedly")
            await self.diagnostics.add(
                level="CRITICAL",
                event="health_monitor_exception",
                message="اجرای مانیتور سلامت با خطای داخلی متوقف شد.",
                detail=f"{type(exc).__name__}: {exc}",
            )
        finally:
            with suppress(LockError, RedisError):
                await lock.release()

    async def run(self) -> None:
        self._official_ready = await self.official_provider_preflight(force=False)
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
            self._cycle_blocked_count = 0
            if not await self.reference_profile_preflight(force=False):
                await self.run_activation_recovery(
                    force=False,
                    checker_lock_held=True,
                )
                return
            target_ids = await self._eligible_target_ids()
            await self._process_target_queue(target_ids, mode="full")
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

    async def _process_target_queue(
        self,
        target_ids: list[int],
        *,
        mode: str,
        positive_only: bool = False,
        bypass_cooldown: bool = False,
    ) -> dict[str, int | float | str]:
        started_at = time.perf_counter()
        counters: dict[str, int] = {
            "planned": len(target_ids),
            "processed": 0,
            "active": 0,
            "deactivated": 0,
            "unknown": 0,
            "rate_limited": 0,
            "deferred": 0,
            "failed": 0,
        }
        deferred_targets: dict[int, str] = {}
        self._cycle_fetch_tasks = {}
        self._cycle_confirmation_tasks = {}
        self._cycle_guest_audit_tasks = {}
        await self.diagnostics.add(
            level="INFO",
            event="checker_cycle_started",
            message=f"چرخه {mode} برای {len(target_ids)} هدف آغاز شد.",
        )
        if target_ids:
            queue: asyncio.Queue[int] = asyncio.Queue()
            for target_id in target_ids:
                queue.put_nowait(target_id)
            worker_count = min(self.settings.check_concurrency, len(target_ids))
            workers = [
                asyncio.create_task(
                    self._worker(
                        queue,
                        positive_only=positive_only,
                        bypass_cooldown=bypass_cooldown,
                        counters=counters,
                        deferred_targets=deferred_targets,
                    )
                )
                for _ in range(worker_count)
            ]
            try:
                await queue.join()
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                for task in self._cycle_fetch_tasks.values():
                    if not task.done():
                        task.cancel()
                for task in self._cycle_confirmation_tasks.values():
                    if not task.done():
                        task.cancel()
                for task in self._cycle_guest_audit_tasks.values():
                    if not task.done():
                        task.cancel()
                self._cycle_fetch_tasks = {}
                self._cycle_confirmation_tasks = {}
                self._cycle_guest_audit_tasks = {}
            if deferred_targets:
                await self._record_deferred_checks(deferred_targets)

        duration = round(time.perf_counter() - started_at, 3)
        metrics: dict[str, int | float | str] = {
            **counters,
            "mode": mode,
            "duration_seconds": duration,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.redis.set(
            self.CYCLE_METRICS_KEY,
            json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
            ex=86400,
        )
        await self.diagnostics.add(
            level="INFO" if counters["failed"] == 0 else "WARNING",
            event="checker_cycle_completed",
            message=(
                f"چرخه {mode} پایان یافت: {counters['processed']} بررسی واقعی، "
                f"{counters['deferred']} تعویق و {counters['failed']} خطا."
            ),
            detail=json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
        )
        return metrics

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

    async def _worker(
        self,
        queue: asyncio.Queue[int],
        *,
        positive_only: bool = False,
        bypass_cooldown: bool = False,
        counters: dict[str, int] | None = None,
        deferred_targets: dict[int, str] | None = None,
    ) -> None:
        while True:
            target_id = await queue.get()
            made_request = False
            try:
                if self._lock_lost.is_set():
                    if deferred_targets is None:
                        await self._record_deferred_check(target_id, "LockLost")
                    else:
                        deferred_targets[target_id] = "LockLost"
                    if counters is not None:
                        counters["deferred"] += 1
                    continue
                if (
                    self._rate_limited.is_set()
                    and not self._official_ready
                    and not bypass_cooldown
                ):
                    if deferred_targets is None:
                        await self._record_deferred_check(
                            target_id,
                            "CircuitDeferred",
                        )
                    else:
                        deferred_targets[target_id] = "CircuitDeferred"
                    if counters is not None:
                        counters["deferred"] += 1
                    continue
                made_request = True
                result = await self._check_target(
                    target_id,
                    positive_only=positive_only,
                    bypass_cooldown=bypass_cooldown,
                    share_cycle=True,
                )
                if counters is not None:
                    counters["processed"] += 1
                    if result is None:
                        counters["unknown"] += 1
                    else:
                        counters[result.outcome.value] += 1
            except Exception as exc:
                if counters is not None:
                    counters["failed"] += 1
                logger.exception("Failed to process target %s", target_id)
                await self.diagnostics.add(
                    level="ERROR",
                    event="target_processing_failed",
                    message=f"پردازش هدف داخلی {target_id} با خطا متوقف شد.",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            finally:
                queue.task_done()
            if made_request and not self._lock_lost.is_set():
                await asyncio.sleep(
                    random.uniform(
                        self.settings.page_check_delay_min_seconds,
                        self.settings.page_check_delay_max_seconds,
                    )
                )

    async def _eligible_target_ids(
        self,
        *,
        only_deactivated: bool = False,
        limit: int | None = None,
    ) -> list[int]:
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
                TargetPage.last_known_status.label("target_status"),
                TargetPage.last_checked_at.label("last_checked_at"),
                func.row_number()
                .over(
                    partition_by=TargetPage.user_id,
                    order_by=(
                        case(
                            (TargetPage.last_known_status == PageStatus.DEACTIVATED, 0),
                            (TargetPage.last_known_status.is_(None), 1),
                            else_=2,
                        ),
                        TargetPage.last_checked_at.asc().nulls_first(),
                        TargetPage.id,
                    ),
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
            statement = select(ranked_targets.c.target_id).where(
                ranked_targets.c.target_rank <= ranked_targets.c.target_limit
            )
            if only_deactivated:
                statement = statement.where(
                    ranked_targets.c.target_status == PageStatus.DEACTIVATED
                )
            statement = statement.order_by(
                case(
                    (ranked_targets.c.target_status == PageStatus.DEACTIVATED, 0),
                    (ranked_targets.c.target_status.is_(None), 1),
                    else_=2,
                ),
                ranked_targets.c.last_checked_at.asc().nulls_first(),
                ranked_targets.c.target_id,
            )
            if limit is not None:
                statement = statement.limit(max(1, limit))
            result = await session.scalars(
                statement
            )
            return list(result)

    async def run_activation_recovery(
        self,
        *,
        force: bool = False,
        checker_lock_held: bool = False,
    ) -> dict[str, object]:
        """Check inactive targets for positive evidence even during an outage."""
        if not force:
            due = await self.redis.set(
                self.RECOVERY_DUE_KEY,
                "1",
                ex=self.settings.preflight_cache_seconds,
                nx=True,
            )
            if not due:
                return {"mode": "activation-recovery", "skipped": "not_due"}
        checker_lock: Lock | None = None
        if not checker_lock_held:
            checker_lock = self.redis.lock(
                self.LOCK_KEY,
                timeout=600,
                blocking_timeout=0,
            )
            if not await checker_lock.acquire(blocking=False):
                return {"mode": "activation-recovery", "skipped": "checker_busy"}
        lock = self.redis.lock(
            self.RECOVERY_LOCK_KEY,
            timeout=600,
            blocking_timeout=0,
        )
        if not await lock.acquire(blocking=False):
            if checker_lock is not None:
                with suppress(LockError, RedisError):
                    await checker_lock.release()
            return {"mode": "activation-recovery", "skipped": "locked"}
        try:
            target_ids = await self._eligible_target_ids(
                only_deactivated=True,
                limit=self.settings.recovery_batch_size,
            )
            return await self._process_target_queue(
                target_ids,
                mode="activation-recovery",
                positive_only=True,
                bypass_cooldown=True,
            )
        finally:
            with suppress(LockError, RedisError):
                await lock.release()
            if checker_lock is not None:
                with suppress(LockError, RedisError):
                    await checker_lock.release()

    async def check_target_now(self, target_id: int) -> ProfileResult | None:
        lock = self.redis.lock(
            f"{self.MANUAL_CHECK_LOCK_PREFIX}{target_id}",
            timeout=120,
            blocking_timeout=0,
        )
        if not await lock.acquire(blocking=False):
            return None
        try:
            healthy = await self.reference_profile_preflight(force=False)
            return await self._check_target(
                target_id,
                positive_only=not healthy,
                bypass_cooldown=not healthy,
                share_cycle=False,
            )
        finally:
            with suppress(LockError, RedisError):
                await lock.release()

    async def get_profile_timeline(
        self,
        target_id: int,
        *,
        user_id: int | None = None,
        limit: int = 30,
    ) -> list[ProfileTimelineEntry]:
        """Return detached forensic entries without keeping a DB session open."""
        safe_limit = max(1, min(limit, 30))
        conditions = [PageSnapshotHistory.target_page_id == target_id]
        if user_id is not None:
            conditions.append(PageSnapshotHistory.user_id == user_id)
        async with self.session_factory() as session:
            rows = list(
                await session.scalars(
                    select(PageSnapshotHistory)
                    .where(*conditions)
                    .order_by(
                        PageSnapshotHistory.observed_at.desc(),
                        PageSnapshotHistory.id.desc(),
                    )
                    .limit(safe_limit)
                )
            )
        return [
            ProfileTimelineEntry(
                observed_at=row.observed_at,
                status=row.status,
                username=self._normalize_text(row.username),
                follower_count=row.follower_count,
                following_count=row.following_count,
                post_count=row.post_count,
                external_link=self._normalize_text(row.external_link) or None,
                account_type=self._normalize_account_type(row.account_type),
                guest_searchable=row.guest_searchable,
                evidence_source=self._normalize_text(row.evidence_source) or None,
            )
            for row in rows
        ]

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
            target.consecutive_inconclusive_checks += 1
            await session.commit()

    async def _record_deferred_check(self, target_id: int, outcome: str) -> None:
        async with self.session_factory() as session:
            target = await session.get(TargetPage, target_id)
            if target is None:
                return
            target.last_check_outcome = outcome
            target.consecutive_inconclusive_checks += 1
            await session.commit()

    async def _record_deferred_checks(self, targets: dict[int, str]) -> None:
        grouped: dict[str, list[int]] = {}
        for target_id, outcome in targets.items():
            grouped.setdefault(outcome, []).append(target_id)
        async with self.session_factory() as session:
            for outcome, target_ids in grouped.items():
                await session.execute(
                    update(TargetPage)
                    .where(TargetPage.id.in_(target_ids))
                    .values(
                        last_check_outcome=outcome,
                        consecutive_inconclusive_checks=(
                            TargetPage.consecutive_inconclusive_checks + 1
                        ),
                    )
                )
            await session.commit()

    async def _fetch_cycle_profile(
        self,
        username: str,
        *,
        bypass_cooldown: bool,
        activate_circuit: bool,
        positive_only: bool,
    ) -> ProfileResult:
        task = self._cycle_fetch_tasks.get(username)
        if task is None:
            if positive_only:
                task = asyncio.create_task(
                    self._fetch_positive_recovery_profile(username)
                )
            else:
                task = asyncio.create_task(
                    self.fetch_profile(
                        username,
                        allow_stale=False,
                        force_refresh=True,
                        bypass_cooldown=bypass_cooldown,
                        activate_circuit=activate_circuit,
                    )
                )
            self._cycle_fetch_tasks[username] = task
        return await asyncio.shield(task)

    async def _fetch_positive_recovery_profile(self, username: str) -> ProfileResult:
        """Use at most two exact Curl probes and accept positive evidence only."""
        responses: list[CurlResponse] = []
        if self.settings.instagram_proxy_url:
            responses.append(
                await self._execute_curl_once(
                    username,
                    force_http2=False,
                    use_proxy=True,
                )
            )
        responses.append(
            await self._execute_curl_once(
                username,
                force_http2=False,
                use_proxy=False,
            )
        )
        for response in responses:
            parsed = self._parse_profile_response(
                response.body,
                username,
                response.status_code or 0,
            )
            if parsed is not None:
                await self._cache_profile(parsed, username)
                return parsed
        statuses = {response.status_code for response in responses}
        if statuses and statuses <= {404}:
            return ProfileResult(
                CheckOutcome.DEACTIVATED,
                source="recovery_probe_absence_untrusted",
                http_status=404,
            )
        blocked_status = next(
            (status for status in statuses if status in {401, 403, 429}),
            None,
        )
        if blocked_status is not None:
            return ProfileResult(
                CheckOutcome.RATE_LIMITED,
                source="recovery_probe_blocked",
                http_status=blocked_status,
                retry_after=self.settings.preflight_cache_seconds,
            )
        return ProfileResult(
            CheckOutcome.UNKNOWN,
            source="recovery_probe_unknown",
            http_status=next((status for status in statuses if status), None),
        )

    async def _fetch_cycle_confirmation(
        self,
        username: str,
        confirmation_number: int,
        *,
        bypass_cooldown: bool,
    ) -> ProfileResult:
        key = (username, confirmation_number)
        task = self._cycle_confirmation_tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                self.fetch_profile(
                    username,
                    allow_stale=False,
                    force_refresh=True,
                    bypass_cooldown=bypass_cooldown,
                    activate_circuit=False,
                )
            )
            self._cycle_confirmation_tasks[key] = task
        return await asyncio.shield(task)

    async def _audit_cycle_guest_searchability(
        self,
        username: str,
    ) -> bool | None:
        task = self._cycle_guest_audit_tasks.get(username)
        if task is None:
            task = asyncio.create_task(self._audit_guest_searchability(username))
            self._cycle_guest_audit_tasks[username] = task
        return await asyncio.shield(task)

    async def _check_target(
        self,
        target_id: int,
        *,
        positive_only: bool = False,
        bypass_cooldown: bool = False,
        share_cycle: bool = False,
    ) -> ProfileResult | None:
        async with self.session_factory() as session:
            snapshot = await session.get(TargetPage, target_id)
            if snapshot is None:
                return None
            username = snapshot.instagram_username
            known_status = snapshot.last_known_status

        # Background state transitions must only use a live Instagram response.
        # Stale metadata is reserved for user-facing profile previews.
        observation_started_at = datetime.now(timezone.utc)
        if share_cycle:
            result = await self._fetch_cycle_profile(
                username,
                bypass_cooldown=bypass_cooldown,
                activate_circuit=not positive_only,
                positive_only=positive_only,
            )
        else:
            result = await self.fetch_profile(
                username,
                allow_stale=False,
                force_refresh=True,
                expected_profile_id=snapshot.last_known_id,
                bypass_cooldown=bypass_cooldown,
                activate_circuit=not positive_only,
            )
        if result.outcome == CheckOutcome.ACTIVE:
            if result.source == "graphql_username_search":
                searchable = True
            elif positive_only:
                searchable = None
            elif share_cycle:
                searchable = await self._audit_cycle_guest_searchability(username)
            else:
                searchable = await self._audit_guest_searchability(username)
            result = replace(result, guest_searchable=searchable)
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
                f"from_cache={result.from_cache}; source={result.source}"
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
        if positive_only and result.outcome != CheckOutcome.ACTIVE:
            await self._record_inconclusive_check(
                target_id,
                result,
                outcome=f"RecoveryProbe:{result.outcome.value}",
            )
            return result

        streak_key = f"{self.DEACTIVATION_STREAK_PREFIX}{target_id}"
        await self.redis.delete(streak_key)
        if (
            result.outcome == CheckOutcome.DEACTIVATED
            and known_status != PageStatus.DEACTIVATED
        ):
            confirmations = 1
            confirmation_sources = [result.source]
            while confirmations < self.settings.deactivation_confirmations:
                delay = self.settings.deactivation_confirmation_delay_seconds
                await asyncio.sleep(random.uniform(delay * 0.8, delay * 1.2))
                if share_cycle:
                    confirmation = await self._fetch_cycle_confirmation(
                        username,
                        confirmations,
                        bypass_cooldown=bypass_cooldown,
                    )
                else:
                    confirmation = await self.fetch_profile(
                        username,
                        allow_stale=False,
                        force_refresh=True,
                        expected_profile_id=snapshot.last_known_id,
                        bypass_cooldown=bypass_cooldown,
                        activate_circuit=False,
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
                confirmation_sources.append(confirmation.source)
            await self.diagnostics.add(
                level="INFO",
                event="deactivation_confirmed_immediately",
                message=("غیرفعال‌شدن پیج با چند پاسخ قطعی در همان چرخه تأیید شد."),
                username=username,
                http_status=result.http_status,
                detail=(
                    f"target_id={target_id}; confirmations={confirmations}; "
                    f"sources={','.join(confirmation_sources)}"
                ),
            )

        notifications: list[NotificationPayload] = []
        recipient_id: int | None = None
        admin_report_categories: set[str] = set()
        searchability_issue = False
        async with self.session_factory() as session:
            target = await session.scalar(
                select(TargetPage).where(TargetPage.id == target_id).with_for_update()
            )
            if target is None:
                return None
            previous_evidence_at = target.last_evidence_at
            if previous_evidence_at is not None:
                if previous_evidence_at.tzinfo is None:
                    previous_evidence_at = previous_evidence_at.replace(
                        tzinfo=timezone.utc
                    )
                if previous_evidence_at > observation_started_at:
                    return result
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
            target.last_evidence_source = result.source
            target.last_evidence_at = observation_started_at
            target.status_confirmed = True
            target.consecutive_inconclusive_checks = 0
            if new_status == PageStatus.ACTIVE:
                target.consecutive_active_checks += 1
                target.consecutive_deactivated_checks = 0
            else:
                target.last_deactivation_evidence_at = checked_at
                target.consecutive_deactivated_checks = max(
                    self.settings.deactivation_confirmations,
                    target.consecutive_deactivated_checks + 1,
                )
                target.consecutive_active_checks = 0
            if previous_status != new_status or target.last_status_changed_at is None:
                target.last_status_changed_at = checked_at
            username_changed = False
            identity_changed = False
            follower_spike_detected = False

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
                effective_private = bool(
                    result.is_private is True
                    or (snapshot is not None and snapshot.is_private is True)
                )
                if snapshot is None:
                    snapshot = PageSnapshot(
                        target_page_id=target.id,
                        user_id=target.user_id,
                    )
                    session.add(snapshot)
                else:
                    escaped_page = html.escape(target.instagram_username)
                    previous_full_name = self._normalize_text(snapshot.full_name)
                    current_full_name = self._normalize_text(result.full_name)
                    previous_biography = self._normalize_text(snapshot.biography)
                    current_biography = self._normalize_text(result.biography)
                    previous_external_link = self._normalize_text(
                        snapshot.external_link
                    )
                    current_external_link = self._normalize_text(result.external_link)
                    previous_account_type = self._normalize_account_type(
                        snapshot.account_type
                    )
                    current_account_type = self._normalize_account_type(
                        result.account_type
                    )
                    if (
                        snapshot.follower_count is not None
                        and result.follower_count is not None
                    ):
                        observed_before = snapshot.updated_at or checked_at
                        if observed_before.tzinfo is None:
                            observed_before = observed_before.replace(
                                tzinfo=timezone.utc
                            )
                        follower_velocity_delta = (
                            result.follower_count - snapshot.follower_count
                        )
                        elapsed_seconds = max(
                            1,
                            int((checked_at - observed_before).total_seconds()),
                        )
                        follower_spike_detected = bool(
                            follower_velocity_delta
                            >= getattr(self.settings, "follower_spike_threshold", 1000)
                            and elapsed_seconds
                            <= getattr(
                                self.settings,
                                "follower_spike_window_seconds",
                                3600,
                            )
                        )
                        if follower_spike_detected:
                            session.add(
                                PageEvent(
                                    target_page_id=target.id,
                                    user_id=target.user_id,
                                    event_type="follower_spike_detected",
                                    description=(
                                        "جهش غیرعادی فالوور شناسایی شد؛ "
                                        f"افزایش {follower_velocity_delta} در "
                                        f"{elapsed_seconds} ثانیه."
                                    ),
                                )
                            )
                            notifications.append(
                                NotificationPayload(
                                    message=(
                                        "🚨 جهش بحرانی فالوور شناسایی شد\n\n"
                                        f"پیج: <b>@{html.escape(target.instagram_username)}</b>\n"
                                        f"افزایش: <code>{self._format_count(follower_velocity_delta, signed=True)}</code>\n"
                                        f"بازه زمانی: <code>{self._format_duration(elapsed_seconds)}</code>\n\n"
                                        "رشد فالوور با سرعت غیرعادی در حال رخ‌دادن است. "
                                        "احتمال حمله فالوور فیک یا اسپم وجود دارد. پیشنهاد می‌کنیم "
                                        "برای کاهش ریسک جریمه الگوریتمی یا موج مسدودسازی خودکار، "
                                        "پیج را موقتاً خصوصی کنید."
                                    ),
                                    username=target.instagram_username,
                                    title="هشدار امنیتی جهش فالوور",
                                    category="CRITICAL",
                                    primary_label="افزایش ناگهانی",
                                    primary_value=self._format_count(
                                        follower_velocity_delta,
                                        signed=True,
                                    ),
                                    secondary_label="بازه تشخیص",
                                    secondary_value=self._format_duration(
                                        elapsed_seconds
                                    ),
                                    accent="red",
                                )
                            )
                    if (
                        not effective_private
                        and result.is_verified is not None
                        and snapshot.is_verified != result.is_verified
                    ):
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
                        not effective_private
                        and picture_key
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
                        snapshot.following_count is not None
                        and result.following_count is not None
                        and snapshot.following_count != result.following_count
                    ):
                        previous_following = snapshot.following_count
                        session.add(
                            PageEvent(
                                target_page_id=target.id,
                                user_id=target.user_id,
                                event_type="following_count_changed",
                                description=(
                                    f"تعداد دنبال‌شونده‌ها از {previous_following} "
                                    f"به {result.following_count} تغییر کرد."
                                ),
                            )
                        )
                        notifications.append(
                            NotificationPayload(
                                message=(
                                    "تعداد دنبال‌شونده‌های پیج تغییر کرد 🔄\n\n"
                                    f"پیج: <b>@{escaped_page}</b>\n"
                                    f"مقدار قبلی: <code>{self._format_count(previous_following)}</code>\n"
                                    f"مقدار جدید: <code>{self._format_count(result.following_count)}</code>"
                                ),
                                username=target.instagram_username,
                                title="تعداد دنبال‌شونده‌ها تغییر کرد",
                                category="CONTENT",
                                primary_label="تعداد فعلی",
                                primary_value=self._format_count(
                                    result.following_count
                                ),
                                secondary_label="مقدار قبلی",
                                secondary_value=self._format_count(previous_following),
                                accent="blue",
                            )
                        )
                    if (
                        not effective_private
                        and previous_full_name
                        and (result.metadata_complete or result.full_name is not None)
                        and previous_full_name != current_full_name
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
                                    f"نام قبلی: <b>{html.escape(previous_full_name)}</b>\n"
                                    f"نام جدید: <b>{html.escape(current_full_name or 'حذف شده')}</b>"
                                ),
                                username=target.instagram_username,
                                title="نام اصلی پیج عوض شد",
                                category="PROFILE",
                                primary_label="نام جدید",
                                primary_value=current_full_name or "حذف شده",
                                secondary_label="نام قبلی",
                                secondary_value=previous_full_name,
                                accent="blue",
                            )
                        )
                    if (
                        not effective_private
                        and snapshot.biography is not None
                        and (result.metadata_complete or result.biography is not None)
                        and previous_biography != current_biography
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
                    if result.external_link_observed:
                        if not snapshot.external_link_initialized:
                            snapshot.external_link_initialized = True
                        elif previous_external_link != current_external_link:
                            suspicious_reasons = self._malicious_link_reasons(
                                current_external_link
                            )
                            reason_text = (
                                "، ".join(suspicious_reasons)
                                if suspicious_reasons
                                else "الگوی مخرب شناخته‌شده‌ای دیده نشد"
                            )
                            session.add(
                                PageEvent(
                                    target_page_id=target.id,
                                    user_id=target.user_id,
                                    event_type="external_link_changed",
                                    description=(
                                        "لینک خارجی بیو تغییر کرد؛ "
                                        f"ارزیابی: {reason_text}."
                                    ),
                                )
                            )
                            notifications.append(
                                NotificationPayload(
                                    message=(
                                        "🚨 لینک خارجی بیو تغییر کرد\n\n"
                                        f"پیج: <b>@{escaped_page}</b>\n"
                                        f"لینک قبلی: <code>{html.escape(previous_external_link or 'نداشت')}</code>\n"
                                        f"لینک جدید: <code>{html.escape(current_external_link or 'حذف شده')}</code>\n"
                                        f"ارزیابی امنیتی: <b>{html.escape(reason_text)}</b>\n\n"
                                        "اگر این تغییر را انجام نداده‌اید، دسترسی‌های پیج را فوراً بررسی کنید."
                                    ),
                                    username=target.instagram_username,
                                    title="تغییر امنیتی لینک بیو",
                                    category="CRITICAL",
                                    primary_label="وضعیت لینک جدید",
                                    primary_value=(
                                        "مشکوک" if suspicious_reasons else "تغییر کرده"
                                    ),
                                    secondary_label="نشانه‌های خطر",
                                    secondary_value=reason_text,
                                    accent="red",
                                )
                            )
                    if result.account_type_observed:
                        if not snapshot.account_type_initialized:
                            snapshot.account_type_initialized = True
                        elif (
                            previous_account_type
                            in {"business", "creator", "professional"}
                            and current_account_type == "personal"
                        ):
                            session.add(
                                PageEvent(
                                    target_page_id=target.id,
                                    user_id=target.user_id,
                                    event_type="professional_account_downgraded",
                                    description=(
                                        "نوع حساب از حالت حرفه‌ای/تجاری به شخصی تغییر کرد."
                                    ),
                                )
                            )
                            notifications.append(
                                NotificationPayload(
                                    message=(
                                        "🚨 نوع حساب پیج تغییر کرد\n\n"
                                        f"پیج: <b>@{escaped_page}</b>\n"
                                        f"نوع قبلی: <b>{self._account_type_fa(previous_account_type)}</b>\n"
                                        "نوع جدید: <b>شخصی</b>\n\n"
                                        "اگر این تغییر را انجام نداده‌اید، تنظیمات حرفه‌ای و دسترسی‌های پیج را بررسی کنید."
                                    ),
                                    username=target.instagram_username,
                                    title="تبدیل حساب حرفه‌ای به شخصی",
                                    category="CRITICAL",
                                    primary_label="نوع جدید",
                                    primary_value="شخصی",
                                    secondary_label="نوع قبلی",
                                    secondary_value=self._account_type_fa(
                                        previous_account_type
                                    ),
                                    accent="red",
                                )
                            )
                    if result.guest_searchable is not None:
                        if not snapshot.guest_searchable_initialized:
                            snapshot.guest_searchable_initialized = True
                        elif (
                            snapshot.guest_searchable is True
                            and result.guest_searchable is False
                        ):
                            session.add(
                                PageEvent(
                                    target_page_id=target.id,
                                    user_id=target.user_id,
                                    event_type="searchability_throttling_suspected",
                                    description=(
                                        "پیج فعال است اما در جست‌وجوی عمومی مهمان دیده نشد؛ "
                                        "احتمال محدودسازی جست‌وجو یا Shadowban وجود دارد."
                                    ),
                                )
                            )
                            searchability_issue = True
                if result.follower_count is not None:
                    follower_now = datetime.now(timezone.utc)
                    follower_baseline = notification_settings.follower_report_baseline
                    if (
                        follower_baseline is None
                        or not notification_settings.notify_follower_change
                        or follower_spike_detected
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
                        if (
                            hourly_due or threshold_due
                        ) and not follower_spike_detected:
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
                if result.metadata_complete or result.full_name is not None:
                    snapshot.full_name = self._truncate(
                        self._normalize_text(result.full_name) or None,
                        255,
                    )
                if not effective_private and (
                    result.metadata_complete or result.biography is not None
                ):
                    snapshot.biography = self._truncate(
                        self._normalize_text(result.biography) or None,
                        2000,
                    )
                if result.external_link_observed:
                    snapshot.external_link = self._truncate(
                        self._normalize_text(result.external_link) or None,
                        2000,
                    )
                    snapshot.external_link_initialized = True
                if result.account_type_observed:
                    snapshot.account_type = self._normalize_account_type(
                        result.account_type
                    )
                    snapshot.account_type_initialized = True
                    snapshot.category_name = self._truncate(
                        self._normalize_text(result.category_name) or None,
                        255,
                    )
                if result.guest_searchable is not None:
                    snapshot.guest_searchable = result.guest_searchable
                    snapshot.guest_searchable_initialized = True
                if result.metadata_complete or result.follower_count is not None:
                    snapshot.follower_count = result.follower_count
                if result.metadata_complete or result.following_count is not None:
                    snapshot.following_count = result.following_count
                if result.metadata_complete or result.post_count is not None:
                    snapshot.post_count = result.post_count
                if result.metadata_complete or result.is_private is not None:
                    snapshot.is_private = result.is_private
                if result.is_verified is not None:
                    snapshot.is_verified = result.is_verified
                snapshot.updated_at = checked_at

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
                        description=(
                            "پیج از وضعیت فعال به غیرفعال تغییر کرد؛ "
                            f"منبع شاهد: {result.source}."
                        ),
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

            current_snapshot = await session.get(PageSnapshot, target.id)
            session.add(
                PageSnapshotHistory(
                    target_page_id=target.id,
                    user_id=target.user_id,
                    observed_at=checked_at,
                    status=new_status,
                    username=self._normalize_text(target.instagram_username),
                    follower_count=(
                        result.follower_count
                        if result.outcome == CheckOutcome.ACTIVE
                        and result.follower_count is not None
                        else (
                            current_snapshot.follower_count
                            if current_snapshot is not None
                            else None
                        )
                    ),
                    following_count=(
                        result.following_count
                        if result.outcome == CheckOutcome.ACTIVE
                        and result.following_count is not None
                        else (
                            current_snapshot.following_count
                            if current_snapshot is not None
                            else None
                        )
                    ),
                    post_count=(
                        result.post_count
                        if result.outcome == CheckOutcome.ACTIVE
                        and result.post_count is not None
                        else current_snapshot.post_count
                        if current_snapshot
                        else None
                    ),
                    external_link=(
                        current_snapshot.external_link if current_snapshot else None
                    ),
                    account_type=(
                        current_snapshot.account_type if current_snapshot else None
                    ),
                    guest_searchable=(
                        current_snapshot.guest_searchable if current_snapshot else None
                    ),
                    evidence_source=result.source,
                )
            )
            await session.flush()
            if hasattr(session, "scalars"):
                stale_history_ids = list(
                    await session.scalars(
                        select(PageSnapshotHistory.id)
                        .where(PageSnapshotHistory.target_page_id == target.id)
                        .order_by(
                            PageSnapshotHistory.observed_at.desc(),
                            PageSnapshotHistory.id.desc(),
                        )
                        .offset(30)
                    )
                )
                if stale_history_ids:
                    await session.execute(
                        delete(PageSnapshotHistory).where(
                            PageSnapshotHistory.id.in_(stale_history_ids)
                        )
                    )
            recipient_id = target.user_id
            deliveries: list[tuple[int, NotificationPayload, str]] = []
            for index, notification in enumerate(notifications):
                deliveries.append((recipient_id, notification, f"owner:{index}"))
                if (
                    admin_report_categories
                    and recipient_id != self.settings.admin_telegram_id
                    and notification.category in admin_report_categories
                ):
                    deliveries.append(
                        (
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
                            f"admin-copy:{index}",
                        )
                    )
            event_stamp = checked_at.isoformat(timespec="microseconds")
            for delivery_recipient, delivery_payload, delivery_suffix in deliveries:
                session.add(
                    NotificationOutbox(
                        event_key=(
                            f"target:{target.id}:{event_stamp}:"
                            f"{delivery_payload.category}:{delivery_suffix}"
                        ),
                        recipient_id=delivery_recipient,
                        target_page_id=target.id,
                        category=delivery_payload.category,
                        payload_json=self._notification_payload_json(delivery_payload),
                        status="Pending",
                        next_attempt_at=checked_at,
                    )
                )
            await session.commit()

        if searchability_issue:
            await self.diagnostics.add(
                level="WARNING",
                event="searchability_throttling_suspected",
                message="پیج فعال است اما در ممیزی جست‌وجوی مهمان دیده نشد.",
                username=username,
                detail=(
                    f"target_id={target_id}; tag="
                    "Potential Search Shadowban / Throttling"
                ),
            )
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
                f"source={result.source}; metadata_complete={result.metadata_complete}; "
                f"admin_categories={','.join(sorted(admin_report_categories)) or 'none'}"
            ),
        )

        return result

    @staticmethod
    def _notification_payload_json(payload: NotificationPayload) -> str:
        data = {
            "message": payload.message,
            "username": payload.username,
            "title": payload.title,
            "category": payload.category,
            "primary_label": payload.primary_label,
            "primary_value": payload.primary_value,
            "secondary_label": payload.secondary_label,
            "secondary_value": payload.secondary_value,
            "accent": payload.accent,
        }
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _notification_payload_from_json(raw: str) -> NotificationPayload:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("notification payload is not an object")
        return NotificationPayload(**payload)

    async def dispatch_notification_outbox(
        self,
        *,
        limit: int | None = None,
    ) -> int:
        """Deliver due notifications outside database transactions with retry."""
        lock = self.redis.lock(
            self.OUTBOX_LOCK_KEY,
            timeout=300,
            blocking_timeout=0,
        )
        if not await lock.acquire(blocking=False):
            return 0
        delivered_count = 0
        try:
            now = datetime.now(timezone.utc)
            batch_size = limit or self.settings.outbox_batch_size
            async with self.session_factory() as session:
                rows = list(
                    await session.scalars(
                        select(NotificationOutbox)
                        .where(
                            NotificationOutbox.status.in_(("Pending", "Retry")),
                            NotificationOutbox.next_attempt_at <= now,
                        )
                        .order_by(
                            NotificationOutbox.next_attempt_at,
                            NotificationOutbox.id,
                        )
                        .limit(max(1, min(batch_size, 100)))
                    )
                )

            for row in rows:
                try:
                    payload = self._notification_payload_from_json(row.payload_json)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    result = DeliveryResult(
                        False,
                        error=f"invalid payload: {type(exc).__name__}: {exc}",
                        terminal=True,
                    )
                else:
                    result = await self._deliver_notification(
                        row.recipient_id,
                        payload,
                    )

                async with self.session_factory() as session:
                    stored = await session.scalar(
                        select(NotificationOutbox)
                        .where(NotificationOutbox.id == row.id)
                        .with_for_update()
                    )
                    if stored is None or stored.status == "Sent":
                        continue
                    stored.attempt_count += 1
                    if result.delivered:
                        stored.status = "Sent"
                        stored.sent_at = datetime.now(timezone.utc)
                        stored.last_error = None
                        delivered_count += 1
                    else:
                        stored.last_error = self._truncate(result.error, 2000)
                        exhausted = (
                            stored.attempt_count >= self.settings.outbox_max_attempts
                        )
                        if result.terminal or exhausted:
                            stored.status = "Dead"
                        else:
                            stored.status = "Retry"
                            delay = result.retry_after or min(
                                21600,
                                30 * (2 ** min(stored.attempt_count - 1, 10)),
                            )
                            stored.next_attempt_at = datetime.now(
                                timezone.utc
                            ) + timedelta(seconds=max(1, delay))
                    await session.commit()

                if not result.delivered:
                    await self.diagnostics.add(
                        level="ERROR" if result.terminal else "WARNING",
                        event=(
                            "notification_delivery_dead"
                            if result.terminal
                            else "notification_delivery_retry"
                        ),
                        message=(
                            "ارسال اعلان برای همیشه متوقف شد."
                            if result.terminal
                            else "ارسال اعلان ناموفق بود و دوباره تلاش می‌شود."
                        ),
                        detail=(
                            f"outbox_id={row.id}; recipient={row.recipient_id}; "
                            f"error={result.error or 'unknown'}"
                        ),
                    )
                await asyncio.sleep(0.05)
            return delivered_count
        finally:
            with suppress(LockError, RedisError):
                await lock.release()

    async def retry_failed_notifications(self) -> int:
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            rows = list(
                await session.scalars(
                    select(NotificationOutbox).where(
                        NotificationOutbox.status.in_(("Retry", "Dead"))
                    )
                )
            )
            for row in rows:
                row.status = "Retry"
                row.attempt_count = 0
                row.next_attempt_at = now
                row.last_error = None
            await session.commit()
            return len(rows)

    async def outbox_status_counts(self) -> dict[str, int]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(NotificationOutbox.status, func.count(NotificationOutbox.id))
                    .group_by(NotificationOutbox.status)
                )
            ).all()
        return {str(status): int(count) for status, count in rows}

    async def cleanup_notification_outbox(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            await session.execute(
                delete(NotificationOutbox).where(
                    NotificationOutbox.status == "Sent",
                    NotificationOutbox.sent_at < now - timedelta(days=30),
                )
            )
            await session.execute(
                delete(NotificationOutbox).where(
                    NotificationOutbox.status == "Dead",
                    NotificationOutbox.created_at < now - timedelta(days=90),
                )
            )
            await session.commit()

    async def _notify(
        self,
        telegram_id: int,
        notification: str | NotificationPayload,
    ) -> bool:
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
        result = await self._deliver_notification(telegram_id, payload)
        if not result.delivered:
            logger.warning(
                "Telegram notification failed for user %s: %s",
                telegram_id,
                result.error or "unknown error",
            )
        return result.delivered

    async def _deliver_notification(
        self,
        telegram_id: int,
        payload: NotificationPayload,
    ) -> DeliveryResult:
        card: bytes | None = None
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
        except Exception as exc:
            logger.warning("Could not render notification card: %s", exc)

        if card is not None:
            try:
                await self.bot.send_photo(
                    telegram_id,
                    BufferedInputFile(card, filename="farstar-security-report.jpg"),
                    caption=payload.message,
                    reply_markup=payload.reply_markup,
                )
                return DeliveryResult(True)
            except TelegramRetryAfter as exc:
                return DeliveryResult(
                    False,
                    error=f"TelegramRetryAfter: {exc}",
                    retry_after=max(1, int(exc.retry_after)),
                )
            except TelegramForbiddenError as exc:
                return DeliveryResult(
                    False,
                    error=f"TelegramForbiddenError: {exc}",
                    terminal=True,
                )
            except TelegramAPIError as exc:
                logger.warning(
                    "Telegram photo notification failed for user %s: %s",
                    telegram_id,
                    exc,
                )

        try:
            await self.bot.send_message(
                telegram_id,
                payload.message,
                reply_markup=payload.reply_markup,
            )
            return DeliveryResult(True)
        except TelegramRetryAfter as exc:
            return DeliveryResult(
                False,
                error=f"TelegramRetryAfter: {exc}",
                retry_after=max(1, int(exc.retry_after)),
            )
        except TelegramForbiddenError as exc:
            return DeliveryResult(
                False,
                error=f"TelegramForbiddenError: {exc}",
                terminal=True,
            )
        except TelegramAPIError as exc:
            return DeliveryResult(False, error=f"TelegramAPIError: {exc}")

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _normalize_text(value: object) -> str:
        return str(value or "").strip()

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
    def _plain_count(value: object) -> int | None:
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
    def _format_duration(seconds: int) -> str:
        if seconds >= 3600:
            value = f"{seconds / 3600:.1f} ساعت"
        elif seconds >= 60:
            value = f"{seconds / 60:.1f} دقیقه"
        else:
            value = f"{seconds} ثانیه"
        return value.replace(".0", "").translate(PERSIAN_DIGITS)

    @staticmethod
    def _normalize_account_type(value: object) -> str | None:
        normalized = str(value or "").strip().lower()
        aliases = {
            "business": "business",
            "creator": "creator",
            "professional": "professional",
            "personal": "personal",
        }
        return aliases.get(normalized)

    @staticmethod
    def _account_type_fa(value: str | None) -> str:
        return {
            "business": "تجاری",
            "creator": "تولیدکننده محتوا",
            "professional": "حرفه‌ای",
            "personal": "شخصی",
        }.get(value or "", "نامشخص")

    @staticmethod
    def _malicious_link_reasons(value: str | None) -> list[str]:
        if not value:
            return []
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        searchable = f"{hostname}{parsed.path}?{parsed.query}".lower()
        reasons: list[str] = []
        signatures = {
            r"(?:^|[.\-_/])(login|signin|verify|verification)(?:[.\-_/]|$)": (
                "الگوی ورود یا تأیید هویت"
            ),
            r"(?:casino|gambl|betting|sportsbet|jackpot)": "الگوی قمار یا شرط‌بندی",
            r"(?:airdrop|claim[-_]?bonus|free[-_]?crypto|wallet[-_]?connect)": (
                "الگوی جایزه یا رمزارز مشکوک"
            ),
            r"(?:phish|account[-_]?recovery|copyright[-_]?appeal)": (
                "الگوی فیشینگ یا بازیابی جعلی"
            ),
        }
        for pattern, label in signatures.items():
            if re.search(pattern, searchable, flags=re.IGNORECASE):
                reasons.append(label)
        if hostname.startswith("xn--") or ".xn--" in hostname:
            reasons.append("دامنه بین‌المللی رمزگذاری‌شده")
        try:
            ipaddress.ip_address(hostname.strip("[]"))
        except ValueError:
            pass
        else:
            reasons.append("استفاده مستقیم از نشانی آی‌پی")
        return list(dict.fromkeys(reasons))

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
        existing_ttl = await self.redis.ttl(self.STATUS_COOLDOWN_KEY)
        if existing_ttl > 0:
            return existing_ttl
        if existing_ttl == -1:
            await self.redis.delete(self.STATUS_COOLDOWN_KEY)
        created = await self.redis.set(
            self.STATUS_COOLDOWN_KEY,
            str(cooldown),
            ex=cooldown,
            nx=True,
        )
        if not created:
            current_ttl = await self.redis.ttl(self.STATUS_COOLDOWN_KEY)
            return current_ttl if current_ttl > 0 else cooldown
        preflight_ttl = int(getattr(self.settings, "preflight_cache_seconds", 300))
        await self.redis.delete(self.RECOVERY_DUE_KEY)
        await self.redis.set(
            self.PREFLIGHT_CACHE_KEY,
            json.dumps(
                {
                    "ok": False,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "detail": "target_rate_limit_circuit_open",
                },
                separators=(",", ":"),
            ),
            ex=max(60, preflight_ttl),
        )
        logger.warning(
            "Instagram rate limit detected; pausing checks for %s seconds",
            cooldown,
        )
        return cooldown
