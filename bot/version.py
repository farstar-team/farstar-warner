from __future__ import annotations


APP_VERSION = "3.1.1"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۱"
RELEASE_TITLE = "رفع خطای راه‌اندازی کانتینر WARP"
RELEASE_NOTES = (
    "حذف no-new-privileges ناسازگار با entrypoint و sudo داخلی تصویر WARP",
    "حذف forward اشتباه GOST به پورت تنظیم‌نشده ۴۰۰۰۰",
    "استفاده از پراکسی پیش‌فرض و خودکار WARP روی پورت داخلی ۱۰۸۰",
    "افزایش مهلت health-check برای ثبت اولیه حساب و اتصال daemon",
    "جلوگیری از متوقف‌شدن کل ربات هنگام تأخیر یا اختلال کانتینر WARP",
)
RELEASE_REDIS_KEY = "farstar:release:notified-version"


def version_message(*, activated: bool = False) -> str:
    heading = "نسخه جدید ربات فعال شد! ✅" if activated else "نسخه فعال ربات ℹ️"
    notes = "\n".join(f"• {note}" for note in RELEASE_NOTES)
    return (
        f"{heading}\n\n"
        f"نسخه: <code>{APP_VERSION}</code>\n"
        f"عنوان انتشار: <b>{RELEASE_TITLE}</b>\n"
        f"تاریخ انتشار: {RELEASE_DATE}\n\n"
        f"تغییرات این نسخه:\n{notes}"
    )
