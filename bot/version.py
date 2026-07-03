from __future__ import annotations


APP_VERSION = "5.0.1"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۲"
RELEASE_TITLE = "موتور اجماع مقاوم برای پیج‌های عمومی و خصوصی"
RELEASE_NOTES = (
    "افزودن baseline قابل‌تنظیم با اولویت پیج عمومی کم‌ترافیک",
    "افزودن HTTP Embed خام پیش از fallback کنترل‌شده Playwright",
    "پشتیبانی از آمار عمومی پیج خصوصی بدون مقایسه بیوگرافی پنهان",
    "محدودکردن delta خصوصی به نام کاربری، فالوور، فالووینگ و پست",
    "تشخیص با اجماع Meta رسمی، Web Profile، GraphQL و Embed عمومی",
    "جلوگیری از بازشدن مدار محافظ پیش از تکمیل fallbackهای مستقل",
    "بازنویسی worker queue بدون sentinel و با join/cancel کنترل‌شده",
    "نرمال‌سازی نام و بیو برای حذف اعلان‌های کاذب None و رشته خالی",
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
