import os
import re
import asyncio
import nest_asyncio
import instaloader
import traceback
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

# patch asyncio for hosted envs
nest_asyncio.apply()

# configuration
TOKEN        = os.getenv("TOKEN")
ADMIN_ID     = os.getenv("ADMIN_ID")       # your Telegram admin user ID
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")   # e.g. "-1001234567890"
WEBHOOK_URL  = os.getenv("WEBHOOK_URL")    # e.g. "https://your-app.onrender.com/"
PORT         = int(os.getenv("PORT", "10000"))
DATABASE_URL = os.getenv("DATABASE_URL")   # Postgres URL
COOLDOWN_SEC = 60

# sqlalchemy async setup
engine = create_async_engine(DATABASE_URL, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# helpers
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

# initialize tables
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
      id        INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id   INTEGER,
      shortcode TEXT,
      username  TEXT,
      UNIQUE(user_id, shortcode)
    );
    CREATE TABLE IF NOT EXISTS views (
      reel_id   INTEGER,
      timestamp TEXT,
      count     INTEGER
    );
    CREATE TABLE IF NOT EXISTS cooldowns (
      user_id     INTEGER PRIMARY KEY,
      last_submit TEXT
    );
    CREATE TABLE IF NOT EXISTS audit (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id      INTEGER,
      action       TEXT,
      shortcode    TEXT,
      timestamp    TEXT
    );
    """
    async with engine.begin() as conn:
        for stmt in ddl.split(";"):
            s = stmt.strip()
            if s:
                await conn.execute(text(s))

# background view tracking
async def track_all_views():
    L = instaloader.Instaloader()
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT id, shortcode FROM reels"))
        rows = result.all()
    for reel_id, code in rows:
        for _ in range(3):
            try:
                post = instaloader.Post.from_shortcode(L.context, code)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        text("INSERT INTO views (reel_id, timestamp, count) VALUES (:r, :t, :c)"),
                        {"r": reel_id, "t": ts, "c": post.video_view_count}
                    )
                    await session.commit()
                break
            except:
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
        "/addaccount <tg_id> @insta   → assign Instagram account(s)\n"
        "/userstats <tg_id>           → view that user’s stats\n"
        "/submit <Reel URL>           → submit a reel (60 s cooldown)\n"
        "/stats                       → your stats\n"
        "/remove <Reel URL>           → remove a reel\n"
        "Admin only:\n"
        "/adminstats /auditlog /broadcast /deleteuser /deletereel"
    )

# /addaccount
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

# /userstats
async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or len(context.args) != 1:
        return await update.message.reply_text("Usage: /userstats <tg_id>")
    target = int(context.args[0])
    async with AsyncSessionLocal() as session:
        res1 = await session.execute(text("SELECT insta_handle FROM user_accounts WHERE user_id=:u"), {"u": target})
        handles = [r[0] for r in res1.all()]
        res2 = await session.execute(text("SELECT id, shortcode FROM reels WHERE user_id=:u"), {"u": target})
        reels = res2.all()
    total_views = 0
    details = []
    for rid, code in reels:
        async with AsyncSessionLocal() as session:
            rv = await session.execute(
                text("SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"),
                {"r": rid}
            )
            row = rv.fetchone()
        cnt = row[0] if row else 0
        total_views += cnt
        details.append((code, cnt))
    details.sort(key=lambda x: x[1], reverse=True)
    lines = [
        f"Stats for user {target}:",
        f"• Instagram: {', '.join(handles) or 'None'}",
        f"• Total videos: {len(reels)}",
        f"• Total views: {total_views}",
        "Reels (highest→lowest):"
    ]
    for i, (code, cnt) in enumerate(details, 1):
        lines.append(f"{i}. https://instagram.com/reel/{code} – {cnt} views")
    await update.message.reply_text("\n".join(lines))
    await log_to_group(context.bot, f"Admin @{update.effective_user.username} viewed stats for {target}")

# /submit
async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Usage: /submit <Instagram Reel URL>")

    now = datetime.now()
    # cooldown
    async with AsyncSessionLocal() as session:
        cd = await session.execute(text("SELECT last_submit FROM cooldowns WHERE user_id=:u"), {"u": uid})
        row = cd.fetchone()
        if row:
            last = datetime.fromisoformat(row[0])
            remain = COOLDOWN_SEC - (now - last).total_seconds()
            if remain > 0:
                msg = await update.message.reply_text(f"⏱ Please wait {int(remain)}s.")
                async def _del():
                    await asyncio.sleep(5)
                    try:
                        await context.bot.delete_message(chat_id=update.effective_chat.id,
                                                         message_id=msg.message_id)
                    except:
                        pass
                asyncio.create_task(_del())
                return
        await session.execute(
            text("INSERT OR REPLACE INTO cooldowns (user_id, last_submit) VALUES (:u, :t)"),
            {"u": uid, "t": now.isoformat()}
        )
        await session.commit()

    code = extract_shortcode(context.args[0])
    if not code:
        return await update.message.reply_text("❌ Invalid Reel URL.")

    # check allowed accounts
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT insta_handle FROM user_accounts WHERE user_id=:u"), {"u": uid})
        rows = res.all()
    allowed = [h[0].lstrip('@').lower() for h in rows]
    if not allowed:
        return await update.message.reply_text("⚠️ No account assigned. Ask admin.")
    # fetch post
    L = instaloader.Instaloader()
    try:
        post = instaloader.Post.from_shortcode(L.context, code)
    except:
        return await update.message.reply_text("⚠️ Fetch failed; must be public.")
    if post.owner_username.lower() not in allowed:
        return await update.message.reply_text(
            f"❌ Reel not from your accounts: {', '.join('@'+a for a in allowed)}"
        )

    views0 = post.video_view_count
    ts_str = now.strftime("%Y-%m-%d %H:%M:%S")

    async with AsyncSessionLocal() as session:
        # upsert user info
        await session.execute(
            text("INSERT OR REPLACE INTO users (user_id, username) VALUES (:u, :n)"),
            {"u": uid, "n": update.effective_user.username or ""}
        )
        try:
            await session.execute(
                text("INSERT INTO reels (user_id, shortcode, username) VALUES (:u, :c, :n)"),
                {"u": uid, "c": code, "n": post.owner_username}
            )
            await session.execute(
                text("INSERT INTO views (reel_id, timestamp, count) VALUES ("
                     "(SELECT id FROM reels WHERE user_id=:u AND shortcode=:c), :t, :v)"),
                {"u": uid, "c": code, "t": ts_str, "v": views0}
            )
            await session.execute(
                text("INSERT INTO audit (user_id, action, shortcode, timestamp) "
                     "VALUES (:u, 'submitted', :c, :t)"),
                {"u": uid, "c": code, "t": ts_str}
            )
            await session.commit()
            await update.message.reply_text(f"✅ @{post.owner_username} submitted ({views0} views).")
            await log_to_group(context.bot, f"User @{update.effective_user.username} submitted {code}")
        except Exception:
            await update.message.reply_text("⚠️ Already submitted.")

# /stats
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT id, username FROM reels WHERE user_id=:u"), {"u": uid})
        reels = res.all()
    if not reels:
        return await update.message.reply_text("📭 No reels tracked.")
    total, users = 0, set()
    details = []
    for rid, uname in reels:
        async with AsyncSessionLocal() as session:
            rv = await session.execute(
                text("SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"), {"r": rid}
            )
            row = rv.fetchone()
        cnt = row[0] if row else 0
        total += cnt
        users.add(uname)
        details.append((uname, cnt))
    # sort by count desc
    details.sort(key=lambda x: x[1], reverse=True)
    text = [
        "Your stats:",
        f"• Total videos: {len(reels)}",
        f"• Total views: {total}",
        f"• Accounts linked: {', '.join(users)}",
        "Reels (highest→lowest):"
    ]
    for i, (uname, cnt) in enumerate(details, 1):
        text.append(f"{i}. @{uname} – {cnt} views")
    await update.message.reply_text("\n".join(text))
    await log_to_group(context.bot, f"User @{update.effective_user.username} checked stats")

# /remove
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Usage: /remove <Instagram Reel URL>")
    code = extract_shortcode(context.args[0])
    if not code:
        return await update.message.reply_text("❌ Invalid Reel URL.")
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            text("SELECT id FROM reels WHERE user_id=:u AND shortcode=:c"), {"u": uid, "c": code}
        )
        row = res.fetchone()
        if not row:
            return await update.message.reply_text("❌ You never submitted that reel.")
        rid = row[0]
        await session.execute(text("DELETE FROM views WHERE reel_id=:r"), {"r": rid})
        await session.execute(text("DELETE FROM reels WHERE id=:r"), {"r": rid})
        await session.execute(
            text("INSERT INTO audit (user_id, action, shortcode, timestamp) VALUES "
                 "(:u, 'removed', :c, :t)"),
            {"u": uid, "c": code, "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        )
        # cleanup user if none left
        rc = await session.execute(text("SELECT COUNT(*) FROM reels WHERE user_id=:u"), {"u": uid})
        if rc.fetchone()[0] == 0:
            await session.execute(text("DELETE FROM users WHERE user_id=:u"), {"u": uid})
        await session.commit()
    await update.message.reply_text(f"✅ Removed `{code}`.")
    await log_to_group(context.bot, f"User @{update.effective_user.username} removed {code}")

# /adminstats (text file)
async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    users_data = []
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT user_id, username FROM users"))
        for user_id, uname in res.all():
            rres = await session.execute(text("SELECT id, shortcode FROM reels WHERE user_id=:u"), {"u": user_id})
            reels = rres.all()
            total_views = 0
            details = []
            for rid, code in reels:
                vres = await session.execute(
                    text("SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"), {"r": rid}
                )
                cnt = vres.fetchone()[0] if vres.fetchone() else 0
                total_views += cnt
                details.append((code, cnt))
            details.sort(key=lambda x: x[1], reverse=True)
            users_data.append((uname or str(user_id), len(reels), total_views, details))
    # sort desc by views
    users_data.sort(key=lambda x: x[2], reverse=True)
    lines = []
    for uname, vids, views, det in users_data:
        lines.append(f"@{uname}")
        lines.append(f"Total views: {views}")
        lines.append(f"Total videos: {vids}")
        for code, cnt in det:
            lines.append(f"https://instagram.com/reel/{code} – {cnt}")
        lines.append("")
    path = "/mnt/data/admin_stats.txt"
    with open(path, "w") as f:
        f.write("\n".join(lines))
    await update.message.reply_document(open(path, "rb"), filename="admin_stats.txt")
    await log_to_group(context.bot, f"Admin @{update.effective_user.username} generated full report")

# /auditlog
async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    lines = []
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT user_id, action, shortcode, timestamp FROM audit ORDER BY id DESC LIMIT 20"))
        for u, action, code, ts in res.all():
            lines.append(f"{ts} — @{(await session.execute(text('SELECT username FROM users WHERE user_id=:u'), {'u': u})).fetchone()[0]} {action} `{code}`")
    await update.message.reply_text("📋 Recent Activity:\n" + "\n".join(lines))

# /broadcast
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args:
        return
    text = "📢 " + " ".join(context.args)
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT user_id FROM users"))
        for (u,) in res.all():
            try:
                await context.bot.send_message(chat_id=u, text=text)
            except:
                pass
    await update.message.reply_text("✅ Broadcast sent.")

# /deleteuser
async def deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args:
        return
    target = context.args[0]
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE user_id=:t)"), {"t": target})
        await session.execute(text("DELETE FROM reels WHERE user_id=:t"), {"t": target})
        await session.execute(text("DELETE FROM users WHERE user_id=:t"), {"t": target})
        await session.commit()
    await update.message.reply_text(f"🧹 Deleted user {target}.")

# /deletereel
async def deletereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid) or not context.args:
        return
    code = context.args[0]
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE shortcode=:c)"), {"c": code})
        await session.execute(text("DELETE FROM reels WHERE shortcode=:c"), {"c": code})
        await session.commit()
    await update.message.reply_text(f"✅ Deleted reel `{code}`.")

# global error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    await log_to_group(context.bot, f"❗️ Error\n<pre>{tb}</pre>")

# startup
if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(init_db())
    app = ApplicationBuilder().token(TOKEN).build()

    # user commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addaccount", addaccount))
    app.add_handler(CommandHandler("userstats", userstats))
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

    # run webhook
    print("🤖 Running in webhook mode…")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        close_loop=False
    )
