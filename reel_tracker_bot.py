import os
import re
import asyncio
import logging
from datetime import datetime

from dotenv import load_dotenv
import httpx
from bs4 import BeautifulSoup

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
ZYTE_API_KEY = os.getenv("ZYTE_API_KEY")
PORT         = int(os.getenv("PORT", 8000))
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", 60))

if not all([TOKEN, DATABASE_URL, ZYTE_API_KEY]):
    print("❌ TOKEN, DATABASE_URL, and ZYTE_API_KEY must be set in .env")
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
        # create tables
        await conn.run_sync(Base.metadata.create_all)
        # add owner_handle if missing
        await conn.execute(text(
            "ALTER TABLE reels ADD COLUMN IF NOT EXISTS owner_handle VARCHAR"
        ))
        # allowed_accounts table
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS allowed_accounts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                insta_handle VARCHAR NOT NULL
            )
        """))

class Reel(Base):
    __tablename__ = "reels"
    id           = Column(Integer, primary_key=True)
    user_id      = Column(BigInteger, nullable=False)
    shortcode    = Column(String, nullable=False)
    last_views   = Column(BigInteger, default=0)
    owner_handle = Column(String, nullable=True)

class User(Base):
    __tablename__ = "users"
    id         = Column(BigInteger, primary_key=True)
    username   = Column(String, nullable=True)
    registered = Column(Integer, default=0)

# ─── Utilities ─────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

user_cooldowns: dict[int, datetime] = {}

def can_use_command(user_id: int) -> bool:
    now = datetime.utcnow()
    last = user_cooldowns.get(user_id)
    if not last or (now - last).total_seconds() >= COOLDOWN_SEC:
        user_cooldowns[user_id] = now
        return True
    return False

def debug_handler(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await fn(update, context)
        except Exception as e:
            logger.exception("Error in handler")
            if update.message:
                await update.message.reply_text(f"⚠️ Error: {e}")
            raise
    return wrapper

# ─── Zyte scraping ───────────────────────────────────────────────────────────────
async def scrape_instagram_reel_views(shortcode: str) -> int:
    url = f"https://www.instagram.com/reel/{shortcode}/"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://api.zyte.com/v1/extract",
                params={
                    "apikey": ZYTE_API_KEY,
                    "url": url,
                    "render_js": "true"
                }
            )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.find_all("script"):
            txt = tag.string or ""
            if "video_view_count" in txt:
                start = txt.find('"video_view_count":') + len('"video_view_count":')
                end   = txt.find(",", start)
                return int(txt[start:end])
        return -1
    except Exception as e:
        logger.error(f"Scraping error: {e}")
        return -1

# ─── Telegram Handlers ──────────────────────────────────────────────────────────
@debug_handler
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmds = [
        "👋 <b>Welcome to Reel Tracker Bot!</b>",
        "",
        "📋 <b>Available Commands:</b>",
        "• <code>/addreel &lt;link&gt;</code> – Add a reel to track",
        "• <code>/removereel &lt;shortcode&gt;</code> – Remove your reel",
        "• <code>/myreels</code> – List your tracked reels",
        "• <code>/stats</code> – Your stats"
    ]
    if is_admin(update.effective_user.id):
        cmds += [
            "• <code>/addaccount &lt;user_id&gt; &lt;@handle&gt;</code> – Allow user’s IG account",
            "• <code>/removeaccount &lt;user_id&gt;</code> – Revoke allowed IG account",
            "• <code>/forceupdate</code> – Force update all reels",
            "• <code>/checkapi</code> – API health check",
            "• <code>/leaderboard</code> – Global leaderboard",
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
        acct = await session.execute(text(
            "SELECT insta_handle FROM allowed_accounts WHERE user_id = :u"
        ), {"u": uid})
        row = acct.fetchone()
        if not row:
            return await update.message.reply_text(
                "🚫 You have no allowed Instagram account. Ask admin to /addaccount.",
                parse_mode=ParseMode.HTML
            )
        handle = row[0]
        exists = await session.execute(text(
            "SELECT 1 FROM reels WHERE shortcode = :s"
        ), {"s": shortcode})
        if exists.scalar():
            return await update.message.reply_text("⚠️ Already tracking.", parse_mode=ParseMode.HTML)
        await session.execute(text(
            "INSERT INTO reels (user_id, shortcode, last_views, owner_handle) "
            "VALUES (:u, :s, 0, :h)"
        ), {"u": uid, "s": shortcode, "h": handle})
        await session.commit()
    await update.message.reply_text("✅ Reel added!", parse_mode=ParseMode.HTML)

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
    await update.message.reply_text("🗑️ Reel removed.", parse_mode=ParseMode.HTML)

@debug_handler
async def myreels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        res = await session.execute(text(
            "SELECT shortcode FROM reels WHERE user_id = :u"
        ), {"u": uid})
        reels = [r[0] for r in res.fetchall()]
    if not reels:
        return await update.message.reply_text("😔 No reels yet.")
    lines = ["🎥 <b>Your Reels:</b>"]
    for sc in reels:
        lines.append(f"• <a href=\"https://www.instagram.com/reel/{sc}/\">{sc}</a>")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

@debug_handler
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        tot = await session.execute(text(
            "SELECT COALESCE(SUM(last_views), 0), COUNT(id) FROM reels WHERE user_id = :u"
        ), {"u": uid})
        total_views, total_videos = tot.fetchone()
        top = await session.execute(text(
            "SELECT shortcode, last_views FROM reels "
            "WHERE user_id = :u ORDER BY last_views DESC LIMIT 10"
        ), {"u": uid})
        top_reels = top.fetchall()
    msg = [
        f"📊 <b>Your Stats</b>",
        f"• Total views: <b>{total_views}</b>",
        f"• Total videos: <b>{total_videos}</b>",
        "",
        "🎥 <b>Top 10 Reels:</b>"
    ]
    for sc, v in top_reels:
        msg.append(f"• <a href=\"https://www.instagram.com/reel/{sc}/\">{sc}</a> – {v} views")
    await update.message.reply_text(
        "\n".join(msg),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

@debug_handler
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 <b>Unauthorized.</b>", parse_mode=ParseMode.HTML)
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("""
            SELECT u.username,
                   COUNT(r.id)             AS vids,
                   COALESCE(SUM(r.last_views), 0) AS views
            FROM users u
            LEFT JOIN reels r ON r.user_id = u.id
            GROUP BY u.username
            ORDER BY views DESC
        """))
        data = res.fetchall()
    if not data:
        return await update.message.reply_text("🏁 No data available.")
    lines = ["🏆 <b>Global Leaderboard</b>"]
    for uname, vids, views in data:
        lines.append(f"• {uname or '—'} – {vids} vids – {views} views")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

@debug_handler
async def forceupdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 <b>Unauthorized.</b>", parse_mode=ParseMode.HTML)
    if not can_use_command(update.effective_user.id):
        return await update.message.reply_text(f"⏳ Cooldown. Wait {COOLDOWN_SEC}s.")
    prog = await update.message.reply_text("🔄 Updating all reels...")
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT id, shortcode FROM reels"))
        reels = res.fetchall()
        count = 0
        for rid, sc in reels:
            v = await scrape_instagram_reel_views(sc)
            if v >= 0:
                await session.execute(text(
                    "UPDATE reels SET last_views = :v WHERE id = :i"
                ), {"v": v, "i": rid})
                count += 1
            await asyncio.sleep(1)
        await session.commit()
    await prog.edit_text(f"✅ Updated {count} reels.")

@debug_handler
async def checkapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 <b>Unauthorized.</b>", parse_mode=ParseMode.HTML)
    v = await scrape_instagram_reel_views("Cx9L5JkNkfJ")
    if v >= 0:
        await update.message.reply_text("✅ Zyte API OK.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Zyte API issue.", parse_mode=ParseMode.HTML)

# ─── Bot startup ────────────────────────────────────────────────────────────────
async def run_bot():
    # Initialize DB and apply migrations
    await init_db()

    # Start FastAPI health check
    asyncio.create_task(start_health_check_server())

    # Build Telegram app
    app = ApplicationBuilder().token(TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addaccount", addaccount))
    app.add_handler(CommandHandler("removeaccount", removeaccount))
    app.add_handler(CommandHandler("addreel", addreel))
    app.add_handler(CommandHandler("removereel", removereel))
    app.add_handler(CommandHandler("myreels", myreels))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("forceupdate", forceupdate))
    app.add_handler(CommandHandler("checkapi", checkapi))

    # Start the bot
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Keep alive
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(run_bot())
