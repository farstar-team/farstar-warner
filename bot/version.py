from __future__ import annotations


APP_VERSION = "3.1.0"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۱"
RELEASE_TITLE = "معماری پایدار WARP و اعلان تغییرات پیج"
RELEASE_NOTES = (
    "افزودن پراکسی داخلی WARP با health-check و وابستگی سلامت Docker",
    "افزودن preflight قبل از هر چرخه و توقف امن هنگام پاسخ ۴۰۳ یا ۴۲۹",
    "افزودن هشدار زیرساختی مدیر پس از سه چرخه شکست متوالی مسیر WARP",
    "افزودن فاصله تصادفی ۱۵ تا ۴۵ ثانیه و retry با backoff تصاعدی",
    "افزودن اعلان تغییر فالوور، تعداد پست، نام اصلی و بیوگرافی پیج",
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
