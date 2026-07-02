from __future__ import annotations


APP_VERSION = "4.2.0"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۱"
RELEASE_TITLE = "پایش قطعی وضعیت و مدیریت حرفه‌ای کاربران"
RELEASE_NOTES = (
    "حذف کامل قابلیت قرارداد و هر پیام پرداخت پس از فعال‌شدن پیج",
    "افزودن انتخاب مستقل نوع رونوشت گزارش برای هر کاربر بدون اطلاع او",
    "افزودن پرونده کامل مدیریت کاربران، خریدها، فیش‌ها و پیج‌های ثبت‌شده",
    "بررسی اجباری پیج مرجع @instagram پیش از تغییر وضعیت هر چرخه",
    "تأیید دو مرحله‌ای دی‌اکتیوشدن در همان چرخه و اعلان بدون انتظار چرخه بعد",
    "فعال‌کردن workerهای هم‌زمان و failover مستقیم هنگام اختلال WARP",
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
