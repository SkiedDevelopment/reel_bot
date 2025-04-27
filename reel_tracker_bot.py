#!/usr/bin/env python3

import os
import sys
import re
import asyncio
import traceback
import requests
from datetime import datetime
from aiohttp import web
from telegram import Update, Document
from telegram.constants import ParseMode
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

load_dotenv()

# --- Debug Handler Decorator ---
def debug_handler(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception as e:
            print(f"❌ Exception in {func.__name__}: {e}")
            await update.message.reply_text("⚠️ Oops! Something went wrong while processing your command.")
    return wrapper

# Bot Config
TOKEN        = os.getenv("TOKEN")
ADMIN_IDS    = [x.strip() for x in os.getenv("ADMIN_ID", "").split(",") if x.strip()]
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")
PORT         = int(os.getenv("PORT", "10000"))
DATABASE_URL = os.getenv("DATABASE_URL")
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "60"))
SCRAPINGBEE_API_KEY = os.getenv("SCRAPINGBEE_API_KEY")

if not TOKEN or not DATABASE_URL or not SCRAPINGBEE_API_KEY:
    sys.exit("❌ TOKEN, DATABASE_URL, and SCRAPINGBEE_API_KEY must be set in .env")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Remove any webhook
try: requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=true")
except: pass

# Database setup
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
            if stmt:
                await conn.execute(text(stmt))

# Helper functions
def extract_shortcode(link: str) -> str | None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(uid: int) -> bool:
    return str(uid) in ADMIN_IDS

async def log_to_group(bot, msg: str):
    if not LOG_GROUP_ID:
        print("⚠️ no LOG_GROUP_ID:", msg)
        return
    try:
        await bot.send_message(chat_id=int(LOG_GROUP_ID), text=msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        print("❌ log_to_group failed:", e)

# Fetch reel views using ScrapingBee
async def fetch_reel_views(shortcode: str) -> int | None:
    try:
        url = f"https://www.instagram.com/reel/{shortcode}/"
        params = {
            "api_key": SCRAPINGBEE_API_KEY,
            "url": url,
            "render_js": "true",
        }
        response = requests.get("https://app.scrapingbee.com/api/v1/", params=params, timeout=20)
        if response.status_code != 200:
            print(f"⚠️ ScrapingBee error {response.status_code}: {response.text}")
            return None

        html = response.text
        match = re.search(r'"video_view_count":(\d+)', html)
        if match:
            return int(match.group(1))
        else:
            print(f"⚠️ View count not found for {shortcode}")
            return None
    except Exception as e:
        print(f"⚠️ fetch_reel_views failed: {e}")
        return None

# Healthcheck server
async def health(request: web.Request) -> web.Response:
    return web.Response(text="✅ Alive!")

async def start_health():
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

# --- User Commands ---

# Decorator for Debugging
def debug_entry(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        name = f"@{user.username}" if user.username else (user.first_name or "") or str(user.id)
        cmd = update.message.text.split()[0] if update.message and update.message.text else "?"
        log_line = f"🛠 {name} ran {cmd} args={context.args}"
        print(log_line)
        await log_to_group(context.bot, log_line)
        try:
            return await fn(update, context, *args, **kwargs)
        except Exception as e:
            tb = "".join(traceback.format_exception(None, e, e.__traceback__))
            err = f"❌ Error in {cmd} by {name}:\n<pre>{tb}</pre>"
            print(err)
            await log_to_group(context.bot, err)
            await update.message.reply_text("⚠️ Oops, something went wrong.")
    return wrapper

# /start
@debug_entry
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 <b>Welcome to Reel Tracker Bot!</b>\n\n"
        "📄 Available Commands:\n"
        "• /submit <links> — Track up to 5 reels\n"
        "• /stats — Your tracked reels & views\n"
        "• /remove <link> — Remove a tracked reel\n"
        "• /checkapi — Check ScrapingBee API status\n"
        "• /forceupdate — Force update all views\n",
        parse_mode=ParseMode.HTML
    )

# /submit
@debug_entry
async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text or ""
    payload = raw[len("/submit"):].strip()
    links = [l.strip() for l in payload.replace("\n", " ").split(",") if l.strip()]
    if not links or len(links) > 5:
        return await update.message.reply_text("❌ Usage: /submit <up to 5 Reel URLs>")

    uid, now = update.effective_user.id, datetime.now()

    # Cooldown check
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            "SELECT last_submit FROM cooldowns WHERE user_id=:u"
        ), {"u": uid})).fetchone()

        if row:
            last = datetime.fromisoformat(row[0])
            rem = COOLDOWN_SEC - (now - last).total_seconds()
            if rem > 0:
                return await update.message.reply_text(f"⌛ Please wait {int(rem)} seconds before submitting again.")

        await session.execute(text(
            "INSERT INTO cooldowns(user_id, last_submit) VALUES(:u, :t) "
            "ON CONFLICT(user_id) DO UPDATE SET last_submit=EXCLUDED.last_submit"
        ), {"u": uid, "t": now.isoformat()})
        await session.commit()

    # Save reels
    successes, failures = 0, []
    for link in links:
        sc = extract_shortcode(link)
        if not sc:
            failures.append((link, "Invalid URL"))
            continue

        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        async with AsyncSessionLocal() as session:
            try:
                await session.execute(text(
                    "INSERT INTO reels(user_id, shortcode, username) VALUES(:u, :c, '')"
                ), {"u": uid, "c": sc})

                await session.execute(text(
                    "INSERT INTO views(reel_id, timestamp, count) VALUES("
                    "(SELECT id FROM reels WHERE user_id=:u AND shortcode=:c), :t, 0)"
                ), {"u": uid, "c": sc, "t": ts})

                await session.execute(text(
                    "INSERT INTO audit(user_id, action, shortcode, timestamp) VALUES"
                    "(:u, 'submitted', :c, :t)"
                ), {"u": uid, "c": sc, "t": ts})

                await session.commit()
                successes += 1
            except Exception as e:
                await session.rollback()
                failures.append((link, "Already submitted or error"))

    lines = [f"✅ Submitted {successes} reels."]
    if failures:
        lines.append("⚠️ Failed:")
        for link, reason in failures:
            lines.append(f"- {link}: {reason}")

    await update.message.reply_text("\n".join(lines))

# /stats
@debug_entry
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with AsyncSessionLocal() as session:
        rres = await session.execute(text("SELECT id, shortcode FROM reels WHERE user_id=:u"), {"u": uid})
        reels = rres.fetchall()

    if not reels:
        return await update.message.reply_text("📭 You are not tracking any reels.")

    total_views = 0
    details = []

    for rid, sc in reels:
        async with AsyncSessionLocal() as session:
            row = await session.execute(text(
                "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
            ), {"r": rid})
            count_row = row.fetchone()

        count = count_row[0] if count_row else 0
        total_views += count
        details.append((sc, count))

    details.sort(key=lambda x: x[1], reverse=True)

    msg = [
        f"📊 <b>Your Stats</b>\n",
        f"• Total Videos: {len(reels)}",
        f"• Total Views: {total_views}",
        "\n<b>Reels:</b>"
    ]
    for idx, (sc, views) in enumerate(details, 1):
        msg.append(f"{idx}. <a href='https://www.instagram.com/reel/{sc}/'>Link</a> — {views} views")

    await update.message.reply_text("\n".join(msg), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# /remove
@debug_entry
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❌ Usage: /remove <Reel URL>")

    uid = update.effective_user.id
    sc = extract_shortcode(context.args[0])
    if not sc:
        return await update.message.reply_text("❌ Invalid URL.")

    async with AsyncSessionLocal() as session:
        row = await session.execute(text(
            "SELECT id FROM reels WHERE user_id=:u AND shortcode=:c"
        ), {"u": uid, "c": sc})
        found = row.fetchone()

        if not found:
            return await update.message.reply_text("⚠️ Reel not found in your tracked list.")

        rid = found[0]

        await session.execute(text("DELETE FROM views WHERE reel_id=:r"), {"r": rid})
        await session.execute(text("DELETE FROM reels WHERE id=:r"), {"r": rid})
        await session.execute(text(
            "INSERT INTO audit(user_id, action, shortcode, timestamp) VALUES"
            "(:u, 'removed', :c, :t)"
        ), {"u": uid, "c": sc, "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        await session.commit()

    await update.message.reply_text("🗑 Reel removed successfully.")

# --- Admin Commands & Background Jobs ---

# / auditlog
@debug_handler
async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            "SELECT user_id, action, shortcode, timestamp FROM audit ORDER BY id DESC LIMIT 20"
        ))).fetchall()

    if not rows:
        await update.message.reply_text("📭 No recent audit logs found.")
        return

    lines = ["🕵️‍♂️ <b>Last 20 Actions:</b>"]
    for uid, action, shortcode, ts in rows:
        lines.append(f"• <b>{action}</b> by <code>{uid}</code> → <i>{shortcode}</i> ({ts})")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# /User stats
@debug_handler
async def userstatsid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: /userstatsid <telegram_user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Please provide a valid numeric user ID.")
        return

    async with AsyncSessionLocal() as session:
        user_acc = await session.execute(text(
            "SELECT insta_handle FROM user_accounts WHERE user_id = :uid"
        ), {"uid": target_id})
        accounts = [row[0] for row in user_acc.fetchall()]

        reels_data = await session.execute(text(
            "SELECT shortcode FROM reels WHERE user_id = :uid"
        ), {"uid": target_id})
        reels = [row[0] for row in reels_data.fetchall()]

    if not reels:
        await update.message.reply_text("😶 This user has no tracked reels.")
        return

    lines = [f"📋 <b>User ID:</b> <code>{target_id}</code>"]
    if accounts:
        lines.append("🔗 <b>Linked Accounts:</b> " + ", ".join(accounts))
    else:
        lines.append("🔗 No accounts linked.")

    lines.append(f"🎬 <b>Total Reels:</b> {len(reels)}")
    lines.append("")
    lines.append("<b>Reels:</b>")

    for idx, shortcode in enumerate(reels, 1):
        lines.append(f"{idx}. https://instagram.com/reel/{shortcode}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


# Track views
async def track_all_views():
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("SELECT id, shortcode FROM reels"))).all()
        for rid, sc in rows:
            try:
                views = await fetch_reel_views(sc)
                if views is not None:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    await session.execute(text(
                        "INSERT INTO views(reel_id, timestamp, count) VALUES(:r, :t, :c)"
                    ), {"r": rid, "t": ts, "c": views})
                    await session.commit()
            except Exception as e:
                await session.rollback()
                print(f"⚠️ Error tracking {sc}: {e}")
            await asyncio.sleep(1)  # gentle delay to avoid rate limit

# Background tracking loop
async def track_loop():
    await asyncio.sleep(5)  # give a small delay at start
    while True:
        print("🔄 Auto-tracking views...")
        await track_all_views()
        await asyncio.sleep(12 * 3600)  # every 12 hours

# /forceupdate
@debug_entry
async def force_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")

    await update.message.reply_text("🔄 Force updating all reels...")
    await track_all_views()
    await update.message.reply_text("✅ Force update completed.")

# /checkapi
@debug_entry
async def check_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        test_url = "https://app.scrapingbee.com/api/v1/"
        params = {
            "api_key": SCRAPINGBEE_API_KEY,
            "url": "https://www.instagram.com/",
            "render_js": "true",
        }
        response = requests.get(test_url, params=params, timeout=20)

        if response.status_code == 200:
            await update.message.reply_text("✅ ScrapingBee API is working.")
        else:
            await update.message.reply_text(f"⚠️ ScrapingBee returned {response.status_code}: {response.text}")
    except Exception as e:
        await update.message.reply_text(f"❌ API Error: {e}")

# /leaderboard
@debug_handler
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ You are not authorized to view the leaderboard.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT user_id FROM reels"))
        user_ids = set([row[0] for row in result.all()])

    leaderboard_text = "🏆 <b>Leaderboard</b> 🏆\n\n"

    for uid in user_ids:
        async with AsyncSessionLocal() as session:
            count = await session.execute(text("SELECT COUNT(*) FROM reels WHERE user_id=:uid"), {"uid": uid})
            views = await session.execute(text(
                "SELECT SUM(current_views) FROM reels WHERE user_id=:uid"
            ), {"uid": uid})
            
            total_reels = count.scalar_one()
            total_views = views.scalar_one() or 0

        leaderboard_text += f"👤 User {uid}\n🎬 Reels: {total_reels} | 👀 Views: {total_views}\n\n"

    await update.message.reply_text(leaderboard_text, parse_mode="HTML")

# Error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    await log_to_group(app.bot, f"❗️ Unhandled error:\n<pre>{tb}</pre>")


# --- Main Bot Setup & Start ---

async def main():
    await init_db()

    global app
    app = ApplicationBuilder().token(TOKEN).build()

    # --- USER COMMANDS ---
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("submit", submit))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("remove", remove))

    # --- ADMIN COMMANDS ---
    app.add_handler(CommandHandler("forceupdate", force_update))
    app.add_handler(CommandHandler("checkapi", check_api))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("userstatsid", userstatsid))
    app.add_handler(CommandHandler("auditlog", auditlog))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("deleteuser", deleteuser))
    app.add_handler(CommandHandler("deletereel", deletereel))

    # --- UPLOAD SESSION COMMAND ---
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("uploadsession", upload_session_start)],
        states={1: [MessageHandler(filters.Document.ALL, upload_session)]},
        fallbacks=[CommandHandler("cancel", upload_session_cancel)]
    )
    app.add_handler(conv_handler)

    app.add_error_handler(error_handler)

    # Background tasks
    asyncio.create_task(start_health_check_server())
    asyncio.create_task(track_loop())

    print("🤖 Bot is alive and running...")
    await app.run_polling(drop_pending_updates=True)

# --- Actual Start ---
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("👋 Bot stopped manually.")
