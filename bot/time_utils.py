from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
TEHRAN_TZ = ZoneInfo("Asia/Tehran")


def to_persian_digits(value: object) -> str:
    return str(value).translate(PERSIAN_DIGITS)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    j_days_in_month = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]

    gy -= 1600
    gm -= 1
    gd -= 1

    g_day_no = 365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400
    for i in range(gm):
        g_day_no += g_days_in_month[i]
    if gm > 1 and ((gy + 1600) % 4 == 0 and ((gy + 1600) % 100 != 0 or (gy + 1600) % 400 == 0)):
        g_day_no += 1
    g_day_no += gd

    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053

    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461

    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365

    jm = 0
    while jm < 11 and j_day_no >= j_days_in_month[jm]:
        j_day_no -= j_days_in_month[jm]
        jm += 1

    return jy, jm + 1, j_day_no + 1


def format_datetime_dual(value: datetime | None) -> str:
    if value is None:
        return "ثبت نشده"

    utc_value = ensure_aware_utc(value)
    tehran_value = utc_value.astimezone(TEHRAN_TZ)
    jy, jm, jd = gregorian_to_jalali(
        tehran_value.year,
        tehran_value.month,
        tehran_value.day,
    )
    utc_text = utc_value.strftime("%Y/%m/%d - %H:%M UTC")
    tehran_text = f"{jy:04d}/{jm:02d}/{jd:02d} - {tehran_value:%H:%M} تهران"
    return (
        f"میلادی/جهانی: <code>{to_persian_digits(utc_text)}</code>\n"
        f"شمسی/تهران: <code>{to_persian_digits(tehran_text)}</code>"
    )


def format_datetime_dual_plain(value: datetime | None) -> str:
    if value is None:
        return "Not recorded"

    utc_value = ensure_aware_utc(value)
    tehran_value = utc_value.astimezone(TEHRAN_TZ)
    jy, jm, jd = gregorian_to_jalali(
        tehran_value.year,
        tehran_value.month,
        tehran_value.day,
    )
    return (
        f"Gregorian UTC: {utc_value:%Y-%m-%d %H:%M:%S %Z}; "
        f"Jalali Tehran: {jy:04d}/{jm:02d}/{jd:02d} {tehran_value:%H:%M:%S}"
    )
