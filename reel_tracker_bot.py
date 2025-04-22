import os
import re
import asyncio
import nest_asyncio
import instaloader
import aiosqlite
from datetime import datetime
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# Patch asyncio so we can mix run_webhook in hosted envs
nest_asyncio.apply()

# ── Configuration ─────────────────────────────────────────────────────────────
TOKEN       = os.getenv("TOKEN")
ADMIN_ID    = os.getenv("ADMIN_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. "https://your-app.onrender.com/"
PORT        = int(os.getenv("PORT", "10000"))
DB_FILE     = "reels.db"

# ── Database Initialization ────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reels (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER,
                shortcode TEXT,
                username  TEXT,
                UNIQUE(user_id, shortcode)
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS views (
                reel_id   INTEGER,
                timestamp TEXT,
                count     INTEGER
            )""")
        await db.commit()

# ── Helpers ────────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return ADMIN_ID and str(user_id) == str(ADMIN_ID)

def extract_shortcode(link: str) -> str|None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

# ── View Tracking ──────────────────────────────────────────────────────────────
async def track_all_views():
    L = instaloader.Instaloader()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT id, shortcode FROM reels") as cur:
            for reel_id, shortcode in await cur.fetchall():
                for attempt in range(3):
                    try:
                        post = instaloader.Post.from_shortcode(L.context, shortcode)
                        now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        views = post.video_view_count
                        await db.execute(
                            "INSERT INTO views (reel_id, timestamp, count) VALUES (?, ?, ?)",
                            (reel_id, now, views)
                        )
                        await db.commit()
                        break
                    except Exception as e:
                        print(f"[Retry {attempt+1}] {shortcode} error: {e}")
                        await asyncio.sleep(2)

async def track_loop():
    await asyncio.sleep(5)
    while True:
        await track_all_views()
        await asyncio.sleep(12 * 3600)  # every 12 hours

# ── Command Handlers ──────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! /submit to add a reel, /stats to view your stats, /remove to delete."
    )

async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me the public Instagram reel URL:")
    try:
        msg = await context.bot.wait_for_message(
            chat_id=update.effective_chat.id, timeout=60
        )
    except asyncio.TimeoutError:
        return await update.message.reply_text("⏰ Timeout—please try /submit again.")

    link = msg.text or ""
    code = extract_shortcode(link)
    if not code:
        return await update.message.reply_text("❌ Invalid reel link.")

    L = instaloader.Instaloader()
    try:
        post = instaloader.Post.from_shortcode(L.context, code)
    except Exception:
        return await update.message.reply_text("⚠️ Couldn't fetch—ensure it's a public reel.")

    uid      = update.effective_user.id
    username = post.owner_username
    views0   = post.video_view_count

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        try:
            await db.execute(
                "INSERT INTO reels (user_id, shortcode, username) VALUES (?, ?, ?)",
                (uid, code, username)
            )
            await db.execute(
                "INSERT INTO views (reel_id, timestamp, count) "
                "VALUES ((SELECT id FROM reels WHERE user_id=? AND shortcode=?), ?, ?)",
                (uid, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), views0)
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            return await update.message.reply_text("⚠️ You've already submitted this reel.")

    await update.message.reply_text(f"✅ @{username} submitted with {views0} views.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        cur   = await db.execute("SELECT id, username FROM reels WHERE user_id=?", (uid,))
        reels = await cur.fetchall()
    if not reels:
        return await update.message.reply_text("📭 You have no submitted reels.")

    total, users = 0, set()
    async with aiosqlite.connect(DB_FILE) as db:
        for rid, uname in reels:
            users.add(uname)
            vcur = await db.execute(
                "SELECT count FROM views WHERE reel_id=? ORDER BY timestamp DESC LIMIT 1", (rid,)
            )
            row = await vcur.fetchone()
            if row:
                total += row[0]

    await update.message.reply_text(
        f"📊 Total Videos: {len(reels)}\n"
        f"📈 Total Views: {total}\n"
        f"👤 Accounts: {', '.join(users)}"
    )

async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        cur   = await db.execute("SELECT shortcode, username FROM reels WHERE user_id=?", (uid,))
        reels = await cur.fetchall()
    if not reels:
        return await update.message.reply_text("❌ No reels to remove.")

    msg = "🗑️ Your reels:\n" + "\n".join(f"- {sc} (@{u})" for sc,u in reels)
    msg += "\n\nReply with the shortcode to delete:"
    await update.message.reply_text(msg)

    try:
        reply = await context.bot.wait_for_message(chat_id=uid, timeout=60)
    except asyncio.TimeoutError:
        return await update.message.reply_text("⏰ Timeout.")

    sc = (reply.text or "").strip()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE user_id=? AND shortcode=?)",
            (uid, sc)
        )
        await db.execute(
            "DELETE FROM reels WHERE user_id=? AND shortcode=?", (uid, sc)
        )
        await db.commit()
    await update.message.reply_text(f"✅ Removed `{sc}`.")

# ── Admin Commands (unchanged logic) ───────────────────────────────────────────
# ... adminstats, broadcast, deleteuser, deletereel as before ...

# ── Bootstrap & Webhook Startup ───────────────────────────────────────────────
if __name__ == "__main__":
    # 1) Initialize DB
    asyncio.get_event_loop().run_until_complete(init_db())

    # 2) Build the application
    app = ApplicationBuilder().token(TOKEN).build()

    # 3) Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("submit", submit))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("remove", remove))
    # register admin handlers here...

    # 4) Start background tracking
    asyncio.get_event_loop().create_task(track_loop())

    # 5) Launch webhook (blocks here)
    print("🤖 Running in webhook mode…")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        close_loop=False
    )
