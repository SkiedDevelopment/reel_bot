```python
import os
import re
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update
from telegram.constants import ParseMode
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, String, BigInteger, text
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
import uvicorn

# Load environment variables
load_dotenv()

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_ID", "").split(",")))
ZYTE_API_KEY = os.getenv("ZYTE_API_KEY")  # Zyte API key
PORT = int(os.getenv("PORT", 8000))
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", 60))

if not all([TOKEN, DATABASE_URL, ZYTE_API_KEY]):
    print("❌ Must set TOKEN, DATABASE_URL, and ZYTE_API_KEY in .env")
    exit(1)

# FastAPI health check
app_fastapi = FastAPI()

@app_fastapi.get("/")
async def root():
    return {"message": "Bot is running 🚀"}

async def start_health_check_server():
    config = uvicorn.Config(app_fastapi, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

# Logging setup
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Error wrapper
def debug_handler(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception as e:
            if update.message:
                await update.message.reply_text(f"⚠️ Error: {e}")
            raise
    return wrapper

# Database setup
Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db():
    async with engine.begin() as conn:
        # Create tables if not exist
        await conn.run_sync(Base.metadata.create_all)
        # Add missing 'last_views' column if needed
        await conn.execute(text(
            "ALTER TABLE reels ADD COLUMN IF NOT EXISTS last_views BIGINT DEFAULT 0"
        ))

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

# Admin check
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# Zyte scraping functions
def scrape_instagram_reel_views(shortcode: str) -> int:
    try:
        url = f"https://www.instagram.com/reel/{shortcode}/"
        response = httpx.get(
            "https://api.zyte.com/v1/extract",
            params={
                "apikey": ZYTE_API_KEY,
                "url": url,
                "render_js": "true"
            },
            timeout=30
        )
        response.raise_for_status()
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script"):
            if script.string and '"video_view_count"' in script.string:
                text_block = script.string
                start = text_block.find('"video_view_count":') + len('"video_view_count":')
                end = text_block.find(',', start)
                return int(text_block[start:end])
        return -1
    except Exception as e:
        logger.error(f"Zyte scraping error: {e}")
        return -1

# Cooldown tracking
user_cooldowns = {}

def can_use_command(user_id: int) -> bool:
    now = datetime.utcnow()
    last = user_cooldowns.get(user_id)
    if not last or (now - last).total_seconds() >= COOLDOWN_SEC:
        user_cooldowns[user_id] = now
        return True
    return False

async def fetch_reel_page(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://api.zyte.com/v1/extract",
                params={
                    "apikey": ZYTE_API_KEY,
                    "url": url,
                    "render_js": "true"
                }
            )
            resp.raise_for_status()
            return {"status_code": resp.status_code, "content": resp.text}
    except Exception as e:
        logger.error(f"Async Zyte fetch error: {e}")
        return None

# Telegram command handlers
@debug_handler
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    commands = (
        "👋 Welcome to *Reel Tracker Bot*!\n"
        "\n📋 *Available Commands:*\n"
        "/addreel <link> - Add a reel to track 🎯\n"
        "/myreels - View your tracked reels 📋\n"
        "/stats - Show your stats 📊\n"
        "/leaderboard - Global leaderboard 🏆\n"
        "/forceupdate - Force refresh views 🔄\n"
        "/checkapi - Check Zyte API status 🛠️\n"
        "/userstatsid <user_id> - Admin: Stats by user ID 🔍\n"
        "/auditlog - Admin: Download activity log 📂\n"
        "/deleteuser <user_id> - Admin: Remove user's reels 🚫\n"
        "/deletereel <shortcode> - Admin: Remove a reel ❌\n"
        "/broadcast <message> - Admin: Broadcast a message 📣"
    )
    await update.message.reply_text(commands, parse_mode=ParseMode.MARKDOWN)

# ... rest of handlers unchanged ...

# Bot setup and main
app = Application.builder().token(TOKEN).build()
# add handlers...

async def main():
    # Initialize DB with migrations
    await init_db()
    # Start health-check server
    asyncio.create_task(start_health_check_server())
    # Start bot
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    print("Bot is running 🚀")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
```
