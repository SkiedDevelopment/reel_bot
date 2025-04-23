import os
import sys
import re
import asyncio
import nest_asyncio
import instaloader
import traceback
from datetime import datetime
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

# ── Patch asyncio for hosted environments ───────────────────────────────────────
nest_asyncio.apply()

# ── Configuration ────────────────────────────────────────────────────────────────
TOKEN         = os.getenv("TOKEN")
ADMIN_ID      = os.getenv("ADMIN_ID")
LOG_GROUP_ID  = os.getenv("LOG_GROUP_ID")
WEBHOOK_URL   = os.getenv("WEBHOOK_URL")
PORT          = int(os.getenv("PORT", "10000"))
DATABASE_URL  = os.getenv("DATABASE_URL")
COOLDOWN_SEC  = 60

if not DATABASE_URL:
    sys.exit("❌ Missing DATABASE_URL environment variable!")

# Rewrite both postgres:// and postgresql:// to asyncpg URL
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# ── SQLAlchemy Async Setup ──────────────────────────────────────────────────────
engine = create_async_engine(DATABASE_URL, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ── Helpers ──────────────────────────────────────────────────────────────────────
def extract_shortcode(link: str) -> str | None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(uid: int) -> bool:
    return ADMIN_ID and str(uid) == str(ADMIN_ID)

async def log_to_group(bot, text: str):
    if LOG_GROUP_ID:
        try:
            await bot.send_message(chat_id=int(LOG_GROUP_ID), text=text)
        except:
            pass

# ── Database Initialization ──────────────────────────────────────────────────────
async def init_db():
    ddl = """
    CREATE TABLE IF NOT EXISTS users (
      user_id   INTEGER PRIMARY KEY,
      username  TEXT
    );
    CREATE TABLE IF NOT EXISTS user_accounts (
      user_id      INTEGER,
      insta_handle TEXT,
      PRIMARY KEY (user_id, insta_handle)
    );
    CREATE TABLE IF NOT EXISTS reels (
      id        INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id   INTEGER,
      shortcode TEXT,
      username  TEXT,
      UNIQUE(user_id, shortcode)
    );
    CREATE TABLE IF NOT EXISTS views (
      reel_id   INTEGER,
      timestamp TEXT,
      count     INTEGER
    );
    CREATE TABLE IF NOT EXISTS cooldowns (
      user_id     INTEGER PRIMARY KEY,
      last_submit TEXT
    );
    CREATE TABLE IF NOT EXISTS audit (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id      INTEGER,
      action       TEXT,
      shortcode    TEXT,
      timestamp    TEXT
    );
    """
    async with engine.begin() as conn:
        for stmt in ddl.split(";"):
            s = stmt.strip()
            if s:
                await conn.execute(text(s))

# ── Background View Tracking ────────────────────────────────────────────────────
async def track_all_views():
    L = instaloader.Instaloader()
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT id, shortcode FROM reels"))
        rows = result.all()
    for reel_id, code in rows:
        for _ in range(3):
            try:
                post = instaloader.Post.from_shortcode(L.context, code)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        text("INSERT INTO views (reel_id, timestamp, count) VALUES (:r, :t, :c)"),
                        {"r": reel_id, "t": ts, "c": post.video_view_count}
                    )
                    await session.commit()
                break
            except:
                await asyncio.sleep(2)

async def track_loop():
    await asyncio.sleep(5)
    while True:
        await track_all_views()
        await asyncio.sleep(12*3600)

# ── Health Check Endpoint ───────────────────────────────────────────────────────
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

# ── Command Handlers ─────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\n"
        "/addaccount <tg_id> @insta   → assign Instagram account(s)\n"
        "/userstats <tg_id>           → view that user’s stats\n"
        "/submit <Reel URL>           → submit a reel (60s cooldown)\n"
        "/stats                       → your stats\n"
        "/remove <Reel URL>           → remove a reel\n"
        "Admin only:\n"
        "/adminstats /auditlog /broadcast /deleteuser /deletereel"
    )

# … [all other handlers stay the same as before] …

# ── Bootstrap & Webhook Startup ─────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(init_db())
    app = ApplicationBuilder().token(TOKEN).build()

    # mount health endpoint
    app._web_app.router.add_get("/health", health)

    # add all CommandHandlers…
    # app.add_handler(CommandHandler("start", start))
    # … etc …

    app.add_error_handler(lambda u, c: asyncio.create_task(log_to_group(app.bot, f"❗️ Error\n<pre>{c.error}</pre>")))

    asyncio.get_event_loop().create_task(track_loop())

    print("🤖 Running in webhook mode…")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        close_loop=False
    )
