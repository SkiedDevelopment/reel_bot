from pyvirtualdisplay import Display
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync
import json

def save_vps_cookies():
    # 🚀 FORCE use Xvfb instead of Xephyr
    display = Display(backend="xvfb", size=(1280, 720))
    display.start()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="en-US"
        )
        page = context.new_page()
        stealth_sync(page)
        page.goto("https://www.instagram.com/accounts/login/")

        input("👉 Please login manually, then press Enter here...")

        cookies = context.cookies()
        with open("session_cookies.json", "w") as f:
            json.dump(cookies, f, indent=2)

        browser.close()
        display.stop()

if __name__ == "__main__":
    save_vps_cookies()
