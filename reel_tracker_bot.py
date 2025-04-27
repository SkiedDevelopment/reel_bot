#!/usr/bin/env python3
import os
import re
import asyncio
import logging
import requests
import json
from datetime import datetime
from aiohttp import web
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from dotenv import load_dotenv
from bs4 import BeautifulSoup

# ── Load .env ────────────────────────────────────────────────────────────────
load_dotenv()

TOKEN         = os.getenv("TOKEN")
ADMIN_IDS     = [x.strip() for x in os.getenv("ADMIN_ID", "").split(",") if x.strip()]
LOG_GROUP_ID  = os.getenv("LOG_GROUP_ID")
PORT          = int(os.getenv("PORT", "10000"))
DATABASE_URL  = os.getenv("DATABASE_URL")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY")

if not TOKEN or not DATABASE_URL or not SCRAPERAPI_KEY:
    raise Exception("❌ Please set TOKEN, DATABASE_URL, SCRAPERAPI_KEY in .env")

# ── Normalize DATABASE_URL ────────────────────────────────────────────────────
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# ── Database ───────────────────────────────────────────────────────────────────
engine = create_async_engine(DATABASE_URL, future=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ── Helper Functions ───────────────────────────────────────────────────────────
def extract_shortcode(link: str) -> str | None:
    m = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return m.group(1) if m else None

def is_admin(uid: int) -> bool:
    return str(uid) in ADMIN_IDS

# ── Fetch Reel Views using ScraperAPI ───────────────────────────────────────────
async def fetch_reel_views(shortcode: str) -> int | None:
    url = f"https://www.instagram.com/reel/{shortcode}/"

    params = {
        "api_key": SCRAPERAPI_KEY,
        "url": url,
        "render": "true"
    }

    try:
        r = requests.get("http://api.scraperapi.com/", params=params, timeout=30)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            spans = soup.find_all("span")
            for span in spans:
                text = span.get_text()
                if "views" in text.lower():
                    try:
                        number_part = text.lower().replace("views", "").strip().replace(",", "")
                        return int(number_part)
                    except ValueError:
                        continue
        else:
            print(f"⚠️ ScraperAPI error: {r.status_code}")
            return None
    except Exception as e:
        print(f"❌ ScraperAPI fetch failed: {e}")
        return None

# ── User Commands ──────────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to *Reel Tracker Bot*!\n\n"
        "✨ Available Commands:\n"
        "• /submit <links> — 📥 Track up to 5 Reels\n"
        "• /stats — 📊 View your tracked Reels\n"
        "• /remove <URL> — 🗑 Remove a tracked Reel\n"
        "• /checkapi — 🛡️ Check ScraperAPI status\n",
        parse_mode=ParseMode.MARKDOWN
    )

async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text or ""
    payload = raw[len("/submit"):].strip()
    links = [l.strip() for l in payload.replace("\n", " ").split(",") if l.strip()]
    if not links or len(links) > 5:
        return await update.message.reply_text("❌ Usage: /submit <up to 5 comma-separated Reel URLs>")
    uid = update.effective_user.id
    now = datetime.now()
    successes, failures = 0, []
    for link in links:
        sc = extract_shortcode(link)
        if not sc:
            failures.append((link, "invalid URL"))
            continue
        async with AsyncSessionLocal() as s:
            try:
                await s.execute(text(
                    "INSERT INTO reels(user_id, shortcode, created_at) VALUES (:u, :c, :t)"
                ), {"u": uid, "c": sc, "t": now.strftime("%Y-%m-%d %H:%M:%S")})
                await s.commit()
                successes += 1
            except Exception:
                failures.append((link, "already submitted"))
    msg = [f"✅ Submitted {successes} Reel(s)."]
    if failures:
        msg.append("❌ Failures:")
        for l, r in failures:
            msg.append(f"- {l}: {r}")
    await update.message.reply_text("\n".join(msg))

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with AsyncSessionLocal() as s:
        rres = await s.execute(text("SELECT shortcode FROM reels WHERE user_id=:u"), {"u": uid})
        reels = rres.fetchall()
    if not reels:
        return await update.message.reply_text("📭 No tracked Reels yet.")
    msg = ["📈 *Your Reels:*"]
    for (sc,) in reels:
        msg.append(f"🔗 https://www.instagram.com/reel/{sc}")
    await update.message.reply_text("\n".join(msg), parse_mode=ParseMode.HTML)

async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❌ Usage: /remove <Reel URL>")
    uid = update.effective_user.id
    sc = extract_shortcode(context.args[0])
    if not sc:
        return await update.message.reply_text("❌ Invalid URL.")
    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM reels WHERE user_id=:u AND shortcode=:c"), {"u": uid, "c": sc})
        await s.commit()
    await update.message.reply_text(f"🗑️ Removed Reel: {sc}")

async def checkapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    views = await fetch_reel_views("CRmYX-ppVn8")  # random public reel
    if views is not None:
        await update.message.reply_text("✅ ScraperAPI key is *working fine!* 🚀", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("⚠️ ScraperAPI key might have issues or limits.")

# ── Admin Commands ──────────────────────────────────────────────────────────────
async def forceupdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")
    async with AsyncSessionLocal() as s:
        rres = await s.execute(text("SELECT id,shortcode FROM reels"))
        reels = rres.fetchall()
    msg = ["🔄 Force updating Reels..."]
    for rid, sc in reels:
        views = await fetch_reel_views(sc)
        if views is not None:
            async with AsyncSessionLocal() as s:
                await s.execute(text(
                    "UPDATE reels SET last_views=:v WHERE id=:i"
                ), {"v": views, "i": rid})
                await s.commit()
            msg.append(f"✅ {sc}: {views} views")
        await asyncio.sleep(1)
    await update.message.reply_text("\n".join(msg))

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")
    stats = []
    async with AsyncSessionLocal() as s:
        users = (await s.execute(text("SELECT DISTINCT user_id FROM reels"))).all()
    for (uid,) in users:
        async with AsyncSessionLocal() as s:
            reels = (await s.execute(text("SELECT id, shortcode FROM reels WHERE user_id=:u"), {"u": uid})).all()
        total_views = sum(r[0] for r in reels)
        stats.append((uid, total_views))
    stats.sort(key=lambda x: x[1], reverse=True)
    lines = ["🏆 *Leaderboard:*"]
    for i, (uid, views) in enumerate(stats, 1):
        lines.append(f"{i}. `{uid}` — {views} views")
    reels = (await s.execute(text("SELECT id, shortcode FROM reels WHERE user_id=:u"), {"u": uid})).all()

async def userstatsid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 1:
        return await update.message.reply_text("❌ Usage: /userstatsid <telegram_id>")
    tgt = int(context.args[0])
    async with AsyncSessionLocal() as s:
        reels = (await s.execute(text("SELECT shortcode,last_views FROM reels WHERE user_id=:u"), {"u": tgt})).fetchall()
    if not reels:
        return await update.message.reply_text("📭 No reels found for this user.")
    lines = [f"📊 Stats for user `{tgt}`:"]
    total = 0
    for sc, v in reels:
        lines.append(f"🔗 https://instagram.com/reel/{sc} — {v} views")
        total += v
    lines.append(f"\n**Total views:** {total}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args:
        return await update.message.reply_text("❌ Usage: /broadcast <message>")
    msg = "📢 " + " ".join(context.args)
    async with AsyncSessionLocal() as s:
        users = (await s.execute(text("SELECT DISTINCT user_id FROM reels"))).all()
    for (uid,) in users:
        try:
            await context.bot.send_message(uid, msg)
        except:
            pass
    await update.message.reply_text("✅ Broadcast sent.")

async def auditlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")
    # (optional) — if you want full logging feature later

async def adminstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ You are not authorized.")
    stats = []
    async with AsyncSessionLocal() as s:
        users = (await s.execute(text("SELECT DISTINCT user_id FROM reels"))).all()
    for (uid,) in users:
        async with AsyncSessionLocal() as s:
            reels = (await s.execute(text("SELECT last_views FROM reels WHERE user_id=:u"), {"u": uid})).all()
        total_views = sum(r[0] for r in reels)
        stats.append((uid, len(reels), total_views))
    stats.sort(key=lambda x: x[2], reverse=True)
    lines = ["📋 *Admin Stats:*"]
    for uid, count, views in stats:
        lines.append(f"👤 {uid} — {count} reels, {views} views")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 1:
        return await update.message.reply_text("❌ Usage: /deleteuser <telegram_id>")
    tgt = int(context.args[0])
    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM reels WHERE user_id=:u"), {"u": tgt})
        await s.commit()
    await update.message.reply_text(f"✅ Deleted user data: {tgt}")

async def deletereel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 1:
        return await update.message.reply_text("❌ Usage: /deletereel <shortcode>")
    code = context.args[0]
    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM reels WHERE shortcode=:c"), {"c": code})
        await s.commit()
    await update.message.reply_text(f"✅ Deleted reel: {code}")

# ── Health Server ──────────────────────────────────────────────────────────────
async def health(request: web.Request) -> web.Response:
    return web.Response(text="✅ Bot is healthy.")

async def start_health():
    srv = web.Application()
    srv.router.add_get("/health", health)
    runner = web.AppRunner(srv)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS reels (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                shortcode TEXT,
                created_at TEXT,
                last_views INTEGER DEFAULT 0
            )
        """))
    asyncio.create_task(start_health())

if __name__ == "__main__":
    import asyncio

    # Start health server
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.create_task(start_health())

    # Setup bot
    app = ApplicationBuilder().token(TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("submit", submit))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CommandHandler("checkapi", checkapi))

    # Admin commands
    app.add_handler(CommandHandler("forceupdate", forceupdate))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("userstatsid", userstatsid))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("adminstats", adminstats))
    app.add_handler(CommandHandler("deleteuser", deleteuser))
    app.add_handler(CommandHandler("deletereel", deletereel))
    app.add_handler(CommandHandler("auditlog", auditlog))

    print("🤖 Bot is running...")
    app.run_polling(drop_pending_updates=True)
