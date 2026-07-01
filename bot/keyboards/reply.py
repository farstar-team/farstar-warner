from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_menu_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [
            KeyboardButton(text="مدیریت پیج‌ها 📊"),
            KeyboardButton(text="خرید اشتراک 💎"),
        ],
        [
            KeyboardButton(text="تنظیمات اعلان‌ها ⚙️"),
            KeyboardButton(text="حساب کاربری 👤"),
        ],
    ]
    if is_admin:
        keyboard.append([KeyboardButton(text="پنل مدیریت 🛡️")])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="یک گزینه را انتخاب کنید",
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="لغو عملیات ↩️")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
