import os
import re
import time
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv
import platform
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError

# ---------------- Env & config ----------------
load_dotenv()
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

ECHIROLLES_URL = "https://www.doctolib.fr/cabinet-medical/echirolles/cente-digestif-des-cedres"
HEADLESS_MODE = True
STEP_TIMEOUT = 20000          # ms
WAIT_AFTER_FLOW_MS = 1500
SLOTS_WAIT_MS = 40000         # how long to wait for actual slots
NO_SLOTS_WAIT_MS = 30000      # how long to wait for the "no-slots" UI
CI = os.getenv("GITHUB_ACTIONS") == "true"

# Directories for debug artifacts (uploaded in GH Actions)
ART_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts"))
ART_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- Notifications ----------------
def send_email_notification(msg_text):
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("⚠️ EMAIL_ADDRESS/EMAIL_PASSWORD not set; skipping email.")
        return
    msg = EmailMessage()
    msg["Subject"] = "🚨 Doctolib: appointment signal"
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = EMAIL_ADDRESS
    msg.set_content(msg_text)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        smtp.send_message(msg)

# ---------------- Helpers ----------------
def click_first_visible(page, locator, desc, timeout=STEP_TIMEOUT, delay=0.6):
    loc = page.locator(locator) if isinstance(locator, str) else locator
    loc.first.wait_for(state="visible", timeout=timeout)
    loc.first.scroll_into_view_if_needed(timeout=timeout)
    loc.first.click(timeout=timeout)
    print(f"✅ Clicked: {desc}")
    if delay:
        time.sleep(delay)

def try_click_variants(page, desc, variants, timeout=STEP_TIMEOUT):
    last_err = None
    for i, variant in enumerate(variants, 1):
        try:
            click_first_visible(page, variant, f"{desc} (variant {i})", timeout=timeout)
            return True
        except (PlaywrightTimeout, PlaywrightError) as e:
            last_err = e
    print(f"⚠️ Could not click {desc}: {last_err}")
    return False

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")

def find_slot_times_in_frame(frame):
    """Return a list of visible time labels (HH:MM) within this frame."""
    times = set()

    # Specific Doctolib slot buttons
    try:
        btns = frame.locator("button.dl-button-slot")
        count = btns.count()
        for i in range(min(count, 200)):  # safety cap
            el = btns.nth(i)
            if el.is_visible():
                txt = el.inner_text(timeout=2000).strip()
                for m in TIME_RE.findall(txt):
                    # m is ('HH','MM') due to groups; rebuild time safely:
                    # But because of groups, use regex on whole text below instead:
                    pass
                # simpler: extract all times from this text
                for t in re.findall(TIME_RE, txt):
                    # When using grouping, t could be a tuple; normalize:
                    if isinstance(t, tuple):
                        # t[0] is hour; we need the original match; rerun:
                        for mm in re.finditer(r"\b([01]?\d|2[0-3]):[0-5]\d\b", txt):
                            times.add(mm.group(0))
                    else:
                        times.add(t)
    except Exception:
        pass

    # Generic: any clickable with a time
    try:
        generic = frame.locator("button, [role=button], a")
        count = generic.count()
        for i in range(min(count, 400)):
            el = generic.nth(i)
            if el.is_visible():
                txt = (el.inner_text(timeout=1000) or "").strip()
                if ":" in txt and TIME_RE.search(txt):
                    # add every time-like token
                    for mm in re.finditer(r"\b([01]?\d|2[0-3]):[0-5]\d\b", txt):
                        times.add(mm.group(0))
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
    We wait for positive proof of slots; otherwise check explicit 'no slots';
    if neither shows up, we return 'unknown' (no email; log artifacts).
    """
    # Small settle
    page.wait_for_timeout(WAIT_AFTER_FLOW_MS)

    # 1) Wait up to SLOTS_WAIT_MS for any slot button/time to appear (in page or any frame)
    deadline = time.time() + (SLOTS_WAIT_MS / 1000.0)
    while time.time() < deadline:
        # check main page + frames
        for fr in page.frames:
            times = find_slot_times_in_frame(fr)
            if times:
                return ("available", times)
        page.wait_for_timeout(600)

    # 2) If no slots found, wait up to NO_SLOTS_WAIT_MS for an explicit "no slots" UI
    try:
        page.wait_for_timeout(600)
        page.wait_for_load_state("networkidle", timeout=min(15000, NO_SLOTS_WAIT_MS))
    except PlaywrightTimeout:
        pass

    deadline = time.time() + (NO_SLOTS_WAIT_MS / 1000.0)
    while time.time() < deadline:
        for fr in page.frames:
            if any_no_slots_ui_in_frame(fr):
                return ("none", [])
        page.wait_for_timeout(600)

    # 3) Neither appeared: unknown (don’t alert)
    return ("unknown", [])

def save_artifacts(page, label="final"):
    try:
        page.screenshot(path=str(ART_DIR / f"{label}.png"), full_page=True)
    except Exception:
        pass
    try:
        html = page.content()
        (ART_DIR / f"{label}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass
    try:
        # Light text dump (helps search in logs)
        body_text = page.locator("body").inner_text(timeout=3000)
        (ART_DIR / f"{label}.txt").write_text(body_text[:20000], encoding="utf-8")
    except Exception:
        pass

# ---------------- Main ----------------
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
            page.goto(ECHIROLLES_URL, wait_until="domcontentloaded", timeout=60000)

            # Cookie banner (varies a lot on runners)
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

            # Booking flow (same as before)
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
                "Non",
                [
                    page.get_by_role("button", name=re.compile(r"^\s*Non\s*$", re.I)),
                    "xpath=//button[.//p[normalize-space()='Non']]",
                ],
            )
            try_click_variants(
                page,
                "Au cabinet",
                [
                    page.get_by_role("button", name=re.compile(r"au\s+cabinet", re.I)),
                    "xpath=//button[.//p[normalize-space()='Au cabinet']]",
                ],
            )
            try_click_variants(
                page,
                "Première consultation d'hépato-gastro-entérologie",
                [
                    page.get_by_role("button", name=re.compile("Première consultation d'hépato-gastro-entérologie", re.I)),
                    "xpath=//button[.//*[contains(., \"Première consultation d'hépato-gastro-entérologie\")]]",
                ],
            )
            try_click_variants(
                page,
                "Je n'ai pas de préférence",
                [
                    page.get_by_role("button", name=re.compile("Je n'?ai pas de préférence", re.I)),
                    "xpath=//button[.//*[contains(., \"Je n'ai pas de préférence\")]]",
                ],
            )

            # --------- Availability detection (new 3-state) ---------
            state, times = detect_availability(page)
            save_artifacts(page, label=f"state_{state}")

            if state == "available":
                print(f"✅ Slots found: {times}")
                send_email_notification(f"Slots found on Doctolib: {', '.join(times)}\n\n{ECHIROLLES_URL}")
            elif state == "none":
                print("❌ No appointments (explicit UI).")
            else:
                print("🤷 No positive signal and no 'no-slots' UI → UNKNOWN. Not sending email.")
        finally:
            context.close()
            browser.close()

if __name__ == "__main__":
    main()
