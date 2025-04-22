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

# Patch asyncio loop for hosted envs
nest_asyncio.apply()

# --- Configuration from Environment ---
TOKEN       = os.getenv("TOKEN")
ADMIN_ID    = os.getenv("ADMIN_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. "https://your-app.onrender.com/"
PORT        = int(os.getenv("PORT", "10000"))
DB_FILE     = "reels.db"

# --- Database Initialization ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reels (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER,
                shortcode TEXT,
                username  TEXT,
                UNIQUE(user_id, shortcode)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS views (
                reel_id   INTEGER,
                timestamp TEXT,
                count     INTEGER
            )
        """)
        await db.commit()

# --- Helpers ---
def is_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and str(user_id) == str(ADMIN_ID)

def extract_shortcode(link: str) -> str | None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

# --- View Tracking ---
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

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! Use /submit to track a reel; /stats to view your stats; /remove to delete; admin commands if you’re admin."
    )

async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me the public Instagram reel URL:")
    try:
        msg = await context.bot.wait_for_message(chat_id=update.effective_chat.id, timeout=60)
    except asyncio.TimeoutError:
        return await update.message.reply_text("⏰ Timeout, try /submit again.")

    link = msg.text or ""
    code = extract_shortcode(link)
    if not code:
        return await update.message.reply_text("❌ Invalid reel link.")

    L = instaloader.Instaloader()
    try:
        post = instaloader.Post.from_shortcode(L.context, code)
    except Exception:
        return await update.message.reply_text("⚠️ Couldn't fetch—ensure it's public.")

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
        cur = await db.execute("SELECT id, username FROM reels WHERE user_id=?", (uid,))
        reels = await cur.fetchall()
        if not reels:
            return await update.message.reply_text("📭 No reels yet.")

        total, users = 0, set()
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
        await db.execute(
            "DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE user_id=? AND shortcode=?)",
            (uid, sc)
        )
        await db.execute(
            "DELETE FROM reels WHERE user_id=? AND shortcode=?",
            (uid, sc)
        )
        await db.commit()
        await update.message.reply_text(f"✅ Removed `{sc}`.")

# --- Admin Commands ---
async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    async with aiosqlite.connect(DB_FILE) as db:
        ucur = await db.execute("SELECT COUNT(*) FROM users")
        rcur = await db.execute("SELECT COUNT(*) FROM reels")
        t_users = (await ucur.fetchone())[0]
        t_reels = (await rcur.fetchone())[0]
        top = await db.execute(
            "SELECT username, COUNT(*) AS c FROM reels GROUP BY username ORDER BY c DESC LIMIT 5"
        )
        tops = await top.fetchall()
        msg = f"🛠️ Admin Stats:\nUsers: {t_users}\nReels: {t_reels}\nTop Accounts:\n"
        for uname, cnt in tops:
            msg += f"- @{uname}: {cnt}\n"
        await update.message.reply_text(msg)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /broadcast Your message")
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
    await update.message.reply_text(f"✅ Deleted reel {sc}.")

# --- Main & Webhook Startup ---
async def main():
    await init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    # user commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("submit", submit))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("remove", remove))

    # admin commands
    app.add_handler(CommandHandler("adminstats", adminstats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("deleteuser", deleteuser))
    app.add_handler(CommandHandler("deletereel", deletereel))

    # start background tracker
    asyncio.create_task(track_loop())

    print("🤖 Running in webhook mode…")
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        close_loop=False
    )

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
