from __future__ import annotations


PERSIAN_DIGITS = str.maketrans("0123456789,", "۰۱۲۳۴۵۶۷۸۹٬")
SUPPORTED_CURRENCIES = {"TOMAN", "USD"}


def normalize_currency(value: str | None) -> str:
    normalized = (value or "TOMAN").upper()
    return normalized if normalized in SUPPORTED_CURRENCIES else "TOMAN"


def currency_name(value: str | None) -> str:
    return "دلار" if normalize_currency(value) == "USD" else "تومان"


def format_money(amount: int | None, currency: str | None) -> str:
    safe_amount = max(0, int(amount or 0))
    number = f"{safe_amount:,}".translate(PERSIAN_DIGITS)
    return f"{number} {currency_name(currency)}"
