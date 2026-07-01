import httpx
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from bot.models import TargetPage, User
from aiogram import Bot

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "X-IG-App-ID": "936619743392459",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.instagram.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin"
}

async def check_single_instagram_page(username: str) -> dict:
    """درخواست ناهمگام به ای‌پ‌آی وب اینستاگرام با پروتکل HTTP/2"""
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    custom_headers = HEADERS.copy()
    custom_headers["Referer"] = f"https://www.instagram.com/{username}/"
    
    async with httpx.AsyncClient(http2=True, follow_redirects=False, timeout=15.0) as client:
        try:
            response = await client.get(url, headers=custom_headers)
            
            if response.status_code == 200:
                data = response.json()
                user_data = data.get("data", {}).get("user")
                if not user_data:
                    return {"status": "deactivated"}
                
                return {
                    "status": "active",
                    "id": user_data.get("id"),
                    "full_name": user_data.get("full_name"),
                    "followers": user_data.get("edge_followed_by", {}).get("count", 0),
                    "following": user_data.get("edge_follow", {}).get("count", 0),
                    "posts": user_data.get("edge_owner_to_timeline_media", {}).get("count", 0),
                }
            elif response.status_code == 404:
                return {"status": "deactivated"}
            else:
                logger.warning(f"Unexpected status {response.status_code} for {username}")
                return {"status": "error", "code": response.status_code}
                
        except Exception as e:
            logger.error(f"Error checking {username}: {str(e)}")
            return {"status": "error", "msg": str(e)}

async def run_monitoring_cycle(session_factory, bot: Bot):
    """بررسی تمام پیج‌های سیستم و ارسال اعلان‌های تغییرات به زبان فارسی"""
    async with session_factory() as session:
        result = await session.execute(select(TargetPage))
        pages = result.scalars().all()
        
        for page in pages:
            res = await check_single_instagram_page(page.instagram_username)
            
            if res["status"] == "error":
                continue
                
            # سناریوی اول: پیج دی‌اکتیو شده است
            if res["status"] == "deactivated":
                if page.last_known_status != "deactivated":
                    page.last_known_status = "deactivated"
                    await session.commit()
                    
                    alert_text = f"⚠️ **اعلان مانیتورینگ Farstar**\n\nپیج اینستاگرامی **@{page.instagram_username}** دی‌اکتیو یا حذف شده است! 🔴"
                    try:
                        await bot.send_message(page.user_id, alert_text, parse_mode="Markdown")
                    except Exception:
                        pass
                        
            # سناریوی دوم: پیج فعال و آنلاین است
            elif res["status"] == "active":
                # الف) پیج قبلاً دی‌اکتیو بوده و الان فعال شده
                if page.last_known_status == "deactivated":
                    page.last_known_status = "active"
                    alert_text = f"🎉 **اعلان مانیتورینگ Farstar**\n\nپیج **@{page.instagram_username}** دوباره فعال و اکتیو شد! 🟢"
                    try:
                        await bot.send_message(page.user_id, alert_text, parse_mode="Markdown")
                    except Exception:
                        pass
                
                # ب) بررسی تغییرات آمار پیج (فالوور، پست و نام)
                changes = []
                if page.follower_count and res["followers"] != page.follower_count:
                    diff = res["followers"] - page.follower_count
                    sign = "+" if diff > 0 else ""
                    changes.append(f"👥 تغییر فالوور: {page.follower_count} ➡️ {res['followers']} ({sign}{diff})")
                    
                if page.post_count and res["posts"] != page.post_count:
                    changes.append(f"📸 تعداد پست‌ها: {page.post_count} ➡️ {res['posts']}")
                    
                if page.full_name and res["full_name"] != page.full_name:
                    changes.append(f"🔄 تغییر نام اصلی: {page.full_name} ➡️ {res['full_name']}")
                
                # بروزرسانی مقادیر جدید در دیتابیس
                page.last_known_status = "active"
                page.instagram_id = res["id"]
                page.follower_count = res["followers"]
                page.following_count = res["following"]
                page.post_count = res["posts"]
                page.full_name = res["full_name"]
                await session.commit()
                
                # ارسال گزارش تغییرات آماری در صورت وجود
                if changes:
                    changes_str = "\n".join(changes)
                    report_text = f"📊 **بروزرسانی پیج @{page.instagram_username}**\n\n{changes_str}"
                    try:
                        await bot.send_message(page.user_id, report_text, parse_mode="Markdown")
                    except Exception:
                        pass
