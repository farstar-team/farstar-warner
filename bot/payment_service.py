from __future__ import annotations

import logging
import re
from typing import Any

import httpx


logger = logging.getLogger(__name__)
AUTHORITY_RE = re.compile(r"^[A-Za-z0-9]{10,64}$")


class ZarinpalProvider:
    REQUEST_URL = "https://api.zarinpal.com/pg/v4/payment/request.json"
    VERIFY_URL = "https://api.zarinpal.com/pg/v4/payment/verify.json"
    START_PAY_URL = "https://www.zarinpal.com/pg/StartPay/{authority}"

    def __init__(
        self,
        merchant_id: str | None,
        *,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.merchant_id = str(merchant_id or "").strip()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            http2=True,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.merchant_id)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def request_payment(
        self,
        amount_toman: int,
        description: str,
        callback_url: str,
    ) -> tuple[str | None, str | None]:
        if not self.enabled:
            return None, "merchant_not_configured"
        if isinstance(amount_toman, bool) or amount_toman <= 0:
            return None, "invalid_amount"
        if not callback_url.startswith(("https://", "http://")):
            return None, "invalid_callback_url"
        payload = {
            "merchant_id": self.merchant_id,
            "amount": int(amount_toman),
            "currency": "IRT",
            "description": str(description or "پرداخت فارستار وارنر")[:255],
            "callback_url": callback_url,
        }
        try:
            response = await self._client.post(self.REQUEST_URL, json=payload)
            body = self._json_object(response)
        except httpx.HTTPError as exc:
            logger.warning("Zarinpal payment request failed: %s", type(exc).__name__)
            return None, f"network_{type(exc).__name__.lower()}"
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        code = self._integer(data.get("code"))
        authority = str(data.get("authority") or "").strip()
        if (
            response.status_code == 200
            and code == 100
            and AUTHORITY_RE.fullmatch(authority)
        ):
            return authority, self.START_PAY_URL.format(authority=authority)
        return None, self._error_code(body, response.status_code, code)

    async def verify_payment(
        self,
        amount_toman: int,
        authority: str,
    ) -> tuple[bool, str | None]:
        normalized_authority = str(authority or "").strip()
        if not self.enabled:
            return False, "merchant_not_configured"
        if isinstance(amount_toman, bool) or amount_toman <= 0:
            return False, "invalid_amount"
        if AUTHORITY_RE.fullmatch(normalized_authority) is None:
            return False, "invalid_authority"
        payload = {
            "merchant_id": self.merchant_id,
            "amount": int(amount_toman),
            "authority": normalized_authority,
        }
        try:
            response = await self._client.post(self.VERIFY_URL, json=payload)
            body = self._json_object(response)
        except httpx.HTTPError as exc:
            logger.warning("Zarinpal verification failed: %s", type(exc).__name__)
            return False, f"network_{type(exc).__name__.lower()}"
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        code = self._integer(data.get("code"))
        if response.status_code == 200 and code in {100, 101}:
            ref_id = str(data.get("ref_id") or "").strip()
            return True, ref_id or normalized_authority
        return False, self._error_code(body, response.status_code, code)

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _integer(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value)
        return None

    @classmethod
    def _error_code(
        cls,
        payload: dict[str, Any],
        http_status: int,
        data_code: int | None,
    ) -> str:
        errors = payload.get("errors")
        if isinstance(errors, dict):
            error_code = cls._integer(errors.get("code"))
            if error_code is not None:
                return str(error_code)
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            error_code = cls._integer(errors[0].get("code"))
            if error_code is not None:
                return str(error_code)
        if data_code is not None:
            return str(data_code)
        return f"http_{http_status}"
