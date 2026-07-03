from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import create_engine, inspect

from bot import money
from bot.models import Base, PaymentInvoice
from bot.money import convert_usd_to_toman, fetch_live_usd_rate
from bot.payment_service import ZarinpalProvider


class FakeRedis:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.ttl: int | None = None

    async def get(self, _: str) -> str | None:
        return self.value

    async def set(self, _: str, value: str, *, ex: int) -> None:
        self.value = value
        self.ttl = ex


def test_convert_usd_to_toman_rounds_half_up() -> None:
    assert convert_usd_to_toman(1.25, 100_001) == 125_001
    with pytest.raises(ValueError):
        convert_usd_to_toman(float("nan"), 100_000)


@pytest.mark.asyncio
async def test_live_rate_parses_tgju_rial_and_caches_toman(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = '<span data-col="info.last_trade.PDrCotVal">1,753,950</span>'

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(money.httpx, "AsyncClient", lambda **_: client)
    redis = FakeRedis()

    rate = await fetch_live_usd_rate(redis)  # type: ignore[arg-type]

    assert rate == 175_395
    assert redis.value == "175395"
    assert redis.ttl == 7200


@pytest.mark.asyncio
async def test_zarinpal_request_and_verify_use_irt_and_are_idempotent_codes() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if request.url.path.endswith("request.json"):
            assert payload["currency"] == "IRT"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "code": 100,
                        "authority": "A000000000000000000000000000000000001",
                    },
                    "errors": [],
                },
            )
        return httpx.Response(
            200,
            json={"data": {"code": 101, "ref_id": 987654}, "errors": []},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ZarinpalProvider("merchant-id", client=client)
    try:
        authority, payment_url = await provider.request_payment(
            250_000,
            "خرید آزمایشی",
            "https://example.com/payment/callback",
        )
        verified, ref_id = await provider.verify_payment(250_000, authority or "")
    finally:
        await client.aclose()

    assert authority == "A000000000000000000000000000000000001"
    assert payment_url == (
        "https://www.zarinpal.com/pg/StartPay/A000000000000000000000000000000000001"
    )
    assert verified is True
    assert ref_id == "987654"
    assert requests[1]["amount"] == 250_000


def test_payment_invoice_schema_has_unique_authority_and_paid_flag() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    columns = {
        column["name"]
        for column in inspect(engine).get_columns(PaymentInvoice.__tablename__)
    }
    unique_columns = {
        column
        for constraint in inspect(engine).get_unique_constraints(
            PaymentInvoice.__tablename__
        )
        for column in constraint["column_names"]
    }

    assert {"zarinpal_authority", "is_paid", "amount_toman"} <= columns
    assert "zarinpal_authority" in unique_columns
