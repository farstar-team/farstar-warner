from __future__ import annotations


APP_VERSION = "5.0.0"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۲"
RELEASE_TITLE = "موتور اجماع چندمنبعی و شواهد قطعی وضعیت پیج"
RELEASE_NOTES = (
    "افزودن جست‌وجوی مستقل GraphQL بر اساس معماری فعال Instaloader",
    "جلوگیری از بازشدن مدار محافظ پیش از آزمایش منبع مستقل نام کاربری",
    "تشخیص فعال یا غیرفعال با اجماع Web Profile، جست‌وجو و Meta رسمی",
    "ثبت منبع و زمان آخرین شاهد قطعی برای هر پیج در PostgreSQL",
    "تأیید دوباره غیرفعال‌شدن با فاصله زمانی برای حذف هشدار کاذب",
    "حفظ اطلاعات قبلی هنگام دریافت پاسخ فعال اما ناقص از جست‌وجو",
    "افزودن تست مستقیم GraphQL روی WARP و مسیر مستقیم به farstar doctor",
    "افزودن تست یکپارچه اعلان انتقال فعال به غیرفعال",
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
