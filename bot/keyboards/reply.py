from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

def get_main_menu() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="📊 مدیریت پیج‌ها"), KeyboardButton(text="💎 خرید/تمدید اشتراک")],
        [KeyboardButton(text="⚙️ تنظیمات اعلان‌ها"), KeyboardButton(text="👤 حساب کاربری")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, is_persistent=True)
