import os
import sys
import re
import asyncio
import nest_asyncio
import instaloader
import traceback
import requests
from datetime import datetime
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

# ── Patch asyncio for hosted envs ───────────────────────────────────────────────
nest_asyncio.apply()

# ── Configuration ────────────────────────────────────────────────────────────────
TOKEN        = os.getenv("TOKEN")
ADMIN_IDS    = [aid.strip() for aid in os.getenv("ADMIN_ID", "").split(",") if aid.strip()]
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")
PORT         = int(os.getenv("PORT", "10000"))
DATABASE_URL = os.getenv("DATABASE_URL")
COOLDOWN_SEC = 60  # seconds

if not TOKEN or not DATABASE_URL:
    sys.exit("❌ TOKEN and DATABASE_URL must be set in your .env")

# ensure asyncpg driver
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# clear old webhook to avoid conflicts
try:
    requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=true")
except:
    pass

# ── Database setup ───────────────────────────────────────────────────────────────
engine = create_async_engine(DATABASE_URL, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

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

# ── Helpers ──────────────────────────────────────────────────────────────────────
def extract_shortcode(link: str) -> str | None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(uid: int) -> bool:
    return str(uid) in ADMIN_IDS

async def log_to_group(bot, text: str):
    if LOG_GROUP_ID:
        try:
            await bot.send_message(chat_id=int(LOG_GROUP_ID), text=text)
        except:
            pass

# ── Background tracking ──────────────────────────────────────────────────────────
async def track_all_views():
    L = instaloader.Instaloader()
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("SELECT id, shortcode FROM reels"))).all()
    for reel_id, code in rows:
        for _ in range(3):
            try:
                post = instaloader.Post.from_shortcode(L.context, code)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                async with AsyncSessionLocal() as s2:
                    await s2.execute(
                        text("INSERT INTO views (reel_id, timestamp, count) VALUES (:r, :t, :c)"),
                        {"r": reel_id, "t": ts, "c": post.video_view_count}
                    )
                    await s2.commit()
                break
            except:
                await asyncio.sleep(2)

async def track_loop():
    await asyncio.sleep(5)
    while True:
        await track_all_views()
        await asyncio.sleep(12 * 3600)

# ── Health endpoint ─────────────────────────────────────────────────────────────
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def start_health():
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ── Command handlers ────────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! I’m ReelTrackerBot.\n"
        "Use /ping to check I’m alive.\n"
        "Commands:\n"
        "/submit <Reel URL> — submit a reel to track\n"
        "/stats — show your stats\n"
        "/remove <Reel URL> — stop tracking a reel\n\n"
        "Admin commands:\n"
        "/addaccount <tg_id> @handle — assign account\n"
        "/removeaccount <tg_id> @handle — unassign account\n"
        "/userstats <tg_id> — show a user’s stats\n"
        "/adminstats — download all stats\n"
        "/auditlog — recent activity\n"
        "/broadcast <msg> — message all users\n"
        "/deleteuser <tg_id> — wipe user data\n"
        "/deletereel <shortcode> — wipe a reel"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! Bot is active and ready.")

async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return await update.message.reply_text("❌ You’re not authorized.")
    if len(context.args) != 2:
        return await update.message.reply_text("❌ Usage: /addaccount <tg_id> @insta_handle")
    target, handle = context.args
    if not handle.startswith("@"):
        return await update.message.reply_text("❌ Handle must start with '@'.")
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("INSERT OR IGNORE INTO user_accounts (user_id, insta_handle) VALUES (:u, :h)"),
                {"u": int(target), "h": handle}
            )
            await session.commit()
        await update.message.reply_text(f"✅ Assigned {handle} to user {target}.")
        await log_to_group(context.bot, f"Admin @{user.username} assigned {handle} to {target}")
    except Exception:
        await update.message.reply_text("⚠️ Couldn’t assign account—admin notified.")
        tb = traceback.format_exc()
        await log_to_group(context.bot, f"Error in /addaccount:\n<pre>{tb}</pre>")

async def removeaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return await update.message.reply_text("❌ You’re not authorized.")
    if len(context.args) != 2:
        return await update.message.reply_text("❌ Usage: /removeaccount <tg_id> @insta_handle")
    target, handle = context.args
    try:
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                text("DELETE FROM user_accounts WHERE user_id=:u AND insta_handle=:h RETURNING *"),
                {"u": int(target), "h": handle}
            )
            await session.commit()
        if res.rowcount:
            await update.message.reply_text(f"✅ Removed {handle} from user {target}.")
            await log_to_group(context.bot, f"Admin @{user.username} removed {handle} from {target}")
        else:
            await update.message.reply_text("⚠️ No such assignment found.")
    except Exception:
        await update.message.reply_text("⚠️ Couldn’t remove account—admin notified.")
        tb = traceback.format_exc()
        await log_to_group(context.bot, f"Error in /removeaccount:\n<pre>{tb}</pre>")

async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args)!=1:
        return await update.message.reply_text("❌ Usage: /userstats <tg_id>")
    target = int(context.args[0])
    async with AsyncSessionLocal() as session:
        handles = [r[0] for r in (await session.execute(text(
            "SELECT insta_handle FROM user_accounts WHERE user_id=:u"),{"u":target})).all()]
        reels = (await session.execute(text(
            "SELECT id, shortcode FROM reels WHERE user_id=:u"),{"u":target})).all()
    total_views=0; details=[]
    for rid,code in reels:
        row=(await session.execute(text(
            "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"),{"r":rid}
        )).fetchone()
        cnt=row[0] if row else 0
        total_views+=cnt; details.append((code,cnt))
    details.sort(key=lambda x:x[1],reverse=True)
    lines=[f"Stats for {target}:",
           f"• Accounts: {', '.join(handles) or 'None'}",
           f"• Videos: {len(reels)}",
           f"• Views: {total_views}",
           "Reels (high→low):"]
    for i,(c,v) in enumerate(details,1):
        lines.append(f"{i}. https://instagram.com/reel/{c} – {v} views")
    await update.message.reply_text("\n".join(lines))

async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❌ Usage: /submit <Reel URL>")
    uid=update.effective_user.id; now=datetime.now()
    async with AsyncSessionLocal() as session:
        row=(await session.execute(text(
            "SELECT last_submit FROM cooldowns WHERE user_id=:u"),{"u":uid}
        )).fetchone()
        if row:
            last=datetime.fromisoformat(row[0])
            rem=COOLDOWN_SEC-(now-last).total_seconds()
            if rem>0:
                msg=await update.message.reply_text(f"⌛ Wait {int(rem)}s.")
                asyncio.create_task(asyncio.sleep(5) and context.bot.delete_message(update.effective_chat.id,msg.message_id))
                return
        await session.execute(text(
            "INSERT OR REPLACE INTO cooldowns (user_id, last_submit) VALUES (:u,:t)"
        ),{"u":uid,"t":now.isoformat()})
        await session.commit()
    code=extract_shortcode(context.args[0])
    if not code:
        return await update.message.reply_text("❌ Invalid Reel URL.")
    async with AsyncSessionLocal() as session:
        allowed=[h[0].lstrip("@").lower() for h in (await session.execute(text(
            "SELECT insta_handle FROM user_accounts WHERE user_id=:u"),{"u":uid}
        )).all())]
    if not allowed:
        return await update.message.reply_text("⚠️ No account assigned. Ask admin.")
    L=instaloader.Instaloader()
    try:
        post=instaloader.Post.from_shortcode(L.context,code)
    except:
        return await update.message.reply_text("⚠️ Couldn’t fetch reel; must be public.")
    if post.owner_username.lower() not in allowed:
        return await update.message.reply_text(f"❌ Reel not from your account(s): {', '.join('@'+a for a in allowed)}")
    ts=now.strftime("%Y-%m-%d %H:%M:%S"); views0=post.video_view_count
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "INSERT OR REPLACE INTO users (user_id, username) VALUES (:u,:n)"
        ),{"u":uid,"n":update.effective_user.username or ""})
        try:
            await session.execute(text(
                "INSERT INTO reels (user_id, shortcode, username) VALUES (:u,:c,:n)"
            ),{"u":uid,"c":code,"n":post.owner_username})
            await session.execute(text(
                "INSERT INTO views (reel_id,timestamp,count) VALUES ("
                "(SELECT id FROM reels WHERE user_id=:u AND shortcode=:c),:t,:v)"
            ),{"u":uid,"c":code,"t":ts,"v":views0})
            await session.execute(text(
                "INSERT INTO audit (user_id,action,shortcode,timestamp) VALUES (:u,'submitted',:c,:t)"
            ),{"u":uid,"c":code,"t":ts})
            await session.commit()
            await update.message.reply_text(f"✅ @{post.owner_username} submitted ({views0} views).")
        except:
            await update.message.reply_text("⚠️ You already submitted that reel.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    async with AsyncSessionLocal() as session:
        reels=(await session.execute(text(
            "SELECT id,username FROM reels WHERE user_id=:u"),{"u":uid}
        )).all()
    if not reels:
        return await update.message.reply_text("📭 You have no tracked reels.")
    total=0; details=[]
    for rid,uname in reels:
        row=(await session.execute(text(
            "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
        ),{"r":rid})).fetchone()
        cnt=row[0] if row else 0; total+=cnt; details.append((uname,cnt))
    details.sort(key=lambda x:x[1],reverse=True)
    lines=["Your stats:",
           f"• Videos: {len(reels)}",
           f"• Views: {total}",
           "Reels:"]
    for i,(u,v) in enumerate(details,1):
        lines.append(f"{i}. @{u} – {v} views")
    await update.message.reply_text("\n".join(lines))

async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❌ Usage: /remove <Reel URL>")
    uid=update.effective_user.id; code=extract_shortcode(context.args[0])
    if not code:
        return await update.message.reply_text("❌ Invalid URL.")
    async with AsyncSessionLocal() as session:
        row=(await session.execute(text(
            "SELECT id FROM reels WHERE user_id=:u AND shortcode=:c"
        ),{"u":uid,"c":code})).fetchone()
        if not row:
            return await update.message.reply_text("⚠️ That reel isn’t tracked by you.")
        rid=row[0]
        await session.execute(text("DELETE FROM views WHERE reel_id=:r"),{"r":rid})
        await session.execute(text("DELETE FROM reels WHERE id=:r"),{"r":rid})
        await session.execute(text(
            "INSERT INTO audit (user_id,action,shortcode,timestamp) VALUES "
            "(:u,'removed',:c,:t)"
        ),{"u":uid,"c":code,"t":datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        await session.commit()
    await update.message.reply_text(f"🗑 Removed reel {code}.")

async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You’re not authorized.")
    data=[]
    async with AsyncSessionLocal() as session:
        users=(await session.execute(text("SELECT user_id,username FROM users"))).all()
    for uid,uname in users:
        async with AsyncSessionLocal() as session:
            reels=(await session.execute(text(
                "SELECT id,shortcode FROM reels WHERE user_id=:u"
            ),{"u":uid})).all()
        tv=0; det=[]
        for rid,code in reels:
            row=(await session.execute(text(
                "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
            ),{"r":rid})).fetchone()
            cnt=row[0] if row else 0; tv+=cnt; det.append((code,cnt))
        det.sort(key=lambda x:x[1],reverse=True)
        data.append((uname or str(uid),len(reels),tv,det))
    data.sort(key=lambda x:x[2],reverse=True)
    lines=[]
    for uname,vids,views,det in data:
        lines.append(f"@{uname} • vids={vids} views={views}")
        for code,cnt in det:
            lines.append(f"  - https://instagram.com/reel/{code} → {cnt}")
        lines.append("")
    fn="/tmp/admin_stats.txt"
    open(fn,"w").write("\n".join(lines))
    await update.message.reply_document(open(fn,"rb"),filename="admin_stats.txt")

async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You’re not authorized.")
    rows=(await AsyncSessionLocal().execute(text(
        "SELECT user_id,action,shortcode,timestamp FROM audit ORDER BY id DESC LIMIT 20"
    ))).all()
    lines=["Recent activity:"]
    for u,a,c,t in rows:
        lines.append(f"{t} — {u} {a} {c}")
    await update.message.reply_text("\n".join(lines))

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return await update.message.reply_text("❌ Usage: /broadcast <message>")
    msg="📢 "+" ".join(context.args)
    async with AsyncSessionLocal() as session:
        users=(await session.execute(text("SELECT user_id FROM users"))).all()
    for (u,) in users:
        try: await context.bot.send_message(u,msg)
        except: pass
    await update.message.reply_text("✅ Broadcast sent.")

async def deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args)!=1:
        return await update.message.reply_text("❌ Usage: /deleteuser <tg_id>")
    t=int(context.args[0])
    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE user_id=:u)"),{"u":t})
        await s.execute(text("DELETE FROM reels WHERE user_id=:u"),{"u":t})
        await s.execute(text("DELETE FROM user_accounts WHERE user_id=:u"),{"u":t})
        await s.execute(text("DELETE FROM users WHERE user_id=:u"),{"u":t})
        await s.commit()
    await update.message.reply_text(f"✅ All data for {t} deleted.")

async def deletereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args)!=1:
        return await update.message.reply_text("❌ Usage: /deletereel <shortcode>")
    code=context.args[0]
    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE shortcode=:c)"),{"c":code})
        await s.execute(text("DELETE FROM reels WHERE shortcode=:c"),{"c":code})
        await s.commit()
    await update.message.reply_text(f"✅ Reel {code} removed globally.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb="".join(traceback.format_exception(None,context.error,context.error.__traceback__))
    await log_to_group(app.bot,f"❗️ Unhandled error:\n<pre>{tb}</pre>")

# ── Main Entrypoint ─────────────────────────────────────────────────────────────
if __name__=="__main__":
    loop=asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    loop.create_task(start_health())
    loop.create_task(track_loop())

    app=ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",       start_cmd))
    app.add_handler(CommandHandler("ping",        ping))
    app.add_handler(CommandHandler("addaccount",  addaccount))
    app.add_handler(CommandHandler("removeaccount",removeaccount))
    app.add_handler(CommandHandler("userstats",   userstats))
    app.add_handler(CommandHandler("submit",      submit))
    app.add_handler(CommandHandler("stats",       stats))
    app.add_handler(CommandHandler("remove",      remove))
    app.add_handler(CommandHandler("adminstats",  adminstats))
    app.add_handler(CommandHandler("auditlog",    auditlog))
    app.add_handler(CommandHandler("broadcast",   broadcast))
    app.add_handler(CommandHandler("deleteuser",  deleteuser))
    app.add_handler(CommandHandler("deletereel",  deletereel))
    app.add_error_handler(error_handler)

    print("🤖 Bot running in polling mode…")
    app.run_polling(drop_pending_updates=True, close_loop=False)
