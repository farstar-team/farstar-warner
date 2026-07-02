from __future__ import annotations


APP_VERSION = "4.4.0"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۱"
RELEASE_TITLE = "مدار محافظ اینستاگرام و بازیابی خودکار WARP"
RELEASE_NOTES = (
    "تشخیص HTTP 401 همراه Please wait به‌عنوان رد موقت دسترسی اینستاگرام",
    "توقف فوری retry storm و فعال‌شدن circuit breaker پانزده‌دقیقه‌ای",
    "تفکیک کامل سلامت Cloudflare WARP از پاسخ Web Profile اینستاگرام",
    "افزودن supervisor برای connect مجدد تونل ثبت‌شده هنگام warp=off",
    "تلاش حداکثر یک‌باره روی WARP و مسیر مستقیم پیش از بازشدن مدار محافظ",
    "حفظ وضعیت تمام پیج‌ها هنگام رد دسترسی بدون تولید هشدار اشتباه",
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
