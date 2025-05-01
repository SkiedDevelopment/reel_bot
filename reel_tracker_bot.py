import os
import re
import asyncio
import logging
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from fastapi import FastAPI
import uvicorn

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, String, BigInteger, text

from instaloader import Instaloader, Post

# ─── Load config ─────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = set(map(int, os.getenv("ADMIN_ID", "").split(",")))
PORT = int(os.getenv("PORT", 8000))
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", 0))

if not all([TOKEN, DATABASE_URL]):
    print("❌ TOKEN and DATABASE_URL must be set in .env")
    exit(1)

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── FastAPI for health check ──────────────────────────────────────────────────
app_fastapi = FastAPI()

@app_fastapi.get("/")
async def root():
    return {"message": "Bot is running 🚀"}

async def start_health_check_server():
    config = uvicorn.Config(app_fastapi, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

# ─── Database setup ───────────────────────────────────────────────────────────
Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # ensure total_views column
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_views BIGINT DEFAULT 0"
        ))
        # ensure allowed_accounts table
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS allowed_accounts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                insta_handle VARCHAR NOT NULL
            )
        """))

# ─── ORM models ───────────────────────────────────────────────────────────────
class Reel(Base):
    __tablename__ = "reels"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    shortcode = Column(String, nullable=False)

class User(Base):
    __tablename__ = "users"
    user_id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    registered = Column(Integer, default=0)
    total_views = Column(BigInteger, default=0)

# ─── Utilities ─────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def debug_handler(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # log command
        if LOG_GROUP_ID and update.message:
            user = update.effective_user
            name = user.full_name
            handle = f"@{user.username}" if user.username else ""
            text = update.message.text or ""
            try:
                await context.bot.send_message(LOG_GROUP_ID, f"{name} {handle}: {text}")
            except Exception:
                logger.warning("Failed to send log message")
        try:
            return await fn(update, context)
        except Exception as e:
            logger.exception("Handler error")
            if update.message:
                await update.message.reply_text(f"⚠️ Error: {e}")
            raise
    return wrapper

# ─── Telegram handlers ─────────────────────────────────────────────────────────
@debug_handler
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmds = [
        "👋 <b>Welcome to Reel Tracker Bot!</b>",
        "", "📋 <b>Commands:</b>",
        "• /addreel <link>",
        "• /removelink <shortcode>",
        "• /stats"
    ]
    if is_admin(update.effective_user.id):
        cmds += [
            "• /addaccount <user_id> <@handle>",
            "• /removeaccount <user_id>",
            "• /userstats <user_id>",
            "• /allstats",
            "• /clearreels",
            "• /addviews <user_id> <views>",
            "• /removeviews <user_id> <views>",
            "• /exportstats"
        ]
    await update.message.reply_text("\n".join(cmds), parse_mode=ParseMode.HTML)

@debug_handler
async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized")
    if len(context.args) != 2:
        return await update.message.reply_text("Usage: /addaccount <user_id> <@handle>")
    uid = int(context.args[0]); handle = context.args[1].lstrip("@")
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "INSERT INTO allowed_accounts (user_id, insta_handle) VALUES (:u, :h)"
        ), {"u": uid, "h": handle})
        await session.commit()
    await update.message.reply_text(f"✅ Linked @{handle} to {uid}")

@debug_handler
async def removeaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("🚫 Unauthorized")
    if len(context.args)!=1: return await update.message.reply_text("Usage: /removeaccount <user_id>")
    uid=int(context.args[0])
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM allowed_accounts WHERE user_id=:u"),{"u":uid})
        await session.commit()
    await update.message.reply_text(f"🗑️ Unlinked {uid}")

@debug_handler
async def addreel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Provide reel link.")
    url=context.args[0]
    m=re.search(r"^(?:https?://)?(?:www\.|m\.)?instagram\.com/(?:(?P<sup>[^/]+)/)?reel/(?P<code>[^/?#&]+)",url)
    if not m: return await update.message.reply_text("Invalid reel URL.")
    sup=m.group("sup"); code=m.group("code"); uid=update.effective_user.id
    async with AsyncSessionLocal() as s:
        acc=await s.execute(text("SELECT insta_handle FROM allowed_accounts WHERE user_id=:u"),{"u":uid})
        row=acc.fetchone();
        if not row: return await update.message.reply_text("🚫 No IG linked.")
        exp=row[0]
        if sup:
            if sup.lower()!=exp.lower(): return await update.message.reply_text(f"🚫 Not @{exp}'s reel.")
        else:
            try: post=Post.from_shortcode(Instaloader().context,code)
            except: return await update.message.reply_text("❌ Can't fetch reel.")
            if post.owner_username.lower()!=exp.lower(): return await update.message.reply_text(f"🚫 Reel of @{post.owner_username}.")
        dup=await s.execute(text("SELECT 1 FROM reels WHERE shortcode=:c"),{"c":code})
        if dup.scalar(): return await update.message.reply_text("⚠️ Already added.")
        await s.execute(text("INSERT INTO reels (user_id,shortcode) VALUES (:u,:c)"),{"u":uid,"c":code})
        await s.commit()
    await update.message.reply_text("✅ Reel added!")

@debug_handler
async def removereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code=context.args[0] if context.args else None
    if not code: return await update.message.reply_text("Provide shortcode.")
    uid=update.effective_user.id
    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM reels WHERE shortcode=:c AND user_id=:u"),{"c":code,"u":uid})
        await s.commit()
    await update.message.reply_text("🗑️ Removed.")

@debug_handler
async def clearreels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("🚫 Unauthorized")
    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM reels")); await s.commit()
    await update.message.reply_text("✅ Cleared all reels.")

@debug_handler
async def addviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("🚫 Unauthorized")
    if len(context.args)!=2: return await update.message.reply_text("Usage: /addviews <user_id> <views>")
    tid, v=map(int,context.args)
    async with AsyncSessionLocal() as s:
        r=await s.execute(text("SELECT 1 FROM users WHERE user_id=:u"),{"u":tid})
        if r.scalar(): await s.execute(text("UPDATE users SET total_views=total_views+:v WHERE user_id=:u"),{"v":v,"u":tid})
        else: await s.execute(text("INSERT INTO users(user_id,username,total_views) VALUES(:u,NULL,:v)"),{"u":tid,"v":v})
        await s.commit()
    await update.message.reply_text(f"✅ Added {v} to {tid}")

@debug_handler
async def removeviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("🚫 Unauthorized")
    if len(context.args)!=2: return await update.message.reply_text("Usage: /removeviews <user_id> <views>")
    tid, v=map(int,context.args)
    async with AsyncSessionLocal() as s:
        await s.execute(text("UPDATE users SET total_views=GREATEST(total_views-:v,0) WHERE user_id=:u"),{"v":v,"u":tid})
        await s.commit()
    await update.message.reply_text(f"✅ Removed {v} from {tid}")

@debug_handler
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    async with AsyncSessionLocal() as s:
        tv=await s.execute(text("SELECT COUNT(*) FROM reels WHERE user_id=:u"),{"u":uid}); total_videos=tv.scalar() or 0
        vw=await s.execute(text("SELECT total_views FROM users WHERE user_id=:u"),{"u":uid}); row=vw.fetchone(); total_views=row[0] if row else 0
        rl=await s.execute(text("SELECT shortcode FROM reels WHERE user_id=:u"),{"u":uid}); reels=[r[0] for r in rl.fetchall()]
        ah=await s.execute(text("SELECT insta_handle FROM allowed_accounts WHERE user_id=:u"),{"u":uid}); handles=[r[0] for r in ah.fetchall()]
    msg=[f"📊<b>Your Stats</b>",f"•Vids:<b>{total_videos}</b>",f"•Views:<b>{total_views}</b>"]
    if handles: msg+= ["👤<b>Linked:</b>"]+[f"@{h}" for h in handles]
    if reels: msg+= ["🎥<b>Reels:</b>"]+[f"{sc}" for sc in reels]
    await update.message.reply_text("\n".join(msg),parse_mode=ParseMode.HTML)

@debug_handler
async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("🚫 Unauthorized")
    if len(context.args)!=1: return await update.message.reply_text("Usage: /userstats <user_id>")
    tid=int(context.args[0])
    async with AsyncSessionLocal() as s:
        vw=await s.execute(text("SELECT total_views FROM users WHERE user_id=:u"),{"u":tid}); row=vw.fetchone(); views=row[0] if row else 0
        rl=await s.execute(text("SELECT shortcode FROM reels WHERE user_id=:u"),{"u":tid}); reels=[r[0] for r in rl.fetchall()]
        ah=await s.execute(text("SELECT insta_handle FROM allowed_accounts WHERE user_id=:u"),{"u":tid}); row=ah.fetchone(); handle=row[0] if row else '—'
    msg=[f"📊<b>Stats {tid} (@{handle})</b>",f"•Views:<b>{views}</b>","🎥<b>Reels:</b>"]+reels
    await update.message.reply_text("\n".join(msg),parse_mode=ParseMode.HTML)

@debug_handler
async def allstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("🚫 Unauthorized")
    async with AsyncSessionLocal() as s:
        uids=[r[0] for r in (await s.execute(text("SELECT DISTINCT user_id FROM reels"))).fetchall()]
        for uid in uids:
            ah=await s.execute(text("SELECT insta_handle FROM allowed_accounts WHERE user_id=:u"),{"u":uid}); row=ah.fetchone(); handle=row[0] if row else '—'
            rl=await s.execute(text("SELECT shortcode FROM reels WHERE user_id=:u"),{"u":uid}); reels=[r[0] for r in rl.fetchall()]
            msg=[f"👤<b>User {uid} (@{handle})</b>","🎥<b>Reels:</b>"]+reels
            await update.message.reply_text("\n".join(msg),parse_mode=ParseMode.HTML)

@debug_handler
async def exportstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("🚫 Unauthorized")
    async with AsyncSessionLocal() as s:
        users_data=(await s.execute(text("SELECT u.user_id,u.username,u.total_views,a.insta_handle FROM users u LEFT JOIN allowed_accounts a ON u.user_id=a.user_id"))).fetchall()
        reels_data=(await s.execute(text("SELECT user_id,shortcode FROM reels"))).fetchall()
    lines=[]
    for uid,uname,views,insta in users_data:
        acct=f"@{insta}" if insta else '—'
        lines.append(f"User {uid}({uname or '—'}),Views:{views},Insta:{acct}")
        for u,sc in reels_data:
            if u==uid: lines.append(f" - {sc}")
        lines.append("")
    buf=__import__('io').BytesIO("\n".join(lines).encode());buf.name='stats.txt'
    await update.message.reply_document(document=buf,filename='stats.txt')

async def run_bot():
    await init_db(); asyncio.create_task(start_health_check_server())
    app=ApplicationBuilder().token(TOKEN).build()
    for cmd,handler in [("start",start_cmd),("addaccount",addaccount),("removeaccount",removeaccount),
                        ("addreel",addreel),("removelink",removereel),("clearreels",clearreels),
                        ("addviews",addviews),("removeviews",removeviews),("stats",stats),
                        ("userstats",userstats),("allstats",allstats),("exportstats",exportstats)]:
        app.add_handler(CommandHandler(cmd,handler))
    await app.initialize();await app.start();await app.updater.start_polling(drop_pending_updates=True);await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(run_bot())
