from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from redis.asyncio import Redis
from sqlalchemy import func, select

from bot.checker import CheckOutcome, InstagramChecker
from bot.config import Settings
from bot.database import SessionFactory
from bot.handlers.user import AddPageState
from bot.keyboards.inline import admin_panel_keyboard, admin_plan_keyboard
from bot.keyboards.reply import cancel_keyboard, main_menu_keyboard
from bot.models import PageStatus, PlanTier, TargetPage, User, UserStatus
from bot.profile_preview import ProfilePreviewService


router = Router(name="admin")
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
PLAN_NAMES = {
    PlanTier.FREE: "رایگان",
    PlanTier.PREMIUM: "پریمیوم",
    PlanTier.VIP: "ویژه",
}


class AdminRenewState(StatesGroup):
    waiting_for_user_id = State()
    choosing_plan = State()
    waiting_for_days = State()


class AdminScheduleState(StatesGroup):
    waiting_for_interval = State()


def _digits(value: object) -> str:
    return str(value).translate(PERSIAN_DIGITS)


async def _reject_message(message: Message, settings: Settings) -> bool:
    if not message.from_user or message.from_user.id != settings.admin_telegram_id:
        await message.answer("شما اجازه دسترسی به این بخش را ندارید.")
        return True
    return False


async def _reject_callback(callback: CallbackQuery, settings: Settings) -> bool:
    if callback.from_user.id != settings.admin_telegram_id:
        await callback.answer("شما اجازه دسترسی به این بخش را ندارید.", show_alert=True)
        return True
    return False


@router.message(
    StateFilter(
        AdminRenewState.waiting_for_user_id,
        AdminRenewState.choosing_plan,
        AdminRenewState.waiting_for_days,
        AdminScheduleState.waiting_for_interval,
    ),
    F.text == "لغو عملیات ↩️",
)
async def cancel_admin_message(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if await _reject_message(message, settings):
        await state.clear()
        return
    await state.clear()
    await message.answer(
        "عملیات لغو شد.",
        reply_markup=main_menu_keyboard(is_admin=True),
    )


@router.message(F.text == "پنل مدیریت 🛡️")
@router.message(Command("admin"))
async def admin_panel(message: Message, settings: Settings, state: FSMContext) -> None:
    if await _reject_message(message, settings):
        return
    await state.clear()
    await message.answer(
        "پنل مدیریت اصلی فارستار وارنر 🛡️\n\nیک گزینه را انتخاب کنید:",
        reply_markup=admin_panel_keyboard(),
    )


@router.callback_query(F.data == "admin:add_target")
async def begin_admin_add_target(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.set_state(AddPageState.waiting_for_username)
    if callback.message:
        await callback.message.answer(
            "نام کاربری یا لینک پیج اینستاگرام را ارسال کنید.\n\n"
            "پیج پس از تأیید تصویری در حساب پایش مدیر ثبت می‌شود.",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "admin:instagram_health")
async def instagram_connection_health(
    callback: CallbackQuery,
    settings: Settings,
    checker: InstagramChecker,
    profile_preview: ProfilePreviewService,
    redis: Redis,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await callback.answer("در حال آزمایش مسیر عمومی اینستاگرام…")
    browser_ok, browser_version = await profile_preview.browser_health()
    result = await checker.fetch_profile("instagram")
    cooldown_ttl = await redis.ttl(checker.STATUS_COOLDOWN_KEY)

    outcome_names = {
        CheckOutcome.ACTIVE: "پاسخ معتبر دریافت شد ✅",
        CheckOutcome.DEACTIVATED: "پاسخ ۴۰۴ دریافت شد",
        CheckOutcome.RATE_LIMITED: "محدودیت درخواست فعال است ⚠️",
        CheckOutcome.UNKNOWN: "پاسخ قطعی دریافت نشد ⚠️",
    }
    browser_text = (
        f"فعال ✅ — نسخه {browser_version or 'نامشخص'}"
        if browser_ok
        else "غیرفعال یا در دسترس نیست ❌"
    )
    cooldown_text = (
        f"فعال — {_digits(cooldown_ttl)} ثانیه باقی‌مانده"
        if cooldown_ttl > 0
        else "غیرفعال ✅"
    )
    http_text = _digits(result.http_status) if result.http_status else "ثبت نشد"
    text = (
        "وضعیت اتصال اینستاگرام 🌐\n\n"
        "حالت دسترسی: <b>نمای عمومی بدون ورود</b>\n"
        "وضعیت ورود: <b>وارد نشده</b>\n"
        "ذخیره رمز یا کوکی: <b>غیرفعال</b>\n"
        f"Chromium: <b>{browser_text}</b>\n"
        f"نتیجه تست: <b>{outcome_names[result.outcome]}</b>\n"
        f"کد HTTP: <code>{http_text}</code>\n"
        f"توقف موقت چکر: <b>{cooldown_text}</b>\n\n"
        "پاسخ نامشخص هیچ‌گاه وضعیت پیج را به دی‌اکتیو تغییر نمی‌دهد."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())


@router.callback_query(F.data == "admin:stats")
async def system_stats(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        total_users = await session.scalar(select(func.count()).select_from(User))
        active_users = await session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.status == UserStatus.ACTIVE)
        )
        valid_subscriptions = await session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.status == UserStatus.ACTIVE,
                User.subscription_expiry > now,
            )
        )
        total_pages = await session.scalar(select(func.count()).select_from(TargetPage))
        active_pages = await session.scalar(
            select(func.count())
            .select_from(TargetPage)
            .where(TargetPage.last_known_status == PageStatus.ACTIVE)
        )
        deactivated_pages = await session.scalar(
            select(func.count())
            .select_from(TargetPage)
            .where(TargetPage.last_known_status == PageStatus.DEACTIVATED)
        )

    text = (
        "آمار سیستم 📈\n\n"
        f"کل کاربران: {_digits(total_users or 0)}\n"
        f"کاربران فعال: {_digits(active_users or 0)}\n"
        f"اشتراک‌های معتبر: {_digits(valid_subscriptions or 0)}\n\n"
        f"کل پیج‌ها: {_digits(total_pages or 0)}\n"
        f"پیج‌های فعال: {_digits(active_pages or 0)}\n"
        f"پیج‌های دی‌اکتیو: {_digits(deactivated_pages or 0)}"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:renew")
async def begin_renew_subscription(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.set_state(AdminRenewState.waiting_for_user_id)
    if callback.message:
        await callback.message.answer(
            "شناسه عددی تلگرام کاربر را ارسال کنید:",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(AdminRenewState.waiting_for_user_id)
async def receive_renew_user_id(
    message: Message,
    state: FSMContext,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_message(message, settings):
        await state.clear()
        return
    try:
        user_id = int((message.text or "").strip())
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("شناسه واردشده معتبر نیست. یک شناسه عددی مثبت ارسال کنید.")
        return

    async with session_factory() as session:
        user = await session.get(User, user_id)
    if user is None:
        await message.answer("کاربری با این شناسه پیدا نشد. دوباره تلاش کنید.")
        return

    await state.update_data(renew_user_id=user_id)
    await state.set_state(AdminRenewState.choosing_plan)
    await message.answer(
        "پلن جدید کاربر را انتخاب کنید:",
        reply_markup=admin_plan_keyboard(),
    )


@router.callback_query(AdminRenewState.choosing_plan, F.data.startswith("admin:plan:"))
async def choose_renew_plan(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if await _reject_callback(callback, settings):
        await state.clear()
        return
    plan_value = (callback.data or "").rsplit(":", 1)[1]
    try:
        plan = PlanTier(plan_value)
    except ValueError:
        await callback.answer("پلن انتخاب‌شده معتبر نیست.", show_alert=True)
        return
    await state.update_data(renew_plan=plan.value)
    await state.set_state(AdminRenewState.waiting_for_days)
    if callback.message:
        await callback.message.answer(
            "تعداد روزهای تمدید را وارد کنید؛ عددی بین ۱ تا ۳۶۵۰:",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(AdminRenewState.waiting_for_days)
async def finish_renew_subscription(
    message: Message,
    state: FSMContext,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_message(message, settings):
        await state.clear()
        return
    try:
        days = int((message.text or "").strip())
        if not 1 <= days <= 3650:
            raise ValueError
    except ValueError:
        await message.answer("تعداد روز معتبر نیست. عددی بین ۱ تا ۳۶۵۰ ارسال کنید.")
        return

    data = await state.get_data()
    user_id = int(data["renew_user_id"])
    plan = PlanTier(data["renew_plan"])
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(User.telegram_id == user_id).with_for_update()
        )
        if user is None:
            await state.clear()
            await message.answer("کاربر در زمان انجام عملیات حذف شده است.")
            return
        base_date = max(now, user.subscription_expiry)
        user.subscription_expiry = base_date + timedelta(days=days)
        user.plan_tier = plan
        user.status = UserStatus.ACTIVE
        new_expiry = user.subscription_expiry
        await session.commit()

    await state.clear()
    expiry_text = _digits(
        new_expiry.astimezone(timezone.utc).strftime("%Y/%m/%d - %H:%M")
    )
    await message.answer(
        "اشتراک کاربر با موفقیت تمدید شد. ✅\n\n"
        f"شناسه کاربر: <code>{_digits(user_id)}</code>\n"
        f"پلن: {PLAN_NAMES[plan]}\n"
        f"اعتبار جدید: {expiry_text} به وقت جهانی",
        reply_markup=main_menu_keyboard(is_admin=True),
    )


@router.callback_query(F.data == "admin:schedule")
async def begin_schedule_change(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    redis: Redis,
) -> None:
    if await _reject_callback(callback, settings):
        return
    stored_interval = await redis.get("farstar:checker:interval")
    current_interval = int(stored_interval or settings.check_interval_seconds)
    await state.set_state(AdminScheduleState.waiting_for_interval)
    if callback.message:
        await callback.message.answer(
            "فاصله فعلی بررسی‌ها "
            f"{_digits(current_interval)} ثانیه است.\n\n"
            "فاصله جدید را به ثانیه و بین ۳۰ تا ۸۶۴۰۰ ارسال کنید:",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "admin:check_now")
async def run_checker_now(
    callback: CallbackQuery,
    settings: Settings,
    scheduler: AsyncIOScheduler,
) -> None:
    if await _reject_callback(callback, settings):
        return
    scheduler.modify_job(
        "instagram-checker",
        next_run_time=datetime.now(timezone.utc),
    )
    await callback.answer("بررسی فوری در صف اجرا قرار گرفت. ✅", show_alert=True)


@router.message(AdminScheduleState.waiting_for_interval)
async def finish_schedule_change(
    message: Message,
    state: FSMContext,
    settings: Settings,
    redis: Redis,
    scheduler: AsyncIOScheduler,
) -> None:
    if await _reject_message(message, settings):
        await state.clear()
        return
    try:
        interval = int((message.text or "").strip())
        if not 30 <= interval <= 86400:
            raise ValueError
    except ValueError:
        await message.answer("مقدار معتبر نیست. عددی بین ۳۰ تا ۸۶۴۰۰ ارسال کنید.")
        return

    scheduler.reschedule_job(
        "instagram-checker",
        trigger=IntervalTrigger(seconds=interval, timezone="UTC"),
    )
    await redis.set("farstar:checker:interval", str(interval))
    await state.clear()
    await message.answer(
        f"فاصله بررسی‌ها روی {_digits(interval)} ثانیه تنظیم شد. ✅",
        reply_markup=main_menu_keyboard(is_admin=True),
    )


@router.callback_query(F.data == "admin:cancel")
async def cancel_admin_operation(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "عملیات لغو شد. پنل مدیریت:",
            reply_markup=admin_panel_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:"))
async def reject_unknown_admin_callback(
    callback: CallbackQuery, settings: Settings
) -> None:
    if await _reject_callback(callback, settings):
        return
    await callback.answer("این گزینه دیگر معتبر نیست.", show_alert=True)
