#!/usr/bin/env python3
import os
import sys
import re
import asyncio
import traceback
import requests
import instaloader

from datetime import datetime
from aiohttp import web
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

import nest_asyncio
nest_asyncio.apply()

# ── Configuration ────────────────────────────────────────────────────────────────
TOKEN        = os.getenv("TOKEN")
ADMIN_IDS    = [x.strip() for x in os.getenv("ADMIN_ID","").split(",") if x.strip()]
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")
PORT         = int(os.getenv("PORT","10000"))
DATABASE_URL = os.getenv("DATABASE_URL")
COOLDOWN_SEC = 60

# Instagram credentials (optional)
IG_USERNAME  = os.getenv("IG_USERNAME")
IG_PASSWORD  = os.getenv("IG_PASSWORD")
SESSION_FILE = f"{IG_USERNAME}.session" if IG_USERNAME else None

if not TOKEN or not DATABASE_URL:
    sys.exit("❌ TOKEN and DATABASE_URL must be set in your .env")

# Normalize Postgres URL to asyncpg
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Clear any old webhook
try:
    requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=true")
except:
    pass

# ── Instagram session ─────────────────────────────────────────────────────────────
INSTALOADER_SESSION = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_comments=False,
)
if IG_USERNAME and IG_PASSWORD:
    try:
        INSTALOADER_SESSION.load_session_from_file(IG_USERNAME, SESSION_FILE)
        print("🔒 Loaded Instagram session from file")
    except FileNotFoundError:
        try:
            INSTALOADER_SESSION.login(IG_USERNAME, IG_PASSWORD)
            INSTALOADER_SESSION.save_session_to_file(IG_USERNAME, SESSION_FILE)
            print("✅ Logged in & saved Instagram session")
        except Exception as e:
            print("⚠️ Instagram login failed:", e)

# ── Database setup ───────────────────────────────────────────────────────────────
engine = create_async_engine(DATABASE_URL, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

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

# ── Helpers ────────────────────────────────────────────────────────────────────────
def extract_shortcode(link: str) -> str | None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(uid: int) -> bool:
    val = str(uid)
    print(f"DEBUG is_admin? uid={val}, ADMIN_IDS={ADMIN_IDS}")
    return val in ADMIN_IDS

async def log_to_group(bot, msg: str):
    if LOG_GROUP_ID:
        try:
            await bot.send_message(chat_id=int(LOG_GROUP_ID), text=msg)
        except:
            pass

# ── Background view tracker ──────────────────────────────────────────────────────
async def track_all_views():
    loader = INSTALOADER_SESSION
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("SELECT id, shortcode FROM reels"))).all()
    for reel_id, code in rows:
        for _ in range(3):
            try:
                post = instaloader.Post.from_shortcode(loader.context, code)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                async with AsyncSessionLocal() as s2:
                    await s2.execute(
                        text("INSERT INTO views (reel_id, timestamp, count) VALUES (:r,:t,:c)"),
                        {"r": reel_id, "t": ts, "c": post.video_view_count}
                    )
                    await s2.commit()
                break
            except:
                await asyncio.sleep(2)

async def track_loop():
    await asyncio.sleep(5)
    while True:
        await track_all_views()
        await asyncio.sleep(12 * 3600)

# ── Health endpoint ───────────────────────────────────────────────────────────────
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def start_health():
    srv = web.Application()
    srv.router.add_get("/health", health)
    runner = web.AppRunner(srv)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ── Command Handlers ──────────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Welcome to ReelTracker — Your Instagram Reel Insights Partner\n\n"
        "/submit <Reel URL>  — Track a new reel’s view counts\n"
        "/stats              — See your tracked reels & latest views\n"
        "/remove <Reel URL>  — Stop tracking a previously submitted reel"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! Bot is active and ready.")

# … (other admin handlers: addaccount, removeaccount, userstats, adminstats, auditlog,
#      broadcast, deleteuser, deletereel — same as before) …

# ── Instagram login via bot ─────────────────────────────────────────────────────
IG_USER, IG_PASS, IG_2FA = range(3)

async def setig_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    await update.message.reply_text("🔑 Enter Instagram username:")
    return IG_USER

async def setig_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['ig_username'] = update.message.text.strip()
    await update.message.reply_text("🔒 Enter Instagram password:")
    return IG_PASS

async def setig_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['ig_password'] = update.message.text.strip()
    await update.message.reply_text(
        "🔐 If you have 2FA, enter the code now; otherwise send /skip"
    )
    return IG_2FA

async def setig_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    user = context.user_data['ig_username']
    pwd  = context.user_data['ig_password']
    try:
        INSTALOADER_SESSION.login(user, pwd)
        INSTALOADER_SESSION.two_factor_login(code)
        INSTALOADER_SESSION.save_session_to_file(user, f"{user}.session")
        await update.message.reply_text("✅ Logged in & saved session!")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Login failed: {e}")
    return ConversationHandler.END

async def setig_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = context.user_data['ig_username']
    pwd  = context.user_data['ig_password']
    try:
        INSTALOADER_SESSION.login(user, pwd)
        INSTALOADER_SESSION.save_session_to_file(user, f"{user}.session")
        await update.message.reply_text("✅ Logged in (no 2FA) & saved session!")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Login failed: {e}")
    return ConversationHandler.END

async def setig_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ IG login cancelled.")
    return ConversationHandler.END

async def removeig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return await update.message.reply_text("❌ Not authorized.")
    if not IG_USERNAME:
        return await update.message.reply_text("⚠️ No IG credentials set.")
    try:
        os.remove(f"{IG_USERNAME}.session")
        global INSTALOADER_SESSION
        INSTALOADER_SESSION = instaloader.Instaloader()
        await update.message.reply_text("✅ Instagram session removed.")
    except OSError:
        await update.message.reply_text("⚠️ No session file found.")
        
# ── Error Handler ────────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    await log_to_group(app.bot, f"❗️ Unhandled error:\n<pre>{tb}</pre>")

# ── Main Entrypoint ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    loop.create_task(start_health())
    loop.create_task(track_loop())

    app = ApplicationBuilder().token(TOKEN).build()

    # user & admin
    app.add_handler(CommandHandler("start",        start_cmd))
    app.add_handler(CommandHandler("ping",         ping))
    # … register other handlers …

    # Instagram login conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("setig", setig_start)],
        states={
            IG_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, setig_user)],
            IG_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setig_pass)],
            IG_2FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, setig_2fa),
                CommandHandler("skip", setig_skip),
            ],
        },
        fallbacks=[CommandHandler("cancel", setig_cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("removeig", removeig))

    app.add_error_handler(error_handler)

    print("🤖 Bot running in polling mode…")
    app.run_polling(drop_pending_updates=True, close_loop=False)
