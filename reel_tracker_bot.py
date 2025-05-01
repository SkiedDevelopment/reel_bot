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

# ─── Load config ────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN        = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS    = set(map(int, os.getenv("ADMIN_ID", "").split(",")))  # e.g. "12345,67890"
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
        # create tables for reels and users
        await conn.run_sync(Base.metadata.create_all)
        # ensure manual views column
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_views BIGINT DEFAULT 0"
        ))
        # ensure allowed_accounts table
        await conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS allowed_accounts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                insta_handle VARCHAR NOT NULL
            )
            """
        ))

class Reel(Base):
    __tablename__ = "reels"
    id        = Column(Integer, primary_key=True)
    user_id   = Column(BigInteger, nullable=False)
    shortcode = Column(String, nullable=False)

class User(Base):
    __tablename__ = "users"
    id          = Column(BigInteger, primary_key=True)
    username    = Column(String, nullable=True)
    registered  = Column(Integer, default=0)
    total_views = Column(BigInteger, default=0)

# ─── Utilities ─────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def debug_handler(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # forward every command to log group
        if LOG_GROUP_ID and update.message:
            try:
                await context.bot.forward_message(
                    chat_id=LOG_GROUP_ID,
                    from_chat_id=update.effective_chat.id,
                    message_id=update.message.message_id
                )
            except Exception as e:
                logger.warning(f"Failed to forward log: {e}")
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
        "• <code>/addreel &lt;link&gt;</code> – Add a reel link to track",
        "• <code>/stats</code> – Your stats",
    ]
    if is_admin(update.effective_user.id):
        cmds += [
            "• <code>/addaccount &lt;user_id&gt; &lt;@handle&gt;</code> – Allow user's IG account",
            "• <code>/removeaccount &lt;user_id&gt;</code> – Revoke allowed IG account",
            "• <code>/clearreels</code> – Clear all reel links",
            "• <code>/addviews &lt;user_id&gt; &lt;views&gt;</code> – Add manual views to user",
            "• <code>/exportstats</code> – Export all users and their reel links",
        ]
    await update.message.reply_text("\n".join(cmds), parse_mode=ParseMode.HTML)

@debug_handler
async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 <b>Unauthorized.</b>", parse_mode=ParseMode.HTML)
    if len(context.args) != 2:
        return await update.message.reply_text("❗ Usage: /addaccount <user_id> <@instahandle>")
    uid = int(context.args[0])
    handle = context.args[1].lstrip("@")
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "INSERT INTO allowed_accounts (user_id, insta_handle) VALUES (:u, :h)"
        ), {"u": uid, "h": handle})
        await session.commit()
    await update.message.reply_text(f"✅ Allowed @{handle} for user {uid}.", parse_mode=ParseMode.HTML)

@debug_handler
async def removeaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 <b>Unauthorized.</b>", parse_mode=ParseMode.HTML)
    if len(context.args) != 1:
        return await update.message.reply_text("❗ Usage: /removeaccount <user_id>")
    uid = int(context.args[0])
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "DELETE FROM allowed_accounts WHERE user_id = :u"
        ), {"u": uid})
        await session.commit()
    await update.message.reply_text(f"🗑️ Removed allowed account for user {uid}.", parse_mode=ParseMode.HTML)

@debug_handler
async def addreel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❗ Provide a reel link.")
    match = re.search(r"/reel/([^/?]+)/?", context.args[0])
    if not match:
        return await update.message.reply_text("❌ Invalid link.")
    shortcode = match.group(1)
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        exists = await session.execute(text(
            "SELECT 1 FROM reels WHERE shortcode = :s"
        ), {"s": shortcode})
        if exists.scalar():
            return await update.message.reply_text("⚠️ Already added.", parse_mode=ParseMode.HTML)
        await session.execute(text(
            "INSERT INTO reels (user_id, shortcode) VALUES (:u, :s)"
        ), {"u": uid, "s": shortcode})
        await session.commit()
    await update.message.reply_text("✅ Reel link added!", parse_mode=ParseMode.HTML)

@debug_handler
async def removereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❗ Provide shortcode.")
    shortcode = context.args[0]
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "DELETE FROM reels WHERE shortcode = :s AND user_id = :u"
        ), {"s": shortcode, "u": uid})
        await session.commit()
    await update.message_reply_text("🗑️ Reel removed.", parse_mode=ParseMode.HTML)

@debug_handler
async def clearreels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message_reply_text("🚫 <b>Unauthorized.</b>", parse_mode=ParseMode.HTML)
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM reels"))
        await session.commit()
    await update.message.reply_text("✅ All reel links cleared.", parse_mode=ParseMode.HTML)

@debug_handler
async def addviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 <b>Unauthorized.</b>", parse_mode=ParseMode.HTML)
    if len(context.args) != 2:
        return await update.message.reply_text("❗ Usage: /addviews <user_id> <views>")
    target_id = int(context.args[0])
    views = int(context.args[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT 1 FROM users WHERE id = :u"), {"u": target_id})
        if res.scalar():
            await session.execute(text(
                "UPDATE users SET total_views = total_views + :v WHERE id = :u"
            ), {"v": views, "u": target_id})
        else:
            await session.execute(text(
                "INSERT INTO users (id, username, total_views) VALUES (:u, :un, :v)"
            ), {"u": target_id, "un": None, "v": views})
        await session.commit()
    await update.message.reply_text(f"✅ Added {views} views to user {target_id}.", parse_mode=ParseMode.HTML)

@debug_handler
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        res = await session.execute(text(
            "SELECT COUNT(*), COALESCE((SELECT total_views FROM users WHERE id = :u), 0) FROM reels WHERE user_id = :u"
        ), {"u": uid})
        total_videos, total_views = res.fetchone()
        res2 = await session.execute(text(
            "SELECT shortcode FROM reels WHERE user_id = :u"
        ), {"u": uid})
        reels = [r[0] for r in res2.fetchall()]
    msg = [
        f"📊 <b>Your Stats</b>",
        f"• Total vids: <b>{total_videos}</b>",
        f"• Total views: <b>{total_views}</b>",
        "",
        "🎥 <b>Your Reel Links:</b>"
    ]
    for sc in reels:
        msg.append(f"• https://www.instagram.com/reel/{sc}/")
    await update.message.reply_text("\n".join(msg), parse_mode=ParseMode.HTML)

@debug_handler
async def exportstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 <b>Unauthorized.</b>", parse_mode=ParseMode.HTML)
    async with AsyncSessionLocal() as session:
        users = await session.execute(text("SELECT id, username, total_views FROM users"))
        users_data = users.fetchall()
        reels = await session.execute(text("SELECT user_id, shortcode FROM reels"))
        reels_data = reels.fetchall()
    lines = []
    for uid, username, total_views in users_data:
        lines.append(f"User {uid} (@{username or '—'}) - Total views: {total_views}")
        user_reels = [sc for (u, sc) in reels_data if u == uid]
        for sc in user_reels:
            lines.append(f"  - https://www.instagram.com/reel/{sc}/")
        lines.append("")
    import io
    buf = io.BytesIO("\n".join(lines).encode('utf-8'))
    buf.name = 'stats.txt'
    await update.message.reply_document(document=buf, filename='stats.txt')

# ─── Bot startup ────────────────────────────────────────────────────────────────
async def run_bot():
    await init_db()
    asyncio.create_task(start_health_check_server())

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addaccount", addaccount))
    app.add_handler(CommandHandler("removeaccount", removeaccount))
    app.add_handler(CommandHandler("addreel", addreel))
    app.add_handler(CommandHandler("removereel", removereel))
    app.add_handler(CommandHandler("clearreels", clearreels))
    app.add_handler(CommandHandler("addviews", addviews))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("exportstats", exportstats))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(run_bot())
