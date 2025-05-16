import os
import re
import asyncio
import logging
import requests

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from fastapi import FastAPI
import uvicorn

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, String, BigInteger, text

# ─── Load config ─────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = set(map(int, os.getenv("ADMIN_ID", "").split(",")))
PORT = int(os.getenv("PORT", 8000))
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", 0))
ENSEMBLE_TOKEN = os.getenv("ENSEMBLE_TOKEN")

if not all([TOKEN, DATABASE_URL, ENSEMBLE_TOKEN]):
    print("❌ TOKEN, DATABASE_URL, and ENSEMBLE_TOKEN must be set in .env")
    exit(1)

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── FastAPI health check ──────────────────────────────────────────────────────
app_fastapi = FastAPI()

@app_fastapi.get("/")
async def root():
    return {"message": "Bot is running 🚀"}

async def start_health_check_server():
    config = uvicorn.Config(app_fastapi, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

# ─── Database setup ───────────────────────────────────────────────────────────
Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_views BIGINT DEFAULT 0"
        ))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS allowed_accounts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                insta_handle VARCHAR NOT NULL
            )
        """))

# ─── ORM models ───────────────────────────────────────────────────────────────
class Reel(Base):
    __tablename__ = "reels"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    shortcode = Column(String, nullable=False)

class User(Base):
    __tablename__ = "users"
    user_id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    registered = Column(Integer, default=0)
    total_views = Column(BigInteger, default=0)

# ─── Utilities ─────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def debug_handler(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if LOG_GROUP_ID and update.message:
            user = update.effective_user
            name = user.full_name
            handle = f"@{user.username}" if user.username else ""
            text = update.message.text or ""
            try:
                await context.bot.send_message(LOG_GROUP_ID, f"{name} {handle}: {text}")
            except Exception:
                logger.warning("Failed to send log message")
        try:
            return await fn(update, context)
        except Exception as e:
            logger.exception("Handler error")
            if update.message:
                await update.message.reply_text(f"⚠️ Error: {e}")
            raise
    return wrapper

async def get_reel_data(shortcode: str) -> dict:
    """Get reel data from EnsembleData API"""
    try:
        api_url = 'https://ensembledata.com/apis/instagram/user/reels'
        params = {
            'depth': 1,
            'include_feed_video': True,
            'token': ENSEMBLE_TOKEN
        }
        
        response = requests.get(api_url, params=params)
        response.raise_for_status()
        
        data = response.json()
        if not data.get('data', {}).get('reels'):
            raise Exception("No reels found")
            
        # Find the specific reel by shortcode
        reels = data['data']['reels']
        for reel in reels:
            if reel['media']['code'] == shortcode:
                return {
                    'owner_username': reel['media']['user']['username'],
                    'view_count': reel['media'].get('view_count', 0),
                    'play_count': reel['media'].get('play_count', 0)
                }
                
        raise Exception("Reel not found")
    except Exception as e:
        raise Exception(f"Error fetching reel data: {str(e)}")

# ─── Telegram Handlers ─────────────────────────────────────────────────────────
@debug_handler
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmds = [
        "👋 <b>Welcome to Reel Tracker Bot!</b>",
        "",
        "📋 <b>Commands:</b>",
        "• <code>/addreel &lt;link&gt;</code> - Submits your reel to track",
        "• <code>/removelink &lt;shortcode or URL&gt;</code> - Removes an added reel",
        "• <code>/stats</code> - Shows your total vids, views, and links",
    ]
    if is_admin(update.effective_user.id):
        cmds += [
            "• <code>/addaccount &lt;user_id&gt; &lt;@handle&gt;</code> - Link an IG account",
            "• <code>/removeaccount &lt;user_id&gt;</code> - Unlink an IG account",
            "• <code>/userstats &lt;user_id&gt;</code> - Stats for a specific user",
            "• <code>/allstats</code> - Lists reels for all users",
            "• <code>/broadcast_all &lt;message&gt;</code> - Send message to all users",
            "• <code>/broadcast &lt;user_id&gt; &lt;message&gt;</code> - Send message to one user",
            "• <code>/clearreels</code> - Clear all reel links",
            "• <code>/addviews &lt;user_id&gt; &lt;views&gt;</code> - Add manual views",
            "• <code>/removeviews &lt;user_id&gt; &lt;views&gt;</code> - Remove manual views",
            "• <code>/exportstats</code> - Export stats.txt for all users",
        ]
    await update.message.reply_text("\n".join(cmds), parse_mode=ParseMode.HTML)

@debug_handler
async def addreel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❗ Provide a reel link.")
    raw = context.args[0]
    m = re.search(r"^(?:https?://)?(?:www\.|m\.)?instagram\.com/(?:(?P<sup>[^/]+)/)?reel/(?P<code>[^/?#&]+)", raw)
    if not m:
        return await update.message.reply_text("❌ Invalid reel URL.")
    sup, code = m.group("sup"), m.group("code")
    uid = update.effective_user.id
    
    async with AsyncSessionLocal() as s:
        acc = (await s.execute(text("SELECT insta_handle FROM allowed_accounts WHERE user_id=:u"), {"u": uid})).fetchone()
        if not acc:
            return await update.message.reply_text("🚫 No IG linked—ask admin to /addaccount.")
        expected = acc[0]
        
        try:
            reel_data = await get_reel_data(code)
            if reel_data['owner_username'].lower() != expected.lower():
                return await update.message.reply_text(f"🚫 That reel belongs to @{reel_data['owner_username']}.")
        except Exception as e:
            return await update.message.reply_text(f"❌ {str(e)}")
            
        dup = (await s.execute(text("SELECT 1 FROM reels WHERE shortcode=:c"), {"c": code})).scalar()
        if dup:
            return await update.message.reply_text("⚠️ Already added.")
            
        await s.execute(text("INSERT INTO reels(user_id,shortcode) VALUES(:u,:c)"), {"u": uid, "c": code})
        await s.commit()
        
    await update.message.reply_text("✅ Reel added!")

# ... rest of the handlers remain the same ...

async def run_bot():
    # Initialize database
    await init_db()
    
    # Start health check server
    asyncio.create_task(start_health_check_server())
    
    # Start bot
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addreel", addreel))
    app.add_handler(CommandHandler("removelink", removereel))
    app.add_handler(CommandHandler("stats", stats))
    
    if ADMIN_IDS:
        app.add_handler(CommandHandler("addaccount", addaccount))
        app.add_handler(CommandHandler("removeaccount", removeaccount))
        app.add_handler(CommandHandler("userstats", userstats))
        app.add_handler(CommandHandler("allstats", allstats))
        app.add_handler(CommandHandler("broadcast_all", broadcast_all))
        app.add_handler(CommandHandler("broadcast", broadcast))
        app.add_handler(CommandHandler("clearreels", clearreels))
        app.add_handler(CommandHandler("addviews", addviews))
        app.add_handler(CommandHandler("removeviews", removeviews))
        app.add_handler(CommandHandler("exportstats", exportstats))
    
    # Start polling
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(run_bot()) 