from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.config import Settings
from bot.database import SessionFactory
from bot.keyboards.inline import (
    confirm_delete_keyboard,
    notification_settings_keyboard,
    page_details_keyboard,
    pages_keyboard,
    settings_pages_keyboard,
    subscription_keyboard,
)
from bot.keyboards.reply import cancel_keyboard, main_menu_keyboard
from bot.models import (
    NotificationSettings,
    PageStatus,
    PlanTier,
    TargetPage,
    User,
    UserStatus,
)


router = Router(name="user")

PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9._]{0,28}[A-Za-z0-9_])?$")
PLAN_NAMES = {
    PlanTier.FREE: "رایگان",
    PlanTier.PREMIUM: "پریمیوم",
    PlanTier.VIP: "ویژه",
}
PAGE_STATUS_NAMES = {
    PageStatus.ACTIVE: "فعال 🟢",
    PageStatus.DEACTIVATED: "دی‌اکتیو 🔴",
    None: "در انتظار اولین بررسی ⚪",
}


class AddPageState(StatesGroup):
    waiting_for_username = State()


class UserAccessMiddleware(BaseMiddleware):
    def __init__(self, session_factory: SessionFactory, settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_user = data.get("event_from_user")
        if telegram_user is None:
            return await handler(event, data)

        async with self.session_factory() as session:
            user = await session.get(User, telegram_user.id)
            if user is None:
                user = User(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    subscription_expiry=datetime.now(timezone.utc)
                    + timedelta(days=self.settings.free_trial_days),
                )
                session.add(user)
            elif user.username != telegram_user.username:
                user.username = telegram_user.username
            await session.commit()

        if user.status == UserStatus.BANNED:
            text = (
                "دسترسی شما به ربات مسدود شده است. برای پیگیری با پشتیبانی تماس بگیرید."
            )
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(text)
            return None

        data["db_user"] = user
        return await handler(event, data)


def register_middlewares(session_factory: SessionFactory, settings: Settings) -> None:
    middleware = UserAccessMiddleware(session_factory, settings)
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)


def to_persian_digits(value: object) -> str:
    return str(value).translate(PERSIAN_DIGITS)


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "ثبت نشده"
    utc_value = value.astimezone(timezone.utc)
    return to_persian_digits(utc_value.strftime("%Y/%m/%d - %H:%M")) + " به وقت جهانی"


def normalize_instagram_username(raw_value: str) -> str | None:
    value = raw_value.strip()
    if value.startswith("@"):
        value = value[1:]
    elif value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        host = parsed.netloc.lower().split(":", 1)[0]
        if host not in {"instagram.com", "www.instagram.com"}:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            return None
        value = parts[0]

    if not USERNAME_RE.fullmatch(value) or ".." in value:
        return None
    return value.lower()


async def _load_pages(
    session_factory: SessionFactory, user_id: int
) -> list[TargetPage]:
    async with session_factory() as session:
        result = await session.scalars(
            select(TargetPage)
            .where(TargetPage.user_id == user_id)
            .order_by(TargetPage.created_at, TargetPage.id)
        )
        return list(result)


def _add_guard_message(user: User, target_count: int) -> str | None:
    if user.subscription_expiry <= datetime.now(timezone.utc):
        return (
            "اعتبار اشتراک شما به پایان رسیده است. لطفاً ابتدا اشتراک خود را تمدید کنید."
        )
    if target_count >= user.plan_tier.target_limit:
        limit = to_persian_digits(user.plan_tier.target_limit)
        return f"ظرفیت پلن شما تکمیل است. سقف پلن فعلی {limit} پیج است."
    return None


@router.message(CommandStart())
async def start(message: Message, db_user: User) -> None:
    await message.answer(
        "سلام! به فارستار وارنر خوش آمدید. 🌟\n\n"
        "از اینجا می‌توانید وضعیت پیج‌های عمومی اینستاگرام را پایش کنید و هنگام تغییر وضعیت اعلان بگیرید.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "راهنمای فارستار وارنر 📘\n\n"
        "برای افزودن یا حذف پیج از «مدیریت پیج‌ها» استفاده کنید. "
        "در بخش «تنظیمات اعلان‌ها» می‌توانید اعلان هر پیج را جداگانه تغییر دهید. "
        "نام کاربری را به‌صورت @username یا لینک کامل اینستاگرام بفرستید.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(StateFilter("*"), F.text == "لغو عملیات ↩️")
async def cancel_operation(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("عملیات لغو شد.", reply_markup=main_menu_keyboard())


@router.message(F.text == "مدیریت پیج‌ها 📊")
async def manage_pages(
    message: Message, db_user: User, session_factory: SessionFactory
) -> None:
    pages = await _load_pages(session_factory, db_user.telegram_id)
    text = (
        "پیج‌های زیر در حال پایش هستند. برای مشاهده جزئیات، یک پیج را انتخاب کنید."
        if pages
        else "هنوز پیجی برای پایش ثبت نکرده‌اید."
    )
    await message.answer(text, reply_markup=pages_keyboard(pages))


@router.callback_query(F.data == "page:list")
async def list_pages_callback(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    pages = await _load_pages(session_factory, db_user.telegram_id)
    text = (
        "پیج‌های زیر در حال پایش هستند. برای مشاهده جزئیات، یک پیج را انتخاب کنید."
        if pages
        else "هنوز پیجی برای پایش ثبت نکرده‌اید."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=pages_keyboard(pages))
    await callback.answer()


@router.callback_query(F.data == "page:add")
async def begin_add_page(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        target_count = await session.scalar(
            select(func.count(TargetPage.id)).where(
                TargetPage.user_id == db_user.telegram_id
            )
        )
    guard_message = _add_guard_message(db_user, int(target_count or 0))
    if guard_message:
        await callback.answer(guard_message, show_alert=True)
        return

    await state.set_state(AddPageState.waiting_for_username)
    if callback.message:
        await callback.message.answer(
            "نام کاربری پیج عمومی اینستاگرام را ارسال کنید.\nنمونه: <b>@instagram</b>",
            reply_markup=cancel_keyboard(),
        )
    await callback.answer()


@router.message(AddPageState.waiting_for_username, F.text)
async def add_page(
    message: Message,
    state: FSMContext,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    username = normalize_instagram_username(message.text or "")
    if username is None:
        await message.answer(
            "نام کاربری معتبر نیست. فقط حروف انگلیسی، عدد، نقطه و زیرخط مجاز است. دوباره تلاش کنید."
        )
        return

    try:
        async with session_factory() as session:
            user = await session.scalar(
                select(User)
                .where(User.telegram_id == db_user.telegram_id)
                .with_for_update()
            )
            if user is None:
                await message.answer(
                    "حساب کاربری شما پیدا نشد. لطفاً دستور /start را اجرا کنید."
                )
                return
            target_count = await session.scalar(
                select(func.count(TargetPage.id)).where(
                    TargetPage.user_id == user.telegram_id
                )
            )
            guard_message = _add_guard_message(user, int(target_count or 0))
            if guard_message:
                await state.clear()
                await message.answer(guard_message, reply_markup=main_menu_keyboard())
                return

            existing = await session.scalar(
                select(TargetPage.id).where(
                    TargetPage.user_id == user.telegram_id,
                    TargetPage.instagram_username == username,
                )
            )
            if existing is not None:
                await message.answer("این پیج قبلاً به فهرست شما اضافه شده است.")
                return

            target = TargetPage(instagram_username=username, user_id=user.telegram_id)
            session.add(target)
            await session.flush()
            session.add(
                NotificationSettings(user_id=user.telegram_id, target_page_id=target.id)
            )
            await session.commit()
    except IntegrityError:
        await message.answer("این پیج قبلاً به فهرست شما اضافه شده است.")
        return

    await state.clear()
    await message.answer(
        f"پیج <b>@{html.escape(username)}</b> با موفقیت اضافه شد. اولین وضعیت پس از بررسی بعدی ثبت می‌شود. ✅",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("page:view:"))
async def view_page(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    page_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        page = await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == db_user.telegram_id,
            )
        )
    if page is None:
        await callback.answer("این پیج پیدا نشد.", show_alert=True)
        return

    status_text = PAGE_STATUS_NAMES[page.last_known_status]
    text = (
        f"جزئیات پیج <b>@{html.escape(page.instagram_username)}</b>\n\n"
        f"وضعیت: {status_text}\n"
        f"آخرین بررسی: {format_datetime(page.last_checked_at)}\n"
        f"شناسه اینستاگرام: <code>{html.escape(page.last_known_id or 'ثبت نشده')}</code>"
    )
    if callback.message:
        await callback.message.edit_text(
            text, reply_markup=page_details_keyboard(page.id)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("page:delete:"))
async def ask_delete_page(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    page_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        page = await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == db_user.telegram_id,
            )
        )
    if page is None:
        await callback.answer("این پیج پیدا نشد.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            f"آیا از حذف <b>@{html.escape(page.instagram_username)}</b> مطمئن هستید؟",
            reply_markup=confirm_delete_keyboard(page.id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("page:confirm_delete:"))
async def delete_page(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    page_id = int((callback.data or "").rsplit(":", 1)[1])
    async with session_factory() as session:
        page = await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == db_user.telegram_id,
            )
        )
        if page is None:
            await callback.answer("این پیج قبلاً حذف شده است.", show_alert=True)
            return
        await session.delete(page)
        await session.commit()

    pages = await _load_pages(session_factory, db_user.telegram_id)
    if callback.message:
        await callback.message.edit_text(
            "پیج با موفقیت حذف شد. ✅",
            reply_markup=pages_keyboard(pages),
        )
    await callback.answer()


@router.message(F.text == "تنظیمات اعلان‌ها ⚙️")
async def notification_settings_menu(
    message: Message,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    pages = await _load_pages(session_factory, db_user.telegram_id)
    if not pages:
        await message.answer("ابتدا از بخش مدیریت پیج‌ها یک پیج اضافه کنید.")
        return
    await message.answer(
        "برای تنظیم جداگانه اعلان‌ها، یک پیج را انتخاب کنید:",
        reply_markup=settings_pages_keyboard(pages),
    )


async def _get_page_settings(
    session_factory: SessionFactory,
    user_id: int,
    page_id: int,
) -> tuple[TargetPage | None, NotificationSettings | None]:
    async with session_factory() as session:
        page = await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == user_id,
            )
        )
        if page is None:
            return None, None
        settings = await session.get(NotificationSettings, (user_id, page_id))
        if settings is None:
            settings = NotificationSettings(user_id=user_id, target_page_id=page_id)
            session.add(settings)
            await session.commit()
        return page, settings


@router.callback_query(F.data.startswith("settings:view:"))
async def view_notification_settings(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    page_id = int((callback.data or "").rsplit(":", 1)[1])
    page, settings = await _get_page_settings(
        session_factory, db_user.telegram_id, page_id
    )
    if page is None or settings is None:
        await callback.answer("این پیج پیدا نشد.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            f"تنظیم اعلان‌های <b>@{html.escape(page.instagram_username)}</b>\n\n"
            "برای فعال یا غیرفعال‌کردن هر اعلان، روی آن بزنید.",
            reply_markup=notification_settings_keyboard(page.id, settings),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("toggle:"))
async def toggle_notification(
    callback: CallbackQuery,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    _, page_id_raw, field = (callback.data or "").split(":", 2)
    page_id = int(page_id_raw)
    field_map = {
        "activation": "notify_activation",
        "deactivation": "notify_deactivation",
        "username": "notify_username_change",
    }
    attribute = field_map.get(field)
    if attribute is None:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return

    async with session_factory() as session:
        page = await session.scalar(
            select(TargetPage).where(
                TargetPage.id == page_id,
                TargetPage.user_id == db_user.telegram_id,
            )
        )
        if page is None:
            await callback.answer("این پیج پیدا نشد.", show_alert=True)
            return
        settings = await session.get(
            NotificationSettings, (db_user.telegram_id, page_id)
        )
        if settings is None:
            settings = NotificationSettings(
                user_id=db_user.telegram_id,
                target_page_id=page_id,
            )
            session.add(settings)
            await session.flush()
        setattr(settings, attribute, not getattr(settings, attribute))
        await session.commit()

    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=notification_settings_keyboard(page_id, settings)
        )
    await callback.answer("تنظیم اعلان ذخیره شد. ✅")


@router.message(F.text == "خرید اشتراک 💎")
async def subscription_menu(message: Message) -> None:
    await message.answer(
        "پلن‌های اشتراک فارستار وارنر 💎\n\n"
        "رایگان: پایش ۱ پیج\n"
        "پریمیوم: پایش تا ۱۰ پیج\n"
        "ویژه: پایش تا ۵۰ پیج\n\n"
        "برای ارتباط با مدیر و دریافت جزئیات، پلن موردنظر را انتخاب کنید.",
        reply_markup=subscription_keyboard(),
    )


@router.callback_query(F.data.startswith("subscription:request:"))
async def request_subscription(
    callback: CallbackQuery,
    db_user: User,
    bot: Bot,
    redis: Redis,
    settings: Settings,
) -> None:
    plan_value = (callback.data or "").rsplit(":", 1)[1]
    if plan_value not in {PlanTier.PREMIUM.value, PlanTier.VIP.value}:
        await callback.answer("پلن انتخاب‌شده معتبر نیست.", show_alert=True)
        return

    request_key = f"farstar:subscription-request:{db_user.telegram_id}:{plan_value}"
    accepted = await redis.set(request_key, "1", ex=3600, nx=True)
    if not accepted:
        await callback.answer(
            "درخواست شما قبلاً ثبت شده است. لطفاً منتظر پاسخ مدیر بمانید.",
            show_alert=True,
        )
        return

    plan = PlanTier(plan_value)
    username_text = f"@{html.escape(db_user.username)}" if db_user.username else "ندارد"
    await bot.send_message(
        settings.admin_telegram_id,
        "درخواست جدید خرید اشتراک 💎\n\n"
        f"شناسه کاربر: <code>{db_user.telegram_id}</code>\n"
        f"نام کاربری: {username_text}\n"
        f"پلن درخواستی: <b>{PLAN_NAMES[plan]}</b>",
    )
    await callback.answer("درخواست شما برای مدیر ارسال شد. ✅", show_alert=True)


@router.message(F.text == "حساب کاربری 👤")
async def account_info(
    message: Message,
    db_user: User,
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        target_count = await session.scalar(
            select(func.count(TargetPage.id)).where(
                TargetPage.user_id == db_user.telegram_id
            )
        )
    validity = (
        "فعال ✅"
        if db_user.subscription_expiry > datetime.now(timezone.utc)
        else "منقضی‌شده ❌"
    )
    await message.answer(
        "اطلاعات حساب کاربری 👤\n\n"
        f"شناسه تلگرام: <code>{to_persian_digits(db_user.telegram_id)}</code>\n"
        f"پلن: <b>{PLAN_NAMES[db_user.plan_tier]}</b>\n"
        f"وضعیت اشتراک: {validity}\n"
        f"تاریخ پایان: {format_datetime(db_user.subscription_expiry)}\n"
        f"تعداد پیج‌ها: {to_persian_digits(target_count or 0)} از "
        f"{to_persian_digits(db_user.plan_tier.target_limit)}",
        reply_markup=main_menu_keyboard(),
    )


@router.message()
async def unknown_message(message: Message) -> None:
    await message.answer(
        "متوجه درخواست شما نشدم. لطفاً یکی از گزینه‌های منوی اصلی را انتخاب کنید.",
        reply_markup=main_menu_keyboard(),
    )
