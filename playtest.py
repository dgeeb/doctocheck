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
HEADLESS_MODE = True          # Set False to watch it interact locally
SHOW_POPUP = True             # Windows-only; ignored on GitHub runners
STEP_TIMEOUT = 20000          # ms per UI step
SLOTS_WAIT_MS = 40000         # wait up to 40s for real time slots to appear
NO_SLOTS_WAIT_MS = 30000      # wait up to 30s for the explicit "no slots" UI
WAIT_AFTER_FLOW_MS = 1500     # small settle wait before detection

# Regex to capture HH:MM without capturing groups (returns the full match strings)
TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")

# --------------- Notifications ---------------
def send_email_notification(message_text: str):
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("⚠️ EMAIL_ADDRESS/EMAIL_PASSWORD not set; skipping email.")
        return
    msg = EmailMessage()
    msg["Subject"] = "🚨 Doctolib: appointment signal"
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = EMAIL_ADDRESS
    msg.set_content(message_text)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        smtp.send_message(msg)

def show_popup(title, message):
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 1)
    except Exception:
        pass

# --------------- Click helpers ---------------
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

# --------------- Detection helpers ---------------
def find_slot_times_in_frame(frame):
    """Return a sorted list of visible time labels (HH:MM) within this frame."""
    times = set()

    # 1) Doctolib slot buttons (common case)
    try:
        btns = frame.locator("button.dl-button-slot")
        count = btns.count()
        for i in range(min(count, 300)):  # safety cap
            el = btns.nth(i)
            if el.is_visible():
                txt = (el.inner_text(timeout=2000) or "").strip()
                for m in TIME_RE.finditer(txt):
                    times.add(m.group(0))
    except Exception:
        pass

    # 2) Any clickable elements containing a time label
    try:
        generic = frame.locator("button, [role=button], a")
        count = generic.count()
        for i in range(min(count, 600)):
            el = generic.nth(i)
            if el.is_visible():
                txt = (el.inner_text(timeout=1200) or "").strip()
                if ":" in txt:
                    for m in TIME_RE.finditer(txt):
                        times.add(m.group(0))
    except Exception:
        pass

    return sorted(times)

def any_no_slots_ui_in_frame(frame):
    """Detect explicit 'no slots' UI in this frame."""
    btn_regex = re.compile(r"cherch(er)?\s+un\s+autre\s+(soignant|professionnel|praticien)", re.I)
    try:
        if frame.get_by_role("button", name=btn_regex).first.is_visible():
            return True
        if frame.get_by_role("link", name=btn_regex).first.is_visible():
            return True
    except Exception:
        pass
    try:
        body_text = frame.locator("body").inner_text()
        if re.search(r"n'est malheureusement pas disponible", body_text, re.I):
            return True
    except Exception:
        pass
    return False

def detect_availability(page):
    """
    Returns ('available', times) | ('none', []) | ('unknown', [])
    - AVAILABLE: we saw time labels (HH:MM) in any frame.
    - NONE: we saw the explicit 'no slots' UI/message.
    - UNKNOWN: neither appeared within the wait windows (don’t alert).
    """
    # Let the page settle
    page.wait_for_timeout(WAIT_AFTER_FLOW_MS)

    # 1) Wait for actual slots
    deadline = time.time() + (SLOTS_WAIT_MS / 1000.0)
    while time.time() < deadline:
        for fr in page.frames:
            times = find_slot_times_in_frame(fr)
            if times:
                return ("available", times)
        page.wait_for_timeout(600)

    # 2) If no slots, wait for explicit "no slots" UI
    try:
        page.wait_for_load_state("networkidle", timeout=min(15000, NO_SLOTS_WAIT_MS))
    except PlaywrightTimeout:
        pass

    deadline = time.time() + (NO_SLOTS_WAIT_MS / 1000.0)
    while time.time() < deadline:
        for fr in page.frames:
            if any_no_slots_ui_in_frame(fr):
                return ("none", [])
        page.wait_for_timeout(600)

    # 3) Neither showed up
    return ("unknown", [])

# --------------- Main ---------------
def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS_MODE, args=["--disable-gpu"])
        context = browser.new_context(
            viewport={"width": 1366, "height": 860},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        page = context.new_page()

        try:
            page.goto(TEST_URL, wait_until="domcontentloaded", timeout=60000)

            # Cookie banner
            try_click_variants(
                page,
                "Accept cookies",
                [
                    page.get_by_role("button", name=re.compile("tout accepter|accepter|j'accepte|ok", re.I)),
                    "button:has-text('Accepter')",
                    "button:has-text('TOUT ACCEPTER')",
                ],
                timeout=8000,
            )

            # --- Booking flow (mirrors your previous steps) ---
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

            # --------- Availability detection (3-state) ---------
            state, times = detect_availability(page)

            if state == "available":
                print(f"✅ Slots found: {times}")
                send_email_notification(f"Slots found on Doctolib: {', '.join(times)}\n\n{TEST_URL}")
                if SHOW_POPUP:
                    show_popup("Doctor Checker", f"Slots: {', '.join(times)}")
            elif state == "none":
                print("❌ No appointments (explicit UI).")
                if SHOW_POPUP:
                    show_popup("Doctor Checker", "No appointment available.")
            else:
                print("🤷 No positive slots and no explicit 'no-slots' UI → UNKNOWN. Not sending email.")

        finally:
            context.close()
            browser.close()

if __name__ == "__main__":
    main()
