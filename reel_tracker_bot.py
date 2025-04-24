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
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

# ── Patch asyncio for hosted environments ───────────────────────────────────────
import nest_asyncio; nest_asyncio.apply()

# ── Configuration ────────────────────────────────────────────────────────────────
TOKEN        = os.getenv("TOKEN")
ADMIN_IDS    = [aid.strip() for aid in os.getenv("ADMIN_ID","").split(",") if aid.strip()]
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")
PORT         = int(os.getenv("PORT","10000"))
DATABASE_URL = os.getenv("DATABASE_URL")
COOLDOWN_SEC = 60

if not TOKEN or not DATABASE_URL:
    sys.exit("❌ TOKEN and DATABASE_URL must be set in your .env")

# normalize asyncpg URL
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# clear old webhook
try:
    requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=true")
except:
    pass

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

# ── Helpers ──────────────────────────────────────────────────────────────────────
def extract_shortcode(link: str) -> str | None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(uid: int) -> bool:
    return str(uid) in ADMIN_IDS

async def log_to_group(bot, text: str):
    if LOG_GROUP_ID:
        try:
            await bot.send_message(chat_id=int(LOG_GROUP_ID), text=text)
        except:
            pass

# ── Background View Tracking ────────────────────────────────────────────────────
async def track_all_views():
    L = instaloader.Instaloader()
    async with AsyncSessionLocal() as session:
        reels = (await session.execute(text("SELECT id, shortcode FROM reels"))).all()
    for reel_id, shortcode in reels:
        for _ in range(3):
            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                async with AsyncSessionLocal() as s2:
                    await s2.execute(
                        text("INSERT INTO views (reel_id, timestamp, count) VALUES (:r, :t, :c)"),
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

# ── Health Endpoint ─────────────────────────────────────────────────────────────
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def start_health():
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ── Command Handlers ─────────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to ReelTracker!\n"
        "Use /ping to check I’m alive.\n\n"
        "User commands:\n"
        "/submit <Reel URL>\n"
        "/stats\n"
        "/remove <Reel URL>\n\n"
        "Admin commands:\n"
        "/addaccount <tg_id> @handle\n"
        "/removeaccount <tg_id> @handle\n"
        "/userstats <tg_id>\n"
        "/adminstats\n"
        "/auditlog\n"
        "/broadcast <msg>\n"
        "/deleteuser <tg_id>\n"
        "/deletereel <shortcode>"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! Bot is active and ready.")

async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return await update.message.reply_text("❌ You’re not authorized.")
    if len(context.args) != 2:
        return await update.message.reply_text("❌ Usage: /addaccount <tg_id> @handle")
    target, handle = context.args
    if not handle.startswith("@"):
        return await update.message.reply_text("❌ Handle must start with '@'.")
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(
                text("INSERT OR IGNORE INTO user_accounts (user_id, insta_handle) VALUES (:u, :h)"),
                {"u": int(target), "h": handle}
            )
            await s.commit()
        await update.message.reply_text(f"✅ Assigned {handle} to user {target}.")
        await log_to_group(context.bot, f"Admin @{user.username} assigned {handle} to {target}")
    except Exception:
        await update.message.reply_text("⚠️ Couldn’t assign account—admin notified.")
        tb = traceback.format_exc()
        await log_to_group(context.bot, f"Error in /addaccount:\n<pre>{tb}</pre>")

async def removeaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return await update.message.reply_text("❌ You’re not authorized.")
    if len(context.args) != 2:
        return await update.message.reply_text("❌ Usage: /removeaccount <tg_id> @handle")
    target, handle = context.args
    try:
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                text("DELETE FROM user_accounts WHERE user_id=:u AND insta_handle=:h RETURNING *"),
                {"u": int(target), "h": handle}
            )
            await s.commit()
        if res.rowcount:
            await update.message.reply_text(f"✅ Removed {handle} from user {target}.")
            await log_to_group(context.bot, f"Admin @{user.username} removed {handle} from {target}")
        else:
            await update.message.reply_text("⚠️ No such assignment found.")
    except Exception:
        await update.message.reply_text("⚠️ Couldn’t remove account—admin notified.")
        tb = traceback.format_exc()
        await log_to_group(context.bot, f"Error in /removeaccount:\n<pre>{tb}</pre>")

async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id) or len(context.args) != 1:
        return await update.message.reply_text("❌ Usage: /userstats <tg_id>")
    target = int(context.args[0])
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            text("SELECT insta_handle FROM user_accounts WHERE user_id=:u"),
            {"u": target}
        )
        handles = [r[0] for r in rows.all()]
        rows2 = await s.execute(
            text("SELECT id, shortcode FROM reels WHERE user_id=:u"),
            {"u": target}
        )
        reels = rows2.all()
    total_views = 0
    details = []
    for rid, shortcode in reels:
        row = (await s.execute(
            text("SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"),
            {"r": rid}
        )).fetchone()
        cnt = row[0] if row else 0
        total_views += cnt
        details.append((shortcode, cnt))
    details.sort(key=lambda x: x[1], reverse=True)
    lines = [
        f"Stats for {target}:",
        f"• Accounts: {', '.join(handles) or 'None'}",
        f"• Videos: {len(reels)}",
        f"• Views: {total_views}",
        "Reels (high→low):"
    ]
    for i, (code, cnt) in enumerate(details, 1):
        lines.append(f"{i}. https://instagram.com/reel/{code} – {cnt} views")
    await update.message.reply_text("\n".join(lines))

# … (submit, stats, remove, adminstats, auditlog, broadcast, deleteuser,
#    deletereel remain unchanged from previous version; omitted here for brevity)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    await log_to_group(app.bot, f"❗️ Unhandled error:\n<pre>{tb}</pre>")

# ── Main Entrypoint ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    loop.create_task(start_health())
    loop.create_task(track_loop())

    app = ApplicationBuilder().token(TOKEN).build()
    # register handlers
    app.add_handler(CommandHandler("start",       start_cmd))
    app.add_handler(CommandHandler("ping",        ping))
    app.add_handler(CommandHandler("addaccount",  addaccount))
    app.add_handler(CommandHandler("removeaccount",removeaccount))
    app.add_handler(CommandHandler("userstats",   userstats))
    # … register the rest …
    app.add_error_handler(error_handler)

    print("🤖 Bot running in polling mode…")
    app.run_polling(drop_pending_updates=True, close_loop=False)
