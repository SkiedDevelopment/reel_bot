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
    ContextTypes,
    filters,
)

# ── Patch asyncio for hosted envs ───────────────────────────────────────────────
nest_asyncio.apply()

# ── Config from ENV ─────────────────────────────────────────────────────────────
TOKEN       = os.getenv("TOKEN")
ADMIN_ID    = os.getenv("ADMIN_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. "https://your-app.onrender.com/"
PORT        = int(os.getenv("PORT", "10000"))
DB_FILE     = "reels.db"

# Conversation states
SUBMIT_LINK = 0
REMOVE_LINK = 1


# ── Database Init ────────────────────────────────────────────────────────────────
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


# ── Helpers ─────────────────────────────────────────────────────────────────────
def extract_shortcode(link: str) -> str | None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(user_id: int) -> bool:
    try:
        return ADMIN_ID is not None and int(user_id) == int(ADMIN_ID)
    except:
        return False


# ── View Tracking Loop ──────────────────────────────────────────────────────────
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
    # slight startup delay
    await asyncio.sleep(5)
    while True:
        await track_all_views()
        await asyncio.sleep(12 * 3600)  # 12 hours


# ── Command Handlers ────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\n"
        "/submit → track a reel\n"
        "/stats  → view your stats\n"
        "/remove → delete a reel\n"
        "Admin commands available if you’re admin."
    )


# ── /submit Conversation ────────────────────────────────────────────────────────
async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Send me the public Instagram *Reel* URL:")
    return SUBMIT_LINK

async def submit_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    code = extract_shortcode(link)
    if not code:
        await update.message.reply_text("❌ Invalid Reel URL. Please try /submit again.")
        return ConversationHandler.END

    L = instaloader.Instaloader()
    try:
        post = instaloader.Post.from_shortcode(L.context, code)
    except Exception:
        await update.message.reply_text("⚠️ Couldn't fetch—make sure it's public.")
        return ConversationHandler.END

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
            await update.message.reply_text(f"✅ @{username} submitted ({views0} views).")
        except aiosqlite.IntegrityError:
            await update.message.reply_text("⚠️ You've already submitted that Reel.")
    return ConversationHandler.END


# ── /stats Command ──────────────────────────────────────────────────────────────
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
                "SELECT count FROM views WHERE reel_id=? ORDER BY timestamp DESC LIMIT 1",
                (rid,)
            )
            row = await vcur.fetchone()
            if row:
                total += row[0]

    await update.message.reply_text(
        f"📊 Videos: {len(reels)}\n"
        f"📈 Views:  {total}\n"
        f"👤 Accounts: {', '.join(users)}"
    )


# ── /remove Conversation ───────────────────────────────────────────────────────
async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🗑️ Send the *full* Reel URL to remove:")
    return REMOVE_LINK

async def remove_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    code = extract_shortcode(link)
    uid  = update.effective_user.id

    if not code:
        await update.message.reply_text("❌ Invalid Reel URL. Cancelled.")
        return ConversationHandler.END

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT id FROM reels WHERE user_id=? AND shortcode=?", (uid, code)
        )
        row = await cur.fetchone()
        if not row:
            await update.message.reply_text("❌ You never submitted that Reel.")
            return ConversationHandler.END

        reel_id = row[0]
        await db.execute("DELETE FROM views WHERE reel_id=?", (reel_id,))
        await db.execute("DELETE FROM reels WHERE id=?", (reel_id,))

        # if no more reels, remove user record
        cur2 = await db.execute("SELECT COUNT(*) FROM reels WHERE user_id=?", (uid,))
        rem  = (await cur2.fetchone())[0]
        if rem == 0:
            await db.execute("DELETE FROM users WHERE user_id=?", (uid,))

        await db.commit()

    await update.message.reply_text(f"✅ Removed Reel `{code}`.")
    return ConversationHandler.END


# ── Admin Command Handlers ─────────────────────────────────────────────────────
async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    async with aiosqlite.connect(DB_FILE) as db:
        ucur = await db.execute("SELECT COUNT(*) FROM users")
        rcur = await db.execute("SELECT COUNT(*) FROM reels")
        total_users = (await ucur.fetchone())[0]
        total_reels = (await rcur.fetchone())[0]

        top = await db.execute(
            "SELECT username, COUNT(*) AS c FROM reels GROUP BY username ORDER BY c DESC LIMIT 5"
        )
        tops = await top.fetchall()

    msg = (
        f"🛠️ Admin Stats:\n"
        f"• Users: {total_users}\n"
        f"• Reels: {total_reels}\n\n"
        "Top IG Accounts:\n" +
        "\n".join(f"– @{u}: {c}" for u, c in tops)
    )
    await update.message.reply_text(msg)


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /broadcast <message>")
    text = "📢 " + " ".join(context.args)
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id FROM users")
        for (uid,) in await cur.fetchall():
            try:
                await context.bot.send_message(chat_id=uid, text=text)
            except:
                pass
    await update.message.reply_text("✅ Broadcast sent.")


async def deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return await update.message.reply_text("Usage: /deleteuser <telegram_id>")
    targ = context.args[0]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE user_id=?)", (targ,)
        )
        await db.execute("DELETE FROM reels WHERE user_id=?", (targ,))
        await db.execute("DELETE FROM users WHERE user_id=?", (targ,))
        await db.commit()
    await update.message.reply_text(f"🧹 Deleted user {targ}.")


async def deletereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return await update.message.reply_text("Usage: /deletereel <shortcode>")
    sc = context.args[0]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE shortcode=?)", (sc,)
        )
        await db.execute("DELETE FROM reels WHERE shortcode=?", (sc,))
        await db.commit()
    await update.message.reply_text(f"✅ Deleted reel `{sc}`.")


# ── Bootstrap & Webhook Launch ─────────────────────────────────────────────────
if __name__ == "__main__":
    # 1) Initialize the database
    asyncio.get_event_loop().run_until_complete(init_db())

    # 2) Build bot application
    app = ApplicationBuilder().token(TOKEN).build()

    # 3) Register user handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("submit", submit_start)],
        states={ SUBMIT_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, submit_received)] },
        fallbacks=[]
    ))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("remove", remove_start)],
        states={ REMOVE_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_received)] },
        fallbacks=[]
    ))

    # 4) Register admin handlers
    app.add_handler(CommandHandler("adminstats", adminstats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("deleteuser", deleteuser))
    app.add_handler(CommandHandler("deletereel", deletereel))

    # 5) Start background view‑tracking
    asyncio.get_event_loop().create_task(track_loop())

    # 6) Launch webhook (blocks here)
    print("🤖 Running in webhook mode…")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        close_loop=False
    )
