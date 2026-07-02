from __future__ import annotations


APP_VERSION = "4.0.0"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۱"
RELEASE_TITLE = "معماری حرفه‌ای پایش و گزارش تصویری"
RELEASE_NOTES = (
    "افزودن گزارش فالوور آستانه‌ای یا ساعتی با تنظیم مستقل برای هر پیج",
    "افزودن migration خودکار تنظیمات جدید بدون حذف اطلاعات فعلی کاربران",
    "ارسال کارت تصویری مشکی‌طلایی برای تمام رخدادهای پایش",
    "بازنویسی موتور متن دو‌زبانه برای نمایش صحیح فارسی و لاتین",
    "افزودن تست مستقل پنج‌دقیقه‌ای API، WARP و تولید تصویر با هشدار مدیر",
    "تقویت تأیید اکتیوشدن، دی‌اکتیوشدن و جلوگیری از تغییر وضعیت نامطمئن",
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
