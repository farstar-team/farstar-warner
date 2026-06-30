from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import textwrap
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

import arabic_reshaper
import httpx
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont, ImageOps
from playwright.async_api import (
    Browser,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from redis.asyncio import Redis

from bot.checker import USER_AGENTS
from bot.config import Settings


logger = logging.getLogger(__name__)
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


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
    follower_display: str | None = None
    post_count: int | None = None
    is_private: bool | None = None
    is_verified: bool = False


class ProfilePreviewService:
    CACHE_PREFIX = "farstar:embed-preview:"

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
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        await self._http.aclose()

    async def inspect(self, username: str, *, use_cache: bool = True) -> EmbedProfile:
        cache_key = f"{self.CACHE_PREFIX}{username.lower()}"
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
            profile = await self._inspect_with_browser(username)
        if profile.outcome == PreviewOutcome.ACTIVE:
            payload = asdict(profile)
            payload["outcome"] = profile.outcome.value
            await self.redis.set(
                cache_key,
                json.dumps(payload, ensure_ascii=False),
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
                        "Referer": f"{self.settings.instagram_base_url}/{profile.username}/embed/",
                    },
                )
                if response.status_code == 200 and len(response.content) <= 8_000_000:
                    avatar_bytes = response.content
            except httpx.HTTPError:
                logger.info("Could not download avatar for %s", profile.username)
        return await asyncio.to_thread(self._draw_card, profile, avatar_bytes)

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        async with self._start_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._playwright is not None:
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
                ],
            )
            return self._browser

    async def _inspect_with_browser(self, username: str) -> EmbedProfile:
        try:
            browser = await self._ensure_browser()
        except Exception:
            logger.exception("Chromium could not be started for profile preview")
            return EmbedProfile(PreviewOutcome.UNKNOWN, username=username)

        context = await browser.new_context(
            viewport={"width": 430, "height": 900},
            locale="en-US",
            java_script_enabled=True,
        )
        page = await context.new_page()
        url = f"{self.settings.instagram_base_url}/{username}/embed/"
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.settings.profile_preview_timeout_seconds * 1000,
            )
            if response is not None and response.status == 404:
                return EmbedProfile(PreviewOutcome.DEACTIVATED, username=username)
            if response is not None and response.status != 200:
                return EmbedProfile(PreviewOutcome.UNKNOWN, username=username)
            try:
                await page.wait_for_selector(
                    'img[alt*="profile picture"]',
                    state="visible",
                    timeout=self.settings.profile_preview_timeout_seconds * 1000,
                )
            except PlaywrightTimeoutError:
                body_text = (await page.locator("body").inner_text()).lower()
                if "page isn't available" in body_text or "page not found" in body_text:
                    return EmbedProfile(PreviewOutcome.DEACTIVATED, username=username)
                return EmbedProfile(PreviewOutcome.UNKNOWN, username=username)
            try:
                await page.get_by_text(
                    "View full profile on Instagram", exact=True
                ).wait_for(
                    state="visible",
                    timeout=self.settings.profile_preview_timeout_seconds * 1000,
                )
            except PlaywrightTimeoutError:
                logger.info("The complete embed was not rendered for %s", username)

            data = await page.evaluate(
                r"""
                (requestedUsername) => {
                  const clean = value => (value || '').trim();
                  const lines = (document.body.innerText || '')
                    .split(/\n+/).map(clean).filter(Boolean);
                  const images = Array.from(document.querySelectorAll('img'));
                  const profileImage = images.find(img =>
                    clean(img.alt).toLowerCase().includes('profile picture'));
                  const usernameIndex = lines.findIndex(line =>
                    line.toLowerCase() === requestedUsername.toLowerCase());
                  const followersIndex = lines.findIndex(line =>
                    /\bfollowers$/i.test(line));
                  const postsIndex = lines.findIndex(line =>
                    /\bposts$/i.test(line));
                  const followerMatch = followersIndex >= 0
                    ? lines[followersIndex].match(/^([\d,.]+\s*[KMB]?)\s+followers$/i)
                    : null;
                  const postMatch = postsIndex >= 0
                    ? lines[postsIndex].match(/^([\d,]+)\s+posts$/i)
                    : null;
                  const parseMetric = value => {
                    if (!value) return null;
                    const normalized = value.replaceAll(',', '').replaceAll(' ', '').toUpperCase();
                    const number = Number.parseFloat(normalized);
                    if (!Number.isFinite(number)) return null;
                    if (normalized.endsWith('B')) return Math.round(number * 1000000000);
                    if (normalized.endsWith('M')) return Math.round(number * 1000000);
                    if (normalized.endsWith('K')) return Math.round(number * 1000);
                    return Math.round(number);
                  };
                  const numericLabels = Array.from(document.querySelectorAll('[aria-label]'))
                    .map(el => clean(el.getAttribute('aria-label')))
                    .filter(value => /^\d[\d,]*$/.test(value));
                  const numericValues = numericLabels
                    .map(value => Number(value.replaceAll(',', '')))
                    .filter(value => Number.isFinite(value));
                  const fullName = usernameIndex >= 0 ? lines[usernameIndex + 1] : null;
                  const biographyLines = usernameIndex >= 0 && followersIndex > usernameIndex
                    ? lines.slice(usernameIndex + 2, Math.max(usernameIndex + 2, followersIndex - 1))
                    : [];
                  const bodyLower = (document.body.innerText || '').toLowerCase();
                  return {
                    username: usernameIndex >= 0 ? lines[usernameIndex] : requestedUsername,
                    full_name: fullName && !/^\d/.test(fullName) ? fullName : null,
                    biography: biographyLines.length ? biographyLines.join('\n') : null,
                    profile_picture_url: profileImage ? profileImage.currentSrc || profileImage.src : null,
                    follower_count: numericValues.length
                      ? Math.max(...numericValues)
                      : parseMetric(followerMatch ? followerMatch[1] : null),
                    follower_display: followerMatch ? followerMatch[1] : null,
                    post_count: postMatch
                      ? Number(postMatch[1].replaceAll(',', '')) || null
                      : null,
                    is_private: bodyLower.includes('this account is private'),
                    is_verified: Boolean(document.querySelector('[aria-label="Verified"]'))
                  };
                }
                """,
                username,
            )
            return EmbedProfile(PreviewOutcome.ACTIVE, **data)
        except PlaywrightTimeoutError:
            logger.warning("Profile preview timed out for %s", username)
            return EmbedProfile(PreviewOutcome.UNKNOWN, username=username)
        except Exception:
            logger.exception("Profile preview failed for %s", username)
            return EmbedProfile(PreviewOutcome.UNKNOWN, username=username)
        finally:
            await context.close()

    @classmethod
    def _draw_card(cls, profile: EmbedProfile, avatar_bytes: bytes | None) -> bytes:
        width, height = 1080, 1350
        image = Image.new("RGB", (width, height), "#080b17")
        pixels = image.load()
        for y in range(height):
            ratio = y / height
            color = (
                int(10 + 30 * ratio),
                int(13 + 10 * ratio),
                int(28 + 40 * ratio),
            )
            for x in range(width):
                pixels[x, y] = color
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (55, 45, 1025, 1305), radius=55, fill="#11162a", outline="#7148ff", width=5
        )

        regular = cls._font(42, bold=False)
        bold = cls._font(55, bold=True)
        title_font = cls._font(64, bold=True)
        small = cls._font(34, bold=False)
        draw.text(
            (540, 85), "FARSTAR WARNER", font=title_font, fill="#ffffff", anchor="ma"
        )
        draw.text(
            (540, 160),
            cls._rtl("تأیید پیج اینستاگرام"),
            font=regular,
            fill="#b8b9ff",
            anchor="ma",
        )

        avatar = cls._avatar(avatar_bytes, profile.username, 280, bold)
        image.paste(avatar, (400, 225), avatar)
        draw.text(
            (540, 545), f"@{profile.username}", font=bold, fill="#ffffff", anchor="ma"
        )
        if profile.full_name:
            draw.text(
                (540, 615),
                cls._display(profile.full_name),
                font=regular,
                fill="#d8d9e8",
                anchor="ma",
            )

        stats = (
            (
                cls._number(profile.follower_count, profile.follower_display),
                "دنبال‌کننده",
            ),
            (cls._number(profile.post_count), "پست"),
            ("خصوصی" if profile.is_private else "عمومی", "نوع پیج"),
        )
        for index, (value, label) in enumerate(stats):
            left = 95 + index * 315
            draw.rounded_rectangle(
                (left, 700, left + 275, 855), radius=25, fill="#1c2240"
            )
            draw.text(
                (left + 137, 735),
                cls._display(value),
                font=bold,
                fill="#ffffff",
                anchor="ma",
            )
            draw.text(
                (left + 137, 805),
                cls._rtl(label),
                font=small,
                fill="#a9aac3",
                anchor="ma",
            )

        draw.rounded_rectangle((95, 905, 985, 1175), radius=30, fill="#181d35")
        draw.text(
            (940, 940), cls._rtl("بیوگرافی"), font=regular, fill="#b8b9ff", anchor="ra"
        )
        biography = (
            profile.biography or "بیوگرافی در نمای عمومی اینستاگرام ارائه نشده است."
        )
        wrapped = textwrap.wrap(biography, width=48)[:4]
        y = 1010
        for line in wrapped:
            draw.text(
                (940, y), cls._display(line), font=small, fill="#ffffff", anchor="ra"
            )
            y += 48

        verified = "تأییدشده" if profile.is_verified else "بدون نشان تأیید"
        draw.text(
            (540, 1225), cls._rtl(verified), font=small, fill="#65d6ff", anchor="ma"
        )
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=92, optimize=True)
        return output.getvalue()

    @staticmethod
    def _font(size: int, *, bold: bool) -> ImageFont.FreeTypeFont:
        names = (
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
            "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
        for name in names:
            if Path(name).exists():
                return ImageFont.truetype(name, size=size)
        return ImageFont.load_default(size=size)

    @staticmethod
    def _rtl(value: str) -> str:
        return get_display(arabic_reshaper.reshape(value))

    @classmethod
    def _display(cls, value: str) -> str:
        if re.search(r"[\u0600-\u06ff]", value):
            return cls._rtl(value)
        return value

    @staticmethod
    def _number(value: int | None, fallback: str | None = None) -> str:
        if value is not None:
            if value >= 1_000_000_000:
                compact = f"{value / 1_000_000_000:.1f}B"
            elif value >= 1_000_000:
                compact = f"{value / 1_000_000:.1f}M"
            elif value >= 10_000:
                compact = f"{value / 1_000:.1f}K"
            else:
                compact = f"{value:,}".replace(",", "٬")
            return compact.replace(".0", "").translate(PERSIAN_DIGITS)
        if fallback:
            return fallback.translate(PERSIAN_DIGITS)
        return "نامشخص"

    @classmethod
    def _avatar(
        cls,
        avatar_bytes: bytes | None,
        username: str,
        size: int,
        font: ImageFont.FreeTypeFont,
    ) -> Image.Image:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
        if avatar_bytes:
            try:
                avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
                avatar = ImageOps.fit(
                    avatar, (size, size), method=Image.Resampling.LANCZOS
                )
                avatar.putalpha(mask)
                return avatar
            except (OSError, ValueError):
                pass
        avatar = Image.new("RGBA", (size, size), "#7148ff")
        avatar.putalpha(mask)
        draw = ImageDraw.Draw(avatar)
        draw.text(
            (size // 2, size // 2),
            username[:1].upper(),
            font=font,
            fill="white",
            anchor="mm",
        )
        return avatar
