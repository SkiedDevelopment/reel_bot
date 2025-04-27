import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram.ext import Application
from telegram import Update, Document
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, String, BigInteger, select, update, delete, text
from sqlalchemy.orm import sessionmaker

from fastapi import FastAPI
import uvicorn

import aiohttp
from aiohttp import ClientSession

# Load .env variables
load_dotenv()

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_ID", "").split(",")))
SCRAPINGBEE_API_KEY = os.getenv("SCRAPINGBEE_API_KEY")
PORT = int(os.getenv("PORT", 8000))
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", 60))

if not all([TOKEN, DATABASE_URL, SCRAPINGBEE_API_KEY]):
    print("❌ TOKEN, DATABASE_URL, and SCRAPINGBEE_API_KEY must be set in .env")
    exit(1)

app_fastapi = FastAPI()

@app_fastapi.get("/")
async def root():
    return {"message": "Bot is running 🚀"}

async def start_health_check_server():
    config = uvicorn.Config(app_fastapi, host="0.0.0.0", port=int(os.getenv("PORT", 10000)), log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
    
# Setup Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Debughandler
def debug_handler(func):
    async def wrapper(update, context):
        try:
            return await func(update, context)
        except Exception as e:
            if update.message:
                await update.message.reply_text(f"⚠️ Error: {e}")
            raise e
    return wrapper

# Database Setup
Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db():
    """Create all tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

class Reel(Base):
    __tablename__ = 'reels'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    shortcode = Column(String, nullable=False)
    last_views = Column(BigInteger, default=0)


class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    registered = Column(Integer, default=0)

fastapi_app = FastAPI()

@fastapi_app.get("/")
async def health():
    return {"status": "Bot is healthy! ✅"}

def is_admin(user_id: int) -> bool:
    return str(user_id) in str(ADMIN_ID)

async def scrape_instagram_reel_views(shortcode: str) -> int:
    try:
        url = f"https://www.instagram.com/reel/{shortcode}/"
        response = requests.get(
            "https://app.scrapingbee.com/api/v1/",
            params={
                "api_key": SCRAPINGBEE_API_KEY,
                "url": url,
                "render_js": "True",
                "block_resources": "False"
            },
            timeout=30,
        )
        if response.status_code != 200:
            return -1
        
        soup = BeautifulSoup(response.text, "html.parser")
        for script_tag in soup.find_all("script"):
            if script_tag.string and "video_view_count" in script_tag.string:
                text = script_tag.string
                start_index = text.find('"video_view_count":') + len('"video_view_count":')
                end_index = text.find(",", start_index)
                views = int(text[start_index:end_index])
                return views
        return -1
    except Exception as e:
        print(f"Error scraping: {e}")
        return -1

user_cooldowns = {}

def can_use_command(user_id: int) -> bool:
    now = datetime.utcnow()
    if user_id not in user_cooldowns:
        user_cooldowns[user_id] = now
        return True
    elapsed = (now - user_cooldowns[user_id]).total_seconds()
    if elapsed >= COOLDOWN_SEC:
        user_cooldowns[user_id] = now
        return True
    return False

async def fetch_reel_page(url: str) -> dict:
    """Fetch reel data using ScrapingBee API."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://app.scrapingbee.com/api/v1/",
                params={
                    "api_key": SCRAPINGBEE_API_KEY,
                    "url": url,
                    "render_js": "true"
                }
            )
            response.raise_for_status()
            return {"status_code": response.status_code, "content": response.text}
    except Exception as e:
        logger.error(f"Error fetching reel page: {e}")
        return None

# /start
@debug_handler
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    commands = """
👋 Welcome to *Reel Tracker Bot*!

📋 *Available Commands:*
/addreel <link> - Add a reel to track 🎯
/mystats - View your reels and views 📊
/leaderboard - Top users based on views 🏆
/forceupdate - Force refresh views manually 🔄
/checkapi - Check API status 🛠️
/userstatsid <user_id> - Admin: View stats by ID 🔍
/auditlog - Admin: Download full activity log 📂
/deleteuser <user_id> - Admin: Remove a user 🚫
/deletereel <shortcode> - Admin: Remove a reel ❌
"""
    await update.message.reply_text(commands, parse_mode=ParseMode.MARKDOWN)

# /addreel
@debug_handler
async def addreel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        return await update.message.reply_text("❗ Please provide a reel link.")
    
    link = context.args[0]
    match = re.search(r"/reel/([^/?]+)/?", link)
    if not match:
        return await update.message.reply_text("❌ Invalid reel link provided.")

    shortcode = match.group(1)

    async with async_session() as session:
        exists = await session.execute(text("SELECT id FROM reels WHERE shortcode=:s"), {"s": shortcode})
        if exists.first():
            return await update.message.reply_text("⚠️ This reel is already being tracked.")
        
        session.add(Reel(user_id=user.id, shortcode=shortcode, last_views=0))
        await session.commit()
    
    await update.message.reply_text("✅ Reel successfully added for tracking!")

# / myreels
@debug_handler
async def myreels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's added reels."""
    user_id = update.effective_user.id
    async with async_session() as session:
        result = await session.execute(text("SELECT shortcode FROM reels WHERE user_id = :uid"), {"uid": user_id})
        reels = result.scalars().all()
    if reels:
        msg = "\n".join(f"https://www.instagram.com/reel/{r}/" for r in reels)
        await update.message.reply_text(f"🎥 **Your Reels:**\n\n{msg}", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("😔 You have no reels added yet.")

# /checkapi
@debug_handler
async def check_scrapingbee_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check if ScrapingBee API can fetch Instagram pages."""
    sample_reel = "https://www.instagram.com/reel/Cx9L5JkNkfJ/"
    result = await fetch_reel_page(sample_reel)
    
    if result and result.get("status_code") == 200:
        await update.message.reply_text("✅ ScrapingBee API is working properly!")
    else:
        await update.message.reply_text("❌ ScrapingBee API might be blocked or over limit.")

# /mystats
@debug_handler
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's overall statistics."""
    user_id = update.effective_user.id
    async with async_session() as session:
        result = await session.execute(text("SELECT COUNT(*), COALESCE(SUM(last_views), 0) FROM reels WHERE user_id = :uid"), {"uid": user_id})
        count, total_views = result.fetchone()
    
    await update.message.reply_text(
        f"📊 **Your Stats:**\n\n"
        f"🔹 Total Reels: {count}\n"
        f"🔹 Total Views: {total_views}",
        parse_mode=ParseMode.MARKDOWN
    )

# /leaderboard
@debug_handler
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        users = (await session.execute(text("SELECT user_id, SUM(last_views) as total_views FROM reels GROUP BY user_id ORDER BY total_views DESC"))).all()
        if not users:
            return await update.message.reply_text("🏁 No data available.")
        
        lines = []
        for i, (user_id, total_views) in enumerate(users, 1):
            lines.append(f"{i}. User ID {user_id}: *{total_views}* views")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

#/forceupdate
@debug_handler
async def forceupdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not can_use_command(user.id):
        return await update.message.reply_text(f"🕑 Cooldown active. Please wait {COOLDOWN_SEC} seconds.")

    progress = await update.message.reply_text("🔄 Updating all your reels...")
    async with async_session() as session:
        reels = (await session.execute(text("SELECT id, shortcode FROM reels WHERE user_id=:u"), {"u": user.id})).all()
        if not reels:
            return await progress.edit_text("❗ No reels found.")

        updated_count = 0
        for reel_id, shortcode in reels:
            views = await scrape_instagram_reel_views(shortcode)
            if views != -1:
                await session.execute(text("UPDATE reels SET last_views=:v WHERE id=:i"), {"v": views, "i": reel_id})
                updated_count += 1
            await asyncio.sleep(1)  # slow down scraping
        await session.commit()
    
    await progress.edit_text(f"✅ Updated {updated_count} reels!")

# /userstatsid
@debug_handler
async def userstatsid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 You are not authorized to use this command.")
    
    if not context.args:
        return await update.message.reply_text("❗ Please provide a User ID.")
    
    user_id = context.args[0]
    async with async_session() as session:
        reels = (await session.execute(text("SELECT shortcode, last_views FROM reels WHERE user_id=:u"), {"u": user_id})).all()
        if not reels:
            return await update.message.reply_text("❗ No reels found for that user.")
        
        lines = [f"https://www.instagram.com/reel/{shortcode} ➔ *{views}* views" for shortcode, views in sorted(reels, key=lambda x: x[1], reverse=True)]
        await update.message.reply_text(f"📄 Stats for User ID {user_id}:\n\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# /Auditlog
@debug_handler
async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 You are not authorized to use this command.")
    
    async with async_session() as session:
        users = (await session.execute(text("SELECT DISTINCT user_id FROM reels"))).all()
        
        report = []
        for (user_id,) in users:
            reels = (await session.execute(text("SELECT shortcode, last_views FROM reels WHERE user_id=:u"), {"u": user_id})).all()
            total_views = sum(view for _, view in reels)
            report.append(f"User ID: {user_id}\nTotal Reels: {len(reels)}\nTotal Views: {total_views}\nTop Reels:")
            for shortcode, views in sorted(reels, key=lambda x: x[1], reverse=True):
                report.append(f"  https://www.instagram.com/reel/{shortcode} ➔ {views} views")
            report.append("\n")

        path = f"audit_log_{int(time.time())}.txt"
        with open(path, "w") as f:
            f.write("\n".join(report))
        
        await update.message.reply_document(open(path, "rb"))
        os.remove(path)

# /deleteuser
@debug_handler
async def deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 You are not authorized to use this command.")

    if not context.args:
        return await update.message.reply_text("❗ Provide a User ID to delete.")

    user_id = context.args[0]
    async with async_session() as session:
        await session.execute(text("DELETE FROM reels WHERE user_id=:u"), {"u": user_id})
        await session.commit()

    await update.message.reply_text(f"✅ Deleted all reels for User ID {user_id}.")
    
# /deletereel
@debug_handler
async def deletereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 You are not authorized to use this command.")

    if not context.args:
        return await update.message.reply_text("❗ Provide a shortcode to delete.")

    shortcode = context.args[0]
    async with async_session() as session:
        await session.execute(text("DELETE FROM reels WHERE shortcode=:s"), {"s": shortcode})
        await session.commit()

    await update.message.reply_text(f"✅ Deleted reel with shortcode {shortcode}.")

# /broadcast
@debug_handler
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 You are not authorized to use this command.")

    if not context.args:
        return await update.message.reply_text("❗ Provide a message to broadcast.")

    message = " ".join(context.args)
    async with async_session() as session:
        users = (await session.execute(text("SELECT DISTINCT user_id FROM reels"))).all()
        for (user_id,) in users:
            try:
                await context.bot.send_message(chat_id=user_id, text=message)
            except:
                continue
    await update.message.reply_text("✅ Broadcast sent successfully.")

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start_cmd))
app.add_handler(CommandHandler("add", addreel))
app.add_handler(CommandHandler("myreels", myreels))
app.add_handler(CommandHandler("deleteuser", deleteuser))
app.add_handler(CommandHandler("deletereel", deletereel))
app.add_handler(CommandHandler("stats", stats))
app.add_handler(CommandHandler("leaderboard", leaderboard))
app.add_handler(CommandHandler("userstatsid", userstatsid))
app.add_handler(CommandHandler("forceupdate", forceupdate))
app.add_handler(CommandHandler("auditlog", auditlog))
app.add_handler(CommandHandler("broadcast", broadcast))
app.add_handler(CommandHandler("checkapi", check_scraper_api))


async def main():
    # Start health check server in background
    asyncio.create_task(start_health_check_server())
    
    # Start Telegram Bot polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    print("Bot is running 🚀")
    
    # Keep running forever
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

