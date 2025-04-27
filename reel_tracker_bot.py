import os
import re
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update
from telegram.constants import ParseMode
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
ZYTE_API_KEY = os.getenv("ZYTE_API_KEY")
PORT = int(os.getenv("PORT", "8000"))
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "60"))

if not all([TOKEN, DATABASE_URL, ZYTE_API_KEY]):
    print("❌ Must set TOKEN, DATABASE_URL, and ZYTE_API_KEY in .env")
    exit(1)

# FastAPI health check
tb_api = FastAPI()

@tb_api.get("/")
async def health():
    return {"status": "🟢 OK"}

async def run_health():
    config = uvicorn.Config(tb_api, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE reels ADD COLUMN IF NOT EXISTS last_views BIGINT DEFAULT 0;"))
        await conn.execute(text("ALTER TABLE reels ADD COLUMN IF NOT EXISTS owner VARCHAR;"))
        await conn.execute(text("ALTER TABLE allowed_accounts ADD COLUMN IF NOT EXISTS insta_handle VARCHAR;"))

class Reel(Base):
    __tablename__ = 'reels'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    shortcode = Column(String, nullable=False)
    last_views = Column(BigInteger, default=0)
    owner = Column(String)

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    username = Column(String)
    registered = Column(Integer, default=0)

class AllowedAccount(Base):
    __tablename__ = 'allowed_accounts'
    user_id = Column(BigInteger, primary_key=True)
    insta_handle = Column(String, nullable=False)

# Utils
def debug_handler(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception as e:
            if update.message:
                await update.message.reply_text(f"⚠️ Error: {e}")
            raise
    return wrapper

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# Scrape functions
def scrape_views(shortcode: str) -> int:
    url = f"https://www.instagram.com/reel/{shortcode}/"
    r = httpx.get(
        "https://api.zyte.com/v1/extract",
        params={"apikey": ZYTE_API_KEY, "url": url, "render_js": "true"}, timeout=30
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for s in soup.find_all("script"):
        if s.string and '"video_view_count"' in s.string:
            txt = s.string
            start = txt.find('"video_view_count":') + len('"video_view_count":')
            end = txt.find(',', start)
            return int(txt[start:end])
    return -1

async def fetch_page(url: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://api.zyte.com/v1/extract",
            params={"apikey": ZYTE_API_KEY, "url": url, "render_js": "true"}
        )
        r.raise_for_status()
        return {"status": r.status_code, "content": r.text}

# Track cooldowns
token_ts = {}

def can_use(uid: int) -> bool:
    now = datetime.utcnow()
    last = token_ts.get(uid)
    if not last or (now - last).total_seconds() >= COOLDOWN_SEC:
        token_ts[uid] = now
        return True
    return False

# Handlers
@debug_handler
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmds = (
        "🤖 <b>Reel Tracker Bot</b> 🤖\n\n"
        "🔒 <code>/addaccount &lt;uid&gt; &lt;@insta&gt;</code> - Admin: Allow a user's IG handle\n"
        "🔓 <code>/removeaccount &lt;uid&gt;</code> - Admin: Revoke a user's IG handle\n"
        "➕ <code>/addreel &lt;link&gt;</code> - Add a reel to track\n"
        "➖ <code>/removereel &lt;code&gt;</code> - Remove a tracked reel\n"
        "📋 <code>/myreels</code> - List your reels\n"
        "📊 <code>/stats</code> - Your stats\n"
        "🏆 <code>/leaderboard</code> - Admin: Global leaderboard\n"
        "🔧 <code>/checkapi</code> - Admin: Zyte API test\n"
        "🔄 <code>/forceupdate</code> - Admin: Refresh all reels"
    )
    await update.message.reply_text(cmds, parse_mode=ParseMode.HTML)

@debug_handler
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with async_session() as session:
        r_tot = await session.execute(text(
            "SELECT COALESCE(SUM(last_views),0), COUNT(*), COUNT(DISTINCT owner) "
            "FROM reels WHERE user_id = :u"
        ), {"u": uid})
        tot = r_tot.fetchone() or (0, 0, 0)
        r_top = await session.execute(text(
            "SELECT shortcode, last_views FROM reels WHERE user_id = :u "
            "ORDER BY last_views DESC LIMIT 10"
        ), {"u": uid})
        top_list = r_top.fetchall()
    msg = f"📊 <b>Your Stats</b> 📊\n"
    msg += f"• Total Views: <b>{tot[0]}</b>\n"
    msg += f"• Total Videos: <b>{tot[1]}</b>\n"
    msg += f"• Total Accounts: <b>{tot[2]}</b>\n\n"
    for code, views in top_list:
        msg += f"• <a href='https://www.instagram.com/reel/{code}/'>Reel</a> - <b>{views}</b> views\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

@debug_handler
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 <b>Unauthorized!</b>", parse_mode=ParseMode.HTML)
    async with async_session() as session:
        # Start from users to ensure alias u.id is valid
        result = await session.execute(text(
            "SELECT u.username, COUNT(r.id) AS vids, COALESCE(SUM(r.last_views), 0) AS views "
            "FROM users u "
            "LEFT JOIN reels r ON r.user_id = u.id "
            "GROUP BY u.username ORDER BY views DESC"
        ))
        data = result.fetchall()
    if not data:
        return await update.message.reply_text("🏁 No data available.")
    msg_lines = ["🏆 <b>Global Leaderboard</b> 🏆", ""]
    for name, vids, views in data:
        msg_lines.append(f"• {name} - {vids} vids - {views} views")
    msg = "
".join(msg_lines)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

@debug_handler
async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 *Unauthorized!*", parse_mode=ParseMode.MARKDOWN)
    if len(context.args) != 2:
        return await update.message.reply_text("❗ Usage: /addaccount <uid> <@handle>")
    uid = int(context.args[0])
    handle = context.args[1].lstrip('@')
    async with async_session() as s:
        obj = await s.get(AllowedAccount, uid)
        if obj:
            obj.insta_handle = handle
            resp = f"🔄 Updated allowed handle to @{handle} for user {uid}!"
        else:
            s.add(AllowedAccount(user_id=uid, insta_handle=handle))
            resp = f"✅ @{handle} is now allowed for user {uid}!"
        await s.commit()
    await update.message.reply_text(resp)

@debug_handler
async def removeaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 *Unauthorized!*", parse_mode=ParseMode.MARKDOWN)
    if len(context.args) != 1:
        return await update.message.reply_text("❗ Usage: /removeaccount <uid>")
    uid = int(context.args[0])
    async with async_session() as s:
        acc = await s.get(AllowedAccount, uid)
        if not acc:
            return await update.message.reply_text("⚠️ No account mapping found.")
        await s.delete(acc)
        await s.commit()
    await update.message.reply_text(f"🗑️ Removed allowed account for user {uid}.")

@debug_handler
async def addreel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❗ Usage: /addreel <Instagram reel URL>")
    uid = update.effective_user.id
    async with async_session() as s:
        mapping = await s.get(AllowedAccount, uid)
    if not mapping:
        return await update.message.reply_text("❌ You have no allowed Instagram handle. Ask an admin with /addaccount.")
    link = context.args[0]
    m = re.search(r"/reel/([^/?]+)/?", link)
    if not m:
        return await update.message.reply_text("❌ Invalid URL. Please send a valid Instagram reel link.")
    code = m.group(1)
    page = await fetch_page(f"https://www.instagram.com/reel/{code}/")
    owner = re.search(r'"username":"([^"]+)"', page['content'])
    if not owner or owner.group(1).lower() != mapping.insta_handle.lower():
        return await update.message.reply_text(f"❌ You can only add reels from @{mapping.insta_handle}.")
    async with async_session() as s:
        dup = await s.execute(text("SELECT 1 FROM reels WHERE shortcode=:c"), {"c": code})
        if dup.first():
            return await update.message.reply_text("⚠️ This reel is already tracked.")
        s.add(Reel(user_id=uid, shortcode=code, owner=owner.group(1), last_views=0))
        user = await s.get(User, uid)
        if user:
            user.username = update.effective_user.username
        else:
            s.add(User(id=uid, username=update.effective_user.username, registered=1))
        await s.commit()
    await update.message.reply_text("🎉 Reel added successfully!")

@debug_handler
async def removereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❗ Usage: /removereel <shortcode>")
    uid = update.effective_user.id
    code = context.args[0]
    async with async_session() as s:
        res = await s.execute(
            text("SELECT id FROM reels WHERE shortcode=:c AND user_id=:u"), {"c": code, "u": uid}
        )
        if not res.first():
            return await update.message.reply_text("⚠️ Reel not found or doesn't belong to you.")
        await s.execute(
            text("DELETE FROM reels WHERE shortcode=:c AND user_id=:u"), {"c": code, "u": uid}
        )
        await s.commit()
    await update.message.reply_text("🗑️ Reel removed.")

@debug_handler
async def myreels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with async_session() as s:
        res = await s.execute(text("SELECT shortcode FROM reels WHERE user_id=:u"), {"u": uid})
        lst = res.scalars().all()
    if not lst:
        return await update.message.reply_text("😔 You have no tracked reels.")
    msg = "📋 *Your Tracked Reels:*\n" + "\n".join(
        f"https://www.instagram.com/reel/{c}/" for c in lst
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

@debug_handler
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with async_session() as s:
        r1 = await s.execute(text(
            "SELECT COALESCE(SUM(last_views),0), COUNT(*), COUNT(DISTINCT owner) "
            "FROM reels WHERE user_id=:u"), {"u": uid}
        )
        tot = r1.fetchone() or (0, 0, 0)
        r2 = await s.execute(text(
            "SELECT shortcode,last_views FROM reels WHERE user_id=:u "
            "ORDER BY last_views DESC LIMIT 10"), {"u": uid}
        )
        top = r2.fetchall()
    msg = (
        f"📊 *Your Stats* 📊\n"
        f"• Total Views: *{tot[0]}*\n"
        f"• Total Videos: *{tot[1]}*\n"
        f"• Total Accounts: *{tot[2]}*\n\n"
    )
    for c, v in top:
        msg += f"• https://www.instagram.com/reel/{c}/ - *{v}* views\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

@debug_handler
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 *Unauthorized!*", parse_mode=ParseMode.MARKDOWN)
    async with async_session() as s:
        r = await s.execute(text(
            "SELECT u.username, COUNT(r.id), COALESCE(SUM(r.last_views),0) "
            "FROM reels r JOIN users u ON r.user_id=u.id "
            "GROUP BY u.username ORDER BY SUM(r.last_views) DESC"
        ))
        data = r.fetchall()
    if not data:
        return await update.message.reply_text("🏁 No data available.")
    msg = "🏆 *Global Leaderboard* 🏆\n\n"
    for name, vids, views in data:
        msg += f"• {name} - {vids} vids - {views} views\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

@debug_handler
async def checkapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 *Unauthorized!*", parse_mode=ParseMode.MARKDOWN)
    sample = "https://www.instagram.com/reel/Cx9L5JkNkfJ/"
    res = await fetch_page(sample)
    reply = "✅ Zyte API is reachable!" if res["status"] == 200 else "❌ Zyte API failed!"
    await update.message.reply_text(reply)

@debug_handler
async def forceupdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 *Unauthorized!*", parse_mode=ParseMode.MARKDOWN)
    async with async_session() as s:
        reels = await s.execute(text("SELECT id,shortcode FROM reels"))
        count = 0
        for rid, sc in reels.fetchall():
            v = scrape_views(sc)
            await s.execute(
                text("UPDATE reels SET last_views=:v WHERE id=:i"), {"v": v, "i": rid}
            )
            count += 1
            await asyncio.sleep(1)
        await s.commit()
    await update.message.reply_text(f"🔄 Refreshed *{count}* reels!", parse_mode=ParseMode.MARKDOWN)

# Setup
app = Application.builder().token(TOKEN).build()
for cmd, fn in [
    ("start", start_cmd), ("addaccount", addaccount), ("removeaccount", removeaccount),
    ("addreel", addreel), ("removereel", removereel), ("myreels", myreels),
    ("stats", stats), ("leaderboard", leaderboard), ("checkapi", checkapi),
    ("forceupdate", forceupdate)
]:
    app.add_handler(CommandHandler(cmd, fn))

async def main():
    await init_db()
    asyncio.create_task(run_health())
    await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
    logger.info("🤖 Bot is running! 🚀")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
