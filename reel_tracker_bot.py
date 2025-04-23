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
WEBHOOK_URL   = os.getenv("WEBHOOK_URL")    # e.g. "https://<your-service>.onrender.com/"
PORT          = int(os.getenv("PORT", "10000"))
DATABASE_URL  = os.getenv("DATABASE_URL")
COOLDOWN_SEC  = 60  # seconds

if not TOKEN or not DATABASE_URL or not WEBHOOK_URL:
    sys.exit("❌ Missing one of TOKEN, DATABASE_URL, or WEBHOOK_URL env vars!")

# ensure asyncpg driver
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
                        text("INSERT INTO views (reel_id, timestamp, count) VALUES (:r, :t, :c)"),
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

# ── Health Check Endpoint ───────────────────────────────────────────────────────
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def _add_health_route(app):
    app.web_app.router.add_get("/health", health)

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

async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or len(context.args) != 2:
        return await update.message.reply_text("Usage: /addaccount <tg_id> @insta_handle")
    target, handle = context.args
    if not handle.startswith('@'):
        return await update.message.reply_text("Handle must start with '@'")
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("INSERT OR IGNORE INTO user_accounts (user_id, insta_handle) VALUES (:u, :h)"),
            {"u": int(target), "h": handle}
        )
        await session.commit()
    await update.message.reply_text(f"✅ Assigned {handle} to user {target}")
    await log_to_group(context.bot, f"Admin @{update.effective_user.username} assigned {handle} to user {target}")

async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or len(context.args) != 1:
        return await update.message.reply_text("Usage: /userstats <tg_id>")
    target = int(context.args[0])
    async with AsyncSessionLocal() as session:
        handles = [r[0] for r in (await session.execute(text("SELECT insta_handle FROM user_accounts WHERE user_id=:u"), {"u": target})).all()]
        reels   = (await session.execute(text("SELECT id, shortcode FROM reels WHERE user_id=:u"), {"u": target})).all()
    total_views = 0
    details = []
    for rid, code in reels:
        row = (await session.execute(text("SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"), {"r": rid})).fetchone()
        cnt = row[0] if row else 0
        total_views += cnt
        details.append((code, cnt))
    details.sort(key=lambda x: x[1], reverse=True)
    lines = [
        f"Stats for {target}:",
        f"• Instagram: {', '.join(handles) or 'None'}",
        f"• Total videos: {len(reels)}",
        f"• Total views: {total_views}",
        "Reels (high→low):",
    ]
    for i, (code, cnt) in enumerate(details, 1):
        lines.append(f"{i}. https://instagram.com/reel/{code} – {cnt} views")
    await update.message.reply_text("\n".join(lines))
    await log_to_group(context.bot, f"Admin @{update.effective_user.username} viewed stats for {target}")

async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Usage: /submit <Reel URL>")

    now = datetime.now()
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text("SELECT last_submit FROM cooldowns WHERE user_id=:u"), {"u": uid})).fetchone()
        if row:
            last = datetime.fromisoformat(row[0])
            rem = COOLDOWN_SEC - (now - last).total_seconds()
            if rem > 0:
                msg = await update.message.reply_text(f"⏱ Wait {int(rem)}s.")
                async def _del():
                    await asyncio.sleep(5)
                    await context.bot.delete_message(update.effective_chat.id, msg.message_id)
                asyncio.create_task(_del())
                return
        await session.execute(text("INSERT OR REPLACE INTO cooldowns (user_id, last_submit) VALUES (:u, :t)"), {"u": uid, "t": now.isoformat()})
        await session.commit()

    code = extract_shortcode(context.args[0])
    if not code:
        return await update.message.reply_text("❌ Invalid Reel URL.")

    async with AsyncSessionLocal() as session:
        allowed = [h[0].lstrip('@').lower() for h in (await session.execute(text("SELECT insta_handle FROM user_accounts WHERE user_id=:u"), {"u": uid})).all()]
    if not allowed:
        return await update.message.reply_text("⚠️ No account assigned. Ask admin.")

    L = instaloader.Instaloader()
    try:
        post = instaloader.Post.from_shortcode(L.context, code)
    except:
        return await update.message.reply_text("⚠️ Fetch failed; must be public.")
    if post.owner_username.lower() not in allowed:
        return await update.message.reply_text(f"❌ Not from your accounts: {', '.join('@'+a for a in allowed)}")

    views0 = post.video_view_count
    ts_str = now.strftime("%Y-%m-%d %H:%M:%S")

    async with AsyncSessionLocal() as session:
        await session.execute(text("INSERT OR REPLACE INTO users (user_id, username) VALUES (:u, :n)"), {"u": uid, "n": update.effective_user.username or ""})
        try:
            await session.execute(text("INSERT INTO reels (user_id, shortcode, username) VALUES (:u, :c, :n)"), {"u": uid, "c": code, "n": post.owner_username})
            await session.execute(text(
                "INSERT INTO views (reel_id, timestamp, count) VALUES ("
                "(SELECT id FROM reels WHERE user_id=:u AND shortcode=:c), :t, :v)"
            ), {"u": uid, "c": code, "t": ts_str, "v": views0})
            await session.execute(text("INSERT INTO audit (user_id, action, shortcode, timestamp) VALUES (:u, 'submitted', :c, :t)"), {"u": uid, "c": code, "t": ts_str})
            await session.commit()
            await update.message.reply_text(f"✅ @{post.owner_username} submitted ({views0} views).")
            await log_to_group(context.bot, f"User @{update.effective_user.username} submitted {code}")
        except:
            await update.message.reply_text("⚠️ Already submitted.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # similar to userstats but for self...
    pass

async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # implement remove...
    pass

async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # implement adminstats...
    pass

async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # implement auditlog...
    pass

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # implement broadcast...
    pass

async def deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # implement deleteuser...
    pass

async def deletereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # implement deletereel...
    pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    await log_to_group(context.bot, f"❗️ Error\n<pre>{tb}</pre>")

# ── Bootstrap & Webhook Startup ─────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(init_db())

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(_add_health_route)
        .build()
    )

    # register your handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addaccount", addaccount))
    app.add_handler(CommandHandler("userstats", userstats))
    app.add_handler(CommandHandler("submit", submit))
    # ... the rest ...

    app.add_error_handler(error_handler)

    asyncio.get_event_loop().create_task(track_loop())

    print("🤖 Running in webhook mode…")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        close_loop=False,
    )
