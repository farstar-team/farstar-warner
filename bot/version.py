from __future__ import annotations


APP_VERSION = "5.0.0"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۲"
RELEASE_TITLE = "موتور اجماع چندمنبعی و شواهد قطعی وضعیت پیج"
RELEASE_NOTES = (
    "افزودن baseline سه‌گانه @instagram، @cristiano و @nasa پیش از اهداف",
    "تشخیص با اجماع Meta رسمی، Web Profile، GraphQL و Playwright عمومی",
    "جلوگیری از بازشدن مدار محافظ پیش از تکمیل fallbackهای مستقل",
    "ثبت منبع و زمان آخرین شاهد قطعی برای هر پیج در PostgreSQL",
    "تأیید دوباره غیرفعال‌شدن با فاصله زمانی برای حذف هشدار کاذب",
    "بازنویسی worker queue بدون sentinel و با join/cancel کنترل‌شده",
    "نرمال‌سازی نام و بیو برای حذف اعلان‌های کاذب None و رشته خالی",
    "حذف سقف polling و بازنویسی سبک سرویس پیش‌نمایش مرورگر",
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
