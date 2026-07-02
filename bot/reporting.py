from __future__ import annotations


ADMIN_REPORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("ACTIVATED", "فعال‌شدن پیج"),
    ("DEACTIVATED", "غیرفعال‌شدن پیج"),
    ("VERIFICATION", "دریافت یا حذف تیک آبی"),
    ("FOLLOWERS", "تغییرات فالوورها"),
    ("CONTENT", "تغییر تعداد پست‌ها"),
    ("PROFILE", "تغییر پروفایل، نام و بیو"),
    ("IDENTITY", "تغییر نام کاربری یا هویت"),
    ("CRITICAL", "هشدار هویتی بحرانی"),
)

ADMIN_REPORT_KEYS = frozenset(key for key, _ in ADMIN_REPORT_OPTIONS)


def parse_admin_report_categories(value: str | None) -> set[str]:
    return {item for item in (value or "").split(",") if item in ADMIN_REPORT_KEYS}


def serialize_admin_report_categories(categories: set[str]) -> str:
    return ",".join(key for key, _ in ADMIN_REPORT_OPTIONS if key in categories)
