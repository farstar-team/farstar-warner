from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from contextlib import suppress
from dataclasses import asdict, dataclass
from enum import Enum
from urllib.parse import parse_qs, unquote, urlsplit

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
    external_link: str | None = None
    external_link_observed: bool = False
    account_type: str | None = None
    account_type_observed: bool = False
    category_name: str | None = None
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
        self._embed_http = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.instagram_request_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            proxy=settings.instagram_proxy_url,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        self._embed_direct_http = httpx.AsyncClient(
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
        await self._embed_http.aclose()
        await self._embed_direct_http.aclose()

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
            source = (
                "http_public_embed"
                if profile.diagnostic == "http_embed"
                else "playwright_public_embed"
            )
            return ProfileResult(
                CheckOutcome.ACTIVE,
                source=source,
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
                external_link=profile.external_link,
                external_link_observed=profile.external_link_observed,
                account_type=profile.account_type,
                account_type_observed=profile.account_type_observed,
                category_name=profile.category_name,
                http_status=200,
            )
        if profile.outcome == PreviewOutcome.DEACTIVATED:
            source = (
                "http_embed_explicit_absence"
                if (profile.diagnostic or "").startswith("http_embed")
                else "playwright_explicit_absence"
            )
            return ProfileResult(
                CheckOutcome.DEACTIVATED,
                source=source,
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
            profile = await self._inspect_with_http(normalized_username)
            if profile.outcome == PreviewOutcome.UNKNOWN:
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

    async def _inspect_with_http(self, username: str) -> EmbedProfile:
        clients = (
            (("proxy", self._embed_http), ("direct", self._embed_direct_http))
            if self.settings.instagram_proxy_url
            # Without a proxy, use two independent HTTP client pools.  A lone
            # edge 404 is not enough to declare a profile deactivated during
            # registration; both temporal/connection attempts must agree.
            else (
                ("direct-primary", self._embed_direct_http),
                ("direct-confirmation", self._embed_http),
            )
        )
        diagnostics: list[str] = []
        absence_routes = 0
        url = f"{self.settings.instagram_base_url}/{username}/embed/"
        for route, client in clients:
            try:
                response = await client.get(
                    url,
                    headers={
                        "User-Agent": USER_AGENTS[0],
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.instagram.com/",
                    },
                )
            except httpx.HTTPError as exc:
                diagnostics.append(f"{route}:{type(exc).__name__}")
                continue
            diagnostics.append(f"{route}:http_{response.status_code}")
            if response.status_code == 404:
                absence_routes += 1
                continue
            if response.status_code != 200:
                continue
            if len(response.content) > self.MAX_AVATAR_BYTES:
                diagnostics.append(f"{route}:body_too_large")
                continue
            profile = self._parse_embed_html(response.text, username)
            if profile is not None:
                if profile.outcome == PreviewOutcome.DEACTIVATED:
                    absence_routes += 1
                    continue
                return profile
        if absence_routes == len(clients):
            return EmbedProfile(
                PreviewOutcome.DEACTIVATED,
                username=username,
                diagnostic="http_embed_404_consensus",
            )
        return EmbedProfile(
            PreviewOutcome.UNKNOWN,
            username=username,
            diagnostic="embed_" + "_".join(diagnostics[-4:]),
        )

    @classmethod
    def _parse_embed_html(
        cls,
        raw_html: str,
        username: str,
    ) -> EmbedProfile | None:
        lowered = raw_html.lower()
        if any(
            marker in lowered
            for marker in (
                "sorry, this page isn't available",
                "page isn't available",
                "page not found",
            )
        ):
            return EmbedProfile(
                PreviewOutcome.DEACTIVATED,
                username=username,
                diagnostic="http_embed_not_available",
            )
        if "accounts/login" in lowered and username.lower() not in lowered:
            return None

        metadata: dict[str, str] = {}
        for tag in re.findall(r"<meta\b[^>]*>", raw_html, flags=re.IGNORECASE):
            attributes = {
                key.lower(): html.unescape(value)
                for key, _, value in re.findall(
                    r"([:\w-]+)\s*=\s*(['\"])(.*?)\2",
                    tag,
                    flags=re.DOTALL,
                )
            }
            key = attributes.get("property") or attributes.get("name")
            content = attributes.get("content")
            if key and content:
                metadata[key.lower()] = content.strip()

        title = metadata.get("og:title", "")
        description = metadata.get("og:description", "")
        visible = html.unescape(re.sub(r"<[^>]+>", " ", raw_html, flags=re.DOTALL))
        combined = f"{title}\n{description}\n{visible[:200_000]}"
        exact_patterns = (
            rf"(?<![\w.])@{re.escape(username)}(?![\w.])",
            rf"\"username\"\s*:\s*\"{re.escape(username)}\"",
        )
        if not any(
            re.search(pattern, combined, re.IGNORECASE) for pattern in exact_patterns
        ):
            return None

        def metric(label: str) -> int | None:
            match = re.search(
                rf"([\d.,]+\s*[KMB]?)\s+{label}\b",
                combined,
                flags=re.IGNORECASE,
            )
            return cls._parse_metric(match.group(1)) if match else None

        full_name: str | None = None
        name_match = re.search(
            rf"^\s*(.*?)\s*\(@{re.escape(username)}\)",
            title,
            flags=re.IGNORECASE,
        )
        if name_match:
            full_name = cls._normalize_text(name_match.group(1))
        profile_node = cls._find_profile_node(raw_html, username)
        external_link = cls._external_link_from_node(profile_node)
        if external_link is None:
            external_link = cls._external_link_from_anchors(raw_html)
        account_type = cls._account_type_from_node(profile_node)
        category_name = (
            cls._normalize_text(
                profile_node.get("category_name")
                or profile_node.get("business_category_name")
            )
            if profile_node
            else None
        )
        if profile_node:
            full_name = cls._normalize_text(profile_node.get("full_name")) or full_name
        biography = (
            cls._normalize_text(profile_node.get("biography")) if profile_node else None
        )
        private_marker = bool(
            "this account is private" in lowered
            or re.search(r"\"is_private\"\s*:\s*true", raw_html, re.IGNORECASE)
        )
        return EmbedProfile(
            PreviewOutcome.ACTIVE,
            username=username.lower(),
            full_name=full_name,
            biography=biography,
            profile_picture_url=metadata.get("og:image"),
            follower_count=metric("followers"),
            following_count=metric("following"),
            post_count=metric("posts"),
            is_private=True if private_marker else None,
            is_verified=bool(
                re.search(r"\"is_verified\"\s*:\s*true", raw_html, re.IGNORECASE)
            ),
            external_link=external_link,
            external_link_observed=bool(
                profile_node
                and any(
                    key in profile_node
                    for key in ("external_url", "website_url", "bio_links")
                )
            ),
            account_type=account_type,
            account_type_observed=bool(
                profile_node
                and any(
                    key in profile_node
                    for key in (
                        "is_business_account",
                        "is_professional_account",
                        "account_type",
                    )
                )
            ),
            category_name=category_name,
            diagnostic="http_embed",
        )

    @classmethod
    def _find_profile_node(
        cls, raw_html: str, username: str
    ) -> dict[str, object] | None:
        wanted = username.strip().lower()

        def walk(value: object) -> dict[str, object] | None:
            if isinstance(value, dict):
                candidate_username = str(value.get("username") or "").strip().lower()
                if candidate_username == wanted and any(
                    key in value
                    for key in (
                        "edge_followed_by",
                        "follower_count",
                        "is_private",
                        "profile_pic_url",
                    )
                ):
                    return value
                for nested in value.values():
                    found = walk(nested)
                    if found is not None:
                        return found
            elif isinstance(value, list):
                for nested in value:
                    found = walk(nested)
                    if found is not None:
                        return found
            return None

        scripts = re.findall(
            r"<script\b[^>]*type\s*=\s*(['\"])application/json\1[^>]*>(.*?)</script>",
            raw_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for _, encoded in scripts:
            try:
                payload = json.loads(html.unescape(encoded).strip())
            except (json.JSONDecodeError, TypeError):
                continue
            found = walk(payload)
            if found is not None:
                return found
        return None

    @classmethod
    def _external_link_from_node(cls, node: dict[str, object] | None) -> str | None:
        if not node:
            return None
        candidates: list[object] = [node.get("external_url"), node.get("website_url")]
        bio_links = node.get("bio_links")
        if isinstance(bio_links, list):
            for entry in bio_links:
                if isinstance(entry, dict):
                    candidates.extend((entry.get("url"), entry.get("lynx_url")))
        for candidate in candidates:
            normalized = cls._normalize_external_link(candidate)
            if normalized:
                return normalized
        return None

    @classmethod
    def _external_link_from_anchors(cls, raw_html: str) -> str | None:
        for raw_url in re.findall(
            r"<a\b[^>]*href\s*=\s*(['\"])(.*?)\1",
            raw_html,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            normalized = cls._normalize_external_link(html.unescape(raw_url[1]))
            if normalized:
                return normalized
        return None

    @staticmethod
    def _normalize_external_link(value: object) -> str | None:
        normalized = str(value or "").strip()
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
        return normalized[:2000]

    @staticmethod
    def _account_type_from_node(node: dict[str, object] | None) -> str | None:
        if not node:
            return None
        raw_type = str(node.get("account_type") or "").strip().lower()
        if raw_type in {"business", "creator", "professional", "personal"}:
            return raw_type
        if node.get("is_business_account") is True:
            return "business"
        if node.get("is_professional_account") is True:
            return "professional"
        if (
            node.get("is_business_account") is False
            and node.get("is_professional_account") is False
        ):
            return "personal"
        return None

    @staticmethod
    def _parse_metric(value: str) -> int | None:
        normalized = value.replace(",", "").replace(" ", "").upper()
        multiplier = 1
        if normalized.endswith("B"):
            multiplier = 1_000_000_000
            normalized = normalized[:-1]
        elif normalized.endswith("M"):
            multiplier = 1_000_000
            normalized = normalized[:-1]
        elif normalized.endswith("K"):
            multiplier = 1_000
            normalized = normalized[:-1]
        try:
            return round(float(normalized) * multiplier)
        except ValueError:
            return None

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
            proxy = (
                {"server": self.settings.instagram_proxy_url}
                if self.settings.instagram_proxy_url
                else None
            )
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                executable_path=self.settings.chromium_executable,
                proxy=proxy,
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
                    PreviewOutcome.UNKNOWN,
                    username=username,
                    diagnostic="unconfirmed_browser_http_404",
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
                      const wanted = requested.toLowerCase();
                      const exact = text.split(/\n+/).some(line => {
                        const value = line.trim();
                        return value === wanted || value === `@${wanted}`;
                      });
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
                    self._normalize_text(await page.locator("body").inner_text()) or ""
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
                        PreviewOutcome.UNKNOWN,
                        username=username,
                        diagnostic="unconfirmed_browser_not_available_text",
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
                   const externalAnchor = Array.from(document.querySelectorAll('a[href]')).find(anchor => {
                     try {
                       const host = new URL(anchor.href).hostname.toLowerCase();
                       return host && !host.endsWith('instagram.com');
                     } catch (_) { return false; }
                   });
                   return {
                    username: requested.toLowerCase(),
                    full_name: fullName && !/^\d/.test(fullName) ? fullName : null,
                    biography: bio || null,
                    profile_picture_url: avatar ? avatar.currentSrc || avatar.src : null,
                    follower_count: metric(followerLine),
                    follower_display: followerLine ? followerLine.split(/\s+followers/i)[0] : null,
                     post_count: metric(postLine),
                     is_private: lower.includes('this account is private'),
                     is_verified: Boolean(document.querySelector('[aria-label="Verified"]')),
                     external_link: externalAnchor ? externalAnchor.href : null,
                     external_link_observed: false,
                     account_type: null,
                     account_type_observed: false,
                     category_name: null
                   };
                }
                """,
                username,
            )
            parsed_html = self._parse_embed_html(await page.content(), username)
            if parsed_html is not None:
                for field in (
                    "external_link",
                    "account_type",
                    "category_name",
                ):
                    parsed_value = getattr(parsed_html, field)
                    if parsed_value is not None:
                        data[field] = parsed_value
                data["external_link_observed"] = parsed_html.external_link_observed
                data["account_type_observed"] = parsed_html.account_type_observed
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
