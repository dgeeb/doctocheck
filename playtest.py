import os
import re
import time
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv
import ctypes  # Windows popup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError

# --------------- Env ---------------
load_dotenv()
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

# --------------- Config ---------------
TEST_URL = "https://www.doctolib.fr/clinique-privee/gap/polyclinique-des-alpes-du-sud"
HEADLESS_MODE = True         # Set False to watch it interact
SHOW_POPUP = True
STEP_TIMEOUT = 20000         # ms for each step
NO_SLOTS_TIMEOUT_MS = 45000  # Wait up to 45s for the “no slots” UI to appear

# --------------- Notifications ---------------
def send_email_notification():
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("⚠️ EMAIL_ADDRESS/EMAIL_PASSWORD not set; skipping email.")
        return
    msg = EmailMessage()
    msg["Subject"] = "🚨 Appointment Available!"
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = EMAIL_ADDRESS
    msg.set_content(f"An appointment is available! Open: {TEST_URL}")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        smtp.send_message(msg)

def show_popup(title, message):
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 1)
    except Exception:
        pass

# --------------- Helpers ---------------
def click_first_visible(page, locator, desc, timeout=STEP_TIMEOUT, delay=0.6):
    """Clicks the first visible element for the given locator (Locator or string)."""
    loc = page.locator(locator) if isinstance(locator, str) else locator
    loc.first.wait_for(state="visible", timeout=timeout)
    loc.first.scroll_into_view_if_needed(timeout=timeout)
    loc.first.click(timeout=timeout)
    print(f"✅ Clicked: {desc}")
    if delay:
        time.sleep(delay)

def try_click_variants(page, desc, variants, timeout=STEP_TIMEOUT):
    """Try multiple locator strategies until one works."""
    last_err = None
    for i, variant in enumerate(variants, 1):
        try:
            click_first_visible(page, variant, f"{desc} (variant {i})", timeout=timeout)
            return True
        except (PlaywrightTimeout, PlaywrightError) as e:
            last_err = e
    print(f"⚠️ Could not click {desc}: {last_err}")
    return False

def has_no_slots_ui(page):
    """
    Returns True if we detect the 'no appointment' UI:
    - The big blue button “CHERCHER UN AUTRE SOIGNANT”
    - OR the long message about appointments not being available
    """
    btn_regex = re.compile(r"cherch(er)?\s+un\s+autre\s+(soignant|professionnel|praticien)", re.I)
    if page.get_by_role("button", name=btn_regex).first.is_visible():
        return True
    if page.get_by_role("link", name=btn_regex).first.is_visible():
        return True

    try:
        body_text = page.locator("body").inner_text()
        if re.search(r"n'est malheureusement pas disponible", body_text, re.I):
            return True
    except Exception:
        pass
    return False

# --------------- Main ---------------
def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS_MODE, args=["--disable-gpu"])
        context = browser.new_context(
            viewport={"width": 1366, "height": 860},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36")
        )
        page = context.new_page()

        try:
            page.goto(TEST_URL, wait_until="domcontentloaded", timeout=60000)

            # Cookie banner
            try_click_variants(
                page,
                "Accept cookies",
                [
                    page.get_by_role("button", name=re.compile("tout accepter|accepter", re.I)),
                    "button:has-text('Accepter')",
                    "button:has-text('TOUT ACCEPTER')",
                ],
                timeout=8000,
            )

            # --- Booking flow (mirrors your Selenium steps) ---
            try_click_variants(
                page,
                "Prendre rendez-vous",
                [
                    page.get_by_role("link", name=re.compile("Prendre rendez-vous", re.I)),
                    "xpath=//*[contains(.,'Prendre rendez-vous') and (self::a or self::button)]",
                ],
            )
            try_click_variants(
                page,
                "Anesthésiste",
                [
                    page.get_by_role("button", name=re.compile("Anesthésiste", re.I)),
                    "xpath=//*[contains(.,'Anesthésiste') and (self::button or @role='button')]",
                ],
            )
            try_click_variants(
                page,
                "Je n'ai pas de préférence (1)",
                [
                    page.get_by_role("button", name=re.compile(r"Je n'?ai pas de préférence", re.I)),
                    "xpath=//button[.//*[contains(., \"Je n'ai pas de préférence\")]]",
                ],
            )
            try_click_variants(
                page,
                "Consultation d'anesthésie",
                [
                    page.get_by_role("button", name=re.compile(r"Consultation d", re.I)),
                    "xpath=//*[contains(.,'Consultation d') and (self::button or @role='button')]",
                ],
            )
            try_click_variants(
                page,
                "Je n'ai pas de préférence (2)",
                [
                    page.get_by_role("button", name=re.compile(r"Je n'?ai pas de préférence", re.I)),
                    "xpath=//button[.//*[contains(., \"Je n'ai pas de préférence\")]]",
                ],
            )

            # --- Final detection: wait for 'no slots' UI; if not seen, assume slots ---
            print("⏳ Checking for 'no slots' UI…")
            no_slots_detected = False
            try:
                page.wait_for_timeout(1500)  # settle
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                pass

            try:
                page.get_by_role(
                    "button",
                    name=re.compile(r"cherch(er)?\s+un\s+autre\s+(soignant|professionnel|praticien)", re.I)
                ).first.wait_for(state="visible", timeout=NO_SLOTS_TIMEOUT_MS)
                no_slots_detected = True
            except PlaywrightTimeout:
                no_slots_detected = has_no_slots_ui(page)

            if no_slots_detected:
                print("❌ No appointments available (no-slots UI detected).")
                if SHOW_POPUP:
                    show_popup("Doctor Checker", "No appointment available.")
            else:
                print("✅ Appointment likely available! (no 'no-slots' UI detected)")
                send_email_notification()
                if SHOW_POPUP:
                    show_popup("Doctor Checker", "Appointment Available!")

        finally:
            context.close()
            browser.close()

if __name__ == "__main__":
    main()
