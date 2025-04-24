import os
import sys
import re
import asyncio
import nest_asyncio
import instaloader
import traceback
import requests
from datetime import datetime
from aiohttp import web
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

# ── Patch asyncio for hosted environments ───────────────────────────────────────
nest_asyncio.apply()

# ── Configuration ────────────────────────────────────────────────────────────────
TOKEN        = os.getenv("TOKEN")
ADMIN_ID     = os.getenv("ADMIN_ID")
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")
PORT         = int(os.getenv("PORT", "10000"))
DATABASE_URL = os.getenv("DATABASE_URL")
COOLDOWN_SEC = 60  # seconds between /submit

if not TOKEN or not DATABASE_URL:
    sys.exit("❌ You must set TOKEN and DATABASE_URL in your .env")

# Ensure asyncpg driver
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# ── Delete any old webhook so polling won’t conflict ─────────────────────────────
try:
    requests.get(
        f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=true"
    )
except:
    pass

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
      id         SERIAL PRIMARY KEY,
      user_id    INTEGER,
      shortcode  TEXT,
      username   TEXT,
      UNIQUE(user_id, shortcode)
    );
    CREATE TABLE IF NOT EXISTS views (
      reel_id    INTEGER,
      timestamp  TEXT,
      count      INTEGER
    );
    CREATE TABLE IF NOT EXISTS cooldowns (
      user_id     INTEGER PRIMARY KEY,
      last_submit TEXT
    );
    CREATE TABLE IF NOT EXISTS audit (
      id          SERIAL PRIMARY KEY,
      user_id     INTEGER,
      action      TEXT,
      shortcode   TEXT,
      timestamp   TEXT
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
        rows = (await session.execute(text("SELECT id, shortcode FROM reels"))).all()
    for reel_id, code in rows:
        for _ in range(3):
            try:
                post = instaloader.Post.from_shortcode(L.context, code)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                async with AsyncSessionLocal() as session2:
                    await session2.execute(
                        text(
                            "INSERT INTO views (reel_id, timestamp, count) "
                            "VALUES (:r, :t, :c)"
                        ),
                        {"r": reel_id, "t": ts, "c": post.video_view_count}
                    )
                    await session2.commit()
                break
            except:
                await asyncio.sleep(2)

async def track_loop():
    await asyncio.sleep(5)
    while True:
        await track_all_views()
        await asyncio.sleep(12 * 3600)

# ── Health Endpoint ──────────────────────────────────────────────────────────────
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def start_health():
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ── Telegram Handlers ────────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(
            "👋 Hello! I’m your Reel Tracker Bot.\n"
            "Available commands:\n"
            "/ping — check if I’m alive\n"
            "/userstats <tg_id> — stats for a user (admin only)\n"
            "/submit <Reel URL> — submit a reel to track\n"
            "/stats — your stats\n"
            "/remove <Reel URL> — stop tracking a reel\n"
            "Admin only:\n"
            "/adminstats — download all users’ stats\n"
            "/auditlog — recent activity log\n"
             "/addaccount <tg_id> @insta — assign Instagram account(s)\n"
            "/removeaccount <tg_id> @insta — remove assigned account\n"
            "/broadcast <msg> — send to all users\n"
            "/deleteuser <tg_id> — remove all data for a user\n"
            "/deletereel <shortcode> — remove a reel globally"
        )
    except Exception as e:
        await update.message.reply_text("⚠️ Unable to process /start. Admin notified.")
        await log_to_group(context.bot, f"Error in /start: {e}")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("🏓 Pong! Bot is active and ready.")
    except Exception as e:
        await update.message.reply_text("⚠️ /ping failed. Admin notified.")
        await log_to_group(context.bot, f"Error in /ping: {e}")

async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not is_admin(update.effective_user.id) or len(context.args) != 2:
            return await update.message.reply_text("❌ Usage: /addaccount <tg_id> @insta_handle")
        target, handle = context.args
        if not handle.startswith('@'):
            return await update.message.reply_text("❌ Instagram handle must start with '@'.")
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("INSERT OR IGNORE INTO user_accounts (user_id, insta_handle) VALUES (:u, :h)"),
                {"u": int(target), "h": handle}
            )
            await session.commit()
        await update.message.reply_text(f"✅ Assigned {handle} to user {target}.")
        await log_to_group(context.bot, f"Admin @{update.effective_user.username} assigned {handle} to {target}")
    except Exception as e:
        await update.message.reply_text("⚠️ Could not assign account. Admin notified.")
        await log_to_group(context.bot, f"Error in /addaccount: {e}")

async def removeaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not is_admin(update.effective_user.id) or len(context.args) != 2:
            return await update.message.reply_text("❌ Usage: /removeaccount <tg_id> @insta_handle")
        target, handle = context.args
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                text("DELETE FROM user_accounts WHERE user_id=:u AND insta_handle=:h RETURNING *"),
                {"u": int(target), "h": handle}
            )
            await session.commit()
        if res.rowcount:
            await update.message.reply_text(f"✅ Removed {handle} from user {target}.")
            await log_to_group(context.bot, f"Admin @{update.effective_user.username} removed {handle} from {target}")
        else:
            await update.message.reply_text("⚠️ No such account assignment found.")
    except Exception as e:
        await update.message.reply_text("⚠️ Could not remove account. Admin notified.")
        await log_to_group(context.bot, f"Error in /removeaccount: {e}")

# (Other handlers: userstats, submit, stats, remove, adminstats, auditlog, broadcast,
#  deleteuser, deletereel — implement similarly with try/except, professional messages,
#  and log_to_group on key actions.)

# ── Main Entrypoint ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    loop.create_task(start_health())
    loop.create_task(track_loop())

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",      start_cmd))
    app.add_handler(CommandHandler("ping",       ping))
    app.add_handler(CommandHandler("addaccount", addaccount))
    app.add_handler(CommandHandler("removeaccount", removeaccount))
    # ... register other handlers here ...

    # Global error logger
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
        tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
        await log_to_group(app.bot, f"❗️ Unhandled error:\n<pre>{tb}</pre>")

    app.add_error_handler(error_handler)

    print("🤖 Bot running in polling mode…")
    app.run_polling(drop_pending_updates=True, close_loop=False)
