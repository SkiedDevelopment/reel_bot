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
TOKEN         = os.getenv("TOKEN")
ADMIN_ID      = os.getenv("ADMIN_ID")
LOG_GROUP_ID  = os.getenv("LOG_GROUP_ID")
PORT          = int(os.getenv("PORT", "10000"))
DATABASE_URL  = os.getenv("DATABASE_URL")
COOLDOWN_SEC  = 60  # seconds

if not TOKEN or not DATABASE_URL:
    sys.exit("❌ Missing TOKEN or DATABASE_URL environment variable!")

# Ensure we use asyncpg
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# ── SQLAlchemy Async Setup ──────────────────────────────────────────────────────
engine = create_async_engine(DATABASE_URL, future=True)
AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

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

# ── Health Endpoint ─────────────────────────────────────────────────────────────
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def start_health():
    """Start a tiny aiohttp server serving /health."""
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ── Telegram Command Handlers ────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or len(context.args) != 2:
        return await update.message.reply_text(
            "Usage: /addaccount <tg_id> @insta_handle"
        )
    target, handle = context.args
    if not handle.startswith('@'):
        return await update.message.reply_text("Handle must start with '@'")
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT OR IGNORE INTO user_accounts "
                "(user_id, insta_handle) VALUES (:u, :h)"
            ),
            {"u": int(target), "h": handle}
        )
        await session.commit()
    await update.message.reply_text(f"✅ Assigned {handle} to user {target}")
    await log_to_group(
        context.bot,
        f"Admin @{update.effective_user.username} assigned {handle} to user {target}"
    )

async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or len(context.args) != 1:
        return await update.message.reply_text("Usage: /userstats <tg_id>")
    target = int(context.args[0])
    async with AsyncSessionLocal() as session:
        handles = [r[0] for r in (await session.execute(
            text("SELECT insta_handle FROM user_accounts WHERE user_id=:u"),
            {"u": target}
        )).all()]
        reels = (await session.execute(
            text("SELECT id, shortcode FROM reels WHERE user_id=:u"),
            {"u": target}
        )).all()
    total_views = 0
    details = []
    for rid, code in reels:
        row = (await session.execute(
            text(
                "SELECT count FROM views "
                "WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
            ),
            {"r": rid}
        )).fetchone()
        cnt = row[0] if row else 0
        total_views += cnt
        details.append((code, cnt))
    details.sort(key=lambda x: x[1], reverse=True)
    text_lines = [
        f"Stats for user {target}:",
        f"• Instagram: {', '.join(handles) or 'None'}",
        f"• Total videos: {len(reels)}",
        f"• Total views: {total_views}",
        "Reels (highest→lowest):",
    ]
    for i, (code, cnt) in enumerate(details, 1):
        text_lines.append(f"{i}. https://instagram.com/reel/{code} – {cnt} views")
    await update.message.reply_text("\n".join(text_lines))
    await log_to_group(
        context.bot,
        f"Admin @{update.effective_user.username} viewed stats for {target}"
    )

# ... Implement submit, stats, remove, adminstats, auditlog, broadcast,
# deleteuser, deletereel, and error_handler in the same style as above ...

# ── Main Entrypoint ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    loop = asyncio.get_event_loop()

    # 1) Initialize the database
    loop.run_until_complete(init_db())

    # 2) Start health-check server
    loop.create_task(start_health())

    # 3) Start background tracking
    loop.create_task(track_loop())

    # 4) Build Telegram application
    app = ApplicationBuilder().token(TOKEN).build()

    # 5) Register command handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addaccount", addaccount))
    app.add_handler(CommandHandler("userstats", userstats))
    # ... add the rest of your handlers here ...

    # 6) Clear any existing webhook so polling can start without conflicts
    loop.run_until_complete(
        app.bot.delete_webhook(drop_pending_updates=True)
    )

    # 7) Global error logger
    app.add_error_handler(lambda u, c: asyncio.create_task(
        log_to_group(app.bot, f"❗️ Error\n<pre>{c.error}</pre>")
    ))

    # 8) Start long-polling
    print("🤖 Bot running in polling mode…")
    app.run_polling(drop_pending_updates=True, close_loop=False)
