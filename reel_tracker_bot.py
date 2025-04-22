import os
import re
import asyncio
import nest_asyncio
import instaloader
import aiosqlite
import traceback
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── Patch asyncio for hosted envs ───────────────────────────────────────────────
nest_asyncio.apply()

# ── Configuration from ENV ──────────────────────────────────────────────────────
TOKEN        = os.getenv("TOKEN")
ADMIN_ID     = os.getenv("ADMIN_ID")
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")   # e.g. "-1001234567890"
WEBHOOK_URL  = os.getenv("WEBHOOK_URL")    # e.g. "https://your-app.onrender.com/"
PORT         = int(os.getenv("PORT", "10000"))
DB_FILE      = "reels.db"

# ── Helpers ─────────────────────────────────────────────────────────────────────
def extract_shortcode(link: str) -> str|None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(uid: int) -> bool:
    try:
        return ADMIN_ID is not None and int(uid) == int(ADMIN_ID)
    except:
        return False

async def log_to_group(bot, text: str):
    if LOG_GROUP_ID:
        try:
            await bot.send_message(chat_id=int(LOG_GROUP_ID), text=text)
        except:
            pass

# ── Database Initialization ─────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS reels (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER,
            shortcode TEXT,
            username  TEXT,
            UNIQUE(user_id, shortcode)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS views (
            reel_id   INTEGER,
            timestamp TEXT,
            count     INTEGER
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS audit (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            action       TEXT,
            shortcode    TEXT,
            timestamp    TEXT
        )""")
        await db.commit()

# ── View Tracking Loop ──────────────────────────────────────────────────────────
async def track_all_views():
    L = instaloader.Instaloader()
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT id, shortcode FROM reels")
        for reel_id, shortcode in await cursor.fetchall():
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
        await asyncio.sleep(12*3600)  # 12 hours

# ── Command: /start ─────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\n"
        "/submit <URL> → track a reel\n"
        "/stats        → your stats\n"
        "/remove <URL> → delete a reel\n"
        "Admin: /adminstats, /auditlog, /broadcast, /deleteuser, /deletereel"
    )

# ── Command: /submit <URL> ───────────────────────────────────────────────────────
async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /submit <Instagram Reel URL>")
    link = context.args[0]
    code = extract_shortcode(link)
    if not code:
        return await update.message.reply_text("❌ Invalid Reel URL.")

    # fetch post
    L = instaloader.Instaloader()
    try:
        post = instaloader.Post.from_shortcode(L.context, code)
    except:
        return await update.message.reply_text("⚠️ Failed to fetch—make sure it’s public.")

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
                "INSERT INTO views (reel_id, timestamp, count) VALUES ("
                "(SELECT id FROM reels WHERE user_id=? AND shortcode=?), ?, ?)",
                (uid, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), views0)
            )
            await db.execute(
                "INSERT INTO audit (user_id, action, shortcode, timestamp) "
                "VALUES (?, 'submitted', ?, ?)",
                (uid, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            await db.commit()
            await update.message.reply_text(f"✅ @{username} submitted ({views0} views).")
            await log_to_group(context.bot, f"📥 User `{uid}` submitted Reel `{code}` (@{username})")
        except aiosqlite.IntegrityError:
            return await update.message.reply_text("⚠️ You've already submitted that Reel.")

# ── Command: /stats ─────────────────────────────────────────────────────────────
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT id, username FROM reels WHERE user_id=?", (uid,))
        reels = await cursor.fetchall()

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

    text = (
        f"📊 Videos: {len(reels)}\n"
        f"📈 Views:  {total}\n"
        f"👤 Accounts: {', '.join(users)}"
    )
    await update.message.reply_text(text)
    await log_to_group(context.bot, f"📊 Stats for User `{uid}`:\n{text}")

# ── Command: /remove <URL> ──────────────────────────────────────────────────────
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /remove <Instagram Reel URL>")
    link = context.args[0]
    code = extract_shortcode(link)
    uid  = update.effective_user.id
    if not code:
        return await update.message.reply_text("❌ Invalid Reel URL.")

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT id FROM reels WHERE user_id=? AND shortcode=?", (uid, code)
        )
        row = await cur.fetchone()
        if not row:
            return await update.message.reply_text("❌ You never submitted that Reel.")
        reel_id = row[0]
        await db.execute("DELETE FROM views WHERE reel_id=?", (reel_id,))
        await db.execute("DELETE FROM reels WHERE id=?", (reel_id,))
        await db.execute(
            "INSERT INTO audit (user_id, action, shortcode, timestamp) "
            "VALUES (?, 'removed', ?, ?)",
            (uid, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        # cleanup user if no more reels
        cur2 = await db.execute("SELECT COUNT(*) FROM reels WHERE user_id=?", (uid,))
        if (await cur2.fetchone())[0] == 0:
            await db.execute("DELETE FROM users WHERE user_id=?", (uid,))
        await db.commit()

    await update.message.reply_text(f"✅ Removed `{code}`.")
    await log_to_group(context.bot, f"📤 User `{uid}` removed Reel `{code}`")

# ── Admin Commands ─────────────────────────────────────────────────────────────
async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
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
        "Top Accounts:\n" +
        "\n".join(f"- @{u}: {c}" for u, c in tops)
    )
    await update.message.reply_text(msg)
    await log_to_group(context.bot, f"🛠️ Admin `{uid}` viewed stats.")

async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    lines = []
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT user_id, action, shortcode, timestamp FROM audit ORDER BY id DESC LIMIT 20"
        )
        for u,a,s,t in await cur.fetchall():
            lines.append(f"{t} — User {u} {a} `{s}`")
    msg = "📋 Recent Activity:\n" + "\n".join(lines)
    await update.message.reply_text(msg)
    await log_to_group(context.bot, f"🗒️ Admin `{uid}` viewed audit log.")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args:
        return
    text = "📢 " + " ".join(context.args)
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id FROM users")
        for (u,) in await cur.fetchall():
            try:
                await context.bot.send_message(chat_id=u, text=text)
            except:
                pass
    await update.message.reply_text("✅ Broadcast sent.")
    await log_to_group(context.bot, f"📣 Admin `{uid}` broadcasted.")

async def deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args:
        return
    target = context.args[0]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE user_id=?)", (target,))
        await db.execute("DELETE FROM reels WHERE user_id=?", (target,))
        await db.execute("DELETE FROM users WHERE user_id=?", (target,))
        await db.commit()
    await update.message.reply_text(f"🧹 Deleted user {target}.")
    await log_to_group(context.bot, f"🗑️ Admin `{uid}` deleted user {target}.")

async def deletereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args:
        return
    code = context.args[0]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE shortcode=?)", (code,))
        await db.execute("DELETE FROM reels WHERE shortcode=?", (code,))
        await db.commit()
    await update.message.reply_text(f"✅ Deleted reel `{code}`.")
    await log_to_group(context.bot, f"🗑️ Admin `{uid}` deleted Reel `{code}`.")

# ── Global Error Handler ───────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    text = f"❗️ *Error*\n<pre>{tb}</pre>"
    await log_to_group(context.bot, text)

# ── Bootstrap & Webhook Start ───────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(init_db())
    app = ApplicationBuilder().token(TOKEN).build()

    # user commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("submit", submit))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("remove", remove))

    # admin commands
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
