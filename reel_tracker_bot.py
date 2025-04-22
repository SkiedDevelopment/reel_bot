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

nest_asyncio.apply()

# ── Config ───────────────────────────────────────────────────────────────────────
TOKEN         = os.getenv("TOKEN")
ADMIN_ID      = os.getenv("ADMIN_ID")       # Telegram admin ID
LOG_GROUP_ID  = os.getenv("LOG_GROUP_ID")   # e.g. "-1001234567890"
WEBHOOK_URL   = os.getenv("WEBHOOK_URL")    # e.g. "https://your-app.onrender.com/"
PORT          = int(os.getenv("PORT", "10000"))
DB_FILE       = "reels.db"
COOLDOWN_SEC  = 60  # seconds

# ── Helpers ──────────────────────────────────────────────────────────────────────
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

# ── DB Init ──────────────────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_accounts (
                user_id     INTEGER,
                insta_handle TEXT,
                PRIMARY KEY (user_id, insta_handle)
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
            CREATE TABLE IF NOT EXISTS cooldowns (
                user_id     INTEGER PRIMARY KEY,
                last_submit TEXT
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

# ── Background Tracking ─────────────────────────────────────────────────────────
async def track_all_views():
    L = instaloader.Instaloader()
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT id, shortcode FROM reels")
        for reel_id, code in await cur.fetchall():
            for _ in range(3):
                try:
                    post = instaloader.Post.from_shortcode(L.context, code)
                    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    await db.execute(
                        "INSERT INTO views (reel_id, timestamp, count) VALUES (?, ?, ?)",
                        (reel_id, ts, post.video_view_count)
                    )
                    await db.commit()
                    break
                except:
                    await asyncio.sleep(2)

async def track_loop():
    await asyncio.sleep(5)
    while True:
        await track_all_views()
        await asyncio.sleep(12*3600)

# ── /start ──────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\n"
        "/addaccount <tg_id> @insta   → assign Instagram account(s)\n"
        "/userstats <tg_id>           → view that user’s stats\n"
        "/submit <Reel URL>           → submit a reel (60 s cooldown)\n"
        "/stats                       → your stats\n"
        "/remove <Reel URL>           → remove a reel\n"
        "Admin only:\n"
        "/adminstats /auditlog /broadcast /deleteuser /deletereel"
    )

# ── /addaccount ─────────────────────────────────────────────────────────────────
async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or len(context.args) != 2:
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
    await log_to_group(context.bot, f"Admin @{update.effective_user.username} assigned {handle} to user {target}")

# ── /userstats ──────────────────────────────────────────────────────────────────
async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or len(context.args) != 1:
        return await update.message.reply_text("Usage: /userstats <tg_id>")
    target = int(context.args[0])

    async with aiosqlite.connect(DB_FILE) as db:
        # handles
        c1 = await db.execute("SELECT insta_handle FROM user_accounts WHERE user_id=?", (target,))
        handles = [r[0] for r in await c1.fetchall()]
        # reels + views
        c2 = await db.execute("SELECT id, shortcode FROM reels WHERE user_id=?", (target,))
        reels = await c2.fetchall()

    total_views = 0
    details = []
    async with aiosqlite.connect(DB_FILE) as db:
        for rid, code in reels:
            vcur = await db.execute(
                "SELECT count FROM views WHERE reel_id=? ORDER BY timestamp DESC LIMIT 1", (rid,)
            )
            row = await vcur.fetchone()
            cnt = row[0] if row else 0
            total_views += cnt
            details.append((code, cnt))
    # sort desc
    details.sort(key=lambda x: x[1], reverse=True)

    text = [
        f"Stats for user {target}:",
        f"• Instagram: {', '.join(handles) or 'None'}",
        f"• Total videos: {len(reels)}",
        f"• Total views: {total_views}",
        "Reels (highest→lowest):"
    ]
    for i, (code, cnt) in enumerate(details, 1):
        text.append(f"{i}. https://instagram.com/reel/{code} – {cnt} views")

    await update.message.reply_text("\n".join(text))
    await log_to_group(context.bot, f"Admin @{update.effective_user.username} viewed stats for {target}")

# ── /submit ─────────────────────────────────────────────────────────────────────
async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Usage: /submit <Instagram Reel URL>")

    now = datetime.now()
    # cooldown
    async with aiosqlite.connect(DB_FILE) as db:
        cd = await db.execute("SELECT last_submit FROM cooldowns WHERE user_id=?", (uid,))
        row = await cd.fetchone()
        if row:
            last = datetime.fromisoformat(row[0])
            remain = COOLDOWN_SEC - (now - last).total_seconds()
            if remain > 0:
                msg = await update.message.reply_text(f"⏱ Please wait {int(remain)}s.")
                async def _del():
                    await asyncio.sleep(5)
                    try:
                        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg.message_id)
                    except: pass
                asyncio.create_task(_del())
                return
        await db.execute(
            "INSERT OR REPLACE INTO cooldowns (user_id, last_submit) VALUES (?, ?)",
            (uid, now.isoformat())
        )
        await db.commit()

    code = extract_shortcode(context.args[0])
    if not code:
        return await update.message.reply_text("❌ Invalid Reel URL.")

    # allowed accounts
    async with aiosqlite.connect(DB_FILE) as db:
        c1 = await db.execute("SELECT insta_handle FROM user_accounts WHERE user_id=?", (uid,))
        rows = await c1.fetchall()
    allowed = [h[0].lstrip('@').lower() for h in rows]
    if not allowed:
        return await update.message.reply_text("⚠️ No account assigned. Ask admin.")
    # fetch
    L = instaloader.Instaloader()
    try:
        post = instaloader.Post.from_shortcode(L.context, code)
    except:
        return await update.message.reply_text("⚠️ Fetch failed; must be public.")
    if post.owner_username.lower() not in allowed:
        return await update.message.reply_text(
            f"❌ Reel not from your accounts: {', '.join('@'+a for a in allowed)}"
        )

    views0, ts = post.video_view_count, now.strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)",
            (uid, update.effective_user.username or "")
        )
        try:
            await db.execute(
                "INSERT INTO reels (user_id, shortcode, username) VALUES (?, ?, ?)",
                (uid, code, post.owner_username)
            )
            await db.execute(
                "INSERT INTO views (reel_id, timestamp, count) VALUES ("
                "(SELECT id FROM reels WHERE user_id=? AND shortcode=?), ?, ?)",
                (uid, code, ts, views0)
            )
            await db.execute(
                "INSERT INTO audit (user_id, action, shortcode, timestamp) "
                "VALUES (?, 'submitted', ?, ?)",
                (uid, code, ts)
            )
            await db.commit()
            await update.message.reply_text(f"✅ @{post.owner_username} submitted ({views0} views).")
            await log_to_group(context.bot, f"User @{update.effective_user.username} submitted {code}")
        except aiosqlite.IntegrityError:
            await update.message.reply_text("⚠️ Already submitted.")

# ── /stats ──────────────────────────────────────────────────────────────────────
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        c1 = await db.execute("SELECT id, username FROM reels WHERE user_id=?", (uid,))
        reels = await c1.fetchall()
    if not reels:
        return await update.message.reply_text("📭 No reels tracked.")
    total, users = 0, set()
    details = []
    async with aiosqlite.connect(DB_FILE) as db:
        for rid, uname in reels:
            vcur = await db.execute(
                "SELECT count FROM views WHERE reel_id=? ORDER BY timestamp DESC LIMIT 1", (rid,)
            )
            row = await vcur.fetchone()
            cnt = row[0] if row else 0
            total += cnt
            users.add(uname)
            details.append((rid, uname, cnt))
    # sort
    details.sort(key=lambda x: x[2], reverse=True)

    text = [
        f"Your stats:",
        f"• Total videos: {len(reels)}",
        f"• Total views: {total}",
        f"• Accounts linked: {', '.join(users)}",
        "Reels (highest→lowest):"
    ]
    for i, (_, code, cnt) in enumerate(details, 1):
        text.append(f"{i}. https://instagram.com/reel/{code} – {cnt} views")

    await update.message.reply_text("\n".join(text))
    await log_to_group(context.bot, f"User @{update.effective_user.username} checked stats")

# ── /remove ─────────────────────────────────────────────────────────────────────
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Usage: /remove <Instagram Reel URL>")
    code = extract_shortcode(context.args[0])
    if not code:
        return await update.message.reply_text("❌ Invalid Reel URL.")
    async with aiosqlite.connect(DB_FILE) as db:
        c1 = await db.execute("SELECT id FROM reels WHERE user_id=? AND shortcode=?", (uid, code))
        row = await c1.fetchone()
        if not row:
            return await update.message.reply_text("❌ You never submitted that reel.")
        rid = row[0]
        await db.execute("DELETE FROM views WHERE reel_id=?", (rid,))
        await db.execute("DELETE FROM reels WHERE id=?", (rid,))
        await db.execute(
            "INSERT INTO audit (user_id, action, shortcode, timestamp) VALUES (?, 'removed', ?, ?)",
            (uid, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        rc = await db.execute("SELECT COUNT(*) FROM reels WHERE user_id=?", (uid,))
        if (await rc.fetchone())[0] == 0:
            await db.execute("DELETE FROM users WHERE user_id=?", (uid,))
        await db.commit()
    await update.message.reply_text(f"✅ Removed `{code}`.")
    await log_to_group(context.bot, f"User @{update.effective_user.username} removed {code}")

# ── Admin: /adminstats (text file) ─────────────────────────────────────────────
async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    stats = []
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id, username FROM users")
        users = await cur.fetchall()
        for user_id, uname in users:
            # reels + views
            rcur = await db.execute("SELECT id, shortcode FROM reels WHERE user_id=?", (user_id,))
            reels = await rcur.fetchall()
            total_views = 0
            details = []
            for rid, code in reels:
                vcur = await db.execute(
                    "SELECT count FROM views WHERE reel_id=? ORDER BY timestamp DESC LIMIT 1", (rid,)
                )
                row = await vcur.fetchone()
                cnt = row[0] if row else 0
                total_views += cnt
                details.append((code, cnt))
            # sort
            details.sort(key=lambda x: x[1], reverse=True)
            stats.append((user_id, uname, len(reels), total_views, details))
    # descending by views
    stats.sort(key=lambda x: x[3], reverse=True)

    lines = []
    for user_id, uname, vids, views, det in stats:
        lines.append(f"@{uname or user_id}")
        lines.append(f"Total views: {views}")
        lines.append(f"Total videos: {vids}")
        for code, cnt in det:
            lines.append(f"https://instagram.com/reel/{code} – {cnt}")
        lines.append("")  # blank

    path = "/mnt/data/admin_stats.txt"
    with open(path, "w") as f:
        f.write("\n".join(lines))

    await update.message.reply_document(open(path, "rb"), filename="admin_stats.txt")
    await log_to_group(context.bot, f"Admin @{update.effective_user.username} generated full report")

# ── Other admin commands (unchanged): /auditlog, /broadcast, /deleteuser, /deletereel ─
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
            lines.append(f"{t} — @{(await db.execute('SELECT username FROM users WHERE user_id=?',(u,))).fetchone()[0]} {a} `{s}`")
    await update.message.reply_text("📋 Recent Activity:\n" + "\n".join(lines))

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

# ── Error handler ────────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    await log_to_group(context.bot, f"❗️ Error in handler:\n<pre>{tb}</pre>")

# ── Startup ──────────────────────────────────────────────────────────────────────
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

    # Start background tracking
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
