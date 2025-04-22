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
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# Patch asyncio so run_webhook/run_polling works in Render
nest_asyncio.apply()

# States for our conversations
SUBMIT_LINK   = 0
REMOVE_SELECT = 1

# Configuration from ENV
TOKEN       = os.getenv("TOKEN")
ADMIN_ID    = os.getenv("ADMIN_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT        = int(os.getenv("PORT", "10000"))
DB_FILE     = "reels.db"


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

def extract_shortcode(link: str) -> str|None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(user_id: int) -> bool:
    return ADMIN_ID and str(user_id) == str(ADMIN_ID)

# --- Tracking loop (unchanged) ---
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
        await asyncio.sleep(12 * 3600)


# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\n"
        "/submit → track a reel\n"
        "/stats  → view your stats\n"
        "/remove → delete a reel\n"
        "Admin commands available if you’re admin."
    )

# 1) /submit conversation entry
async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Send me the **public Instagram reel** URL:")
    return SUBMIT_LINK

# 2) handle the link
async def submit_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    code = extract_shortcode(link)
    if not code:
        await update.message.reply_text("❌ That doesn’t look like a reel URL. Try /submit again.")
        return ConversationHandler.END

    # fetch data
    L = instaloader.Instaloader()
    try:
        post = instaloader.Post.from_shortcode(L.context, code)
    except Exception:
        await update.message.reply_text("⚠️ Failed to fetch reel. Make sure it’s **public**.")
        return ConversationHandler.END

    uid      = update.effective_user.id
    username = post.owner_username
    views0   = post.video_view_count

    # store in DB
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
            await update.message.reply_text(f"✅ @{username} submitted with {views0} views!")
        except aiosqlite.IntegrityError:
            await update.message.reply_text("⚠️ You already submitted that reel.")
    return ConversationHandler.END

# /stats (unchanged)
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        cur   = await db.execute("SELECT id, username FROM reels WHERE user_id=?", (uid,))
        reels = await cur.fetchall()
    if not reels:
        return await update.message.reply_text("📭 No reels tracked yet.")

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
        f"📊 Videos: {len(reels)}\n"
        f"📈 Views:  {total}\n"
        f"👤 Accounts: {', '.join(users)}"
    )

# 1) /remove conversation entry
async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        cur   = await db.execute("SELECT shortcode, username FROM reels WHERE user_id=?", (uid,))
        reels = await cur.fetchall()

    if not reels:
        await update.message.reply_text("❌ You have no reels to remove.")
        return ConversationHandler.END

    text = "🗑️ Your reels:\n" + "\n".join(f"- {sc} (@{u})" for sc,u in reels)
    text += "\n\nReply with the **shortcode** to delete:"
    await update.message.reply_text(text)
    return REMOVE_SELECT

# 2) handle the removal
async def remove_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sc = update.message.text.strip()
    uid = update.effective_user.id
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
    return ConversationHandler.END

# (Admin handlers unchanged...)

# ── Bootstrap & Webhook Startup ─────────────────────────────────────────────────
if __name__ == "__main__":
    # 1) init database
    asyncio.get_event_loop().run_until_complete(init_db())

    # 2) build application
    app = ApplicationBuilder().token(TOKEN).build()

    # 3) register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("submit", submit_start)],
        states={ SUBMIT_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, submit_received)] },
        fallbacks=[]
    ))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("remove", remove_start)],
        states={ REMOVE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_received)] },
        fallbacks=[]
    ))
    # ... register your admin handlers here ...

    # 4) start background tracker
    asyncio.get_event_loop().create_task(track_loop())

    # 5) launch webhook
    print("🤖 Running in webhook mode…")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        close_loop=False
    )
