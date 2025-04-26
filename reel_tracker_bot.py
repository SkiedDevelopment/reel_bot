#!/usr/bin/env python3
import os
import sys
import re
import json
import asyncio
import traceback
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
from playwright.async_api import async_playwright

# Load environment
load_dotenv()
nest_asyncio.apply()

# --- Config ---
TOKEN = os.getenv("TOKEN")
ADMIN_IDS = [x.strip() for x in os.getenv("ADMIN_ID", "").split(",") if x.strip()]
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")
PORT = int(os.getenv("PORT", "10000"))
DATABASE_URL = os.getenv("DATABASE_URL")
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "60"))
COOKIE_FILE = "session_cookies.json"

# Check essential config
if not TOKEN or not DATABASE_URL:
    sys.exit("❌ TOKEN and DATABASE_URL must be set in .env")

# Normalize database URL for asyncpg
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Database setup
engine = create_async_engine(DATABASE_URL, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# --- Database initializer ---
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

# --- Session cookie helpers ---
async def load_cookies(context):
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, "r") as f:
            cookies = json.load(f)
        await context.add_cookies(cookies)

async def save_cookies(context):
    cookies = await context.cookies()
    with open(COOKIE_FILE, "w") as f:
        json.dump(cookies, f)

# --- Scrape Reel view count using Playwright ---
async def fetch_reel_views(shortcode: str) -> int | None:
    url = f"https://www.instagram.com/reel/{shortcode}/"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context()
            await load_cookies(context)
            page = await context.new_page()
            await page.goto(url, timeout=60000)
            await page.wait_for_selector('video', timeout=10000)

            element = await page.query_selector('xpath=//*[contains(text(),"views")]')
            if element:
                text = await element.inner_text()
                views = int(text.replace(",", "").replace("views", "").strip())
                await browser.close()
                return views
            else:
                await browser.close()
                return None
    except Exception as e:
        print(f"⚠️ Playwright error for {shortcode}: {e}")
        return None

# --- Tracking views in background ---
async def track_all_views():
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("SELECT id,shortcode FROM reels"))).all()
    if not rows:
        print("No reels to track.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        await load_cookies(context)
        page = await context.new_page()

        for rid, sc in rows:
            try:
                url = f"https://www.instagram.com/reel/{sc}/"
                await page.goto(url, timeout=60000)
                await page.wait_for_selector('video', timeout=10000)
                element = await page.query_selector('xpath=//*[contains(text(),"views")]')
                if element:
                    text = await element.inner_text()
                    views = int(text.replace(",", "").replace("views", "").strip())

                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    async with AsyncSessionLocal() as session:
                        await session.execute(text(
                            "INSERT INTO views(reel_id,timestamp,count) VALUES(:r,:t,:c)"
                        ), {"r": rid, "t": ts, "c": views})
                        await session.commit()
            except Exception as e:
                print(f"Error tracking {sc}: {e}")
            await asyncio.sleep(2)

        await browser.close()

# --- Helper functions ---
def extract_shortcode(link: str) -> str | None:
    m = re.search(r"instagram\\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(uid: int) -> bool:
    return str(uid) in ADMIN_IDS

# --- User commands ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Welcome to ReelTracker!\n\n"
        "/submit <links> — Track up to 5 reels\n"
        "/stats — View your stats\n"
        "/remove <link> — Stop tracking a reel\n"
        "/uploadsession — Admin: Upload IG session cookies"
    )

async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text or ""
    payload = raw[len("/submit"):].strip()
    links = [l.strip() for l in payload.replace("\\n", " ").split(",") if l.strip()]
    if not links or len(links) > 5:
        return await update.message.reply_text("❌ Usage: /submit <up to 5 Reel URLs>")

    uid, now = update.effective_user.id, datetime.now()
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text("SELECT last_submit FROM cooldowns WHERE user_id=:u"), {"u": uid})).fetchone()
        if row:
            last = datetime.fromisoformat(row[0])
            rem = COOLDOWN_SEC - (now - last).total_seconds()
            if rem > 0:
                msg = await update.message.reply_text(f"⌛ Try again in {int(rem)}s")
                asyncio.create_task(asyncio.sleep(30) and context.bot.delete_message(update.effective_chat.id, msg.message_id))
                return

        await s.execute(text(
            "INSERT INTO cooldowns(user_id,last_submit) VALUES(:u,:t) "
            "ON CONFLICT(user_id) DO UPDATE SET last_submit=EXCLUDED.last_submit"
        ), {"u": uid, "t": now.isoformat()})
        await s.commit()

    successes, failures = 0, []
    for link in links:
        sc = extract_shortcode(link)
        if not sc:
            failures.append((link, "invalid URL"))
            continue
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        async with AsyncSessionLocal() as s:
            try:
                await s.execute(text(
                    "INSERT INTO reels(user_id,shortcode,username) VALUES(:u,:c,'')"
                ), {"u": uid, "c": sc})
                await s.execute(text(
                    "INSERT INTO views(reel_id,timestamp,count) VALUES((SELECT id FROM reels WHERE user_id=:u AND shortcode=:c),:t,0)"
                ), {"u": uid, "c": sc, "t": ts})
                await s.execute(text(
                    "INSERT INTO audit(user_id,action,shortcode,timestamp) VALUES(:u,'submitted',:c,:t)"
                ), {"u": uid, "c": sc, "t": ts})
                await s.commit()
                successes += 1
            except Exception as e:
                failures.append((link, str(e)))

    lines = [f"✅ Submitted {successes} reel(s)."]
    if failures:
        lines.append("❌ Failures:")
        for l, r in failures:
            lines.append(f"- {l}: {r}")
    await update.message.reply_text("\\n".join(lines))

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with AsyncSessionLocal() as s:
        rres = await s.execute(text("SELECT id,username FROM reels WHERE user_id=:u"), {"u": uid})
        reels = rres.fetchall()
    if not reels:
        return await update.message.reply_text("📭 No tracked reels.")

    total, details = 0, []
    for rid, uname in reels:
        row = (await s.execute(text(
            "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
        ), {"r": rid})).fetchone()
        cnt = row[0] if row else 0
        total += cnt
        details.append((uname, cnt))
    details.sort(key=lambda x: x[1], reverse=True)
    lines = ["Your stats:", f"• Videos: {len(reels)}", f"• Views: {total}", "Reels:"]
    for i, (u, v) in enumerate(details, 1):
        lines.append(f"{i}. @{u or 'unknown'} – {v} views")
    await update.message.reply_text("\\n".join(lines))

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
        ), {"u": uid, "c": sc})).fetchone()
        if not row:
            return await update.message.reply_text("⚠️ Not tracked.")

        rid = row[0]
        await s.execute(text("DELETE FROM views WHERE reel_id=:r"), {"r": rid})
        await s.execute(text("DELETE FROM reels WHERE id=:r"), {"r": rid})
        await s.execute(text(
            "INSERT INTO audit(user_id,action,shortcode,timestamp) VALUES(:u,'removed',:c,:t)"
        ), {"u": uid, "c": sc, "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        await s.commit()
    await update.message.reply_text(f"🗑 Removed {sc}.")

# --- Admin commands ---

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    stats = []
    async with AsyncSessionLocal() as s:
        users = (await s.execute(text("SELECT user_id,username FROM users"))).all()
    for uid, uname in users:
        async with AsyncSessionLocal() as s:
            reels = (await s.execute(text("SELECT id FROM reels WHERE user_id=:u"), {"u": uid})).all()
        vids = len(reels)
        total = 0
        for (rid,) in reels:
            row = (await s.execute(text(
                "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
            ), {"r": rid})).fetchone()
            total += row[0] if row else 0
        stats.append((uname or str(uid), vids, total))
    stats.sort(key=lambda x: x[2], reverse=True)
    lines = ["🏆 Leaderboard:"]
    for u, vids, tot in stats:
        lines.append(f"@{u} — vids:{vids} views:{tot}")
    await update.message.reply_text("\\n".join(lines))

async def addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    if len(context.args) != 2:
        return await update.message.reply_text("❌ Usage: /addaccount <tg_id> @handle")
    tgt, hdl = context.args
    async with AsyncSessionLocal() as s:
        await s.execute(text(
            "INSERT INTO user_accounts(user_id,insta_handle) VALUES(:u,:h) ON CONFLICT DO NOTHING"
        ), {"u": int(tgt), "h": hdl})
        await s.commit()
    await update.message.reply_text(f"✅ Assigned {hdl} to {tgt}.")

async def removeaccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    if len(context.args) != 2:
        return await update.message.reply_text("❌ Usage: /removeaccount <tg_id> @handle")
    tgt, hdl = context.args
    async with AsyncSessionLocal() as s:
        res = await s.execute(text(
            "DELETE FROM user_accounts WHERE user_id=:u AND insta_handle=:h RETURNING *"
        ), {"u": int(tgt), "h": hdl})
        await s.commit()
    if res.rowcount:
        await update.message.reply_text(f"✅ Removed {hdl} from {tgt}.")
    else:
        await update.message.reply_text("⚠️ No such assignment.")

async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    data = []
    async with AsyncSessionLocal() as s:
        users = (await s.execute(text("SELECT user_id,username FROM users"))).all()
    for uid, uname in users:
        async with AsyncSessionLocal() as s:
            reels = (await s.execute(text("SELECT id,shortcode FROM reels WHERE user_id=:u"), {"u": uid})).all()
        vids = len(reels)
        tv = 0
        for rid, sc in reels:
            row = (await s.execute(text(
                "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
            ), {"r": rid})).fetchone()
            tv += row[0] if row else 0
        data.append((uname or str(uid), vids, tv))
    data.sort(key=lambda x: x[2], reverse=True)
    lines = []
    for uname, vids, views in data:
        lines.append(f"@{uname} — {vids} reels — {views} views")
    await update.message.reply_text("\\n".join(lines))

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Not authorized.")
    if not context.args:
        return await update.message.reply_text("❌ Usage: /broadcast <message>")
    msg = "📢 " + " ".join(context.args)
    async with AsyncSessionLocal() as s:
        users = (await s.execute(text("SELECT user_id FROM users"))).fetchall()
    for (u,) in users:
        try:
            await context.bot.send_message(u, msg)
        except:
            pass
    await update.message.reply_text("✅ Broadcast sent.")
# --- Upload session command ---
async def upload_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        return await update.message.reply_text("❌ Not authorized.")
    doc: Document = update.message.document
    if not doc:
        return await update.message.reply_text("❌ Please send a document file.")
    file = await context.bot.get_file(doc.file_id)
    await file.download_to_drive(COOKIE_FILE)
    await update.message.reply_text("✅ Session cookies updated. Restarting bot…")
    os._exit(0)

# --- Health check server ---
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def start_health_server():
    srv = web.Application()
    srv.router.add_get("/health", health)
    runner = web.AppRunner(srv)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

# --- Bot error handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    print(f"❗️ Unhandled error:\n{tb}")

# --- Main runner ---
async def main():
    await init_db()
    asyncio.create_task(start_health_server())
    asyncio.create_task(track_all_views())

    app = ApplicationBuilder().token(TOKEN).build()

    # User Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("submit", submit))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("remove", remove))

    # Admin Commands
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("addaccount", addaccount))
    app.add_handler(CommandHandler("removeaccount", removeaccount))
    app.add_handler(CommandHandler("adminstats", adminstats))
    app.add_handler(CommandHandler("broadcast", broadcast))

    # Upload Session Command
    app.add_handler(CommandHandler("uploadsession", upload_session))

    app.add_error_handler(error_handler)

    print("🤖 Bot running...")
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
