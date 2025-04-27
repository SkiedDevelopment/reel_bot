
import os
import re
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update, ParseMode
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, String, BigInteger, text
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
import uvicorn

# Load environment variables
load_dotenv()

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_ID", "").split(",")))
ZYTE_API_KEY = os.getenv("ZYTE_API_KEY")  # Zyte API key
PORT = int(os.getenv("PORT", 8000))
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", 60))

if not all([TOKEN, DATABASE_URL, ZYTE_API_KEY]):
    print("❌ Must set TOKEN, DATABASE_URL, and ZYTE_API_KEY in .env")
    exit(1)

# FastAPI health check
app_fastapi = FastAPI()

@app_fastapi.get("/")
async def root():
    return {"message": "Bot is running 🚀"}

async def start_health_check_server():
    config = uvicorn.Config(app_fastapi, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

# Logging setup
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Error wrapper
def debug_handler(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception as e:
            if update.message:
                await update.message.reply_text(f"⚠️ Error: {e}")
            raise
    return wrapper

# Database setup
Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

class Reel(Base):
    __tablename__ = 'reels'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    shortcode = Column(String, nullable=False)
    last_views = Column(BigInteger, default=0)

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    registered = Column(Integer, default=0)

# Admin check
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# Zyte scraping functions
async def scrape_instagram_reel_views(shortcode: str) -> int:
    try:
        url = f"https://www.instagram.com/reel/{shortcode}/"
        response = httpx.get(
            "https://api.zyte.com/v1/extract",
            params={
                "apikey": ZYTE_API_KEY,
                "url": url,
                "render_js": "true"
            },
            timeout=30
        )
        response.raise_for_status()
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script"):
            if script.string and '"video_view_count"' in script.string:
                text_block = script.string
                start = text_block.find('"video_view_count":') + len('"video_view_count":')
                end = text_block.find(',', start)
                return int(text_block[start:end])
        return -1
    except Exception as e:
        logger.error(f"Zyte scraping error: {e}")
        return -1

# Cooldown tracking
user_cooldowns = {}

def can_use_command(user_id: int) -> bool:
    now = datetime.utcnow()
    last = user_cooldowns.get(user_id)
    if not last or (now - last).total_seconds() >= COOLDOWN_SEC:
        user_cooldowns[user_id] = now
        return True
    return False

async def fetch_reel_page(url: str) -> dict:
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
            return {"status_code": resp.status_code, "content": resp.text}
    except Exception as e:
        logger.error(f"Async Zyte fetch error: {e}")
        return None

# Telegram command handlers
@debug_handler
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    commands = (
        "👋 Welcome to *Reel Tracker Bot*!\n"
        "\n📋 *Available Commands:*\n"
        "/addreel <link> - Add a reel to track 🎯\n"
        "/myreels - View your tracked reels 📋\n"
        "/stats - Show your stats 📊\n"
        "/leaderboard - Global leaderboard 🏆\n"
        "/forceupdate - Force refresh views 🔄\n"
        "/checkapi - Check Zyte API status 🛠️\n"
        "/userstatsid <user_id> - Admin: Stats by user ID 🔍\n"
        "/auditlog - Admin: Download activity log 📂\n"
        "/deleteuser <user_id> - Admin: Remove user's reels 🚫\n"
        "/deletereel <shortcode> - Admin: Remove a reel ❌\n"
        "/broadcast <message> - Admin: Broadcast a message 📣"
    )
    await update.message.reply_text(commands, parse_mode=ParseMode.MARKDOWN)

@debug_handler
async def addreel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❗ Usage: /addreel <Instagram reel URL>")
    link = context.args[0]
    match = re.search(r"/reel/([^/?]+)/?", link)
    if not match:
        return await update.message.reply_text("❌ Invalid reel URL.")
    shortcode = match.group(1)
    async with async_session() as session:
        exists = (await session.execute(text(
            "SELECT id FROM reels WHERE shortcode=:s"
        ), {"s": shortcode})).first()
        if exists:
            return await update.message.reply_text("⚠️ Already tracking this reel.")
        session.add(Reel(user_id=update.effective_user.id, shortcode=shortcode))
        await session.commit()
    await update.message.reply_text("✅ Reel added!")

@debug_handler
async def myreels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with async_session() as session:
        result = await session.execute(text(
            "SELECT shortcode FROM reels WHERE user_id=:u"
        ), {"u": user_id})
        reels = result.scalars().all()
    if not reels:
        return await update.message.reply_text("😔 You have no reels tracked.")
    msg = "\n".join(f"https://www.instagram.com/reel/{r}/" for r in reels)
    await update.message.reply_text(f"🎥 Your Reels:\n{msg}")

@debug_handler
async def checkapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sample = "https://www.instagram.com/reel/Cx9L5JkNkfJ/"
    res = await fetch_reel_page(sample)
    if res and res.get("status_code") == 200:
        await update.message.reply_text("✅ Zyte API is reachable!")
    else:
        await update.message.reply_text("❌ Zyte API check failed.")

@debug_handler
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with async_session() as session:
        count, total_views = (await session.execute(text(
            "SELECT COUNT(*), COALESCE(SUM(last_views),0) FROM reels WHERE user_id=:u"
        ), {"u": user_id})).fetchone()
    await update.message.reply_text(
        f"📊 You: {count} reels, {total_views} total views.")

@debug_handler
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        rows = await session.execute(text(
            "SELECT user_id, SUM(last_views) AS total_views FROM reels GROUP BY user_id ORDER BY total_views DESC"
        ))
        users = rows.all()
    if not users:
        return await update.message.reply_text("🏁 No data.")
    lines = [f"{i+1}. ID {uid}: {views} views" for i, (uid, views) in enumerate(users)]
    await update.message.reply_text("\n".join(lines))

@debug_handler
async def forceupdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_use_command(user_id):
        return await update.message.reply_text(f"⏳ Wait {COOLDOWN_SEC} seconds.")
    status_msg = await update.message.reply_text("🔄 Updating...")
    async with async_session() as session:
        reels = (await session.execute(text(
            "SELECT id, shortcode FROM reels WHERE user_id=:u"
        ), {"u": user_id})).all()
        if not reels:
            return await status_msg.edit_text("❗ No reels to update.")
        updated = 0
        for rid, sc in reels:
            views = await scrape_instagram_reel_views(sc)
            if views != -1:
                await session.execute(text(
                    "UPDATE reels SET last_views=:v WHERE id=:i"
                ), {"v": views, "i": rid})
                updated += 1
            await asyncio.sleep(1)
        await session.commit()
    await status_msg.edit_text(f"✅ Updated {updated} reels!")

@debug_handler
async def userstatsid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized.")
    if not context.args:
        return await update.message.reply_text("❗ Usage: /userstatsid <user_id>")
    tid = context.args[0]
    async with async_session() as session:
        rows = await session.execute(text(
            "SELECT shortcode, last_views FROM reels WHERE user_id=:u"
        ), {"u": tid})
        reels = rows.all()
    if not reels:
        return await update.message.reply_text("❗ No data for that user.")
    lines = [f"https://www.instagram.com/reel/{sc}/ → {v} views" for sc, v in sorted(reels, key=lambda x: x[1], reverse=True)]
    await update.message.reply_text("📄 Stats for user {}:\n".format(tid) + "\n".join(lines))

@debug_handler
async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized.")
    report = []
    async with async_session() as session:
        users = (await session.execute(text("SELECT DISTINCT user_id FROM reels"))).scalars().all()
        for uid in users:
            rows = (await session.execute(text(
                "SELECT shortcode, last_views FROM reels WHERE user_id=:u"
            ), {"u": uid})).all()
            total = sum(v for _, v in rows)
            report.append(f"User {uid}: {len(rows)} reels, {total} views")
            for sc, v in sorted(rows, key=lambda x: x[1], reverse=True):
                report.append(f"  https://www.instagram.com/reel/{sc}/ → {v} views")
            report.append("")
    path = f"audit_log_{int(datetime.utcnow().timestamp())}.txt"
    with open(path, "w") as f:
        f.write("\n".join(report))
    await update.message.reply_document(open(path, "rb"))
    os.remove(path)

@debug_handler
async def deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized.")
    if not context.args:
        return await update.message.reply_text("❗ Usage: /deleteuser <user_id>")
    uid = context.args[0]
    async with async_session() as session:
        await session.execute(text("DELETE FROM reels WHERE user_id=:u"), {"u": uid})
        await session.commit()
    await update.message.reply_text(f"✅ Removed reels for user {uid}.")

@debug_handler
async def deletereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized.")
    if not context.args:
        return await update.message.reply_text("❗ Usage: /deletereel <shortcode>")
    sc = context.args[0]
    async with async_session() as session:
        await session.execute(text("DELETE FROM reels WHERE shortcode=:s"), {"s": sc})
        await session.commit()
    await update.message.reply_text(f"✅ Deleted reel {sc}.")

@debug_handler
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized.")
    if not context.args:
        return await update.message.reply_text("❗ Usage: /broadcast <message>")
    msg = " ".join(context.args)
    async with async_session() as session:
        users = (await session.execute(text("SELECT DISTINCT user_id FROM reels"))).scalars().all()
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
        except:
            continue
    await update.message.reply_text("✅ Broadcast sent.")

# Bot setup
app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start_cmd))
app.add_handler(CommandHandler("addreel", addreel))
app.add_handler(CommandHandler("myreels", myreels))
app.add_handler(CommandHandler("checkapi", checkapi))
app.add_handler(CommandHandler("stats", stats))
app.add_handler(CommandHandler("leaderboard", leaderboard))
app.add_handler(CommandHandler("forceupdate", forceupdate))
app.add_handler(CommandHandler("userstatsid", userstatsid))
app.add_handler(CommandHandler("auditlog", auditlog))
app.add_handler(CommandHandler("deleteuser", deleteuser))
app.add_handler(CommandHandler("deletereel", deletereel))
app.add_handler(CommandHandler("broadcast", broadcast))

async def main():
    # Initialize DB
    await init_db()
    # Start health-check server
    asyncio.create_task(start_health_check_server())
    # Start bot polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    print("Bot is running 🚀")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
