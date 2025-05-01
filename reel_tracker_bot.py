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

# ─── Load config ────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN        = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS    = set(map(int, os.getenv("ADMIN_ID", "").split(",")))
PORT         = int(os.getenv("PORT", 8000))
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", 0))

if not all([TOKEN, DATABASE_URL]):
    print("❌ TOKEN and DATABASE_URL must be set in .env")
    exit(1)

# ─── Logging ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── FastAPI health check ────────────────────────────────────────────────────────
app_fastapi = FastAPI()

@app_fastapi.get("/")
async def root():
    return {"message": "Bot is running 🚀"}

async def start_health_check_server():
    config = uvicorn.Config(app_fastapi, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

# ─── Database setup ──────────────────────────────────────────────────────────────
Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # manual views column
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_views BIGINT DEFAULT 0"
        ))
        # allowed accounts table
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS allowed_accounts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                insta_handle VARCHAR NOT NULL
            )
        """))

class Reel(Base):
    __tablename__ = "reels"
    id        = Column(Integer, primary_key=True)
    user_id   = Column(BigInteger, nullable=False)
    shortcode = Column(String, nullable=False)

class User(Base):
    __tablename__ = "users"
    user_id     = Column(BigInteger, primary_key=True)
    username    = Column(String, nullable=True)
    registered  = Column(Integer, default=0)
    total_views = Column(BigInteger, default=0)

# ─── Utilities ─────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def debug_handler(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # log every command
        if LOG_GROUP_ID and update.message:
            user = update.effective_user
            name = user.full_name
            handle = f"@{user.username}" if user.username else ""
            text = update.message.text or ""
            log_text = f"{name} {handle} executed: {text}"
            try:
                await context.bot.send_message(LOG_GROUP_ID, log_text)
            except Exception as e:
                logger.warning(f"Failed to send log: {e}")
        try:
            return await fn(update, context)
        except Exception as e:
            logger.exception("Error in handler")
            if update.message:
                await update.message.reply_text(f"⚠️ Error: {e}")
            raise
    return wrapper

# ─── Telegram Handlers ──────────────────────────────────────────────────────────
@debug_handler
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmds = [
        "👋 <b>Welcome to Reel Tracker Bot!</b>",
        "",
        "📋 <b>Available Commands:</b>",
        "• <code>/addreel &lt;link&gt;</code> – Add a reel link",
        "• <code>/removelink &lt;shortcode&gt;</code> – Remove a wrong reel link",
        "• <code>/stats</code> – Your stats",
    ]
    if is_admin(update.effective_user.id):
        cmds += [
            "• <code>/addaccount &lt;user_id&gt; &lt;@handle&gt;</code> – Link an IG account",
            "• <code>/removeaccount &lt;user_id&gt;</code> – Unlink an IG account",
            "• <code>/userstats &lt;user_id&gt;</code> – Get a user’s stats",
            "• <code>/allstats</code> – Broadcast all users’ reel links",
            "• <code>/clearreels</code> – Clear all reel links",
            "• <code>/addviews &lt;user_id&gt; &lt;views&gt;</code> – Add manual views",
            "• <code>/exportstats</code> – Export all user data",
        ]
    await update.message.reply_text("\n".join(cmds), parse_mode=ParseMode.HTML)

@debug_handler
async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized.", parse_mode=ParseMode.HTML)
    if len(context.args) != 2:
        return await update.message.reply_text("❗ Usage: /addaccount <user_id> <@handle>")
    uid = int(context.args[0])
    handle = context.args[1].lstrip("@")
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "INSERT INTO allowed_accounts (user_id, insta_handle) VALUES (:u, :h)"
        ), {"u": uid, "h": handle})
        await session.commit()
    await update.message.reply_text(f"✅ Linked @{handle} to user {uid}.", parse_mode=ParseMode.HTML)

@debug_handler
async def removeaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized.", parse_mode=ParseMode.HTML)
    if len(context.args) != 1:
        return await update.message.reply_text("❗ Usage: /removeaccount <user_id>")
    uid = int(context.args[0])
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "DELETE FROM allowed_accounts WHERE user_id = :u"
        ), {"u": uid})
        await session.commit()
    await update.message.reply_text(f"🗑️ Unlinked IG for user {uid}.", parse_mode=ParseMode.HTML)

@debug_handler
async def addreel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❗ Provide a reel link.")
    url = context.args[0]
    m = re.search(
        r"^(?:https?://)?(?:www\.|m\.)?instagram\.com/(?:(?P<sup>[^/]+)/)?reel/(?P<code>[^/?#&]+)",
        url
    )
    if not m:
        return await update.message.reply_text("❌ Invalid reel URL.")
    supplied = m.group("sup")
    short = m.group("code")
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        acc = await session.execute(text(
            "SELECT insta_handle FROM allowed_accounts WHERE user_id = :u"
        ), {"u": uid})
        row = acc.fetchone()
        if not row:
            return await update.message.reply_text("🚫 No linked account. Ask admin to /addaccount.", ParseMode.HTML)
        exp = row[0]
        if supplied:
            if supplied.lower() != exp.lower():
                return await update.message.reply_text(f"🚫 Reel not from @{exp}.", ParseMode.HTML)
        else:
            try:
                loader = Instaloader()
                post = Post.from_shortcode(loader.context, short)
            except Exception:
                return await update.message.reply_text("❌ Couldn’t fetch reel.", ParseMode.HTML)
            if post.owner_username.lower() != exp.lower():
                return await update.message.reply_text(
                    f"🚫 Reel belongs to @{post.owner_username}, not @{exp}.", ParseMode.HTML
                )
        dup = await session.execute(text(
            "SELECT 1 FROM reels WHERE shortcode = :s"
        ), {"s": short})
        if dup.scalar():
            return await update.message.reply_text("⚠️ Already added.", ParseMode.HTML)
        await session.execute(text(
            "INSERT INTO reels (user_id, shortcode) VALUES (:u, :s)"
        ), {"u": uid, "s": short})
        await session.commit()
    await update.message.reply_text("✅ Reel added!", parse_mode=ParseMode.HTML)

@debug_handler
async def removereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    short = context.args[0] if context.args else None
    if not short:
        return await update.message.reply_text("❗ Provide a shortcode.")
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "DELETE FROM reels WHERE shortcode = :s AND user_id = :u"
        ), {"s": short, "u": uid})
        await session.commit()
    await update.message.reply_text("🗑️ Reel removed.", parse_mode=ParseMode.HTML)

@debug_handler
async def clearreels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message_reply_text("🚫 Unauthorized.", parse_mode=ParseMode.HTML)
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM reels"))
        await session.commit()
    await update.message.reply_text("✅ All reels cleared.", parse_mode=ParseMode.HTML)

@debug_handler
async def addviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized.", parse_mode=ParseMode.HTML)
    if len(context.args)!=2:
        return await update.message.reply_text("❗ Usage: /addviews <user_id> <views>")
    tid, v = map(int, context.args)
    async with AsyncSessionLocal() as session:
        r=await session.execute(text("SELECT 1 FROM users WHERE user_id=:u"),{"u":tid})
        if r.scalar():
            await session.execute(text("UPDATE users SET total_views=total_views+:v WHERE user_id=:u"),{"v":v,"u":tid})
        else:
            await session.execute(text("INSERT INTO users(user_id,username,total_views) VALUES(:u,NULL,:v)"),{"u":tid,"v":v})
        await session.commit()
    await update.message.reply_text(f"✅ Added {v} views to {tid}.", parse_mode=ParseMode.HTML)

@debug_handler
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    async with AsyncSessionLocal() as s:
        tv=s.execute(text("SELECT COUNT(*) FROM reels WHERE user_id=:u"),{"u":uid}); total_videos=(await tv).scalar() or 0
        vw=s.execute(text("SELECT total_views FROM users WHERE user_id=:u"),{"u":uid}); row=(await vw).fetchone(); total_views=row[0] if row else 0
        rl=s.execute(text("SELECT shortcode FROM reels WHERE user_id=:u"),{"u":uid}); reels=[r[0] for r in(await rl).fetchall()]
        ah=s.execute(text("SELECT insta_handle FROM allowed_accounts WHERE user_id=:u"),{"u":uid}); handles=[r[0] for r in(await ah).fetchall()]
    msg=[f"📊<b>Your Stats</b>",f"•Vids:<b>{total_videos}</b>",f"•Views:<b>{total_views}</b>"]
    if handles: msg+= ["\n👤<b>Linked IG:</b>"]+[f"•@{h}" for h in handles]
    if reels: msg+= ["\n🎥<b>Your Reels:</b>"]+[f"•https://insta.../{sc}/" for sc in reels]
    await update.message.reply_text("\n".join(msg),parse_mode=ParseMode.HTML)

@debug_handler
async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message.reply_text("🚫Unauthorized.",parse_mode=ParseMode.HTML)
    if len(context.args)!=1: return await update.message.reply_text("❗ Usage:/userstats <user_id>")
    tid=int(context.args[0])
    async with AsyncSessionLocal() as s:
        hv=s.execute(text("SELECT total_views FROM users WHERE user_id=:u"),{"u":tid}); rv=(await hv).fetchone(); views=rv[0] if rv else 0
        rl=s.execute(text("SELECT shortcode FROM reels WHERE user_id=:u"),{"u":tid}); reels=[r[0] for r in(await rl).fetchall()]
        ah=s.execute(text("SELECT insta_handle FROM allowed_accounts WHERE user_id=:u"),{"u":tid}); row=(await ah).fetchone(); handle=row[0] if row else '—'
    msg=[f"📊<b>Stats for {tid}(@{handle})</b>",f"•Views:<b>{views}</b>","🎥 <b>Reels:</b>"]+[f"•https://insta.../{sc}/" for sc in reels]
    await update.message.reply_text("\n".join(msg),parse_mode=ParseMode.HTML)

@debug_handler
async def allstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return await update.message_reply_text("🚫Unauthorized.",parse_mode=ParseMode.HTML)
    async with AsyncSessionLocal() as s:
        us=s.execute(text("SELECT DISTINCT user_id FROM reels")); uids=[r[0] for r in(await us).fetchall()]
        for uid in uids:
            ah=s.execute(text("SELECT insta_handle FROM allowed_accounts WHERE user_id=:u"),{"u":uid}); h=(await ah).fetchone(); handle=h[0] if h else '—'
            rl=s.execute(text("SELECT shortcode FROM reels WHERE user_id=:u"),{"u":uid}); reels=[r[0] for r in(await rl).fetchall()]
            msg=[f"👤<b>User {uid}(@{handle})</b>","🎥<b>Reels:</b>"]+[f"•https://insta.../{sc}/" for sc in reels]
            await update.message.reply_text("\n".join(msg),parse_mode=ParseMode.HTML)

async def run_bot():
    await init_db()
    asyncio.create_task(start_health_check_server())
    app=ApplicationBuilder().token(TOKEN).build()
    for cmd,handler in [
        ("start",start_cmd),("addaccount",addaccount),("removeaccount",removeaccount),
        ("addreel",addreel),("removelink",removereel),("clearreels",clearreels),
        ("addviews",addviews),("stats",stats),("userstats",userstats),
        ("allstats",allstats),("exportstats",exportstats)
    ]:
        app.add_handler(CommandHandler(cmd,handler))
    await app.initialize();await app.start();await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(run_bot())
