from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass
from enum import Enum

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from redis.asyncio import Redis

from bot.checker import CheckOutcome, ProfileResult, USER_AGENTS
from bot.config import Settings
from bot.report_cards import ProfileCardData, ReportCardRenderer


logger = logging.getLogger(__name__)


class PreviewOutcome(str, Enum):
    ACTIVE = "active"
    DEACTIVATED = "deactivated"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class EmbedProfile:
    outcome: PreviewOutcome
    username: str
    full_name: str | None = None
    biography: str | None = None
    profile_picture_url: str | None = None
    follower_count: int | None = None
    following_count: int | None = None
    follower_display: str | None = None
    post_count: int | None = None
    is_private: bool | None = None
    is_verified: bool = False
    diagnostic: str | None = None


class ProfilePreviewService:
    CACHE_PREFIX = "farstar:embed-preview:"
    MAX_AVATAR_BYTES = 8_000_000

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._start_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(settings.profile_preview_concurrency)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.instagram_request_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def close(self) -> None:
        if self._browser is not None:
            with suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None
        await self._http.aclose()

    async def browser_health(self) -> tuple[bool, str | None]:
        try:
            browser = await self._ensure_browser()
            return browser.is_connected(), browser.version
        except Exception as exc:
            logger.warning("Chromium health check failed: %s", exc)
            return False, None

    async def probe_status(self, username: str) -> ProfileResult:
        profile = await self.inspect(username, use_cache=False)
        if profile.outcome == PreviewOutcome.ACTIVE:
            return ProfileResult(
                CheckOutcome.ACTIVE,
                source="playwright_public_embed",
                metadata_complete=False,
                canonical_username=profile.username.lower(),
                full_name=self._normalize_text(profile.full_name),
                biography=self._normalize_text(profile.biography),
                profile_picture_url=profile.profile_picture_url,
                follower_count=profile.follower_count,
                following_count=profile.following_count,
                post_count=profile.post_count,
                is_private=profile.is_private,
                is_verified=profile.is_verified,
                http_status=200,
            )
        if profile.outcome == PreviewOutcome.DEACTIVATED:
            return ProfileResult(
                CheckOutcome.DEACTIVATED,
                source="playwright_explicit_absence",
                http_status=404,
            )
        return ProfileResult(
            CheckOutcome.UNKNOWN,
            source=f"playwright_{profile.diagnostic or 'unknown'}",
        )

    async def inspect(self, username: str, *, use_cache: bool = True) -> EmbedProfile:
        normalized_username = username.strip().lower()
        cache_key = f"{self.CACHE_PREFIX}{normalized_username}"
        if use_cache:
            cached = await self.redis.get(cache_key)
            if cached:
                try:
                    data = json.loads(cached)
                    data["outcome"] = PreviewOutcome(data["outcome"])
                    return EmbedProfile(**data)
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    await self.redis.delete(cache_key)

        async with self._semaphore:
            profile = await self._inspect_with_browser(normalized_username)
        if profile.outcome == PreviewOutcome.ACTIVE:
            payload = asdict(profile)
            payload["outcome"] = profile.outcome.value
            await self.redis.set(
                cache_key,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ex=self.settings.profile_preview_cache_seconds,
            )
        return profile

    async def render_card(self, profile: EmbedProfile) -> bytes:
        avatar_bytes: bytes | None = None
        if profile.profile_picture_url:
            try:
                response = await self._http.get(
                    profile.profile_picture_url,
                    headers={
                        "User-Agent": USER_AGENTS[0],
                        "Referer": (
                            f"{self.settings.instagram_base_url}/"
                            f"{profile.username}/embed/"
                        ),
                    },
                )
                content_type = response.headers.get("content-type", "").lower()
                if (
                    response.status_code == 200
                    and content_type.startswith("image/")
                    and len(response.content) <= self.MAX_AVATAR_BYTES
                ):
                    avatar_bytes = response.content
            except httpx.HTTPError:
                logger.info("Could not download avatar for %s", profile.username)
        return await asyncio.to_thread(
            ReportCardRenderer.render_profile,
            ProfileCardData(
                username=profile.username,
                full_name=profile.full_name,
                biography=profile.biography,
                follower_count=profile.follower_count,
                following_count=profile.following_count,
                post_count=profile.post_count,
                is_private=profile.is_private,
                is_verified=profile.is_verified,
            ),
            avatar_bytes,
        )

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        async with self._start_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._browser is not None:
                with suppress(Exception):
                    await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                with suppress(Exception):
                    await self._playwright.stop()
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                executable_path=self.settings.chromium_executable,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-sync",
                ],
            )
            return self._browser

    async def _inspect_with_browser(self, username: str) -> EmbedProfile:
        context: BrowserContext | None = None
        try:
            browser = await self._ensure_browser()
            context = await browser.new_context(
                viewport={"width": 430, "height": 900},
                locale="en-US",
                java_script_enabled=True,
                user_agent=USER_AGENTS[0],
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = await context.new_page()
            response = await page.goto(
                f"{self.settings.instagram_base_url}/{username}/embed/",
                wait_until="domcontentloaded",
                timeout=self.settings.profile_preview_timeout_seconds * 1000,
            )
            if response is not None and response.status == 404:
                return EmbedProfile(
                    PreviewOutcome.DEACTIVATED,
                    username=username,
                    diagnostic="http_404",
                )
            if response is not None and response.status != 200:
                return EmbedProfile(
                    PreviewOutcome.UNKNOWN,
                    username=username,
                    diagnostic=f"http_{response.status}",
                )
            if "/accounts/login" in page.url:
                return EmbedProfile(
                    PreviewOutcome.UNKNOWN,
                    username=username,
                    diagnostic="login_redirect",
                )

            try:
                await page.wait_for_function(
                    r"""
                    requested => {
                      const text = (document.body?.innerText || '').toLowerCase();
                      const exact = text.split(/\n+/)
                        .some(line => line.trim() === requested.toLowerCase());
                      const metrics = /\bfollowers\b/.test(text) && /\bposts\b/.test(text);
                      const image = Array.from(document.images).some(img =>
                        (img.alt || '').toLowerCase().includes('profile picture'));
                      return exact && metrics && image;
                    }
                    """,
                    arg=username,
                    timeout=self.settings.profile_preview_timeout_seconds * 1000,
                )
            except PlaywrightTimeoutError:
                body_text = (
                    self._normalize_text(await page.locator("body").inner_text())
                    or ""
                ).lower()
                if "log in" in body_text or "login" in page.url:
                    return EmbedProfile(
                        PreviewOutcome.UNKNOWN,
                        username=username,
                        diagnostic="login_wall",
                    )
                if any(
                    marker in body_text
                    for marker in (
                        "page isn't available",
                        "page not found",
                        "sorry, this page isn't available",
                    )
                ):
                    return EmbedProfile(
                        PreviewOutcome.DEACTIVATED,
                        username=username,
                        diagnostic="not_available_text",
                    )
                return EmbedProfile(
                    PreviewOutcome.UNKNOWN,
                    username=username,
                    diagnostic="profile_content_timeout",
                )

            data = await page.evaluate(
                r"""
                requested => {
                  const clean = value => (value || '').trim();
                  const lines = (document.body.innerText || '')
                    .split(/\n+/).map(clean).filter(Boolean);
                  const usernameIndex = lines.findIndex(line =>
                    line.toLowerCase() === requested.toLowerCase());
                  const followerLine = lines.find(line => /\bfollowers$/i.test(line));
                  const postLine = lines.find(line => /\bposts$/i.test(line));
                  const metric = value => {
                    const match = clean(value).match(/^([\d,.]+\s*[KMB]?)/i);
                    if (!match) return null;
                    const normalized = match[1].replaceAll(',', '').replaceAll(' ', '').toUpperCase();
                    const number = Number.parseFloat(normalized);
                    if (!Number.isFinite(number)) return null;
                    if (normalized.endsWith('B')) return Math.round(number * 1000000000);
                    if (normalized.endsWith('M')) return Math.round(number * 1000000);
                    if (normalized.endsWith('K')) return Math.round(number * 1000);
                    return Math.round(number);
                  };
                  const images = Array.from(document.images);
                  const avatar = images.find(img =>
                    clean(img.alt).toLowerCase().includes('profile picture'));
                  const followerIndex = lines.findIndex(line => line === followerLine);
                  const fullName = usernameIndex >= 0 ? clean(lines[usernameIndex + 1]) : '';
                  const bio = usernameIndex >= 0 && followerIndex > usernameIndex
                    ? lines.slice(usernameIndex + 2, Math.max(usernameIndex + 2, followerIndex - 1)).join('\n')
                    : '';
                  const lower = (document.body.innerText || '').toLowerCase();
                  return {
                    username: requested.toLowerCase(),
                    full_name: fullName && !/^\d/.test(fullName) ? fullName : null,
                    biography: bio || null,
                    profile_picture_url: avatar ? avatar.currentSrc || avatar.src : null,
                    follower_count: metric(followerLine),
                    follower_display: followerLine ? followerLine.split(/\s+followers/i)[0] : null,
                    post_count: metric(postLine),
                    is_private: lower.includes('this account is private'),
                    is_verified: Boolean(document.querySelector('[aria-label="Verified"]'))
                  };
                }
                """,
                username,
            )
            return EmbedProfile(
                PreviewOutcome.ACTIVE,
                diagnostic="rendered_embed",
                **data,
            )
        except PlaywrightTimeoutError:
            logger.warning("Profile preview timed out for %s", username)
            return EmbedProfile(
                PreviewOutcome.UNKNOWN,
                username=username,
                diagnostic="navigation_timeout",
            )
        except Exception:
            logger.exception("Profile preview failed for %s", username)
            return EmbedProfile(
                PreviewOutcome.UNKNOWN,
                username=username,
                diagnostic="browser_exception",
            )
        finally:
            if context is not None:
                with suppress(Exception):
                    await context.close()

    @staticmethod
    def _normalize_text(value: object) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None
