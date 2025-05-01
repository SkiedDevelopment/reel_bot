import os
import re
import asyncio
import logging

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
TOKEN        = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS    = set(map(int, os.getenv("ADMIN_ID", "").split(",")))  # e.g. "12345,67890"
PORT         = int(os.getenv("PORT", 8000))
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

# ─── FastAPI health check ──────────────────────────────────────────────────────
app_fastapi = FastAPI()

@app_fastapi.get("/")
async def root():
    return {"message": "Bot is running 🚀"}

async def start_health_check_server():
    config = uvicorn.Config(
        app_fastapi, host="0.0.0.0", port=PORT, log_level="info"
    )
    server = uvicorn.Server(config)
    await server.serve()

# ─── Database setup ───────────────────────────────────────────────────────────
Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # ensure total_views column exists
        await conn.execute(text("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS total_views BIGINT DEFAULT 0
        """))
        # ensure allowed_accounts table exists
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
        # log every command to the log group
        if LOG_GROUP_ID and update.message:
            user = update.effective_user
            name = user.full_name
            handle = f"@{user.username}" if user.username else ""
            text = update.message.text or ""
            try:
                await context.bot.send_message(
                    LOG_GROUP_ID, f"{name} {handle}: {text}"
                )
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

# ─── Telegram Handlers ─────────────────────────────────────────────────────────

@debug_handler
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmds = [
        "👋 <b>Welcome to Reel Tracker Bot!</b>",
        "",
        "📋 <b>Commands:</b>",
        "• <code>/addreel &lt;link&gt;</code>",
        "• <code>/removelink &lt;shortcode&gt;</code>",
        "• <code>/stats</code>",
    ]
    if is_admin(update.effective_user.id):
        cmds += [
            "• <code>/addaccount &lt;user_id&gt; &lt;@handle&gt;</code>",
            "• <code>/removeaccount &lt;user_id&gt;</code>",
            "• <code>/userstats &lt;user_id&gt;</code>",
            "• <code>/allstats</code>",
            "• <code>/broadcast_all &lt;message&gt;</code>",
            "• <code>/broadcast &lt;user_id&gt; &lt;message&gt;</code>",
            "• <code>/clearreels</code>",
            "• <code>/addviews &lt;user_id&gt; &lt;views&gt;</code>",
            "• <code>/removeviews &lt;user_id&gt; &lt;views&gt;</code>",
            "• <code>/exportstats</code>",
        ]
    await update.message.reply_text(
        "\n".join(cmds), parse_mode=ParseMode.HTML
    )

@debug_handler
async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized")
    if len(context.args) != 2:
        return await update.message.reply_text(
            "Usage: /addaccount <user_id> <@handle>"
        )
    uid = int(context.args[0])
    handle = context.args[1].lstrip("@")
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "INSERT INTO allowed_accounts (user_id, insta_handle) "
            "VALUES (:u, :h)"
        ), {"u": uid, "h": handle})
        await session.commit()
    await update.message.reply_text(f"✅ Linked @{handle} to {uid}")

@debug_handler
async def removeaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized")
    if len(context.args) != 1:
        return await update.message.reply_text(
            "Usage: /removeaccount <user_id>"
        )
    uid = int(context.args[0])
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "DELETE FROM allowed_accounts WHERE user_id = :u"
        ), {"u": uid})
        await session.commit()
    await update.message.reply_text(f"🗑️ Unlinked {uid}")

@debug_handler
async def addreel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❗ Provide a reel link.")
    url = context.args[0]
    m = re.search(
        r"^(?:https?://)?(?:www\.|m\.)?instagram\.com/"
        r"(?:(?P<sup>[^/]+)/)?reel/(?P<code>[^/?#&]+)",
        url
    )
    if not m:
        return await update.message.reply_text("❌ Invalid reel URL.")
    supplied = m.group("sup")
    code = m.group("code")
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        acc = await session.execute(text(
            "SELECT insta_handle FROM allowed_accounts WHERE user_id = :u"
        ), {"u": uid})
        row = acc.fetchone()
        if not row:
            return await update.message.reply_text(
                "🚫 No IG linked—ask admin to /addaccount."
            )
        expected = row[0]
        if supplied:
            if supplied.lower() != expected.lower():
                return await update.message.reply_text(
                    f"🚫 That reel isn't from @{expected}."
                )
        else:
            try:
                post = Post.from_shortcode(
                    Instaloader().context, code
                )
            except Exception:
                return await update.message.reply_text(
                    "❌ Couldn't fetch reel data."
                )
            if post.owner_username.lower() != expected.lower():
                return await update.message.reply_text(
                    f"🚫 That reel belongs to @{post.owner_username}."
                )
        dup = await session.execute(text(
            "SELECT 1 FROM reels WHERE shortcode = :c"
        ), {"c": code})
        if dup.scalar():
            return await update.message.reply_text("⚠️ Already added.")
        await session.execute(text(
            "INSERT INTO reels (user_id, shortcode) VALUES (:u, :c)"
        ), {"u": uid, "c": code})
        await session.commit()
    await update.message.reply_text("✅ Reel added!")

@debug_handler
async def removereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = context.args[0] if context.args else None
    if not code:
        return await update.message.reply_text("❗ Provide a shortcode.")
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "DELETE FROM reels WHERE shortcode = :c AND user_id = :u"
        ), {"c": code, "u": uid})
        await session.commit()
    await update.message.reply_text("🗑️ Reel removed.")

@debug_handler
async def clearreels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized")
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM reels"))
        await session.commit()
    await update.message.reply_text("✅ All reels cleared.")

@debug_handler
async def addviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized")
    if len(context.args) != 2:
        return await update.message.reply_text(
            "Usage: /addviews <user_id> <views>"
        )
    tid, v = map(int, context.args)
    async with AsyncSessionLocal() as session:
        exists = await session.execute(text(
            "SELECT 1 FROM users WHERE user_id = :u"
        ), {"u": tid})
        if exists.scalar():
            await session.execute(text(
                "UPDATE users SET total_views = total_views + :v "
                "WHERE user_id = :u"
            ), {"v": v, "u": tid})
        else:
            await session.execute(text(
                "INSERT INTO users (user_id, username, total_views) "
                "VALUES (:u, NULL, :v)"
            ), {"u": tid, "v": v})
        await session.commit()
    await update.message.reply_text(f"✅ Added {v} views to {tid}")

@debug_handler
async def removeviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized")
    if len(context.args) != 2:
        return await update.message.reply_text(
            "Usage: /removeviews <user_id> <views>"
        )
    tid, v = map(int, context.args)
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            "UPDATE users SET total_views = GREATEST(total_views - :v, 0) "
            "WHERE user_id = :u"
        ), {"v": v, "u": tid})
        await session.commit()
    await update.message.reply_text(f"✅ Removed {v} views from {tid}")

@debug_handler
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        tv = await session.execute(text(
            "SELECT COUNT(*) FROM reels WHERE user_id = :u"
        ), {"u": uid})
        total_videos = tv.scalar() or 0
        vw = await session.execute(text(
            "SELECT total_views FROM users WHERE user_id = :u"
        ), {"u": uid})
        row = vw.fetchone()
        total_views = row[0] if row else 0
        rl = await session.execute(text(
            "SELECT shortcode FROM reels WHERE user_id = :u"
        ), {"u": uid})
        reels = [r[0] for r in rl.fetchall()]
        ah = await session.execute(text(
            "SELECT insta_handle FROM allowed_accounts WHERE user_id = :u"
        ), {"u": uid})
        handles = [r[0] for r in ah.fetchall()]
    msg = [
        f"📊 <b>Your Stats</b>",
        f"• Total vids: <b>{total_videos}</b>",
        f"• Total views: <b>{total_views}</b>",
    ]
    if handles:
        msg.append("👤 <b>Linked Instagram:</b>")
        for h in handles:
            msg.append(f"• @{h}")
    if reels:
        msg.append("🎥 <b>Your Reel Links:</b>")
        for sc in reels:
            msg.append(f"• https://www.instagram.com/reel/{sc}/")
    await update.message.reply_text("\n".join(msg), parse_mode=ParseMode.HTML)

@debug_handler
async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized")
    if len(context.args) != 1:
        return await update.message.reply_text(
            "Usage: /userstats <user_id>"
        )
    tid = int(context.args[0])
    # fetch Telegram name
    try:
        chat = await context.bot.get_chat(tid)
        full_name = (chat.first_name or "") + (" " + chat.last_name if chat.last_name else "")
        full_name = full_name.strip()
    except Exception:
        full_name = str(tid)
    async with AsyncSessionLocal() as session:
        vw = await session.execute(text(
            "SELECT total_views FROM users WHERE user_id = :u"
        ), {"u": tid})
        row = vw.fetchone()
        views = row[0] if row else 0
        rl = await session.execute(text(
            "SELECT shortcode FROM reels WHERE user_id = :u"
        ), {"u": tid})
        reels = [r[0] for r in rl.fetchall()]
        ah = await session.execute(text(
            "SELECT insta_handle FROM allowed_accounts WHERE user_id = :u"
        ), {"u": tid})
        row2 = ah.fetchone()
        handle = row2[0] if row2 else "—"
    msg = [
        f"📊 <b>Stats for {full_name} (@{handle})</b>",
        f"• Total views: <b>{views}</b>",
        "🎥 <b>Reels:</b>",
    ]
    for sc in reels:
        msg.append(f"• https://www.instagram.com/reel/{sc}/")
    await update.message.reply_text("\n".join(msg), parse_mode=ParseMode.HTML)

@debug_handler
async def allstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized")
    async with AsyncSessionLocal() as session:
        uids = [r[0] for r in (await session.execute(text(
            "SELECT DISTINCT user_id FROM reels"
        ))).fetchall()]
        for uid in uids:
            # fetch Telegram name
            try:
                chat = await context.bot.get_chat(uid)
                full_name = (chat.first_name or "") + (" " + chat.last_name if chat.last_name else "")
                full_name = full_name.strip()
            except Exception:
                full_name = str(uid)
            ah = await session.execute(text(
                "SELECT insta_handle FROM allowed_accounts WHERE user_id = :u"
            ), {"u": uid})
            row = ah.fetchone()
            handle = row[0] if row else "—"
            rl = await session.execute(text(
                "SELECT shortcode FROM reels WHERE user_id = :u"
            ), {"u": uid})
            reels = [r[0] for r in rl.fetchall()]
            msg = [
                f"👤 <b>{full_name} (@{handle})</b>",
                "🎥 <b>Reels:</b>",
            ]
            for sc in reels:
                msg.append(f"• https://www.instagram.com/reel/{sc}/")
            await context.bot.send_message(
                update.effective_chat.id,
                "\n".join(msg),
                parse_mode=ParseMode.HTML
            )

@debug_handler
async def broadcast_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized")
    if not context.args:
        return await update.message.reply_text(
            "Usage: /broadcast_all <message>"
        )
    message = " ".join(context.args)
    async with AsyncSessionLocal() as session:
        res = await session.execute(text(
            "SELECT DISTINCT user_id FROM allowed_accounts"
        ))
        uids = [r[0] for r in res.fetchall()]
    for uid in uids:
        try:
            await context.bot.send_message(uid, message)
        except Exception as e:
            logger.warning(f"Broadcast to {uid} failed: {e}")
    await update.message.reply_text("✅ Broadcast sent.", parse_mode=ParseMode.HTML)

@debug_handler
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized")
    if len(context.args) < 2:
        return await update.message.reply_text(
            "Usage: /broadcast <user_id> <message>"
        )
    tid = int(context.args[0])
    message = " ".join(context.args[1:])
    try:
        await context.bot.send_message(tid, message)
        await update.message.reply_text("✅ Message sent.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")

@debug_handler
async def exportstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("🚫 Unauthorized")
    async with AsyncSessionLocal() as session:
        users = (await session.execute(text(
            "SELECT u.user_id,u.username,u.total_views,a.insta_handle "
            "FROM users u LEFT JOIN allowed_accounts a "
            "ON u.user_id = a.user_id"
        ))).fetchall()
        reels = (await session.execute(text(
            "SELECT user_id, shortcode FROM reels"
        ))).fetchall()
    lines = []
    for uid, uname, views, insta in users:
        acct = f"@{insta}" if insta else "—"
        lines.append(f"User {uid} ({uname or '—'}), Views: {views}, Insta: {acct}")
        for u, sc in reels:
            if u == uid:
                lines.append(f"  • https://www.instagram.com/reel/{sc}/")
        lines.append("")
    import io
    buf = io.BytesIO("\n".join(lines).encode())
    buf.name = "stats.txt"
    await update.message.reply_document(document=buf, filename="stats.txt")

async def run_bot():
    await init_db()
    asyncio.create_task(start_health_check_server())

    app = ApplicationBuilder().token(TOKEN).build()

    handlers = [
        ("start", start_cmd),
        ("addaccount", addaccount),
        ("removeaccount", removeaccount),
        ("addreel", addreel),
        ("removelink", removereel),
        ("clearreels", clearreels),
        ("addviews", addviews),
        ("removeviews", removeviews),
        ("stats", stats),
        ("userstats", userstats),
        ("allstats", allstats),
        ("broadcast_all", broadcast_all),
        ("broadcast", broadcast),
        ("exportstats", exportstats),
    ]
    for cmd, handler in handlers:
        app.add_handler(CommandHandler(cmd, handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(run_bot())
