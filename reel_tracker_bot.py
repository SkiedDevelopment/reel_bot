
import asyncio
import re
import instaloader
import aiosqlite
import os
import nest_asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

DB_FILE = "reels.db"
TOKEN = os.getenv("TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
        await db.execute("CREATE TABLE IF NOT EXISTS reels (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, shortcode TEXT, username TEXT, UNIQUE(user_id, shortcode))")
        await db.execute("CREATE TABLE IF NOT EXISTS views (reel_id INTEGER, timestamp TEXT, count INTEGER)")
        await db.commit()

def is_admin(user_id):
    return str(user_id) == str(ADMIN_ID)

def extract_shortcode(link):
    match = re.search(r"instagram\.com/reel/([^/?]+)", link)
    return match.group(1) if match else None

async def track_views_loop():
    await asyncio.sleep(5)
    while True:
        try:
            await track_all_views()
        except Exception as e:
            print(f"[TRACKING ERROR] {e}")
        await asyncio.sleep(43200)

async def track_all_views():
    L = instaloader.Instaloader()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT id, shortcode FROM reels") as cursor:
            reels = await cursor.fetchall()
            for reel_id, shortcode in reels:
                for attempt in range(3):
                    try:
                        post = instaloader.Post.from_shortcode(L.context, shortcode)
                        views = post.video_view_count
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        await db.execute("INSERT INTO views (reel_id, timestamp, count) VALUES (?, ?, ?)", (reel_id, now, views))
                        await db.commit()
                        break
                    except Exception as e:
                        print(f"[Retry {attempt+1}] Failed to fetch {shortcode}: {e}")
                        await asyncio.sleep(2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome! Use /submit to track a reel, /stats to view your stats, and /remove to delete a reel.")

async def submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please send the public Instagram reel link.")
    try:
        msg = await context.bot.wait_for_message(chat_id=update.effective_chat.id, timeout=60)
    except:
        await update.message.reply_text("⏰ Timeout. Try /submit again.")
        return
    shortcode = extract_shortcode(msg.text if msg else "")
    if not shortcode:
        await update.message.reply_text("❌ Invalid reel link.")
        return
    L = instaloader.Instaloader()
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
    except:
        await update.message.reply_text("⚠️ Couldn't fetch reel. Make sure it's public.")
        return
    user_id = update.effective_user.id
    username = post.owner_username
    views = post.video_view_count
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        try:
            await db.execute("INSERT INTO reels (user_id, shortcode, username) VALUES (?, ?, ?)", (user_id, shortcode, username))
            await db.execute("INSERT INTO views (reel_id, timestamp, count) VALUES ((SELECT id FROM reels WHERE user_id = ? AND shortcode = ?), ?, ?)", (user_id, shortcode, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), views))
            await db.commit()
        except aiosqlite.IntegrityError:
            await update.message.reply_text("⚠️ You've already submitted this reel.")
            return
    await update.message.reply_text(f"✅ Reel by @{username} submitted with {views} views.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT id, username FROM reels WHERE user_id = ?", (user_id,))
        reels = await cursor.fetchall()
        if not reels:
            await update.message.reply_text("📭 No reels submitted yet.")
            return
        total_views = 0
        usernames = set()
        for reel_id, username in reels:
            usernames.add(username)
            v_cursor = await db.execute("SELECT count FROM views WHERE reel_id = ? ORDER BY timestamp DESC LIMIT 1", (reel_id,))
            latest = await v_cursor.fetchone()
            if latest:
                total_views += latest[0]
        await update.message.reply_text(f"📊 Stats:\nTotal Videos: {len(reels)}\nTotal Views: {total_views}\nAccounts: {', '.join(usernames)}")

async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT shortcode, username FROM reels WHERE user_id = ?", (user_id,))
        reels = await cursor.fetchall()
        if not reels:
            await update.message.reply_text("❌ You have no reels to remove.")
            return
        msg = "🗑️ Your Reels:\n" + "\n".join([f"- {sc} (@{u})" for sc, u in reels]) + "\n\nSend the shortcode to delete:"
        await update.message.reply_text(msg)
        try:
            reply = await context.bot.wait_for_message(chat_id=update.effective_chat.id, timeout=60)
        except:
            await update.message.reply_text("⏰ Timeout.")
            return
        shortcode = reply.text.strip()
        await db.execute("DELETE FROM views WHERE reel_id IN (SELECT id FROM reels WHERE user_id = ? AND shortcode = ?)", (user_id, shortcode))
        await db.execute("DELETE FROM reels WHERE user_id = ? AND shortcode = ?", (user_id, shortcode))
        await db.commit()
        await update.message.reply_text(f"✅ Reel `{shortcode}` removed.")

async def main():
    await init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("submit", submit))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("remove", remove))

    asyncio.create_task(track_views_loop())

    print("🤖 Running in webhook mode...")

    await app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),  # Render uses dynamic PORT
        webhook_url=os.getenv("WEBHOOK_URL")  # your Render domain
    )

    
if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.get_event_loop().run_until_complete(main())

