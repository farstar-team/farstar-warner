from __future__ import annotations


APP_VERSION = "3.0.3"
RELEASE_DATE = "۱۴۰۵/۰۴/۱۰"
RELEASE_TITLE = "اصلاح خطای ۴۰۱ نمایش زنده پیج"
RELEASE_NOTES = (
    "افزودن درخواست HTTP/2 با هدرهای دقیق Web Profile API اینستاگرام",
    "افزودن fallback چندمرحله‌ای: httpx/http2، سپس curl --http2، سپس curl دقیق سرور",
    "استخراج امن داده فقط از data.user برای جلوگیری از اشتباه با اطلاعات پست‌ها",
    "جلوگیری از تغییر وضعیت پیج هنگام پاسخ ۴۰۱ یا پاسخ نامعتبر",
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
