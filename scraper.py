#!/usr/bin/env python3
"""
Lucky7 Panel Scraper — single file, simple, same site/login — v1.0

- Logs in to the SAME site (nohmy99) using the same credentials.
- Clicks: Casino → Lucky 7 tab → FIRST game tile in that panel.
- In the game window, watches for the single revealed card each round.
- Appends rows to CSV with derived features (Below7/Seven/Above7, Odd/Even, Red/Black).
- Minimal dedupe: skips if same round_id (when present) or same (rank+suit) seen consecutively.

Dependencies:
  pip install selenium webdriver-manager beautifulsoup4

Run:
  python lucky7_samepanel_scraper.py
Stop:
  Ctrl + C
"""

import os, re, csv, time, sys
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from collections import OrderedDict

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

# ===================== CONFIG (same site + login) =====================
URL          = "https://nohmy99.vip/home"
USERNAME     = os.getenv("NOH_USER", "7558714781")      # same as your DT script
PASSWORD     = os.getenv("NOH_PASS", "Atharv@1204")     # same as your DT script
CSV_PATH     = "lucky7_data.csv"
POLL_SEC     = 2.0
ROUND_TIMEOUT= 90
MAX_ROUNDS = int(os.getenv('MAX_ROUNDS', '20'))  # stop after saving this many rounds per run

VISIBLE_BROWSER = True   # keep it visible like your previous script (set False for headless)

# ===================== Helpers & Parsing =====================
HEADERS = [
    "ts_utc","round_id","rank","suit_key","color",
    "is_odd","is_even","is_red","is_black",
    "cat_below7","cat_seven","cat_above7"
]

SUIT_MAP = {
    "S": {"key": "S", "symbol": "♠", "color": "black"},
    "H": {"key": "H", "symbol": "♥", "color": "red"},
    "D": {"key": "D", "symbol": "♦", "color": "red"},
    "C": {"key": "C", "symbol": "♣", "color": "black"},
    # names
    "SPADES":  {"key": "S", "symbol": "♠", "color": "black"},
    "HEARTS":  {"key": "H", "symbol": "♥", "color": "red"},
    "DIAMONDS":{"key": "D", "symbol": "♦", "color": "red"},
    "CLUBS":   {"key": "C", "symbol": "♣", "color": "black"},
    # symbols direct
    "♠": {"key": "S", "symbol": "♠", "color": "black"},
    "♥": {"key": "H", "symbol": "♥", "color": "red"},
    "♦": {"key": "D", "symbol": "♦", "color": "red"},
    "♣": {"key": "C", "symbol": "♣", "color": "black"},
}
RANK_MAP = {"A":1,"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"10":10,"J":11,"Q":12,"K":13}

def parse_rank(text: str) -> Optional[int]:
    if not text: return None
    t = text.strip().upper()
    if t in RANK_MAP: return RANK_MAP[t]
    m = re.search(r"(10|[2-9])", t)
    if m: return int(m.group(1))
    for k in ("A","J","Q","K"):
        if k in t: return RANK_MAP[k]
    return None

def parse_suit(text: str) -> Optional[Dict[str,str]]:
    if not text: return None
    t = text.strip().upper()
    # normalize words
    t = t.replace("SPADE","SPADES").replace("HEART","HEARTS").replace("DIAMOND","DIAMONDS").replace("CLUB","CLUBS")
    # symbol direct
    for sym in ("♠","♥","♦","♣"):
        if sym in text: return SUIT_MAP[sym]
    if t in SUIT_MAP: return SUIT_MAP[t]
    # search for any word hit
    for name in ("SPADES","HEARTS","DIAMONDS","CLUBS"):
        if name in t: return SUIT_MAP[name]
    return None

def derive_features(rank: int, suit_key: str) -> Dict[str, Any]:
    suit_key = suit_key.upper()
    color = "red" if suit_key in ("H","D") else "black"
    return {
        "color": color,
        "is_odd": rank % 2 == 1,
        "is_even": rank % 2 == 0,
        "cat_below7": rank < 7,
        "cat_seven": rank == 7,
        "cat_above7": rank > 7,
        "is_red": color == "red",
        "is_black": color == "black",
    }

# ===================== Selenium Boot =====================
def make_driver():
    opts = Options()
    if not VISIBLE_BROWSER:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1600,900")
    opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.add_argument("--log-level=3")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)

def W(driver, cond, timeout=15):
    return WebDriverWait(driver, timeout).until(cond)

def safe_click(driver, el):
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)

# ===================== Site Navigation (same style, Lucky 7) =====================
def login_same_site(driver):
    driver.get(URL)
    time.sleep(2)
    # click Login link
    for link in driver.find_elements(By.CSS_SELECTOR, "a.auth-link.m-r-5"):
        if link.text.strip().lower() == "login":
            safe_click(driver, link); break
    time.sleep(1.5)
    try:
        user_input = driver.find_element(By.XPATH, "//input[@name='User Name']")
        pass_input = driver.find_element(By.XPATH, "//input[@name='Password']")
        user_input.clear(); user_input.send_keys(USERNAME)
        pass_input.clear(); pass_input.send_keys(PASSWORD)
        pass_input.submit()
        print("✅ Logged in")
    except NoSuchElementException:
        print("⚠️ Login inputs not found; maybe already logged in")

def click_nav_casino(driver):
    el = W(driver, EC.element_to_be_clickable((By.XPATH, "//a[contains(@href, '/casino/99998') or contains(., 'Casino')]")))
    safe_click(driver, el)
    time.sleep(2)

def click_lucky7_subtab(driver):
    # 'Lucky 7' or 'Lucky7' (case-insensitive)
    el = W(driver, EC.element_to_be_clickable((
        By.XPATH,
        "//a[contains(translate(., 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'LUCKY 7') or " +
        "contains(translate(., 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'LUCKY7')]"
    )))
    safe_click(driver, el)
    time.sleep(2)

def click_first_game_in_active_pane(driver):
    """
    Clicks the first game tile under the ACTIVE tab/pane (Lucky 7 panel).
    Tries:
      1) First '.casino-name' ancestor tile
      2) Fallback to first tile-like element in the pane
    Switches to the newly opened tab/window if one appears.
    """
    # Scope to an active pane (div with 'active' class)
    try:
        pane = W(driver, EC.visibility_of_element_located((
            By.XPATH, "//*[contains(@class,'tab-pane') and contains(@class,'active')]"
        )))
    except TimeoutException:
        pane = driver  # fallback to whole doc

    # Option 1: a casino-name inside
    tiles = pane.find_elements(By.XPATH, ".//*[contains(@class,'casino-name')]")
    if tiles:
        tile = tiles[0].find_element(By.XPATH, "..")
        driver.execute_script("arguments[0].scrollIntoView({block:'center',inline:'center'});", tile)
        time.sleep(0.2)
        try:
            tile.click()
        except Exception:
            driver.execute_script("arguments[0].click();", tile)
    else:
        # Option 2: first tile-looking element
        candidates = pane.find_elements(By.XPATH, ".//*[contains(@class,'casinoicon') or contains(@class,'casinoicons') or contains(@class,'casino-') or self::a]")
        if not candidates:
            raise RuntimeError("Could not find any game tiles in the active pane")
        cand = candidates[0]
        driver.execute_script("arguments[0].scrollIntoView({block:'center',inline:'center'});", cand)
        time.sleep(0.2)
        try:
            cand.click()
        except Exception:
            driver.execute_script("arguments[0].click();", cand)

    time.sleep(0.5)
    if len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[-1])
        print("↪️ Switched to game window")

    # Some games are inside an iframe; try the first iframe if present
    time.sleep(3)
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    if iframes:
        driver.switch_to.frame(iframes[0])
        print("↪️ Switched into game iframe")

# ===================== Scraping (Lucky 7: single card) =====================
def find_round_id_text(driver) -> Optional[str]:
    # Best-guess selectors; harmless if missing
    for sel in [".round-id", ".casino-round-id", "span.roundId", "div.round-id"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            txt = el.text.strip()
            if txt: return txt
        except NoSuchElementException:
            continue
    return None

def extract_card_from_page(driver) -> Optional[Dict[str, Any]]:
    """Returns {'rank': int, 'suit_key': 'S|H|D|C'} or None if not visible yet."""
    html = driver.page_source
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: look for one visible card image inside likely containers
    img = None
    # common containers on similar sites
    for q in [
        "div.l-cards img", "div.card1-ctn img", "div.card img", "img[src*='cards']",
        "div.result-card img", "div.open-card img"
    ]:
        node = soup.select_one(q)
        if node and node.get("src"):
            img = node; break

    if img:
        src = img.get("src")
        # try to parse code from filename: e.g., ".../7H.png" or "ace_of_spades.png"
        m = re.search(r"/([AJQK]|10|[2-9])([SHDC])\.", src, re.I)
        if m:
            rank_txt, suit_chr = m.group(1).upper(), m.group(2).upper()
            rank = RANK_MAP["10"] if rank_txt == "10" else (RANK_MAP[rank_txt] if rank_txt in RANK_MAP else None)
            return {"rank": rank, "suit_key": suit_chr}
        # words style
        m2 = re.search(r"(ace|jack|queen|king|10|[2-9]).*?(spade|heart|diamond|club)s?", src, re.I)
        if m2:
            rtxt = m2.group(1).upper()
            stxt = m2.group(2).upper()
            # map rank text to value
            rank = RANK_MAP.get(rtxt) or (10 if rtxt == "10" else (int(rtxt) if rtxt.isdigit() else None))
            suit_key = {"SPADE":"S","HEART":"H","DIAMOND":"D","CLUB":"C"}[stxt]
            return {"rank": rank, "suit_key": suit_key}

    # Strategy 2: textual rank/suit on page
    # Look for suit symbol and a nearby rank
    text = soup.get_text(" ", strip=True)
    sym = None
    for s in ["♠","♥","♦","♣"]:
        if s in text: sym = s; break
    if sym:
        m3 = re.search(r"(A|10|[2-9]|J|Q|K)", text, re.I)
        if m3:
            rank_txt = m3.group(1).upper()
            rank = RANK_MAP.get(rank_txt) or (10 if rank_txt == "10" else (int(rank_txt) if rank_txt.isdigit() else None))
            suit_key = SUIT_MAP[sym]["key"]
            return {"rank": rank, "suit_key": suit_key}

    return None

def ensure_csv(path: str):
    exists = os.path.exists(path)
    if not exists:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HEADERS)
            w.writeheader()

def append_row(path: str, row: Dict[str, Any]):
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writerow({k: row.get(k) for k in HEADERS})

# ===================== Main =====================
def main():
    driver = make_driver()

    try:
        login_same_site(driver)
        click_nav_casino(driver)
        click_lucky7_subtab(driver)
        click_first_game_in_active_pane(driver)
        print("✅ Entered Lucky 7 game")

        ensure_csv(CSV_PATH)
        last_sig = None
        last_round_id = None
        round_counter = 1

        while True:
            print(f"--- Waiting for Round {round_counter} ---")
            start = time.time()
            card = None

            while not card:
                card = extract_card_from_page(driver)
                if card:
                    break
                if time.time() - start > ROUND_TIMEOUT:
                    print("⏳ Round timeout → refresh")
                    driver.refresh()
                    time.sleep(6)
                    # re-enter iframe if present
                    iframes = driver.find_elements(By.TAG_NAME, "iframe")
                    if iframes:
                        driver.switch_to.frame(iframes[0])
                    start = time.time()
                time.sleep(0.5)

            round_id = find_round_id_text(driver)
            rank, suit_key = card["rank"], card["suit_key"]
            feats = derive_features(rank, suit_key)

            # Dedupe
            sig = f"{round_id}|{rank}|{suit_key}"
            if round_id:
                if round_id == last_round_id:
                    time.sleep(POLL_SEC); continue
                last_round_id = round_id
            else:
                if sig == last_sig:
                    time.sleep(POLL_SEC); continue
                last_sig = sig

            row = {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "round_id": round_id,
                "rank": rank,
                "suit_key": suit_key,
                "color": feats["color"],
                "is_odd": int(feats["is_odd"]),
                "is_even": int(feats["is_even"]),
                "is_red": int(feats["is_red"]),
                "is_black": int(feats["is_black"]),
                "cat_below7": int(feats["cat_below7"]),
                "cat_seven": int(feats["cat_seven"]),
                "cat_above7": int(feats["cat_above7"]),
            }
            append_row(CSV_PATH, row)
            print(f"💾 Saved Round: round_id={round_id} card={rank}{suit_key}")

            round_counter += 1
            time.sleep(POLL_SEC)

    except KeyboardInterrupt:
        print("🛑 Stopped by user.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()
