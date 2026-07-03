from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from bot.config import get_settings


PERSIAN_DIGITS = str.maketrans("0123456789,", "۰۱۲۳۴۵۶۷۸۹٬")
LOCALIZED_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
SUPPORTED_CURRENCIES = {"TOMAN", "USD"}
USD_RATE_CACHE_KEY = "farstar:money:usd-toman-rate:v1"
USD_RATE_FALLBACK_KEY = "farstar:money:usd-toman-fallback:v1"
USD_RATE_CACHE_TTL_SECONDS = 7200
TGJU_USD_URL = "https://www.tgju.org/profile/price_dollar_rl"
TGJU_RATE_RE = re.compile(
    r'data-col=["\']info\.last_trade\.PDrCotVal["\'][^>]*>\s*([^<]+)',
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)


def normalize_currency(value: str | None) -> str:
    normalized = (value or "TOMAN").upper()
    return normalized if normalized in SUPPORTED_CURRENCIES else "TOMAN"


def currency_name(value: str | None) -> str:
    return "دلار" if normalize_currency(value) == "USD" else "تومان"


def format_money(amount: int | None, currency: str | None) -> str:
    safe_amount = max(0, int(amount or 0))
    number = f"{safe_amount:,}".translate(PERSIAN_DIGITS)
    return f"{number} {currency_name(currency)}"


def convert_usd_to_toman(usd_amount: float, current_rate: int) -> int:
    if isinstance(current_rate, bool) or current_rate <= 0:
        raise ValueError("current_rate must be a positive integer")
    try:
        amount = Decimal(str(usd_amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("usd_amount must be a finite non-negative number") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("usd_amount must be a finite non-negative number")
    converted = (amount * Decimal(current_rate)).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return int(converted)


def _parse_tgju_usd_toman(raw_html: str) -> int:
    match = TGJU_RATE_RE.search(raw_html)
    if match is None:
        raise ValueError("TGJU USD rate marker was not found")
    raw_rate = re.sub(r"[^0-9]", "", match.group(1).translate(LOCALIZED_DIGITS))
    if not raw_rate:
        raise ValueError("TGJU USD rate was empty")
    rial_rate = int(raw_rate)
    toman_rate = rial_rate // 10
    if not 10_000 <= toman_rate <= 10_000_000:
        raise ValueError("TGJU USD rate was outside the safety range")
    return toman_rate


async def _fallback_usd_rate(redis_client: Redis) -> int:
    try:
        configured = await redis_client.get(USD_RATE_FALLBACK_KEY)
        if configured is not None:
            rate = int(configured)
            if 10_000 <= rate <= 10_000_000:
                return rate
    except (RedisError, TypeError, ValueError):
        logger.warning("Could not read the administrator USD fallback rate")
    try:
        return get_settings().usd_toman_fallback_rate
    except Exception:
        logger.exception("Could not load configured USD fallback rate")
        return 650_000


async def fetch_live_usd_rate(redis_client: Redis) -> int:
    try:
        cached = await redis_client.get(USD_RATE_CACHE_KEY)
        if cached is not None:
            cached_rate = int(cached)
            if 10_000 <= cached_rate <= 10_000_000:
                return cached_rate
    except (RedisError, TypeError, ValueError):
        logger.warning("Could not read the cached USD/Toman rate", exc_info=True)

    try:
        async with httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            trust_env=False,
            timeout=httpx.Timeout(10.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.7",
            },
        ) as client:
            response = await client.get(TGJU_USD_URL)
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("TGJU response exceeded the safety limit")
            live_rate = _parse_tgju_usd_toman(response.text)
        try:
            await redis_client.set(
                USD_RATE_CACHE_KEY,
                str(live_rate),
                ex=USD_RATE_CACHE_TTL_SECONDS,
            )
        except RedisError:
            logger.warning("Could not cache the USD/Toman rate", exc_info=True)
        return live_rate
    except (httpx.HTTPError, ValueError, UnicodeError):
        logger.warning(
            "Live USD/Toman rate fetch failed; using fallback", exc_info=True
        )

    fallback = await _fallback_usd_rate(redis_client)
    try:
        await redis_client.set(USD_RATE_CACHE_KEY, str(fallback), ex=300)
    except RedisError:
        pass
    return fallback
