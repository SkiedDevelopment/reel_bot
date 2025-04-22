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
    ContextTypes,
)

# Patch asyncio for hosted environments
nest_asyncio.apply()

# Configuration from environment
TOKEN         = os.getenv("TOKEN")
ADMIN_ID      = os.getenv("ADMIN_ID")       # your Telegram admin user ID
LOG_GROUP_ID  = os.getenv("LOG_GROUP_ID")   # e.g. "-1001234567890"
WEBHOOK_URL   = os.getenv("WEBHOOK_URL")    # e.g. "https://your-app.onrender.com/"
PORT          = int(os.getenv("PORT", "10000"))
DB_FILE       = "reels.db"
COOLDOWN_SEC  = 60  # one-minute cooldown between /submit

# Helpers
def extract_shortcode(link: str) -> str | None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(uid: int) -> bool:
    return ADMIN_ID is not None and str(uid) == str(ADMIN_ID)

async def log_to_group(bot, text: str):
    if LOG_GROUP_ID:
        try:
            await bot.send_message(chat_id=int(LOG_GROUP_ID), text=text)
        except:
            pass

# Initialize database
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS user_accounts (
            user_id     INTEGER,
            insta_handle TEXT,
            PRIMARY KEY (user_id, insta_handle)
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
        await db.execute("""CREATE TABLE IF NOT EXISTS cooldowns (
            user_id     INTEGER PRIMARY KEY,
            last_submit TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS audit (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            action       TEXT,
            shortcode    TEXT,
            timestamp    TEXT
        )""")
        await db.commit()

# Background view-tracking
async def track_all_views():
    L = instaloader.Instaloader()
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT id, shortcode FROM reels")
        for reel_id, code in await cursor.fetchall():
            for attempt in range(3):
                try:
                    post = instaloader.Post.from_shortcode(L.context, code)
                    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    views = post.video_view_count
                    await db.execute(
                        "INSERT INTO views (reel_id, timestamp, count) VALUES (?, ?, ?)",
                        (reel_id, ts, views)
                    )
                    await db.commit()
                    break
                except Exception:
                    await asyncio.sleep(2)

async def track_loop():
    await asyncio.sleep(5)
    while True:
        await track_all_views()
        await asyncio.sleep(12*3600)

# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\n"
        "/addaccount <tg_id> @insta   → assign an Instagram account to user\n"
        "/userstats <tg_id>           → view that user’s stats\n"
        "/submit <Reel URL>           → submit a reel (1-min cooldown)\n"
        "/stats                       → your stats\n"
        "/remove <Reel URL>           → remove a submitted reel\n"
        "Admin only:\n"
        "/adminstats /auditlog /broadcast /deleteuser /deletereel"
    )

# /addaccount
async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or len(context.args)!=2:
        return await update.message.reply_text("Usage: /addaccount <tg_id> @insta_handle")
    target, handle = context.args
    if not handle.startswith('@'):
        return await update.message.reply_text("Handle must start with '@'")
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO user_accounts (user_id, insta_handle) VALUES (?, ?)",
            (int(target), handle)
        )
        await db.commit()
    await update.message.reply_text(f"✅ Assigned {handle} to user {target}")
    await log_to_group(context.bot, f"Admin {uid} assigned {handle} to user {target}")

# /userstats
async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or len(context.args)!=1:
        return await update.message.reply_text("Usage: /userstats <tg_id>")
    target = int(context.args[0])
    async with aiosqlite.connect(DB_FILE) as db:
        accs = await db.execute(
            "SELECT insta_handle FROM user_accounts WHERE user_id=?", (target,)
        )
        handles = [r[0] for r in await accs.fetchall()]
        reels = await db.execute("SELECT id FROM reels WHERE user_id=?", (target,))
        rlist = await reels.fetchall()
        total_views = 0
        for (rid,) in rlist:
            vcur = await db.execute(
                "SELECT count FROM views WHERE reel_id=? ORDER BY timestamp DESC LIMIT 1", (rid,)
            )
            row = await vcur.fetchone()
            if row: total_views += row[0]
    await update.message.reply_text(
        f"Stats for {target}\n"
        f"• Instagram: {', '.join(handles) or 'None'}\n"
        f"• Reels: {len(rlist)}\n"
        f"• Views: {total_views}"
    )
    await log_to_group(context.bot, f"Admin {uid} viewed stats for user {target}")

# /submit
async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Usage: /submit <Instagram Reel URL>")

    now = datetime.now()
    async with aiosqlite.connect(DB_FILE) as db:
        cd = await db.execute("SELECT last_submit FROM cooldowns WHERE user_id=?", (uid,))
        row = await cd.fetchone()
        if row:
            last = datetime.fromisoformat(row[0])
            if (now - last).total_seconds() < COOLDOWN_SEC:
                wait = int(COOLDOWN_SEC - (now-last).total_seconds())
                return await update.message.reply_text(f"⏱ Please wait {wait}s.")
        await db.execute(
            "INSERT OR REPLACE INTO cooldowns (user_id, last_submit) VALUES (?, ?)",
            (uid, now.isoformat())
        )
        await db.commit()

    link = context.args[0]
    code = extract_shortcode(link)
    if not code:
        return await update.message.reply_text("❌ Invalid Reel URL.")

    # check assigned accounts
    async with aiosqlite.connect(DB_FILE) as db:
        ac = await db.execute("SELECT insta_handle FROM user_accounts WHERE user_id=?", (uid,))
        allowed = [h.lstrip('@').lower() for h,_ in await db.execute_fetchall(ac)]
    if not allowed:
        return await update.message.reply_text("⚠️ No account assigned. Ask admin.")
    # fetch post
    L = instaloader.Instaloader()
    try:
        post = instaloader.Post.from_shortcode(L.context, code)
    except:
        return await update.message.reply_text("⚠️ Fetch failed. Make sure it's public.")
    if post.owner_username.lower() not in allowed:
        return await update.message.reply_text(f"❌ Not your assigned account: {', '.join('@'+a for a in allowed)}")

    username = post.owner_username
    views0   = post.video_view_count
    ts_str   = now.strftime("%Y-%m-%d %H:%M:%S")

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
                (uid, code, ts_str, views0)
            )
            await db.execute(
                "INSERT INTO audit (user_id, action, shortcode, timestamp) VALUES (?, 'submitted', ?, ?)",
                (uid, code, ts_str)
            )
            await db.commit()
            await update.message.reply_text(f"✅ @{username} submitted ({views0} views).")
            await log_to_group(context.bot, f"User {uid} submitted reel {code}")
        except aiosqlite.IntegrityError:
            await update.message.reply_text("⚠️ Already submitted.")

# /stats
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        rcur = await db.execute("SELECT id, username FROM reels WHERE user_id=?", (uid,))
        reels = await rcur.fetchall()
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
            if row: total += row[0]
    text = (f"📊 Videos: {len(reels)}\n"
            f"📈 Views:  {total}\n"
            f"👤 Accounts: {', '.join(users)}")
    await update.message.reply_text(text)
    await log_to_group(context.bot, f"User {uid} checked stats")

# /remove
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Usage: /remove <Instagram Reel URL>")
    link = context.args[0]
    code = extract_shortcode(link)
    if not code:
        return await update.message.reply_text("❌ Invalid Reel URL.")
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT id FROM reels WHERE user_id=? AND shortcode=?", (uid, code))
        row = await cur.fetchone()
        if not row:
            return await update.message.reply_text("❌ You never submitted that reel.")
        rid = row[0]
        await db.execute("DELETE FROM views WHERE reel_id=?", (rid,))
        await db.execute("DELETE FROM reels WHERE id=?", (rid,))
        await db.execute(
            "INSERT INTO audit (user_id, action, shortcode, timestamp) VALUES (?, 'removed', ?, ?)",
            (uid, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        # cleanup user row if none left
        rc = await db.execute("SELECT COUNT(*) FROM reels WHERE user_id=?", (uid,))
        if (await rc.fetchone())[0] == 0:
            await db.execute("DELETE FROM users WHERE user_id=?", (uid,))
        await db.commit()
    await update.message.reply_text(f"✅ Removed `{code}`.")
    await log_to_group(context.bot, f"User {uid} removed reel {code}")

# Admin commands...
async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    async with aiosqlite.connect(DB_FILE) as db:
        ucur = await db.execute("SELECT COUNT(*) FROM users")
        rcur = await db.execute("SELECT COUNT(*) FROM reels")
        total_u = (await ucur.fetchone())[0]
        total_r = (await rcur.fetchone())[0]
        top = await db.execute(
            "SELECT username, COUNT(*) FROM reels GROUP BY username ORDER BY COUNT(*) DESC LIMIT 5"
        )
        tops = await top.fetchall()
    msg = (f"🛠️ Admin Stats:\n• Users: {total_u}\n• Reels: {total_r}\n\n"
           "Top Accounts:\n" + "\n".join(f"- @{u}: {c}" for u,c in tops))
    await update.message.reply_text(msg)
    await log_to_group(context.bot, f"Admin {uid} viewed adminstats")

async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid): return
    lines = []
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT user_id, action, shortcode, timestamp FROM audit ORDER BY id DESC LIMIT 20"
        )
        for u,a,s,t in await cur.fetchall():
            lines.append(f"{t} — User {u} {a} `{s}`")
    await update.message.reply_text("📋 Recent Activity:\n" + "\n".join(lines))
    await log_to_group(context.bot, f"Admin {uid} viewed auditlog")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args: return
    text = "📢 " + " ".join(context.args)
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id FROM users")
        for (u,) in await cur.fetchall():
            try: await context.bot.send_message(chat_id=u, text=text)
            except: pass
    await update.message.reply_text("✅ Broadcast sent.")
    await log_to_group(context.bot, f"Admin {uid} broadcasted")

async def deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args: return
    target = context.args[0]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE user_id=?)", (target,))
        await db.execute("DELETE FROM reels WHERE user_id=?", (target,))
        await db.execute("DELETE FROM users WHERE user_id=?", (target,))
        await db.commit()
    await update.message.reply_text(f"🧹 Deleted user {target}.")
    await log_to_group(context.bot, f"Admin {uid} deleted user {target}")

async def deletereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args: return
    code = context.args[0]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE shortcode=?)", (code,))
        await db.execute("DELETE FROM reels WHERE shortcode=?", (code,))
        await db.commit()
    await update.message.reply_text(f"✅ Deleted reel `{code}`.")
    await log_to_group(context.bot, f"Admin {uid} deleted reel {code}")

# Global error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    await log_to_group(context.bot, f"❗️ Error\n<pre>{tb}</pre>")

# Bootstrap & webhook startup
if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(init_db())
    app = ApplicationBuilder().token(TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addaccount", addaccount))
    app.add_handler(CommandHandler("userstats", userstats))
    app.add_handler(CommandHandler("submit", submit))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("remove", remove))

    # Admin commands
    app.add_handler(CommandHandler("adminstats", adminstats))
    app.add_handler(CommandHandler("auditlog", auditlog))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("deleteuser", deleteuser))
    app.add_handler(CommandHandler("deletereel", deletereel))

    # Error handler
    app.add_error_handler(error_handler)

    # Background tracking
    asyncio.get_event_loop().create_task(track_loop())

    # Run webhook
    print("🤖 Running in webhook mode…")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        close_loop=False
    )
