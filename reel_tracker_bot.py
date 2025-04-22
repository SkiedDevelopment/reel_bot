import os
import re
import asyncio
import nest_asyncio
import instaloader
import aiosqlite
import traceback
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
TOKEN         = os.getenv("TOKEN")
ADMIN_ID      = os.getenv("ADMIN_ID")
WEBHOOK_URL   = os.getenv("WEBHOOK_URL")   # e.g. "https://your-app.onrender.com/"
LOG_GROUP_ID  = os.getenv("LOG_GROUP_ID")  # e.g. "-1001234567890"
PORT          = int(os.getenv("PORT", "10000"))
DB_FILE       = "reels.db"

# Conversation states
SUBMIT_LINK   = 0
REMOVE_LINK   = 1

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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER,
                action       TEXT,
                shortcode    TEXT,
                timestamp    TEXT
            )""")
        await db.commit()

# ── Helpers ─────────────────────────────────────────────────────────────────────
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

# ── View Tracking ────────────────────────────────────────────────────────────────
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
        await asyncio.sleep(12 * 3600)  # every 12h

# ── Command: /start ─────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\n"
        "/submit → track a reel\n"
        "/stats  → view your stats\n"
        "/remove → delete a reel\n"
        "Admin: /adminstats, /broadcast, /auditlog"
    )

# ── /submit Flow ────────────────────────────────────────────────────────────────
async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Send me the public Instagram *Reel* URL:")
    return SUBMIT_LINK

async def submit_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    code = extract_shortcode(link)
    uid  = update.effective_user.id
    if not code:
        await update.message.reply_text("❌ Invalid Reel URL.")
        return ConversationHandler.END

    L = instaloader.Instaloader()
    try:
        post = instaloader.Post.from_shortcode(L.context, code)
    except:
        await update.message.reply_text("⚠️ Couldn't fetch—ensure it's public.")
        return ConversationHandler.END

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
            # audit log
            await db.execute(
                "INSERT INTO audit (user_id, action, shortcode, timestamp) VALUES (?, 'submitted', ?, ?)",
                (uid, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            await db.commit()
            await update.message.reply_text(f"✅ @{username} submitted ({views0} views).")
            # log to group
            await log_to_group(context.bot,
                f"📥 User `{uid}` submitted Reel `{code}` (@{username})"
            )
        except aiosqlite.IntegrityError:
            await update.message.reply_text("⚠️ Already submitted.")
    return ConversationHandler.END

# ── /stats ─────────────────────────────────────────────────────────────────────
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        cur   = await db.execute("SELECT id, username FROM reels WHERE user_id=?", (uid,))
        reels = await cur.fetchall()
    if not reels:
        return await update.message.reply_text("📭 No reels yet.")

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

# ── /remove Flow ────────────────────────────────────────────────────────────────
async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🗑️ Send the full Instagram Reel URL to remove:")
    return REMOVE_LINK

async def remove_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    code = extract_shortcode(link)
    uid  = update.effective_user.id
    if not code:
        await update.message.reply_text("❌ Invalid Reel URL.")
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
        # audit
        await db.execute(
            "INSERT INTO audit (user_id, action, shortcode, timestamp) VALUES (?, 'removed', ?, ?)",
            (uid, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        # cleanup user if no more reels
        cur2 = await db.execute("SELECT COUNT(*) FROM reels WHERE user_id=?", (uid,))
        if (await cur2.fetchone())[0] == 0:
            await db.execute("DELETE FROM users WHERE user_id=?", (uid,))
        await db.commit()

    await update.message.reply_text(f"✅ Removed `{code}`.")
    await log_to_group(context.bot,
        f"📤 User `{uid}` removed Reel `{code}`"
    )
    return ConversationHandler.END

# ── Admin: /adminstats ─────────────────────────────────────────────────────────
async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    async with aiosqlite.connect(DB_FILE) as db:
        ucur = await db.execute("SELECT COUNT(*) FROM users")
        rcur = await db.execute("SELECT COUNT(*) FROM reels")
        total_users = (await ucur.fetchone())[0]
        total_reels = (await rcur.fetchone())[0]
        top = await db.execute(
            "SELECT username, COUNT(*) FROM reels GROUP BY username ORDER BY COUNT(*) DESC LIMIT 5"
        )
        tops = await top.fetchall()
    msg = (
        f"🛠️ Admin Stats:\n"
        f"• Users: {total_users}\n"
        f"• Reels: {total_reels}\n\n"
        "Top IG Accounts:\n" +
        "\n".join(f"- @{u}: {c}" for u, c in tops)
    )
    await update.message.reply_text(msg)

# ── Admin: /auditlog ────────────────────────────────────────────────────────────
async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    entries = []
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id, action, shortcode, timestamp FROM audit ORDER BY id DESC LIMIT 20")
        for uid, act, sc, ts in await cur.fetchall():
            entries.append(f"{ts} — User {uid} {act} `{sc}`")
    await update.message.reply_text("📋 Recent Activity:\n" + "\n".join(entries))

# ── Admin: /broadcast, /deleteuser, /deletereel (unchanged) ─────────────────────
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    text = "📢 " + " ".join(context.args)
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id FROM users")
        for (uid,) in await cur.fetchall():
            try: await context.bot.send_message(chat_id=uid, text=text)
            except: pass
    await update.message.reply_text("✅ Broadcast sent.")

async def deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    targ = context.args[0]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE user_id=?)", (targ,))
        await db.execute("DELETE FROM reels WHERE user_id=?", (targ,))
        await db.execute("DELETE FROM users WHERE user_id=?", (targ,))
        await db.commit()
    await update.message.reply_text(f"🧹 Deleted user {targ}.")

async def deletereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return
    sc = context.args[0]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE shortcode=?)", (sc,))
        await db.execute("DELETE FROM reels WHERE shortcode=?", (sc,))
        await db.commit()
    await update.message.reply_text(f"✅ Deleted reel `{sc}`.")

# ── Error Handler ───────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    text = f"❗️ *Error*\n<pre>{tb}</pre>"
    await log_to_group(context.bot, text)

# ── Bootstrap & Webhook Startup ─────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(init_db())
    app = ApplicationBuilder().token(TOKEN).build()

    # user handlers
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

    # admin handlers
    app.add_handler(CommandHandler("adminstats", adminstats))
    app.add_handler(CommandHandler("auditlog", auditlog))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("deleteuser", deleteuser))
    app.add_handler(CommandHandler("deletereel", deletereel))

    # error handler
    app.add_error_handler(error_handler)

    # background tracking
    asyncio.get_event_loop().create_task(track_loop())

    print("🤖 Running in webhook mode…")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        close_loop=False
    )
