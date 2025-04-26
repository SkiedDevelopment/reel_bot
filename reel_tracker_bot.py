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
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from dotenv import load_dotenv
import nest_asyncio
from playwright.async_api import async_playwright

# --- Load .env and fix event loop ---
load_dotenv()
nest_asyncio.apply()

# --- Config Variables ---
TOKEN = os.getenv("TOKEN")
ADMIN_IDS = [x.strip() for x in os.getenv("ADMIN_ID", "").split(",") if x.strip()]
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID")
PORT = int(os.getenv("PORT", "10000"))
DATABASE_URL = os.getenv("DATABASE_URL")
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "60"))
COOKIE_FILE = "session_cookies.json"

if not TOKEN or not DATABASE_URL:
    sys.exit("❌ TOKEN and DATABASE_URL must be set in .env")

# Normalize DATABASE_URL
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# --- Setup Database ---
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

# --- Playwright Cookies Helpers ---
async def load_cookies(context):
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, "r") as f:
            cookies = json.load(f)
        await context.add_cookies(cookies)

async def save_cookies(context):
    cookies = await context.cookies()
    with open(COOKIE_FILE, "w") as f:
        json.dump(cookies, f, indent=2)

# --- Shortcode Extractor ---
def extract_shortcode(link: str) -> str | None:
    match = re.search(r"reel/([^/?#&]+)", link)
    return match.group(1) if match else None

# --- Admin Checker ---
def is_admin(uid: int) -> bool:
    return str(uid) in ADMIN_IDS

# --- Fetch Reel Views using Playwright ---
async def fetch_reel_views(shortcode: str) -> int | None:
    url = f"https://www.instagram.com/reel/{shortcode}/"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context()
            await load_cookies(context)
            page = await context.new_page()
            await page.goto(url, timeout=60000)
            await page.wait_for_selector('video', timeout=15000)

            spans = await page.query_selector_all("span")
            for span in spans:
                text_content = await span.inner_text()
                if "views" in text_content.lower():
                    views = int(text_content.replace(",", "").replace("views", "").strip())
                    await browser.close()
                    return views

            await browser.close()
            return None
    except Exception as e:
        print(f"⚠️ Playwright error for {shortcode}: {e}")
        return None

# --- Track All Views Task ---
async def track_all_views():
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("SELECT id,shortcode FROM reels"))).all()

    if not rows:
        print("📭 No reels to track.")
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

                spans = await page.query_selector_all("span")
                view_count = None
                for span in spans:
                    text_content = await span.inner_text()
                    if "views" in text_content.lower():
                        view_count = int(text_content.replace(",", "").replace("views", "").strip())
                        break

                if view_count is not None:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    async with AsyncSessionLocal() as session:
                        await session.execute(text(
                            "INSERT INTO views(reel_id,timestamp,count) VALUES(:r,:t,:c)"
                        ), {"r": rid, "t": ts, "c": view_count})
                        await session.commit()

            except Exception as e:
                print(f"⚠️ Error tracking {sc}: {e}")
            await asyncio.sleep(2)

        await browser.close()

# --- /start Command ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 <b>Welcome to ReelTracker!</b>\n\n"
        "<b>Here are your available commands:</b>\n\n"
        "🎯 <b>/submit &lt;links&gt;</b> — Submit up to 5 Instagram Reels for tracking.\n"
        "📈 <b>/stats</b> — View your tracked Reels and total views.\n"
        "🗑 <b>/remove &lt;reel link&gt;</b> — Stop tracking a specific Reel.\n"
        "⚙️ <b>/ping</b> — Check if the bot is alive.\n\n"
        "🔒 <b>Admin Commands:</b>\n"
        "🔄 <b>/forceupdate</b> — Manually refresh all Reel views now.\n"
        "👤 <b>/userstats &lt;user_id&gt;</b> — View a specific user's stats.\n"
        "📋 <b>/adminstats</b> — Download full system stats.\n",
        parse_mode="HTML"
    )

# --- /ping Command ---
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! I'm alive.")

# --- /submit Command ---
async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw = update.message.text or ""
        payload = raw[len("/submit"):].strip()
        links = [l.strip() for l in payload.replace("\n", " ").split(",") if l.strip()]
        if not links or len(links) > 5:
            return await update.message.reply_text("⚠️ Please submit between 1–5 Reel URLs.")

        uid = update.effective_user.id
        now = datetime.now()

        # Cooldown check
        async with AsyncSessionLocal() as s:
            row = (await s.execute(text(
                "SELECT last_submit FROM cooldowns WHERE user_id=:u"
            ), {"u": uid})).fetchone()
            if row:
                last = datetime.fromisoformat(row[0])
                rem = COOLDOWN_SEC - (now - last).total_seconds()
                if rem > 0:
                    msg = await update.message.reply_text(f"⌛ Please wait {int(rem)}s before submitting again.")
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
                failures.append((link, "Invalid URL"))
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
                except Exception:
                    failures.append((link, "Already submitted or database error"))

        lines = [f"✅ <b>Submitted {successes} reel(s)!</b>"]
        if failures:
            lines.append("❌ <b>Failures:</b>")
            for l, r in failures:
                lines.append(f"- {l} → {r}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        print(f"⚠️ /submit error: {e}")
        await update.message.reply_text("❌ Something went wrong. Please try again later.")

# --- /stats Command ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.effective_user.id
        async with AsyncSessionLocal() as s:
            rres = await s.execute(text("SELECT id FROM reels WHERE user_id=:u"), {"u": uid})
            reels = rres.fetchall()

        if not reels:
            return await update.message.reply_text("📭 You are not tracking any Reels yet!")

        total_views = 0
        details = []
        for rid, in reels:
            async with AsyncSessionLocal() as s:
                row = (await s.execute(text(
                    "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
                ), {"r": rid})).fetchone()
            cnt = row[0] if row else 0
            total_views += cnt
            details.append(cnt)

        lines = [
            "📈 <b>Your Stats:</b>",
            f"🎬 Total Videos: {len(reels)}",
            f"👀 Total Views: {total_views}"
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        print(f"⚠️ /stats error: {e}")
        await update.message.reply_text("❌ Something went wrong. Please try again later.")

# --- /remove Command ---
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            return await update.message.reply_text("⚠️ Usage: /remove <Reel URL>")

        uid = update.effective_user.id
        sc = extract_shortcode(context.args[0])
        if not sc:
            return await update.message.reply_text("❌ Invalid Reel URL.")

        async with AsyncSessionLocal() as s:
            row = (await s.execute(text(
                "SELECT id FROM reels WHERE user_id=:u AND shortcode=:c"
            ), {"u": uid, "c": sc})).fetchone()

            if not row:
                return await update.message.reply_text("⚠️ This Reel is not being tracked.")

            rid = row[0]
            await s.execute(text("DELETE FROM views WHERE reel_id=:r"), {"r": rid})
            await s.execute(text("DELETE FROM reels WHERE id=:r"), {"r": rid})
            await s.commit()

        await update.message.reply_text(f"🗑 Successfully removed Reel {sc}!")

    except Exception as e:
        print(f"⚠️ /remove error: {e}")
        await update.message.reply_text("❌ Something went wrong. Please try again later.")
# --- /uploadsession Commands (Admin Only) ---
async def upload_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")
    await update.message.reply_text("📤 Please upload your session_cookies.json file.")
    return 1

async def upload_session_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc: Document = update.message.document
    if not doc or not doc.file_name.endswith(".json"):
        await update.message.reply_text("⚠️ Please upload a valid JSON file.")
        return ConversationHandler.END

    file = await context.bot.get_file(doc.file_id)
    await file.download_to_drive(COOKIE_FILE)
    await update.message.reply_text("✅ Session saved! Restarting...")
    os._exit(0)

# --- /forceupdate Command (Admin) ---
async def forceupdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")

    progress_message = await update.message.reply_text("🔄 Starting manual update...")

    success = 0
    failed = 0

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("SELECT id,shortcode FROM reels"))).all()

    total = len(rows)
    if total == 0:
        return await progress_message.edit_text("📭 No reels to update.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context_browser = await browser.new_context()
        await load_cookies(context_browser)
        page = await context_browser.new_page()

        for idx, (rid, sc) in enumerate(rows, 1):
            try:
                url = f"https://www.instagram.com/reel/{sc}/"
                await page.goto(url, timeout=60000)
                await page.wait_for_selector('video', timeout=10000)

                spans = await page.query_selector_all("span")
                view_count = None
                for span in spans:
                    text_content = await span.inner_text()
                    if "views" in text_content.lower():
                        view_count = int(text_content.replace(",", "").replace("views", "").strip())
                        break

                if view_count is not None:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    async with AsyncSessionLocal() as session:
                        await session.execute(text(
                            "INSERT INTO views(reel_id,timestamp,count) VALUES(:r,:t,:c)"
                        ), {"r": rid, "t": ts, "c": view_count})
                        await session.commit()
                    success += 1
                else:
                    failed += 1

            except Exception as e:
                print(f"⚠️ forceupdate error for {sc}: {e}")
                failed += 1

            # Every 10 reels OR last reel → Update progress
            if idx % 10 == 0 or idx == total:
                try:
                    await progress_message.edit_text(
                        f"🔄 Updating Reels...\n"
                        f"✅ Success: {success}\n"
                        f"❌ Failed: {failed}\n"
                        f"🎯 Progress: {idx}/{total}"
                    )
                except Exception as e:
                    print(f"⚠️ Failed to edit progress message: {e}")

            await asyncio.sleep(2)

        await browser.close()

    await progress_message.edit_text(
        f"✅ Forceupdate Complete!\n"
        f"🎯 Total Reels: {total}\n"
        f"✅ Success: {success}\n"
        f"❌ Failed: {failed}"
    )



# --- /checksession Command ---
async def checksession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")

    await update.message.reply_text("🛡️ Checking Instagram session (strong mode)...")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context_browser = await browser.new_context()
            await load_cookies(context_browser)
            page = await context_browser.new_page()
            response = await page.goto("https://www.instagram.com/", timeout=60000)

            current_url = page.url
            if "login" in current_url or "/accounts/" in current_url:
                await update.message.reply_text("❌ Session invalid or expired.")
            elif response.status >= 400:
                await update.message.reply_text(f"⚠️ HTTP error: {response.status}")
            else:
                await update.message.reply_text("✅ Session is active and working fine!")

            await browser.close()

    except Exception as e:
        print(f"⚠️ checksession error: {e}")
        await update.message.reply_text("⚠️ Unexpected error during session check.")


# --- /userstats Command (Admin) ---
async def userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")
    if len(context.args) != 1:
        return await update.message.reply_text("⚠️ Usage: /userstats <telegram_user_id>")

    uid = int(context.args[0])
    async with AsyncSessionLocal() as s:
        reels = (await s.execute(text("SELECT id,shortcode FROM reels WHERE user_id=:u"), {"u": uid})).fetchall()

    if not reels:
        return await update.message.reply_text("📭 No Reels found for this user.")

    total_views = 0
    details = []

    for rid, shortcode in reels:
        async with AsyncSessionLocal() as s:
            row = (await s.execute(text(
                "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
            ), {"r": rid})).fetchone()
            cnt = row[0] if row else 0
            total_views += cnt
            details.append((shortcode, cnt))

    details.sort(key=lambda x: x[1], reverse=True)
    lines = [
        f"👤 <b>Stats for user {uid}:</b>",
        f"🎬 Total Videos: {len(details)}",
        f"👀 Total Views: {total_views}",
        "\n<b>Top Reels:</b>"
    ]
    for idx, (sc, views) in enumerate(details, 1):
        lines.append(f"{idx}. https://instagram.com/reel/{sc} — {views} views")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# --- /adminstats Command (Admin) ---
async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")

    data = []
    async with AsyncSessionLocal() as s:
        users = (await s.execute(text("SELECT user_id, username FROM users"))).all()

    for uid, uname in users:
        async with AsyncSessionLocal() as s:
            reels = (await s.execute(text("SELECT id,shortcode FROM reels WHERE user_id=:u"), {"u": uid})).all()

        vids = len(reels)
        views = 0
        detail = []

        for rid, shortcode in reels:
            async with AsyncSessionLocal() as s:
                row = (await s.execute(text(
                    "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
                ), {"r": rid})).fetchone()
            v = row[0] if row else 0
            views += v
            detail.append((shortcode, v))

        detail.sort(key=lambda x: x[1], reverse=True)
        data.append((uname or str(uid), vids, views, detail))

    data.sort(key=lambda x: x[2], reverse=True)

    lines = []
    for uname, vids, views, detail in data:
        lines.append(f"Username: @{uname}")
        lines.append(f"Total Videos: {vids}")
        lines.append(f"Total Views: {views}")
        lines.append("Top Reels:")
        for sc, cnt in detail:
            lines.append(f" - https://instagram.com/reel/{sc} — {cnt} views")
        lines.append("\n")

    with open("/tmp/admin_stats.txt", "w") as f:
        f.write("\n".join(lines))

    await update.message.reply_document(open("/tmp/admin_stats.txt", "rb"), filename="admin_stats.txt")

# --- /leaderboard Command (Admin) ---
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")

    stats = []
    async with AsyncSessionLocal() as s:
        users = (await s.execute(text("SELECT user_id, username FROM users"))).all()

    for uid, uname in users:
        async with AsyncSessionLocal() as s:
            reels = (await s.execute(text("SELECT id FROM reels WHERE user_id=:u"), {"u": uid})).all()
        vids = len(reels)
        total = 0
        for rid, in reels:
            async with AsyncSessionLocal() as s:
                row = (await s.execute(text(
                    "SELECT count FROM views WHERE reel_id=:r ORDER BY timestamp DESC LIMIT 1"
                ), {"r": rid})).fetchone()
            total += row[0] if row else 0
        stats.append((uname or str(uid), vids, total))

    stats.sort(key=lambda x: x[2], reverse=True)
    lines = ["🏆 <b>Leaderboard:</b>"]
    for uname, vids, views in stats:
        lines.append(f"@{uname} — 🎬 {vids} Reels | 👀 {views} Views")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# --- /broadcast Command (Admin) ---
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return await update.message.reply_text("⚠️ Usage: /broadcast <message>")
    
    msg = "📢 " + " ".join(context.args)

    async with AsyncSessionLocal() as s:
        users = (await s.execute(text("SELECT user_id FROM users"))).fetchall()

    for (uid,) in users:
        try:
            await context.bot.send_message(uid, msg)
        except Exception as e:
            print(f"⚠️ Failed to send to {uid}: {e}")

    await update.message.reply_text("✅ Broadcast complete.")

# --- /auditlog Command (Admin) ---
async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT user_id, action, shortcode, timestamp FROM audit ORDER BY id DESC LIMIT 20"
        ))).fetchall()

    lines = ["📜 <b>Recent Activity:</b>"]
    for u, a, c, t in rows:
        lines.append(f"{t} — UID {u} {a} {c}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
# --- Health Check Server ---
async def health(request: web.Request) -> web.Response:
    return web.Response(text="✅ OK - ReelTracker is running.")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

# --- Bot Global Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    print(f"❗️ Unhandled error:\n{tb}")

# --- Main Async Runner ---
async def main():
    await init_db()
    asyncio.create_task(start_health_server())
    asyncio.create_task(track_all_views())

    app = ApplicationBuilder().token(TOKEN).build()

    # User Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("submit", submit))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("remove", remove))

    # Admin Commands
    app.add_handler(CommandHandler("forceupdate", forceupdate))
    app.add_handler(CommandHandler("userstats", userstats))
    app.add_handler(CommandHandler("adminstats", adminstats))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("auditlog", auditlog))
    app.add_handler(CommandHandler("checksession", checksession))
    
    # Upload Session Conversation
    upload_conv = ConversationHandler(
        entry_points=[CommandHandler("uploadsession", upload_session)],
        states={1: [MessageHandler(filters.Document.ALL, upload_session_receive)]},
        fallbacks=[]
    )
    app.add_handler(upload_conv)

    # Global error handler
    app.add_error_handler(error_handler)

    print("🤖 ReelTracker Bot is running...")
    await app.run_polling(drop_pending_updates=True)

# --- Entrypoint ---
if __name__ == "__main__":
    nest_asyncio.apply()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
