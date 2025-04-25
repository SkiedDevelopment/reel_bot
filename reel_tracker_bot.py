#!/usr/bin/env python3
import os
import sys
import re
import asyncio
import traceback
import requests
import instaloader
from datetime import datetime
from aiohttp import web
from telegram import Update, Document
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from dotenv import load_dotenv
import nest_asyncio

# ── Load .env & nested asyncio ────────────────────────────────────────────────
load_dotenv()
nest_asyncio.apply()

# ── Config ─────────────────────────────────────────────────────────────────────
TOKEN        = os.getenv("TOKEN")
ADMIN_IDS    = [x.strip() for x in os.getenv("ADMIN_ID","").split(",") if x.strip()]
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")
PORT         = int(os.getenv("PORT","10000"))
DATABASE_URL = os.getenv("DATABASE_URL")
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC","60"))
IG_USERNAME  = os.getenv("IG_USERNAME")
IG_PASSWORD  = os.getenv("IG_PASSWORD")
SESSION_FILE = f"{IG_USERNAME}.session" if IG_USERNAME else None

if not TOKEN or not DATABASE_URL:
    sys.exit("❌ TOKEN and DATABASE_URL must be set in .env")

# Normalize DATABASE_URL
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://","postgresql+asyncpg://",1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://","postgresql+asyncpg://",1)

# Remove any existing webhook
try: requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=true")
except: pass

# ── Instaloader session ────────────────────────────────────────────────────────
INSTALOADER = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_comments=False,
)
if SESSION_FILE and os.path.exists(SESSION_FILE):
    try: INSTALOADER.load_session_from_file(IG_USERNAME, SESSION_FILE); print("🔒 IG session loaded")
    except Exception as e: print("⚠️ IG session load failed:", e)
elif IG_USERNAME and IG_PASSWORD:
    try:
        INSTALOADER.login(IG_USERNAME, IG_PASSWORD)
        INSTALOADER.save_session_to_file(SESSION_FILE)
        print("✅ IG logged in & session saved")
    except Exception as e:
        print("⚠️ IG login failed:", e)
else:
    print("⚠️ No IG session & no credentials provided")

# ── Database ───────────────────────────────────────────────────────────────────
engine = create_async_engine(DATABASE_URL, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
async def init_db():
    ddl = """
    CREATE TABLE IF NOT EXISTS users (
      user_id BIGINT PRIMARY KEY,
      username TEXT
    );
    CREATE TABLE IF NOT EXISTS user_accounts (
      user_id BIGINT,
      insta_handle TEXT,
      PRIMARY KEY(user_id, insta_handle)
    );
    CREATE TABLE IF NOT EXISTS reels (
      id SERIAL PRIMARY KEY,
      user_id BIGINT,
      shortcode TEXT,
      username TEXT,
      UNIQUE(user_id, shortcode)
    );
    CREATE TABLE IF NOT EXISTS views (
      reel_id INTEGER,
      timestamp TEXT,
      count INTEGER
    );
    CREATE TABLE IF NOT EXISTS cooldowns (
      user_id BIGINT PRIMARY KEY,
      last_submit TEXT
    );
    CREATE TABLE IF NOT EXISTS audit (
      id SERIAL PRIMARY KEY,
      user_id BIGINT,
      action TEXT,
      shortcode TEXT,
      timestamp TEXT
    );
    """
    async with engine.begin() as conn:
        for stmt in ddl.split(";"):
            stmt = stmt.strip()
            if stmt: await conn.execute(text(stmt))

# ── Helpers ────────────────────────────────────────────────────────────────────
def extract_shortcode(link: str) -> str|None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(uid: int) -> bool:
    return str(uid) in ADMIN_IDS

async def log_to_group(bot, msg: str):
    if not LOG_GROUP_ID:
        print("⚠️ no LOG_GROUP_ID:", msg); return
    try: await bot.send_message(chat_id=int(LOG_GROUP_ID), text=msg, parse_mode="HTML")
    except Exception as e: print("❌ log_to_group failed:", e)

# ── Debug decorator ─────────────────────────────────────────────────────────────
def debug_entry(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user or update.message.from_user
        name = f"@{user.username}" if user.username else (user.first_name or "") or str(user.id)
        cmd = update.message.text.split()[0] if update.message and update.message.text else "?"
        log_line = f"🛠 {name} ran {cmd} args={context.args}"
        print(log_line); await log_to_group(context.bot, log_line)
        try: return await fn(update, context, *args, **kwargs)
        except Exception as e:
            tb = "".join(traceback.format_exception(None,e,e.__traceback__))
            err = f"❌ Error in {cmd} by {name}:\n<pre>{tb}</pre>"
            print(err); await log_to_group(context.bot, err)
            await update.message.reply_text("⚠️ Oops—something went wrong.")
    return wrapper

# ── Fetch views via Instaloader ────────────────────────────────────────────────
async def fetch_reel_views(shortcode: str) -> int|None:
    try:
        post = instaloader.Post.from_shortcode(INSTALOADER.context, shortcode)
        return post.video_view_count
    except Exception as e:
        print(f"⚠️ Instaloader error for {shortcode}: {e}")
        return None

# ── Tracking & Health ───────────────────────────────────────────────────────────
async def track_all_views():
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("SELECT id,shortcode FROM reels"))).all()
        for rid, sc in rows:
            try:
                views = await fetch_reel_views(sc)
                if views is not None:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    await session.execute(text(
                        "INSERT INTO views(reel_id,timestamp,count) VALUES(:r,:t,:c)"
                    ),{"r":rid,"t":ts,"c":views})
                    await session.commit()
            except Exception as e:
                await session.rollback()
                print(f"⚠️ track error for {sc}: {e}")
            await asyncio.sleep(1)

async def track_loop():
    await asyncio.sleep(5)
    while True:
        await track_all_views()
        await asyncio.sleep(12*3600)

async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def start_health():
    srv = web.Application()
    srv.router.add_get("/health", health)
    runner = web.AppRunner(srv)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

# ── /uploadsession Flow ─────────────────────────────────────────────────────────
UPLOAD_SESSION = 1

@debug_entry
async def uploadsession_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    await update.message.reply_text(
        "📤 Send your Instaloader session file named:\n"
        f"`{SESSION_FILE}`", parse_mode="Markdown"
    )
    return UPLOAD_SESSION

@debug_entry
async def uploadsession_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc: Document = update.message.document
    if not doc or not doc.file_name.endswith(".session"):
        return await update.message.reply_text("❌ That's not a .session file.")
    file = await context.bot.get_file(doc.file_id)
    dest = os.path.join(os.getcwd(), doc.file_name)
    await file.download_to_drive(dest)
    await update.message.reply_text("✅ Session saved. Restarting…")
    os._exit(0)

@debug_entry
async def uploadsession_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Upload cancelled.")
    return ConversationHandler.END

# ── User Commands ──────────────────────────────────────────────────────────────
@debug_entry
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Welcome to ReelTracker\n\n"
        "/submit <links> — Track up to 5 reels\n"
        "/stats          — Your tracked reels & views\n"
        "/remove <URL>   — Stop tracking one reel\n"
        "/uploadsession  — Admin: upload IG session file"
    )

@debug_entry
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! I'm alive.")

@debug_entry
async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text or ""
    payload = raw[len("/submit"):].strip()
    links = [l.strip() for l in payload.replace("\n"," ").split(",") if l.strip()]
    if not links or len(links)>5:
        return await update.message.reply_text("❌ Usage: /submit <up to 5 comma-separated Reel URLs>")
    uid, now = update.effective_user.id, datetime.now()
    # cooldown
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text(
            "SELECT last_submit FROM cooldowns WHERE user_id=:u"
        ),{"u":uid})).fetchone()
        if row:
            last = datetime.fromisoformat(row[0])
            rem = COOLDOWN_SEC - (now-last).total_seconds()
            if rem>0:
                msg = await update.message.reply_text(f"⌛ Try again in {int(rem)}s")
                asyncio.create_task(asyncio.sleep(30) and context.bot.delete_message(update.effective_chat.id, msg.message_id))
                return
        await s.execute(text(
            "INSERT INTO cooldowns(user_id,last_submit) VALUES(:u,:t) "
            "ON CONFLICT(user_id) DO UPDATE SET last_submit=EXCLUDED.last_submit"
        ),{"u":uid,"t":now.isoformat()})
        await s.commit()
    # account check
    async with AsyncSessionLocal() as s:
        res = await s.execute(text(
            "SELECT insta_handle FROM user_accounts WHERE user_id=:u"
        ),{"u":uid})
        allowed = [r[0].lstrip("@").lower() for r in res.fetchall()]
    if not allowed:
        return await update.message.reply_text("⚠️ No IG account assigned.")
    successes, failures = 0, []
    for link in links:
        sc = extract_shortcode(link)
        if not sc:
            failures.append((link,"invalid URL")); continue
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        async with AsyncSessionLocal() as s:
            await s.execute(text(
                "INSERT INTO users(user_id,username) VALUES(:u,:n) "
                "ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username"
            ),{"u":uid,"n":update.effective_user.username or ""})
            try:
                await s.execute(text(
                    "INSERT INTO reels(user_id,shortcode,username) VALUES(:u,:c,:h)"
                ),{"u":uid,"c":sc,"h":allowed[0]})
                await s.execute(text(
                    "INSERT INTO views(reel_id,timestamp,count) VALUES("
                    "(SELECT id FROM reels WHERE user_id=:u AND shortcode=:c),:t,0)"
                ),{"u":uid,"c":sc,"t":ts})
                await s.execute(text(
                    "INSERT INTO audit(user_id,action,shortcode,timestamp) VALUES"
                    "(:u,'submitted',:c,:t)"
                ),{"u":uid,"c":sc,"t":ts})
                await s.commit()
                successes+=1
            except:
                failures.append((link,"already submitted"))
    lines=[f"✅ Submitted {successes} reel(s)."]
    if failures:
        lines.append("❌ Failures:")
        for l,r in failures:
            lines.append(f"- {l}: {r}")
    await update.message.reply_text("\n".join(lines))

@debug_entry
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with AsyncSessionLocal() as s:
        rres = await s.execute(text("SELECT id,username FROM reels WHERE user_id=:u"),{"u":uid})
        reels = rres.fetchall()
    if not reels:
        return await update.message.reply_text("📭 No tracked reels.")
    total, details = 0, []
    for rid, uname in reels:
        row = (await s.execute(text(
            "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
        ),{"r":rid})).fetchone()
        cnt = row[0] if row else 0
        total+=cnt; details.append((uname,cnt))
    details.sort(key=lambda x:x[1],reverse=True)
    lines=[
        "Your stats:",
        f"• Videos: {len(reels)}",
        f"• Views: {total}",
        "Reels (high→low):"
    ]
    for i,(u,v) in enumerate(details,1):
        lines.append(f"{i}. @{u} – {v} views")
    await update.message.reply_text("\n".join(lines))

@debug_entry
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❌ Usage: /remove <Reel URL>")
    uid = update.effective_user.id
    sc = extract_shortcode(context.args[0])
    if not sc:
        return await update.message.reply_text("❌ Invalid URL.")
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text(
            "SELECT id FROM reels WHERE user_id=:u AND shortcode=:c"
        ),{"u":uid,"c":sc})).fetchone()
        if not row:
            return await update.message.reply_text("⚠️ Not tracked.")
        rid = row[0]
        await s.execute(text("DELETE FROM views WHERE reel_id=:r"),{"r":rid})
        await s.execute(text("DELETE FROM reels WHERE id=:r"),{"r":rid})
        await s.execute(text(
            "INSERT INTO audit(user_id,action,shortcode,timestamp) VALUES"
            "(:u,'removed',:c,:t)"
        ),{"u":uid,"c":sc,"t":datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        await s.commit()
    await update.message.reply_text(f"🗑 Removed {sc}.")

# ── Admin commands ──────────────────────────────────────────────────────────────
@debug_entry
async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    if len(context.args)!=2:
        return await update.message.reply_text("❌ Usage: /addaccount <tg_id> @handle")
    tgt,hdl=context.args
    async with AsyncSessionLocal() as s:
        await s.execute(text(
            "INSERT INTO user_accounts(user_id,insta_handle) VALUES(:u,:h) ON CONFLICT DO NOTHING"
        ),{"u":int(tgt),"h":hdl})
        await s.commit()
    await update.message.reply_text(f"✅ Assigned {hdl} to {tgt}.")
    await log_to_group(context.bot,f"👤 Admin assigned {hdl} to {tgt}")

@debug_entry
async def removeaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    if len(context.args)!=2:
        return await update.message.reply_text("❌ Usage: /removeaccount <tg_id> @handle")
    tgt,hdl=context.args
    async with AsyncSessionLocal() as s:
        res=await s.execute(text(
            "DELETE FROM user_accounts WHERE user_id=:u AND insta_handle=:h RETURNING *"
        ),{"u":int(tgt),"h":hdl})
        await s.commit()
    if res.rowcount:
        await update.message.reply_text(f"✅ Removed {hdl} from {tgt}.")
        await log_to_group(context.bot,f"👤 Admin removed {hdl} from {tgt}")
    else:
        await update.message.reply_text("⚠️ No such assignment.")

@debug_entry
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    stats=[]
    async with AsyncSessionLocal() as s:
        users=(await s.execute(text("SELECT user_id,username FROM users"))).all()
    for uid,uname in users:
        async with AsyncSessionLocal() as s:
            reels=(await s.execute(text("SELECT id FROM reels WHERE user_id=:u"),{"u":uid})).all()
        vids=len(reels); total=0
        for (rid,) in reels:
            row=(await s.execute(text(
                "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
            ),{"r":rid})).fetchone()
            total+=row[0] if row else 0
        stats.append((uname or str(uid),vids,total))
    stats.sort(key=lambda x:x[2],reverse=True)
    lines=["🏆 Leaderboard:"]
    for u,vids,tot in stats:
        lines.append(f"@{u} — vids:{vids} views:{tot}")
    await update.message.reply_text("\n".join(lines))

@debug_entry
async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args)!=1:
        return await update.message.reply_text("❌ Usage: /userstats <tg_id>")
    tgt=int(context.args[0])
    async with AsyncSessionLocal() as s:
        acs=await s.execute(text("SELECT insta_handle FROM user_accounts WHERE user_id=:u"),{"u":tgt})
        handles=[r[0] for r in acs.fetchall()]
        rres=await s.execute(text("SELECT id,shortcode FROM reels WHERE user_id=:u"),{"u":tgt})
        reels=rres.fetchall()
    total,det=0,[]
    for rid,sc in reels:
        row=(await s.execute(text(
            "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
        ),{"r":rid})).fetchone()
        cnt=row[0] if row else 0; total+=cnt; det.append((sc,cnt))
    det.sort(key=lambda x:x[1],reverse=True)
    lines=[f"Stats for {tgt}:","Accounts: "+(", ".join(handles) or "None"),f"Videos: {len(reels)}",f"Views: {total}","Reels:"]
    for i,(sc,c) in enumerate(det,1):
        lines.append(f"{i}. https://instagram.com/reel/{sc} — {c}")
    await update.message.reply_text("\n".join(lines))

@debug_entry
async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    data=[]
    async with AsyncSessionLocal() as s:
        users=(await s.execute(text("SELECT user_id,username FROM users"))).all()
    for uid,uname in users:
        async with AsyncSessionLocal() as s:
            reels=(await s.execute(text("SELECT id,shortcode FROM reels WHERE user_id=:u"),{"u":uid})).all()
        tv,det=0,[]
        for rid,sc in reels:
            row=(await s.execute(text(
                "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
            ),{"r":rid})).fetchone()
            cnt=row[0] if row else 0; tv+=cnt; det.append((sc,cnt))
        det.sort(key=lambda x: x[1],reverse=True)
        data.append((uname or str(uid),len(reels),tv,det))
    data.sort(key=lambda x:x[2],reverse=True)
    lines=[]
    for uname,vids,views,det in data:
        lines.append(f"@{uname} • vids={vids} views={views}")
        for sc,c in det:
            lines.append(f"  - https://instagram.com/reel/{sc} → {c}")
        lines.append("")
    fn="/tmp/admin_stats.txt"
    with open(fn,"w") as f:f.write("\n".join(lines))
    await update.message.reply_document(open(fn,"rb"),filename="admin_stats.txt")

@debug_entry
async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    async with AsyncSessionLocal() as s:
        rows=(await s.execute(text(
            "SELECT user_id,action,shortcode,timestamp FROM audit ORDER BY id DESC LIMIT 20"
        ))).fetchall()
    lines=["Recent activity:"]
    for u,a,c,t in rows:
        lines.append(f"{t} — {u} {a} {c}")
    await update.message.reply_text("\n".join(lines))

@debug_entry
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return await update.message.reply_text("❌ Usage: /broadcast <message>")
    msg="📢 "+" ".join(context.args)
    async with AsyncSessionLocal() as s:
        users=(await s.execute(text("SELECT user_id FROM users"))).fetchall()
    for (u,) in users:
        try: await context.bot.send_message(u,msg)
        except: pass
    await update.message.reply_text("✅ Broadcast sent.")

@debug_entry
async def deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args)!=1:
        return await update.message.reply_text("❌ Usage: /deleteuser <tg_id>")
    tgt=int(context.args[0])
    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE user_id=:u)"),{"u":tgt})
        await s.execute(text("DELETE FROM reels WHERE user_id=:u"),{"u":tgt})
        await s.execute(text("DELETE FROM user_accounts WHERE user_id=:u"),{"u":tgt})
        await s.execute(text("DELETE FROM users WHERE user_id=:u"),{"u":tgt})
        await s.commit()
    await update.message.reply_text(f"✅ Data for {tgt} deleted.")

@debug_entry
async def deletereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args)!=1:
        return await update.message.reply_text("❌ Usage: /deletereel <shortcode>")
    code=context.args[0]
    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE shortcode=:c)"),{"c":code})
        await s.execute(text("DELETE FROM reels WHERE shortcode=:c"),{"c":code})
        await s.commit()
    await update.message.reply_text(f"✅ Reel {code} removed.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb="".join(traceback.format_exception(None,context.error,context.error.__traceback__))
    await log_to_group(app.bot,f"❗️ Unhandled error:\n<pre>{tb}</pre>")

# ── Main ────────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    asyncio.create_task(start_health())
    asyncio.create_task(track_loop())

    global app
    app=ApplicationBuilder().token(TOKEN).build()

    # User handlers
    app.add_handler(CommandHandler("start",start_cmd))
    app.add_handler(CommandHandler("ping",ping))
    app.add_handler(CommandHandler("submit",submit))
    app.add_handler(CommandHandler("stats",stats))
    app.add_handler(CommandHandler("remove",remove))

    # Uploadsession
    conv=ConversationHandler(
        entry_points=[CommandHandler("uploadsession",uploadsession_start)],
        states={UPLOAD_SESSION:[MessageHandler(filters.Document.ALL,uploadsession_receive)]},
        fallbacks=[CommandHandler("cancel",uploadsession_cancel)]
    )
    app.add_handler(conv)

    # Admin handlers
    app.add_handler(CommandHandler("addaccount",addaccount))
    app.add_handler(CommandHandler("removeaccount",removeaccount))
    app.add_handler(CommandHandler("leaderboard",leaderboard))
    app.add_handler(CommandHandler("userstats",userstats))
    app.add_handler(CommandHandler("adminstats",adminstats))
    app.add_handler(CommandHandler("auditlog",auditlog))
    app.add_handler(CommandHandler("broadcast",broadcast))
    app.add_handler(CommandHandler("deleteuser",deleteuser))
    app.add_handler(CommandHandler("deletereel",deletereel))

    app.add_error_handler(error_handler)

    print("🤖 Bot running…")
    await app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    asyncio.run(main())
