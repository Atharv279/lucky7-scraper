#!/usr/bin/env python3
"""
Lucky 7 — Single-File Scraper (GitHub-ready, non-headless)  v3.1

Key update:
- If the "Casino" nav click isn't found in CI, we fall back to LUCKY7_PANEL_URL (env),
  opening the Lucky-7 panel page directly.

Outputs clean CSV: data/lucky7_data.csv with columns:
ts_utc, round_id, rank, suit_key, color, result
"""

import os, re, csv, time, random
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

# ===================== CONFIG =====================
URL = os.getenv("LUCKY7_URL", "https://nohmy99.vip/home")
PANEL_URL = os.getenv("LUCKY7_PANEL_URL")  # <- set this in GitHub Actions Variables

USERNAME = os.getenv("NOH_USER")
PASSWORD = os.getenv("NOH_PASS")
if not USERNAME or not PASSWORD:
    raise SystemExit("Missing NOH_USER / NOH_PASS environment variables.")

CSV_PATH = os.getenv("CSV_PATH", "data/lucky7_data.csv")
POLL_SEC = float(os.getenv("POLL_SEC", "1.2"))
ROUND_TIMEOUT = int(os.getenv("ROUND_TIMEOUT", "90"))
MAX_ROUNDS = int(os.getenv("MAX_ROUNDS", "20"))  # collect this many rows per run

# Non-headless; in CI, wrap with: xvfb-run -a -s "-screen 0 1600x900x24" python scraper.py
VISIBLE_BROWSER = True

# ===================== CSV =====================
HEADERS = ["ts_utc", "round_id", "rank", "suit_key", "color", "result"]

def ensure_csv(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=HEADERS).writeheader()

def append_row(path: str, row: Dict[str, Any]):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=HEADERS).writerow({k: row.get(k) for k in HEADERS})

# ===================== Parsing =====================
RANK_MAP = {"A":1,"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"10":10,"J":11,"Q":12,"K":13}
SUIT_KEY = {"S":"S","H":"H","D":"D","C":"C"}

PAT_SIMPLE = re.compile(r"/(A|K|Q|J|10|[2-9])([SHDC])\.(?:png|jpg|jpeg|webp)\b", re.I)        # /7D.png
PAT_DOUBLE = re.compile(r"/(A|K|Q|J|10|[2-9])(SS|HH|DD|CC)\.(?:png|jpg|jpeg|webp)\b", re.I)    # /10CC.webp -> C
PAT_WORDY = re.compile(r"(ace|king|queen|jack|10|[2-9]).*?(spade|heart|diamond|club)s?", re.I) # queen_of_spades.png
PAT_CLASS  = re.compile(r"rank[-_ ]?(A|K|Q|J|10|[2-9]).*?suit[-_ ]?([shdc])", re.I)            # rank-7 suit-h

CLOSED_HINTS = ("closed", "back", "backside", "card-back", "1_card_20_20")

def parse_from_url(url: str) -> Optional[Dict[str,str]]:
    low = url.lower()
    if any(h in low for h in CLOSED_HINTS):
        return None

    m = PAT_SIMPLE.search(url)
    if m:
        r, s = m.group(1).upper(), m.group(2).upper()
        return {"rank": RANK_MAP[r], "suit_key": SUIT_KEY[s]}

    m = PAT_DOUBLE.search(url)
    if m:
        r, ss = m.group(1).upper(), m.group(2).upper()
        return {"rank": RANK_MAP[r], "suit_key": SUIT_KEY[ss[0]]}

    m = PAT_WORDY.search(url)
    if m:
        rtxt, stxt = m.group(1).upper(), m.group(2).upper()
        rank = RANK_MAP[rtxt] if rtxt in RANK_MAP else int(rtxt)
        suit = {"SPADE":"S","HEART":"H","DIAMOND":"D","CLUB":"C"}[stxt]
        return {"rank": rank, "suit_key": suit}

    m = PAT_CLASS.search(url)
    if m:
        r, s = m.group(1).upper(), m.group(2).upper()
        return {"rank": RANK_MAP[r], "suit_key": SUIT_KEY[s]}

    return None

def result_of(rank: int) -> str:
    if rank < 7: return "below7"
    if rank == 7: return "seven"
    return "above7"

# ===================== DOM scraping (DT-style) =====================
def extract_card_img_urls(html: str) -> list[str]:
    """Prefer open-card images from Lucky 7 DOM; skip closed/back."""
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []

    # likely containers: prefer the back face (open side on flip)
    queries = [
        "div.casino-video-cards div.flip-card-back img",
        "div.flip-card-inner div.flip-card-back img",
        "div.lucky7-open img",
        "img.open-card-image",
        # generic fallbacks:
        "div.casino-video-cards img",
        "div.flip-card-container img",
    ]
    for q in queries:
        for img in soup.select(q):
            src = (img.get("src") or "").strip()
            alt = (img.get("alt") or "").strip().lower()
            if not src:
                continue
            if alt == "closed":
                continue
            if any(h in src.lower() for h in CLOSED_HINTS):
                continue
            if src not in urls:
                urls.append(src)

    # final sweep: any <img> with '/img/cards/' or 'card' in URL
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src:
            continue
        low = src.lower()
        if any(h in low for h in CLOSED_HINTS):
            continue
        if "/img/cards/" in low or "card" in low:
            if src not in urls:
                urls.append(src)

    return urls

# ===================== Selenium helpers =====================
def make_driver():
    opts = Options()
    # non-headless; use Xvfb in CI for a virtual display
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1600,900")
    opts.add_argument("--log-level=3")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)

def W(driver, cond, timeout=15):
    return WebDriverWait(driver, timeout).until(cond)

def safe_click(driver, el):
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)

# ===================== Site flow =====================
def login_same_site(driver):
    driver.get(URL)
    time.sleep(3.0)
    # Open login form if visible
    for link in driver.find_elements(By.CSS_SELECTOR, "a.auth-link.m-r-5"):
        if link.text.strip().lower() == "login":
            safe_click(driver, link); break
    time.sleep(1.2)
    # Fill creds (name attributes from your site)
    try:
        user_input = driver.find_element(By.XPATH, "//input[@name='User Name']")
        pass_input = driver.find_element(By.XPATH, "//input[@name='Password']")
        user_input.clear(); user_input.send_keys(USERNAME)
        pass_input.clear(); pass_input.send_keys(PASSWORD)
        pass_input.submit()
    except NoSuchElementException:
        pass  # maybe already logged in
    time.sleep(3.0)

def click_nav_casino_or_fallback(driver):
    """
    Try to click the Casino nav. If not found within 15s, use LUCKY7_PANEL_URL (env) as fallback.
    """
    try:
        # try a few alternative locators
        xpaths = [
            "//a[contains(@href, '/casino/')]",
            "//a[normalize-space()='Casino']",
            "//a[contains(translate(., 'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CASINO')]",
            "//nav//a[contains(@href, '/casino')]",
        ]
        deadline = time.time() + 15
        while time.time() < deadline:
            for xp in xpaths:
                els = driver.find_elements(By.XPATH, xp)
                if els:
                    el = els[0]
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    time.sleep(0.2)
                    safe_click(driver, el)
                    time.sleep(2.0)
                    return True
            time.sleep(0.5)
        raise TimeoutException("Casino link not found")
    except Exception as e:
        if PANEL_URL:
            print("⚠️  Casino link not found; using LUCKY7_PANEL_URL fallback")
            driver.get(PANEL_URL)
            time.sleep(3.0)
            return True
        else:
            print("❌ Casino link not found and no LUCKY7_PANEL_URL provided.")
            raise

def click_lucky7_subtab(driver):
    try:
        el = W(driver, EC.element_to_be_clickable((
            By.XPATH,
            "//a[contains(translate(., 'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'LUCKY 7') or "
            "contains(translate(., 'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'LUCKY7')]"
        )), 15)
        safe_click(driver, el)
        time.sleep(1.2)
    except TimeoutException:
        # If we're already on the Lucky7 detail page, that's fine
        pass

def click_first_game_in_active_pane(driver):
    try:
        try:
            pane = W(driver, EC.visibility_of_element_located((
                By.XPATH, "//*[contains(@class,'tab-pane') and contains(@class,'active')]"
            )), 10)
        except TimeoutException:
            pane = driver
        tiles = pane.find_elements(By.XPATH, ".//*[contains(@class,'casino-name')]")
        target = tiles[0].find_element(By.XPATH, "..") if tiles else None
        if not target:
            cands = pane.find_elements(By.XPATH, ".//*[contains(@class,'casinoicon') or contains(@class,'casinoicons') or contains(@class,'casino-') or self::a]")
            target = cands[0] if cands else None
        if target:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
            time.sleep(0.2); safe_click(driver, target)
            time.sleep(1.5)
        # Switch to game window if opened
        if len(driver.window_handles) > 1:
            driver.switch_to.window(driver.window_handles[-1])
        time.sleep(2.5)
        # Enter iframe if present
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        if iframes:
            driver.switch_to.frame(iframes[0])
    except Exception:
        # If this fails but the panel URL opened the game directly, proceed anyway
        pass

def find_round_id_text(driver) -> Optional[str]:
    for sel in [".round-id", ".casino-round-id", "span.roundId", "div.round-id"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            t = el.text.strip()
            if t: return t
        except NoSuchElementException:
            continue
    return None

# ===================== Main =====================
def main():
    print(f"CSV → {CSV_PATH}")
    driver = make_driver()
    ensure_csv(CSV_PATH)

    try:
        # Login + nav (with fallback)
        login_same_site(driver)
        W(driver, EC.presence_of_element_located((By.TAG_NAME, "body")), 20)
        click_nav_casino_or_fallback(driver)
        click_lucky7_subtab(driver)
        click_first_game_in_active_pane(driver)

        last_sig = None
        saved = 0

        while True:
            t0 = time.time()
            parsed = None

            while not parsed:
                # Try current context
                urls = extract_card_img_urls(driver.page_source)
                for u in urls:
                    parsed = parse_from_url(u)
                    if parsed:
                        break

                if parsed:
                    break

                # Try other iframes depth-1
                driver.switch_to.default_content()
                for fr in driver.find_elements(By.TAG_NAME, "iframe"):
                    try:
                        driver.switch_to.frame(fr)
                        urls = extract_card_img_urls(driver.page_source)
                        for u in urls:
                            parsed = parse_from_url(u)
                            if parsed:
                                break
                        if parsed:
                            break
                    finally:
                        driver.switch_to.default_content()

                if parsed:
                    # Best effort: re-enter first iframe for next cycle
                    try:
                        driver.switch_to.frame(driver.find_elements(By.TAG_NAME, "iframe")[0])
                    except Exception:
                        pass
                    break

                if time.time() - t0 > ROUND_TIMEOUT:
                    driver.refresh(); time.sleep(5)
                    ifr = driver.find_elements(By.TAG_NAME, "iframe")
                    if ifr:
                        driver.switch_to.frame(ifr[0])
                    t0 = time.time()

                time.sleep(0.3 + random.uniform(0.05,0.2))

            rid = find_round_id_text(driver)
            rank, suit = parsed["rank"], parsed["suit_key"]
            res = result_of(rank)
            row = {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "round_id": rid,
                "rank": rank,
                "suit_key": suit,
                "color": "red" if suit in ("H","D") else "black",
                "result": res,
            }

            # Dedupe by signature
            sig = f"{rid}|{rank}|{suit}"
            if sig == last_sig:
                time.sleep(POLL_SEC + random.uniform(0.05,0.2))
                continue
            last_sig = sig

            append_row(CSV_PATH, row)
            saved += 1
            print(f"Saved {saved}: {rank}{suit} → {res}")

            if MAX_ROUNDS and saved >= MAX_ROUNDS:
                print(f"Done — captured {saved} rounds.")
                break

            time.sleep(POLL_SEC + random.uniform(0.05,0.2))

    except KeyboardInterrupt:
        print("Stopped by user")
    finally:
        try: driver.quit()
        except Exception: pass

if __name__ == "__main__":
    main()
