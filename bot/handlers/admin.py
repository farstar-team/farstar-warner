from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from bot.checker import CheckOutcome, InstagramChecker
from bot.config import Settings
from bot.database import SessionFactory
from bot.diagnostics import DiagnosticEntry
from bot.handlers.user import AddPageState
from bot.keyboards.inline import (
    admin_channels_keyboard,
    admin_discounts_keyboard,
    admin_diagnostics_keyboard,
    admin_panel_keyboard,
    admin_plan_keyboard,
    admin_plans_keyboard,
    admin_report_copy_keyboard,
    admin_store_keyboard,
    admin_user_detail_keyboard,
    admin_user_section_keyboard,
    admin_user_subscription_keyboard,
    admin_users_keyboard,
    currency_selection_keyboard,
    payment_config_keyboard,
    receipt_review_keyboard,
)
from bot.keyboards.reply import cancel_keyboard, main_menu_keyboard
from bot.models import (
    DiscountCode,
    PageEvent,
    PageStatus,
    PaymentConfig,
    PaymentReceipt,
    ReceiptStatus,
    ReceiptDiscount,
    RequiredChannel,
    SubscriptionPlan,
    StoreConfig,
    StoreProduct,
    TargetPage,
    User,
    UserStatus,
    UserSubscription,
    PlanTier,
)
from bot.money import format_money, normalize_currency
from bot.profile_preview import PreviewOutcome, ProfilePreviewService
from bot.reporting import (
    ADMIN_REPORT_KEYS,
    parse_admin_report_categories,
    serialize_admin_report_categories,
)
from bot.time_utils import format_datetime_dual
from bot.version import APP_VERSION


router = Router(name="admin")
logger = logging.getLogger(__name__)
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
PLAN_NAMES = {
    PlanTier.FREE: "رایگان",
    PlanTier.PREMIUM: "پریمیوم",
    PlanTier.VIP: "ویژه",
}
DIAGNOSTIC_LEVELS = {
    "INFO": "اطلاعات",
    "WARNING": "هشدار",
    "ERROR": "خطا",
    "CRITICAL": "بحرانی",
}


def _diagnostic_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return format_datetime_dual(parsed).replace("<code>", "").replace("</code>", "")


def _diagnostic_text(entry: DiagnosticEntry, *, compact: bool) -> str:
    status = str(entry.http_status) if entry.http_status is not None else "—"
    fields = [
        f"سطح: {DIAGNOSTIC_LEVELS.get(entry.level, entry.level)}",
        f"رخداد: {entry.event}",
        f"توضیح: {entry.message}",
        f"زمان: {_diagnostic_time(entry.timestamp)}",
    ]
    if entry.trace_id:
        fields.append(f"شناسه رهگیری: {entry.trace_id}")
    if entry.username:
        fields.append(f"پیج: @{entry.username}")
    if entry.transport:
        fields.append(f"مسیر اتصال: {entry.transport}")
    if entry.http_status is not None or entry.return_code is not None:
        fields.append(f"HTTP: {status} | کد اجرا: {entry.return_code or 0}")
    if entry.elapsed_ms is not None:
        fields.append(f"مدت پاسخ: {entry.elapsed_ms}ms")
    if entry.response_bytes is not None:
        fields.append(f"حجم پاسخ: {entry.response_bytes} bytes")
    if entry.detail:
        detail = entry.detail[:180] if compact else entry.detail
        fields.append(f"علت فنی: {detail}")
    return "\n".join(fields)


class AdminRenewState(StatesGroup):
    waiting_for_user_id = State()
    choosing_plan = State()
    waiting_for_days = State()


class AdminScheduleState(StatesGroup):
    waiting_for_interval = State()


class AdminChannelState(StatesGroup):
    waiting_for_identifier = State()
    waiting_for_title = State()
    waiting_for_url = State()


class AdminPlanEditorState(StatesGroup):
    waiting_for_name = State()
    waiting_for_days = State()
    waiting_for_price = State()
    waiting_for_limit = State()


class AdminPaymentState(StatesGroup):
    waiting_for_support = State()
    waiting_for_card_number = State()
    waiting_for_card_holder = State()


class AdminDiscountState(StatesGroup):
    waiting_for_code = State()
    waiting_for_percent = State()
    waiting_for_max_uses = State()
    waiting_for_expiry_days = State()


class AdminStoreState(StatesGroup):
    waiting_for_name = State()
    waiting_for_description = State()
    choosing_currency = State()
    waiting_for_price = State()
    waiting_for_url = State()


class AdminReportCopyState(StatesGroup):
    waiting_for_user_id = State()


class AdminUserState(StatesGroup):
    waiting_for_user_id = State()


def _digits(value: object) -> str:
    return str(value).translate(PERSIAN_DIGITS)


def _parse_integer(value: str | None) -> int:
    normalized = (value or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٬,", "0123456789  "))
    return int(normalized.replace(" ", "").strip())


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
        AdminChannelState.waiting_for_identifier,
        AdminChannelState.waiting_for_title,
        AdminChannelState.waiting_for_url,
        AdminPlanEditorState.waiting_for_name,
        AdminPlanEditorState.waiting_for_days,
        AdminPlanEditorState.waiting_for_price,
        AdminPlanEditorState.waiting_for_limit,
        AdminPaymentState.waiting_for_support,
        AdminPaymentState.waiting_for_card_number,
        AdminPaymentState.waiting_for_card_holder,
        AdminDiscountState.waiting_for_code,
        AdminDiscountState.waiting_for_percent,
        AdminDiscountState.waiting_for_max_uses,
        AdminDiscountState.waiting_for_expiry_days,
        AdminStoreState.waiting_for_name,
        AdminStoreState.waiting_for_description,
        AdminStoreState.choosing_currency,
        AdminStoreState.waiting_for_price,
        AdminStoreState.waiting_for_url,
        AdminReportCopyState.waiting_for_user_id,
        AdminUserState.waiting_for_user_id,
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


@router.callback_query(F.data == "admin:home")
async def admin_home(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "پنل مدیریت اصلی فارستار وارنر 🛡️\n\nیک گزینه را انتخاب کنید:",
            reply_markup=admin_panel_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "admin:channels")
async def admin_channels(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    async with session_factory() as session:
        channels = list(
            await session.scalars(select(RequiredChannel).order_by(RequiredChannel.id))
        )
    lines = ["کانال‌های عضویت اجباری 📢", ""]
    if channels:
        lines.extend(
            f"• {channel.title} — <code>{channel.chat_identifier}</code>"
            for channel in channels
        )
    else:
        lines.append("هنوز کانالی ثبت نشده است.")
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=admin_channels_keyboard(channels),
        )
    await callback.answer()


@router.callback_query(F.data == "admin:channel:add")
async def begin_channel_add(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.set_state(AdminChannelState.waiting_for_identifier)
    if callback.message:
        await callback.message.answer(
            "شناسه کانال را ارسال کنید. برای کانال عمومی <code>@channel</code> "
            "و برای کانال خصوصی شناسه عددی مانند <code>-1001234567890</code> بفرستید.\n\n"
            "ربات باید در کانال مدیر باشد.",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(AdminChannelState.waiting_for_identifier)
async def receive_channel_identifier(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if await _reject_message(message, settings):
        return
    identifier = (message.text or "").strip()
    valid = (
        identifier.startswith("@")
        and 2 <= len(identifier) <= 100
        and identifier[1:].replace("_", "a").isalnum()
    ) or (identifier.startswith("-100") and identifier[1:].isdigit())
    if not valid:
        await message.answer("شناسه کانال معتبر نیست؛ دوباره ارسال کنید.")
        return
    await state.update_data(channel_identifier=identifier)
    await state.set_state(AdminChannelState.waiting_for_title)
    await message.answer(
        "عنوانی که کاربر ببیند را ارسال کنید؛ مانند «کانال رسمی فارستار»."
    )


@router.message(AdminChannelState.waiting_for_title)
async def receive_channel_title(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if await _reject_message(message, settings):
        return
    title = (message.text or "").strip()
    if not 2 <= len(title) <= 100:
        await message.answer("عنوان باید بین ۲ تا ۱۰۰ نویسه باشد.")
        return
    await state.update_data(channel_title=title)
    await state.set_state(AdminChannelState.waiting_for_url)
    await message.answer(
        "لینک عضویت کانال را ارسال کنید؛ مانند <code>https://t.me/channel</code> "
        "یا لینک دعوت خصوصی."
    )


@router.message(AdminChannelState.waiting_for_url)
async def finish_channel_add(
    message: Message,
    state: FSMContext,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_message(message, settings):
        return
    join_url = (message.text or "").strip()
    if not join_url.startswith("https://t.me/") or len(join_url) > 500:
        await message.answer("لینک باید با https://t.me/ شروع شود.")
        return
    data = await state.get_data()
    try:
        async with session_factory() as session:
            session.add(
                RequiredChannel(
                    chat_identifier=data["channel_identifier"],
                    title=data["channel_title"],
                    join_url=join_url,
                )
            )
            await session.commit()
    except IntegrityError:
        await message.answer("این کانال قبلاً ثبت شده است.")
        return
    await state.clear()
    await message.answer(
        "کانال اجباری ثبت شد. ✅",
        reply_markup=main_menu_keyboard(is_admin=True),
    )


@router.callback_query(F.data.startswith("admin:channel:delete:"))
async def delete_required_channel(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    channel_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        channel = await session.get(RequiredChannel, channel_id)
        if channel is not None:
            await session.delete(channel)
            await session.commit()
    await callback.answer("کانال حذف شد. ✅", show_alert=True)
    if callback.message:
        await callback.message.edit_text(
            "کانال حذف شد. برای مشاهده فهرست به پنل بازگردید.",
            reply_markup=admin_panel_keyboard(),
        )


async def _show_admin_plans(
    callback: CallbackQuery,
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        plans = list(
            await session.scalars(
                select(SubscriptionPlan).order_by(
                    SubscriptionPlan.price, SubscriptionPlan.id
                )
            )
        )
    lines = ["مدیریت پلن‌های اشتراک 💎", ""]
    if plans:
        for plan in plans:
            state_text = "فعال" if plan.is_active else "غیرفعال"
            lines.append(
                f"• <b>{plan.name}</b> — {_digits(plan.duration_days)} روز — "
                f"{_digits(f'{plan.price:,}')} تومان — {_digits(plan.target_limit)} پیج — {state_text}"
            )
    else:
        lines.append("هنوز پلنی تعریف نشده است.")
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=admin_plans_keyboard(plans),
        )


@router.callback_query(F.data == "admin:plans")
async def admin_plans(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await _show_admin_plans(callback, session_factory)
    await callback.answer()


@router.callback_query(F.data == "admin:planadd")
async def begin_plan_add(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.clear()
    await state.update_data(plan_edit_id=None)
    await state.set_state(AdminPlanEditorState.waiting_for_name)
    if callback.message:
        await callback.message.answer(
            "نام پلن را ارسال کنید؛ مانند «پریمیوم یک‌ماهه».",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:planedit:"))
async def begin_plan_edit(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    plan_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        plan = await session.get(SubscriptionPlan, plan_id)
    if plan is None:
        await callback.answer("پلن پیدا نشد.", show_alert=True)
        return
    await state.clear()
    await state.update_data(plan_edit_id=plan.id)
    await state.set_state(AdminPlanEditorState.waiting_for_name)
    if callback.message:
        await callback.message.answer(
            f"ویرایش پلن <b>{plan.name}</b>\n\nنام جدید پلن را ارسال کنید:",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(AdminPlanEditorState.waiting_for_name)
async def receive_plan_name(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if await _reject_message(message, settings):
        return
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 80:
        await message.answer("نام پلن باید بین ۲ تا ۸۰ نویسه باشد.")
        return
    await state.update_data(plan_name=name)
    await state.set_state(AdminPlanEditorState.waiting_for_days)
    await message.answer("مدت پلن را به روز وارد کنید؛ عددی بین ۱ تا ۳۶۵۰.")


@router.message(AdminPlanEditorState.waiting_for_days)
async def receive_plan_days(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if await _reject_message(message, settings):
        return
    try:
        days = int((message.text or "").strip())
        if not 1 <= days <= 3650:
            raise ValueError
    except ValueError:
        await message.answer("تعداد روز باید عددی بین ۱ تا ۳۶۵۰ باشد.")
        return
    await state.update_data(plan_days=days)
    await state.set_state(AdminPlanEditorState.waiting_for_price)
    await message.answer("قیمت پلن را به تومان و فقط به‌صورت عدد وارد کنید.")


@router.message(AdminPlanEditorState.waiting_for_price)
async def receive_plan_price(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if await _reject_message(message, settings):
        return
    try:
        price = int((message.text or "").replace(",", "").strip())
        if not 0 <= price <= 10_000_000_000:
            raise ValueError
    except ValueError:
        await message.answer("قیمت معتبر نیست؛ فقط عدد تومان را وارد کنید.")
        return
    await state.update_data(plan_price=price)
    await state.set_state(AdminPlanEditorState.waiting_for_limit)
    await message.answer(
        "حداکثر تعداد پیج این پلن را وارد کنید؛ برای Premium عدد ۱۰۰ پیشنهاد می‌شود."
    )


@router.message(AdminPlanEditorState.waiting_for_limit)
async def finish_plan_editor(
    message: Message,
    state: FSMContext,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_message(message, settings):
        return
    try:
        target_limit = int((message.text or "").strip())
        if not 1 <= target_limit <= 10000:
            raise ValueError
    except ValueError:
        await message.answer("ظرفیت باید عددی بین ۱ تا ۱۰۰۰۰ باشد.")
        return
    data = await state.get_data()
    try:
        async with session_factory() as session:
            plan_id = data.get("plan_edit_id")
            plan = await session.get(SubscriptionPlan, plan_id) if plan_id else None
            if plan is None:
                plan = SubscriptionPlan(name=data["plan_name"])
                session.add(plan)
            plan.name = data["plan_name"]
            plan.duration_days = data["plan_days"]
            plan.price = data["plan_price"]
            plan.target_limit = target_limit
            plan.is_active = True
            await session.commit()
    except IntegrityError:
        await message.answer("پلنی با این نام از قبل وجود دارد.")
        return
    await state.clear()
    await message.answer(
        "پلن با موفقیت ذخیره شد. ✅",
        reply_markup=main_menu_keyboard(is_admin=True),
    )


@router.callback_query(F.data.startswith("admin:plandelete:"))
async def delete_plan(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    plan_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        plan = await session.get(SubscriptionPlan, plan_id)
        if plan is not None:
            await session.delete(plan)
            await session.commit()
    await callback.answer("پلن حذف شد. ✅", show_alert=True)
    if callback.message:
        await callback.message.edit_text(
            "پلن حذف شد.", reply_markup=admin_panel_keyboard()
        )


@router.callback_query(F.data == "admin:payment")
async def admin_payment_config(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    async with session_factory() as session:
        payment = await session.get(PaymentConfig, 1)
    support = (
        html.escape(payment.support_username)
        if payment and payment.support_username
        else "ثبت نشده"
    )
    card = (
        html.escape(payment.card_number)
        if payment and payment.card_number
        else "ثبت نشده"
    )
    holder = (
        html.escape(payment.card_holder)
        if payment and payment.card_holder
        else "ثبت نشده"
    )
    if callback.message:
        await callback.message.edit_text(
            "تنظیمات پرداخت 💳\n\n"
            f"آیدی پشتیبانی: <b>{support}</b>\n"
            f"شماره کارت: <code>{card}</code>\n"
            f"نام صاحب کارت: <b>{holder}</b>",
            reply_markup=payment_config_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "admin:payment:support")
async def begin_support_edit(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.set_state(AdminPaymentState.waiting_for_support)
    if callback.message:
        await callback.message.answer(
            "آیدی پشتیبانی را با @ ارسال کنید.", reply_markup=cancel_keyboard()
        )
    await callback.answer()


@router.message(AdminPaymentState.waiting_for_support)
async def finish_support_edit(
    message: Message,
    state: FSMContext,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_message(message, settings):
        return
    username = (message.text or "").strip()
    if (
        not username.startswith("@")
        or not 2 <= len(username) <= 64
        or not username[1:].replace("_", "a").isalnum()
    ):
        await message.answer("آیدی معتبر نیست؛ نمونه: @support")
        return
    async with session_factory() as session:
        payment = await session.get(PaymentConfig, 1)
        if payment is None:
            payment = PaymentConfig(id=1)
            session.add(payment)
        payment.support_username = username
        await session.commit()
    await state.clear()
    await message.answer(
        "آیدی پشتیبانی ذخیره شد. ✅", reply_markup=main_menu_keyboard(is_admin=True)
    )


@router.callback_query(F.data == "admin:payment:card")
async def begin_card_edit(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.set_state(AdminPaymentState.waiting_for_card_number)
    if callback.message:
        await callback.message.answer(
            "شماره کارت را فقط به‌صورت عدد ارسال کنید.", reply_markup=cancel_keyboard()
        )
    await callback.answer()


@router.message(AdminPaymentState.waiting_for_card_number)
async def receive_card_number(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    if await _reject_message(message, settings):
        return
    card = (message.text or "").replace(" ", "").replace("-", "")
    if not card.isdigit() or not 16 <= len(card) <= 19:
        await message.answer("شماره کارت باید ۱۶ تا ۱۹ رقم باشد.")
        return
    await state.update_data(payment_card_number=card)
    await state.set_state(AdminPaymentState.waiting_for_card_holder)
    await message.answer("نام و نام خانوادگی صاحب کارت را ارسال کنید.")


@router.message(AdminPaymentState.waiting_for_card_holder)
async def finish_card_edit(
    message: Message,
    state: FSMContext,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_message(message, settings):
        return
    holder = (message.text or "").strip()
    if not 2 <= len(holder) <= 100:
        await message.answer("نام صاحب کارت معتبر نیست.")
        return
    data = await state.get_data()
    async with session_factory() as session:
        payment = await session.get(PaymentConfig, 1)
        if payment is None:
            payment = PaymentConfig(id=1)
            session.add(payment)
        payment.card_number = data["payment_card_number"]
        payment.card_holder = holder
        await session.commit()
    await state.clear()
    await message.answer(
        "مشخصات کارت ذخیره شد. ✅", reply_markup=main_menu_keyboard(is_admin=True)
    )


@router.callback_query(F.data == "admin:receipts")
async def pending_receipts(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
    bot: Bot,
) -> None:
    if await _reject_callback(callback, settings):
        return
    async with session_factory() as session:
        receipts = list(
            await session.scalars(
                select(PaymentReceipt)
                .where(PaymentReceipt.status == ReceiptStatus.PENDING)
                .order_by(PaymentReceipt.created_at, PaymentReceipt.id)
                .limit(20)
            )
        )
    if not receipts:
        await callback.answer("فیش در انتظاری وجود ندارد.", show_alert=True)
        return
    await callback.answer(f"{_digits(len(receipts))} فیش در انتظار است.")
    for receipt in receipts:
        caption = (
            f"فیش شماره <code>{_digits(receipt.id)}</code> 🧾\n"
            f"کاربر: <code>{_digits(receipt.user_id)}</code>\n"
            f"پلن: <b>{html.escape(receipt.plan_name)}</b>\n"
            f"مبلغ: <b>{_digits(f'{receipt.amount:,}')} تومان</b>"
        )
        try:
            if receipt.file_type == "photo":
                await bot.send_photo(
                    callback.from_user.id,
                    receipt.file_id,
                    caption=caption,
                    reply_markup=receipt_review_keyboard(receipt.id),
                )
            else:
                await bot.send_document(
                    callback.from_user.id,
                    receipt.file_id,
                    caption=caption,
                    reply_markup=receipt_review_keyboard(receipt.id),
                )
        except TelegramAPIError:
            await bot.send_message(
                callback.from_user.id,
                caption + "\n\nفایل فیش دیگر در دسترس تلگرام نیست.",
            )


async def _finalize_receipt_message(
    callback: CallbackQuery,
    result_text: str,
) -> None:
    if callback.message is None:
        return
    try:
        if callback.message.caption is not None:
            await callback.message.edit_caption(
                caption=f"{callback.message.caption}\n\n{result_text}",
                reply_markup=None,
            )
        else:
            await callback.message.edit_text(result_text, reply_markup=None)
    except TelegramAPIError:
        await callback.message.answer(result_text)


@router.callback_query(F.data.startswith("receipt:approve:"))
async def approve_receipt(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
    bot: Bot,
) -> None:
    if await _reject_callback(callback, settings):
        return
    receipt_id = int((callback.data or "").rsplit(":", 1)[1])
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        receipt = await session.scalar(
            select(PaymentReceipt)
            .where(PaymentReceipt.id == receipt_id)
            .with_for_update()
        )
        if receipt is None:
            await callback.answer("فیش پیدا نشد.", show_alert=True)
            return
        if receipt.status != ReceiptStatus.PENDING:
            await callback.answer("این فیش قبلاً بررسی شده است.", show_alert=True)
            return
        user = await session.scalar(
            select(User).where(User.telegram_id == receipt.user_id).with_for_update()
        )
        if user is None:
            await callback.answer("حساب کاربر پیدا نشد.", show_alert=True)
            return
        receipt_discount = await session.get(ReceiptDiscount, receipt.id)
        discount_code = None
        if receipt_discount is not None:
            discount_code = await session.scalar(
                select(DiscountCode)
                .where(DiscountCode.id == receipt_discount.discount_code_id)
                .with_for_update()
            )
            if (
                discount_code is None
                or not discount_code.is_active
                or (
                    discount_code.max_uses is not None
                    and discount_code.used_count >= discount_code.max_uses
                )
            ):
                await callback.answer(
                    "ظرفیت کد تخفیف تکمیل یا کد غیرفعال شده است؛ فیش را رد کنید.",
                    show_alert=True,
                )
                return
        subscription = await session.get(UserSubscription, user.telegram_id)
        stored_expiry = subscription.expires_at if subscription is not None else None
        if stored_expiry is not None and stored_expiry.tzinfo is None:
            stored_expiry = stored_expiry.replace(tzinfo=timezone.utc)
        current_expiry = (
            stored_expiry if stored_expiry is not None and stored_expiry > now else now
        )
        new_expiry = current_expiry + timedelta(days=receipt.duration_days)
        if subscription is None:
            subscription = UserSubscription(
                user_id=user.telegram_id,
                plan_name=receipt.plan_name,
                target_limit=receipt.target_limit,
                starts_at=now,
                expires_at=new_expiry,
            )
            session.add(subscription)
        subscription.plan_id = receipt.plan_id
        subscription.plan_name = receipt.plan_name
        subscription.target_limit = receipt.target_limit
        subscription.starts_at = now
        subscription.expires_at = new_expiry
        user.status = UserStatus.ACTIVE
        user.plan_tier = (
            PlanTier.PREMIUM if receipt.target_limit <= 100 else PlanTier.VIP
        )
        user.subscription_expiry = new_expiry
        receipt.status = ReceiptStatus.APPROVED
        receipt.reviewed_by = callback.from_user.id
        receipt.reviewed_at = now
        if discount_code is not None:
            discount_code.used_count += 1
        await session.commit()
        recipient_id = user.telegram_id
        plan_name = receipt.plan_name
        duration_days = receipt.duration_days

    try:
        await bot.send_message(
            recipient_id,
            "فیش پرداخت شما تأیید شد. ✅\n\n"
            f"پلن فعال: <b>{html.escape(plan_name)}</b>\n"
            f"مدت افزوده‌شده: {_digits(duration_days)} روز\n"
            f"تاریخ پایان:\n{format_datetime_dual(new_expiry)}",
        )
    except TelegramAPIError:
        logger.exception(
            "Could not notify user %s about receipt approval", recipient_id
        )
    await _finalize_receipt_message(callback, "✅ فیش تأیید و اشتراک فعال شد.")
    await callback.answer("اشتراک کاربر فعال شد. ✅", show_alert=True)


@router.callback_query(F.data.startswith("receipt:reject:"))
async def reject_receipt(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
    bot: Bot,
) -> None:
    if await _reject_callback(callback, settings):
        return
    receipt_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        receipt = await session.scalar(
            select(PaymentReceipt)
            .where(PaymentReceipt.id == receipt_id)
            .with_for_update()
        )
        if receipt is None:
            await callback.answer("فیش پیدا نشد.", show_alert=True)
            return
        if receipt.status != ReceiptStatus.PENDING:
            await callback.answer("این فیش قبلاً بررسی شده است.", show_alert=True)
            return
        receipt.status = ReceiptStatus.REJECTED
        await session.execute(
            delete(ReceiptDiscount).where(ReceiptDiscount.receipt_id == receipt.id)
        )
        receipt.reviewed_by = callback.from_user.id
        receipt.reviewed_at = datetime.now(timezone.utc)
        recipient_id = receipt.user_id
        await session.commit()

    try:
        await bot.send_message(
            recipient_id,
            "فیش پرداخت شما تأیید نشد. ❌\n\n"
            "لطفاً مبلغ، شماره مقصد و خوانابودن فیش را بررسی کنید یا با پشتیبانی تماس بگیرید.",
        )
    except TelegramAPIError:
        logger.exception(
            "Could not notify user %s about receipt rejection", recipient_id
        )
    await _finalize_receipt_message(callback, "❌ فیش توسط مدیر رد شد.")
    await callback.answer("فیش رد شد.", show_alert=True)


@router.callback_query(F.data == "admin:discounts")
async def admin_discounts(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    async with session_factory() as session:
        codes = list(
            await session.scalars(
                select(DiscountCode)
                .where(DiscountCode.is_active.is_(True))
                .order_by(DiscountCode.id)
            )
        )
    text = "مدیریت کدهای تخفیف 🏷\n\n"
    text += (
        "هنوز کدی ثبت نشده است."
        if not codes
        else "برای حذف، کد موردنظر را انتخاب کنید."
    )
    if callback.message:
        await callback.message.edit_text(
            text, reply_markup=admin_discounts_keyboard(codes)
        )
    await callback.answer()


@router.callback_query(F.data == "admin:discount:add")
async def begin_discount_add(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.clear()
    await state.set_state(AdminDiscountState.waiting_for_code)
    if callback.message:
        await callback.message.answer(
            "کد تخفیف جدید را با حروف انگلیسی ارسال کنید:",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(AdminDiscountState.waiting_for_code, F.text)
async def discount_code_received(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if await _reject_message(message, settings):
        return
    code = (message.text or "").strip().upper()
    if (
        not code
        or len(code) > 64
        or not all(ch.isalnum() or ch in {"-", "_"} for ch in code)
    ):
        await message.answer(
            "کد فقط می‌تواند شامل حروف انگلیسی، عدد، خط تیره و زیرخط باشد."
        )
        return
    await state.update_data(discount_code=code)
    await state.set_state(AdminDiscountState.waiting_for_percent)
    await message.answer("درصد تخفیف را بین ۱ تا ۱۰۰ وارد کنید:")


@router.message(AdminDiscountState.waiting_for_percent, F.text)
async def discount_percent_received(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if await _reject_message(message, settings):
        return
    try:
        percent = int((message.text or "").strip())
    except ValueError:
        percent = 0
    if not 1 <= percent <= 100:
        await message.answer("درصد باید عددی بین ۱ تا ۱۰۰ باشد.")
        return
    await state.update_data(discount_percent=percent)
    await state.set_state(AdminDiscountState.waiting_for_max_uses)
    await message.answer(
        "حداکثر تعداد استفاده را وارد کنید؛ برای نامحدود عدد ۰ را بفرستید:"
    )


@router.message(AdminDiscountState.waiting_for_max_uses, F.text)
async def discount_limit_received(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if await _reject_message(message, settings):
        return
    try:
        max_uses = int((message.text or "").strip())
    except ValueError:
        max_uses = -1
    if max_uses < 0:
        await message.answer("تعداد استفاده باید صفر یا یک عدد مثبت باشد.")
        return
    await state.update_data(discount_max_uses=max_uses)
    await state.set_state(AdminDiscountState.waiting_for_expiry_days)
    await message.answer("اعتبار کد چند روز باشد؟ برای بدون انقضا عدد ۰ را بفرستید:")


@router.message(AdminDiscountState.waiting_for_expiry_days, F.text)
async def discount_expiry_received(
    message: Message,
    state: FSMContext,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_message(message, settings):
        return
    try:
        days = int((message.text or "").strip())
    except ValueError:
        days = -1
    if not 0 <= days <= 3650:
        await message.answer("تعداد روز باید بین ۰ تا ۳۶۵۰ باشد.")
        return
    data = await state.get_data()
    try:
        async with session_factory() as session:
            session.add(
                DiscountCode(
                    code=data["discount_code"],
                    percent=data["discount_percent"],
                    max_uses=data["discount_max_uses"] or None,
                    expires_at=(datetime.now(timezone.utc) + timedelta(days=days))
                    if days
                    else None,
                )
            )
            await session.commit()
    except IntegrityError:
        await message.answer("این کد قبلاً ثبت شده است.")
        return
    await state.clear()
    await message.answer(
        "کد تخفیف با موفقیت ساخته شد. ✅",
        reply_markup=main_menu_keyboard(is_admin=True),
    )


@router.callback_query(F.data.startswith("admin:discount:delete:"))
async def delete_discount_code(
    callback: CallbackQuery, settings: Settings, session_factory: SessionFactory
) -> None:
    if await _reject_callback(callback, settings):
        return
    code_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        code = await session.get(DiscountCode, code_id)
        if code is not None:
            code.is_active = False
            await session.commit()
    await admin_discounts(callback, settings, session_factory)


async def _show_admin_store(
    callback: CallbackQuery, session_factory: SessionFactory
) -> None:
    async with session_factory() as session:
        config = await session.get(StoreConfig, 1)
        products = list(
            await session.scalars(
                select(StoreProduct)
                .where(StoreProduct.is_active.is_(True))
                .order_by(StoreProduct.id)
            )
        )
    enabled = bool(config and config.enabled)
    if callback.message:
        await callback.message.edit_text(
            f"مدیریت فروشگاه 🛍️\n\nوضعیت نمایش برای کاربران: {'فعال ✅' if enabled else 'غیرفعال ❌'}",
            reply_markup=admin_store_keyboard(products, enabled),
        )


@router.callback_query(F.data == "admin:store")
async def admin_store(
    callback: CallbackQuery, settings: Settings, session_factory: SessionFactory
) -> None:
    if await _reject_callback(callback, settings):
        return
    await _show_admin_store(callback, session_factory)
    await callback.answer()


@router.callback_query(F.data == "admin:store:toggle")
async def toggle_store(
    callback: CallbackQuery, settings: Settings, session_factory: SessionFactory
) -> None:
    if await _reject_callback(callback, settings):
        return
    async with session_factory() as session:
        config = await session.get(StoreConfig, 1)
        if config is None:
            config = StoreConfig(id=1, enabled=True)
            session.add(config)
        else:
            config.enabled = not config.enabled
        await session.commit()
    await _show_admin_store(callback, session_factory)
    await callback.answer("وضعیت فروشگاه ذخیره شد. ✅")


@router.callback_query(F.data == "admin:store:add")
async def begin_store_product(
    callback: CallbackQuery, settings: Settings, state: FSMContext
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.clear()
    await state.set_state(AdminStoreState.waiting_for_name)
    if callback.message:
        await callback.message.answer(
            "نام محصول را ارسال کنید:", reply_markup=cancel_keyboard()
        )
    await callback.answer()


@router.message(AdminStoreState.waiting_for_name, F.text)
async def store_name_received(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if await _reject_message(message, settings):
        return
    name = (message.text or "").strip()
    if not 2 <= len(name) <= 100:
        await message.answer("نام محصول باید بین ۲ تا ۱۰۰ نویسه باشد.")
        return
    await state.update_data(store_name=name)
    await state.set_state(AdminStoreState.waiting_for_description)
    await message.answer("توضیحات محصول را ارسال کنید:")


@router.message(AdminStoreState.waiting_for_description, F.text)
async def store_description_received(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if await _reject_message(message, settings):
        return
    description = (message.text or "").strip()
    if not 2 <= len(description) <= 2000:
        await message.answer("توضیحات باید بین ۲ تا ۲۰۰۰ نویسه باشد.")
        return
    await state.update_data(store_description=description)
    await state.set_state(AdminStoreState.choosing_currency)
    await message.answer(
        "واحد قیمت محصول را انتخاب کنید:",
        reply_markup=currency_selection_keyboard("admin:store:currency"),
    )


@router.callback_query(F.data.startswith("admin:store:currency:"))
async def store_currency_selected(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if await _reject_callback(callback, settings):
        return
    currency = normalize_currency((callback.data or "").rsplit(":", 1)[1])
    await state.update_data(store_currency=currency)
    await state.set_state(AdminStoreState.waiting_for_price)
    currency_text = "دلار" if currency == "USD" else "تومان"
    if callback.message:
        await callback.message.answer(
            f"قیمت محصول را به {currency_text} و فقط با عدد وارد کنید:"
        )
    await callback.answer()


@router.message(AdminStoreState.waiting_for_price, F.text)
async def store_price_received(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if await _reject_message(message, settings):
        return
    try:
        price = _parse_integer(message.text)
    except ValueError:
        price = -1
    if price < 0:
        await message.answer("قیمت نامعتبر است.")
        return
    await state.update_data(store_price=price)
    await state.set_state(AdminStoreState.waiting_for_url)
    await message.answer(
        "لینک خرید را ارسال کنید؛ برای استفاده از آیدی پشتیبانی یک خط تیره (-) بفرستید:"
    )


@router.message(AdminStoreState.waiting_for_url, F.text)
async def store_url_received(
    message: Message,
    state: FSMContext,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_message(message, settings):
        return
    value = (message.text or "").strip()
    if value != "-" and not value.startswith(("https://", "http://")):
        await message.answer(
            "لینک باید با http:// یا https:// شروع شود؛ یا فقط - بفرستید."
        )
        return
    data = await state.get_data()
    async with session_factory() as session:
        session.add(
            StoreProduct(
                name=data["store_name"],
                description=data["store_description"],
                price=data["store_price"],
                price_currency=data.get("store_currency", "TOMAN"),
                purchase_url=None if value == "-" else value,
            )
        )
        await session.commit()
    await state.clear()
    await message.answer(
        "محصول به فروشگاه افزوده شد. ✅", reply_markup=main_menu_keyboard(is_admin=True)
    )


@router.callback_query(F.data.startswith("admin:store:delete:"))
async def delete_store_product(
    callback: CallbackQuery, settings: Settings, session_factory: SessionFactory
) -> None:
    if await _reject_callback(callback, settings):
        return
    product_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        product = await session.get(StoreProduct, product_id)
        if product is not None:
            product.is_active = False
            await session.commit()
    await _show_admin_store(callback, session_factory)
    await callback.answer("محصول حذف شد. ✅")


@router.callback_query(F.data == "admin:report_copy")
async def begin_report_copy(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.clear()
    await state.set_state(AdminReportCopyState.waiting_for_user_id)
    if callback.message:
        await callback.message.answer(
            "شناسه عددی کاربر را بفرستید. سپس نوع گزارش‌هایی را که باید "
            "برای مدیر رونوشت شوند انتخاب می‌کنید:",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(AdminReportCopyState.waiting_for_user_id, F.text)
async def finish_report_copy(
    message: Message,
    settings: Settings,
    state: FSMContext,
    session_factory: SessionFactory,
) -> None:
    if await _reject_message(message, settings):
        return
    try:
        user_id = _parse_integer(message.text)
    except ValueError:
        await message.answer("شناسه عددی معتبر ارسال کنید.")
        return
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            await message.answer("این کاربر هنوز در ربات ثبت نشده است.")
            return
        if user.telegram_id == settings.admin_telegram_id:
            await message.answer("گزارش‌های پیج‌های خود مدیر مستقیماً ارسال می‌شوند.")
            return
        categories = parse_admin_report_categories(user.admin_report_categories)
        if user.admin_report_copy and not categories:
            categories = set(ADMIN_REPORT_KEYS)
            user.admin_report_categories = serialize_admin_report_categories(categories)
        user.admin_report_copy = False
        await session.commit()
    await state.clear()
    await message.answer(
        f"انتخاب رونوشت گزارش برای کاربر <code>{user_id}</code>\n\n"
        "هر گزینه را جداگانه فعال یا غیرفعال کنید. کاربر از این تنظیم مطلع نمی‌شود.",
        reply_markup=admin_report_copy_keyboard(user_id, categories),
    )


@router.callback_query(F.data.startswith("admin:reportcopy:"))
async def toggle_admin_report_copy(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    try:
        _, _, user_id_text, category = (callback.data or "").split(":", 3)
        user_id = int(user_id_text)
    except (TypeError, ValueError):
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    if category != "NONE" and category not in ADMIN_REPORT_KEYS:
        await callback.answer("نوع گزارش نامعتبر است.", show_alert=True)
        return
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            await callback.answer("کاربر پیدا نشد.", show_alert=True)
            return
        categories = parse_admin_report_categories(user.admin_report_categories)
        if user.admin_report_copy and not categories:
            categories = set(ADMIN_REPORT_KEYS)
        if category == "NONE":
            categories.clear()
        elif category in categories:
            categories.remove(category)
        else:
            categories.add(category)
        user.admin_report_copy = False
        user.admin_report_categories = serialize_admin_report_categories(categories)
        await session.commit()
    if callback.message:
        await callback.message.edit_text(
            f"انتخاب رونوشت گزارش برای کاربر <code>{user_id}</code>\n\n"
            "فقط گزارش‌های علامت‌خورده برای مدیر ارسال می‌شوند و کاربر پیامی درباره این تنظیم نمی‌بیند.",
            reply_markup=admin_report_copy_keyboard(user_id, categories),
        )
    await callback.answer("تنظیم رونوشت ذخیره شد. ✅")


@router.callback_query(F.data.startswith("admin:users:"))
async def list_users(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    try:
        page = max(0, int((callback.data or "").rsplit(":", 1)[1]))
    except ValueError:
        page = 0
    page_size = 10
    async with session_factory() as session:
        total = int(await session.scalar(select(func.count(User.telegram_id))) or 0)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages - 1)
        users = list(
            await session.scalars(
                select(User)
                .order_by(User.created_at.desc(), User.telegram_id.desc())
                .offset(page * page_size)
                .limit(page_size)
            )
        )
    text = (
        "<b>مدیریت کاربران 👥</b>\n\n"
        f"تعداد کل کاربران: <code>{_digits(total)}</code>\n"
        f"صفحه: <code>{_digits(page + 1)}</code> از <code>{_digits(total_pages)}</code>\n\n"
        "برای مشاهده اطلاعات کامل، خریدها و پیج‌ها یک کاربر را انتخاب کنید."
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=admin_users_keyboard(users, page, total_pages),
        )
    await callback.answer()


@router.callback_query(F.data == "admin:user:search")
async def begin_user_search(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await state.clear()
    await state.set_state(AdminUserState.waiting_for_user_id)
    if callback.message:
        await callback.message.answer(
            "شناسه عددی تلگرام کاربر را ارسال کنید:",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(AdminUserState.waiting_for_user_id, F.text)
async def finish_user_search(
    message: Message,
    settings: Settings,
    state: FSMContext,
    session_factory: SessionFactory,
) -> None:
    if await _reject_message(message, settings):
        return
    try:
        user_id = _parse_integer(message.text)
    except ValueError:
        await message.answer("شناسه عددی معتبر ارسال کنید.")
        return
    async with session_factory() as session:
        user = await session.get(User, user_id)
    if user is None:
        await message.answer("کاربری با این شناسه در ربات ثبت نشده است.")
        return
    await state.clear()
    await _send_user_details(message, user_id, session_factory)


async def _user_details_text(
    user_id: int,
    session_factory: SessionFactory,
) -> tuple[str | None, bool]:
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            return None, False
        subscription = await session.get(UserSubscription, user_id)
        page_total = int(
            await session.scalar(
                select(func.count(TargetPage.id)).where(TargetPage.user_id == user_id)
            )
            or 0
        )
        active_pages = int(
            await session.scalar(
                select(func.count(TargetPage.id)).where(
                    TargetPage.user_id == user_id,
                    TargetPage.last_known_status == PageStatus.ACTIVE,
                )
            )
            or 0
        )
        inactive_pages = int(
            await session.scalar(
                select(func.count(TargetPage.id)).where(
                    TargetPage.user_id == user_id,
                    TargetPage.last_known_status == PageStatus.DEACTIVATED,
                )
            )
            or 0
        )
        receipt_total = int(
            await session.scalar(
                select(func.count(PaymentReceipt.id)).where(
                    PaymentReceipt.user_id == user_id
                )
            )
            or 0
        )
        approved_total = int(
            await session.scalar(
                select(func.coalesce(func.sum(PaymentReceipt.amount), 0)).where(
                    PaymentReceipt.user_id == user_id,
                    PaymentReceipt.status == ReceiptStatus.APPROVED,
                )
            )
            or 0
        )
        report_categories = parse_admin_report_categories(user.admin_report_categories)
        if user.admin_report_copy and not report_categories:
            report_categories = set(ADMIN_REPORT_KEYS)

    username = f"@{html.escape(user.username)}" if user.username else "ثبت نشده"
    status_text = "مسدود ❌" if user.status == UserStatus.BANNED else "فعال ✅"
    plan_name = (
        html.escape(subscription.plan_name)
        if subscription
        else PLAN_NAMES[user.plan_tier]
    )
    expiry = subscription.expires_at if subscription else user.subscription_expiry
    target_limit = (
        subscription.target_limit if subscription else user.plan_tier.target_limit
    )
    text = (
        "<b>پرونده کامل کاربر 👤</b>\n\n"
        f"شناسه تلگرام: <code>{_digits(user.telegram_id)}</code>\n"
        f"نام کاربری: <b>{username}</b>\n"
        f"وضعیت حساب: <b>{status_text}</b>\n"
        f"پلن فعلی: <b>{plan_name}</b>\n"
        f"ظرفیت پیج: <code>{_digits(target_limit)}</code>\n"
        f"پایان اعتبار:\n{format_datetime_dual(expiry)}\n\n"
        f"پیج‌های ثبت‌شده: <code>{_digits(page_total)}</code>\n"
        f"فعال: <code>{_digits(active_pages)}</code> | غیرفعال: <code>{_digits(inactive_pages)}</code>\n"
        f"تعداد فیش‌ها: <code>{_digits(receipt_total)}</code>\n"
        f"مجموع خریدهای تأییدشده: <b>{format_money(approved_total, 'TOMAN')}</b>\n"
        f"انواع رونوشت فعال برای مدیر: <code>{_digits(len(report_categories))}</code>\n\n"
        f"تاریخ عضویت:\n{format_datetime_dual(user.created_at)}"
    )
    return text, user.status == UserStatus.BANNED


async def _send_user_details(
    message: Message,
    user_id: int,
    session_factory: SessionFactory,
) -> None:
    text, banned = await _user_details_text(user_id, session_factory)
    if text is None:
        await message.answer("کاربر پیدا نشد.")
        return
    await message.answer(
        text,
        reply_markup=admin_user_detail_keyboard(user_id, banned=banned),
    )


@router.callback_query(F.data.startswith("admin:user:view:"))
async def view_user_details(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    user_id = int((callback.data or "").rsplit(":", 1)[1])
    text, banned = await _user_details_text(user_id, session_factory)
    if text is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=admin_user_detail_keyboard(user_id, banned=banned),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user:reports:"))
async def user_report_copy_settings(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    user_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            await callback.answer("کاربر پیدا نشد.", show_alert=True)
            return
        categories = parse_admin_report_categories(user.admin_report_categories)
        if user.admin_report_copy and not categories:
            categories = set(ADMIN_REPORT_KEYS)
    if callback.message:
        await callback.message.edit_text(
            f"انتخاب رونوشت گزارش برای کاربر <code>{user_id}</code>\n\n"
            "کاربر از فعال یا غیرفعال‌شدن این گزینه‌ها مطلع نمی‌شود.",
            reply_markup=admin_report_copy_keyboard(user_id, categories),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user:subview:"))
async def view_user_subscription(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    user_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        user = await session.get(User, user_id)
        subscription = await session.get(UserSubscription, user_id)
    if user is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    plan_name = (
        html.escape(subscription.plan_name)
        if subscription
        else PLAN_NAMES[user.plan_tier]
    )
    target_limit = (
        subscription.target_limit if subscription else user.plan_tier.target_limit
    )
    expiry = subscription.expires_at if subscription else user.subscription_expiry
    text = (
        f"<b>کنترل اشتراک کاربر <code>{user_id}</code> 💎</b>\n\n"
        f"پلن فعلی: <b>{plan_name}</b>\n"
        f"ظرفیت پایش: <code>{_digits(target_limit)}</code> پیج\n"
        f"پایان اعتبار:\n{format_datetime_dual(expiry)}\n\n"
        "می‌توانید پلن را تغییر دهید، روز اضافه کنید یا اشتراک ویژه را پایان دهید."
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=admin_user_subscription_keyboard(user_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user:sub:renew:"))
async def renew_user_from_profile(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    user_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        user = await session.get(User, user_id)
    if user is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    await state.clear()
    await state.update_data(renew_user_id=user_id, renew_return_user=True)
    await state.set_state(AdminRenewState.choosing_plan)
    if callback.message:
        await callback.message.answer(
            "پلن جدید کاربر را انتخاب کنید:",
            reply_markup=admin_plan_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user:sub:extend"))
async def extend_user_subscription(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 5 or parts[3] not in {"extend30", "extend90"}:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    days = 30 if parts[3] == "extend30" else 90
    user_id = int(parts[4])
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(User.telegram_id == user_id).with_for_update()
        )
        if user is None:
            await callback.answer("کاربر پیدا نشد.", show_alert=True)
            return
        subscription = await session.get(UserSubscription, user_id)
        current_expiry = (
            subscription.expires_at if subscription else user.subscription_expiry
        )
        if current_expiry.tzinfo is None:
            current_expiry = current_expiry.replace(tzinfo=timezone.utc)
        new_expiry = max(now, current_expiry) + timedelta(days=days)
        user.subscription_expiry = new_expiry
        user.status = UserStatus.ACTIVE
        if subscription:
            subscription.expires_at = new_expiry
            subscription.updated_at = now
        await session.commit()
    await callback.answer(
        f"{_digits(days)} روز به اشتراک اضافه شد. ✅",
        show_alert=True,
    )
    if callback.message:
        await callback.message.edit_text(
            f"اشتراک کاربر <code>{user_id}</code> تمدید شد. ✅\n\n"
            f"پایان اعتبار جدید:\n{format_datetime_dual(new_expiry)}",
            reply_markup=admin_user_subscription_keyboard(user_id),
        )


@router.callback_query(F.data.startswith("admin:user:sub:expire:"))
async def expire_user_subscription(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    user_id = int((callback.data or "").rsplit(":", 1)[1])
    if user_id == settings.admin_telegram_id:
        await callback.answer(
            "اشتراک مدیر اصلی قابل پایان‌دادن نیست.",
            show_alert=True,
        )
        return
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(User.telegram_id == user_id).with_for_update()
        )
        if user is None:
            await callback.answer("کاربر پیدا نشد.", show_alert=True)
            return
        subscription = await session.get(UserSubscription, user_id)
        if subscription is not None:
            await session.delete(subscription)
        user.subscription_expiry = now
        user.plan_tier = PlanTier.FREE
        await session.commit()
    if callback.message:
        await callback.message.edit_text(
            f"اشتراک ویژه کاربر <code>{user_id}</code> پایان یافت و حساب به پلن رایگان برگشت.",
            reply_markup=admin_user_subscription_keyboard(user_id),
        )
    await callback.answer("اشتراک ویژه پایان یافت. ✅", show_alert=True)


@router.callback_query(F.data.startswith("admin:user:status:"))
async def toggle_user_status(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    user_id = int((callback.data or "").rsplit(":", 1)[1])
    if user_id == settings.admin_telegram_id:
        await callback.answer("حساب مدیر اصلی قابل مسدودکردن نیست.", show_alert=True)
        return
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            await callback.answer("کاربر پیدا نشد.", show_alert=True)
            return
        user.status = (
            UserStatus.ACTIVE if user.status == UserStatus.BANNED else UserStatus.BANNED
        )
        banned = user.status == UserStatus.BANNED
        await session.commit()
    text, _ = await _user_details_text(user_id, session_factory)
    if callback.message and text:
        await callback.message.edit_text(
            text,
            reply_markup=admin_user_detail_keyboard(user_id, banned=banned),
        )
    await callback.answer("وضعیت کاربر تغییر کرد. ✅", show_alert=True)


@router.callback_query(F.data.startswith("admin:user:pages:"))
async def view_user_pages(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    _, _, _, user_id_text, page_text = (callback.data or "").split(":")
    user_id, page = int(user_id_text), max(0, int(page_text))
    page_size = 10
    async with session_factory() as session:
        user = await session.get(User, user_id)
        pages = list(
            await session.scalars(
                select(TargetPage)
                .where(TargetPage.user_id == user_id)
                .order_by(TargetPage.id.desc())
                .offset(page * page_size)
                .limit(page_size + 1)
            )
        )
    if user is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    has_next = len(pages) > page_size
    pages = pages[:page_size]
    lines = [f"<b>پیج‌های کاربر <code>{user_id}</code> 📊</b>", ""]
    if not pages:
        lines.append("هیچ پیجی در این صفحه ثبت نشده است.")
    for target in pages:
        status = (
            "فعال ✅"
            if target.last_known_status == PageStatus.ACTIVE
            else "غیرفعال ❌"
            if target.last_known_status == PageStatus.DEACTIVATED
            else "نامشخص"
        )
        if not target.status_confirmed:
            status += " — در انتظار تأیید قطعی"
        lines.extend(
            (
                f"<b>@{html.escape(target.instagram_username)}</b>",
                f"شناسه داخلی: <code>{_digits(target.id)}</code> | وضعیت: {status}",
                f"شناسه اینستاگرام: <code>{html.escape(target.last_known_id or 'ثبت نشده')}</code>",
                f"نتیجه آخرین تلاش: <code>{html.escape(target.last_check_outcome or 'ثبت نشده')}</code>",
                f"آخرین پاسخ قطعی:\n{format_datetime_dual(target.last_successful_check_at)}",
                "",
            )
        )
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=admin_user_section_keyboard(
                user_id, page, has_next=has_next, section="pages"
            ),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user:payments:"))
async def view_user_payments(
    callback: CallbackQuery,
    settings: Settings,
    session_factory: SessionFactory,
) -> None:
    if await _reject_callback(callback, settings):
        return
    _, _, _, user_id_text, page_text = (callback.data or "").split(":")
    user_id, page = int(user_id_text), max(0, int(page_text))
    page_size = 8
    async with session_factory() as session:
        user = await session.get(User, user_id)
        subscription = await session.get(UserSubscription, user_id)
        receipts = list(
            await session.scalars(
                select(PaymentReceipt)
                .where(PaymentReceipt.user_id == user_id)
                .order_by(PaymentReceipt.created_at.desc())
                .offset(page * page_size)
                .limit(page_size + 1)
            )
        )
    if user is None:
        await callback.answer("کاربر پیدا نشد.", show_alert=True)
        return
    has_next = len(receipts) > page_size
    receipts = receipts[:page_size]
    status_names = {
        ReceiptStatus.PENDING: "در انتظار ⏳",
        ReceiptStatus.APPROVED: "تأییدشده ✅",
        ReceiptStatus.REJECTED: "ردشده ❌",
    }
    lines = [f"<b>خریدها و فیش‌های کاربر <code>{user_id}</code> 🧾</b>", ""]
    if subscription:
        lines.extend(
            (
                f"اشتراک فعلی: <b>{html.escape(subscription.plan_name)}</b>",
                f"ظرفیت: <code>{_digits(subscription.target_limit)}</code>",
                f"پایان اعتبار:\n{format_datetime_dual(subscription.expires_at)}",
                "",
            )
        )
    if not receipts:
        lines.append("هیچ فیش خریدی در این صفحه ثبت نشده است.")
    for receipt in receipts:
        lines.extend(
            (
                f"فیش <code>#{_digits(receipt.id)}</code> — {status_names[receipt.status]}",
                f"پلن: <b>{html.escape(receipt.plan_name)}</b>",
                f"مبلغ: <b>{format_money(receipt.amount, 'TOMAN')}</b>",
                f"تاریخ:\n{format_datetime_dual(receipt.created_at)}",
                "",
            )
        )
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=admin_user_section_keyboard(
                user_id, page, has_next=has_next, section="payments"
            ),
        )
    await callback.answer()


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
    warp_ok = await checker.proxy_preflight()
    browser_ok, browser_version = await profile_preview.browser_health()
    result = await checker.fetch_profile(
        "instagram",
        allow_stale=False,
        force_refresh=True,
    )
    rendered = await profile_preview.inspect("instagram", use_cache=False)
    cooldown_ttl = await redis.ttl(checker.STATUS_COOLDOWN_KEY)
    proxy_failure_streak = int(await redis.get(checker.PROXY_FAILURE_STREAK_KEY) or 0)

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
    warp_text = "سالم و تأییدشده ✅" if warp_ok else "ناسالم یا مسدود ❌"
    proxy_text = "فعال ✅" if settings.instagram_proxy_url else "غیرفعال ⚠️"
    http_text = _digits(result.http_status) if result.http_status else "ثبت نشد"
    render_names = {
        PreviewOutcome.ACTIVE: "جزئیات پیج رندر شد ✅",
        PreviewOutcome.DEACTIVATED: "پاسخ قطعی عدم دسترسی دریافت شد",
        PreviewOutcome.UNKNOWN: "جزئیات پیج رندر نشد ⚠️",
    }
    diagnostic_names = {
        "rendered_embed": "سالم",
        "chromium_start_failed": "Chromium اجرا نشد",
        "navigation_timeout": "مهلت بازشدن صفحه تمام شد",
        "profile_content_timeout": "محتوای پروفایل بارگذاری نشد",
        "login_redirect": "انتقال به صفحه ورود",
        "login_wall": "صفحه ورود نمایش داده شد",
        "browser_exception": "خطای داخلی مرورگر",
        "http_404": "پاسخ ۴۰۴",
        "not_available_text": "پیام در دسترس نبودن پیج",
    }
    render_diagnostic = diagnostic_names.get(
        rendered.diagnostic or "",
        rendered.diagnostic or "ثبت نشد",
    )
    text = (
        "وضعیت اتصال اینستاگرام 🌐\n\n"
        "حالت دسترسی: <b>نمای عمومی بدون ورود</b>\n"
        "وضعیت ورود: <b>وارد نشده</b>\n"
        "ذخیره رمز یا کوکی: <b>غیرفعال</b>\n"
        f"پراکسی WARP: <b>{proxy_text}</b>\n"
        f"سلامت مسیر WARP: <b>{warp_text}</b>\n"
        f"شکست‌های متوالی مسیر: <b>{_digits(proxy_failure_streak)}</b>\n"
        f"Chromium: <b>{browser_text}</b>\n"
        f"نتیجه تست: <b>{outcome_names[result.outcome]}</b>\n"
        f"کد HTTP: <code>{http_text}</code>\n"
        f"رندر واقعی: <b>{render_names[rendered.outcome]}</b>\n"
        f"تشخیص رندر: <b>{render_diagnostic}</b>\n"
        f"توقف موقت چکر: <b>{cooldown_text}</b>\n\n"
        "پاسخ نامشخص هیچ‌گاه وضعیت پیج را به دی‌اکتیو تغییر نمی‌دهد."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())


async def _show_diagnostics(
    callback: CallbackQuery,
    checker: InstagramChecker,
) -> None:
    entries = await checker.diagnostics.latest(500)
    error_count = sum(entry.level in {"ERROR", "CRITICAL"} for entry in entries)
    warning_count = sum(entry.level == "WARNING" for entry in entries)
    lines = [
        "لاگ کامل و عیب‌یابی 🧰",
        "",
        f"نسخه ربات: <code>{APP_VERSION}</code>",
        f"تعداد رکوردهای ذخیره‌شده: {_digits(len(entries))} از ۵۰۰",
        f"خطاها: {_digits(error_count)} | هشدارها: {_digits(warning_count)}",
        "",
        "آخرین رخدادها:",
    ]
    if not entries:
        lines.append("هنوز رخدادی ثبت نشده است.")
    else:
        for index, entry in enumerate(entries[:4], start=1):
            lines.extend(
                (
                    "",
                    f"<b>رکورد {_digits(index)}</b>",
                    f"<pre>{html.escape(_diagnostic_text(entry, compact=True))}</pre>",
                )
            )
    lines.extend(
        (
            "",
            "برای ارسال گزارش فنی، دکمه «دریافت فایل کامل لاگ» را بزنید. "
            "توکن ربات، رمز دیتابیس و رمز Redis داخل این فایل ذخیره نمی‌شوند.",
        )
    )
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=admin_diagnostics_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "admin:diagnostics")
async def admin_diagnostics(
    callback: CallbackQuery,
    settings: Settings,
    checker: InstagramChecker,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await _show_diagnostics(callback, checker)


@router.callback_query(F.data == "admin:diagnostics:download")
async def download_diagnostics(
    callback: CallbackQuery,
    settings: Settings,
    checker: InstagramChecker,
) -> None:
    if await _reject_callback(callback, settings):
        return
    entries = await checker.diagnostics.latest(500)
    if not entries:
        await callback.answer("هنوز لاگی برای دریافت وجود ندارد.", show_alert=True)
        return
    generated_at = datetime.now(timezone.utc)
    sections = [
        "لاگ عیب‌یابی فارستار وارنر",
        f"نسخه: {APP_VERSION}",
        f"زمان تولید:\n{_diagnostic_time(generated_at.isoformat())}",
        "توضیح امنیتی: اطلاعات محرمانه محیط در این خروجی ثبت نشده‌اند.",
    ]
    for index, entry in enumerate(entries, start=1):
        sections.append(f"رکورد {index}\n{_diagnostic_text(entry, compact=False)}")
    content = ("\n\n" + "=" * 72 + "\n\n").join(sections)
    document = BufferedInputFile(
        content.encode("utf-8-sig"),
        filename=f"farstar-diagnostics-{generated_at:%Y%m%d-%H%M%S}.txt",
    )
    if callback.message:
        await callback.message.answer_document(
            document,
            caption=(
                "فایل کامل لاگ فنی آماده شد. 📄\n"
                "این فایل را می‌توانید برای بررسی خطا ارسال کنید."
            ),
        )
    await callback.answer("فایل لاگ ساخته شد. ✅")


@router.callback_query(F.data == "admin:diagnostics:clear")
async def confirm_clear_diagnostics(
    callback: CallbackQuery,
    settings: Settings,
) -> None:
    if await _reject_callback(callback, settings):
        return
    if callback.message:
        await callback.message.edit_text(
            "همه ۵۰۰ رکورد اخیر عیب‌یابی پاک شوند؟\n\nاین عملیات قابل بازگشت نیست.",
            reply_markup=admin_diagnostics_keyboard(confirm_clear=True),
        )
    await callback.answer()


@router.callback_query(F.data == "admin:diagnostics:clear_confirm")
async def clear_diagnostics(
    callback: CallbackQuery,
    settings: Settings,
    checker: InstagramChecker,
) -> None:
    if await _reject_callback(callback, settings):
        return
    await checker.diagnostics.clear()
    await _show_diagnostics(callback, checker)


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
        active_plans = await session.scalar(
            select(func.count())
            .select_from(SubscriptionPlan)
            .where(SubscriptionPlan.is_active.is_(True))
        )
        pending_receipt_count = await session.scalar(
            select(func.count())
            .select_from(PaymentReceipt)
            .where(PaymentReceipt.status == ReceiptStatus.PENDING)
        )
        events_24h = await session.scalar(
            select(func.count())
            .select_from(PageEvent)
            .where(PageEvent.created_at >= now - timedelta(hours=24))
        )
        required_channels = await session.scalar(
            select(func.count())
            .select_from(RequiredChannel)
            .where(RequiredChannel.is_active.is_(True))
        )

    text = (
        "آمار سیستم 📈\n\n"
        f"کل کاربران: {_digits(total_users or 0)}\n"
        f"کاربران فعال: {_digits(active_users or 0)}\n"
        f"اشتراک‌های معتبر: {_digits(valid_subscriptions or 0)}\n\n"
        f"کل پیج‌ها: {_digits(total_pages or 0)}\n"
        f"پیج‌های فعال: {_digits(active_pages or 0)}\n"
        f"پیج‌های دی‌اکتیو: {_digits(deactivated_pages or 0)}\n"
        f"رخدادهای ۲۴ ساعت اخیر: {_digits(events_24h or 0)}\n\n"
        f"پلن‌های فروش فعال: {_digits(active_plans or 0)}\n"
        f"فیش‌های در انتظار: {_digits(pending_receipt_count or 0)}\n"
        f"کانال‌های عضویت اجباری: {_digits(required_channels or 0)}\n\n"
        f"زمان گزارش:\n{format_datetime_dual(now)}"
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
    return_to_user = bool(data.get("renew_return_user"))
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
        subscription = await session.get(UserSubscription, user.telegram_id)
        if subscription is None:
            subscription = UserSubscription(
                user_id=user.telegram_id,
                plan_name=PLAN_NAMES[plan],
                target_limit=plan.target_limit,
                starts_at=now,
                expires_at=new_expiry,
            )
            session.add(subscription)
        subscription.plan_id = None
        subscription.plan_name = PLAN_NAMES[plan]
        subscription.target_limit = plan.target_limit
        subscription.starts_at = now
        subscription.expires_at = new_expiry
        await session.commit()

    await state.clear()
    await message.answer(
        "اشتراک کاربر با موفقیت تمدید شد. ✅\n\n"
        f"شناسه کاربر: <code>{_digits(user_id)}</code>\n"
        f"پلن: {PLAN_NAMES[plan]}\n"
        f"اعتبار جدید:\n{format_datetime_dual(new_expiry)}",
        reply_markup=(
            admin_user_detail_keyboard(user_id, banned=False)
            if return_to_user
            else main_menu_keyboard(is_admin=True)
        ),
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
