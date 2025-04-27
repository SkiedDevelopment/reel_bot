from pyvirtualdisplay import Display
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync
import json

def save_vps_cookies():
    display = Display(visible=1, size=(1280, 720))
    display.start()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        stealth_sync(page)
        page.goto("https://www.instagram.com/accounts/login/")

        input("👉 Login manually in the browser window, then press Enter here...")

        cookies = context.cookies()
        with open("session_cookies.json", "w") as f:
            json.dump(cookies, f, indent=2)

        browser.close()
        display.stop()

if __name__ == "__main__":
    save_vps_cookies()
