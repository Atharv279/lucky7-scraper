#!/usr/bin/env python3
# Lucky 7 — Scraper (time-capped, deep-iframe, image+text parsers, CI-friendly)  vA.5

import os, re, csv, time, random
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

# ------------ CONFIG / ENVs ------------
URL           = os.getenv("LUCKY7_URL", "https://nohmy99.vip/home")
USERNAME      = os.getenv("NOH_USER")
PASSWORD      = os.getenv("NOH_PASS")
if not USERNAME or not PASSWORD:
    raise SystemExit("Missing NOH_USER / NOH_PASS environment variables.")

CSV_PATH      = os.getenv("CSV_PATH", "lucky7_data.csv")
POLL_SEC      = float(os.getenv("POLL_SEC", "1.2"))
ROUND_TIMEOUT = int(os.getenv("ROUND_TIMEOUT", "90"))
RUN_SECONDS   = int(os.getenv("RUN_SECONDS", "3000"))   # ≈50 min
MAX_ROUNDS    = int(os.getenv("MAX_ROUNDS", "0"))       # 0 = ignore rounds cap

HEADERS = ["ts_utc", "round_id", "rank", "suit_key", "color", "result"]

# ------------ CSV helpers ------------
def ensure_csv(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=HEADERS).writeheader()

def append_row(path: str, row: Dict[str, Any]):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=HEADERS).writerow({k: row.get(k) for k in HEADERS})

# ------------ Debug ------------
def dump_debug(driver, tag="snapshot"):
    try:
        os.makedirs("debug", exist_ok=True)
        with open(f"debug/{tag}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        try: driver.save_screenshot(f"debug/{tag}.png")
        except Exception: pass
    except Exception: pass

# ------------ Parsing ------------
RANK_MAP  = {"A":1,"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"10":10,"J":11,"Q":12,"K":13}
SUIT_KEYN = {"S":"S","H":"H","D":"D","C":"C"}
SUIT_WORD = {"SPADE":"S","HEART":"H","DIAMOND":"D","CLUB":"C"}
SUIT_SYMBOL = {"♠":"S","♥":"H","♦":"D","♣":"C"}

CLOSED_HINTS = ("closed","back","backside","card-back","1_card_20_20")

PAT_SIMPLE = re.compile(r"/(A|K|Q|J|10|[2-9])([SHDC])\.(?:png|jpg|jpeg|webp)\b", re.I)
PAT_DOUBLE = re.compile(r"/(A|K|Q|J|10|[2-9])(SS|HH|DD|CC)\.(?:png|jpg|jpeg|webp)\b", re.I)
PAT_WORDY  = re.compile(r"(ace|king|queen|jack|10|[2-9]).*?(spade|heart|diamond|club)s?", re.I)
PAT_CLASS  = re.compile(r"rank[-_ ]?(A|K|Q|J|10|[2-9]).*?suit[-_ ]?([shdc])", re.I)

# Text/state fallbacks:
PAT_SYM     = re.compile(r"\b(A|K|Q|J|10|[2-9])\s*([♠♥♦♣])", re.I)  # e.g., "8♦", "Q♠"
PAT_OFWORD  = re.compile(r"\b(A|K|Q|J|10|[2-9])\s*(?:OF\s+)?(SPADES?|HEARTS?|DIAMONDS?|CLUBS?)\b", re.I)
PAT_JSON_1  = re.compile(r'"rank"\s*:\s*(\d+)\s*,\s*"suit"\s*:\s*"(SPADE|HEART|DIAMOND|CLUB)"', re.I)
PAT_JSON_2  = re.compile(r'"card"\s*:\s*"(A|K|Q|J|10|[2-9])\s*([SHDC])"', re.I)

def card_from(rank_txt: str, suit_txt: str) -> Optional[Dict[str,Any]]:
    # rank_txt can be "A"/"10"/"7"
    # suit_txt can be "S"/"SPADE"/"♠"
    rtxt = rank_txt.upper()
    if rtxt not in RANK_MAP: return None
    # suit
    stxt = suit_txt.upper()
    if stxt in SUIT_KEYN: s = SUIT_KEYN[stxt]
    elif stxt in SUIT_WORD: s = SUIT_WORD[stxt]
    elif suit_txt in SUIT_SYMBOL: s = SUIT_SYMBOL[suit_txt]
    else: return None
    return {"rank": RANK_MAP[rtxt], "suit_key": s}

def parse_from_url(url: str) -> Optional[Dict[str, Any]]:
    low = url.lower()
    if any(h in low for h in CLOSED_HINTS): return None
    m = PAT_SIMPLE.search(url)
    if m: return card_from(m.group(1), m.group(2))
    m = PAT_DOUBLE.search(url)
    if m: return card_from(m.group(1), m.group(2)[0])
    m = PAT_WORDY.search(url)
    if m: return card_from(m.group(1), m.group(2))
    m = PAT_CLASS.search(url)
    if m: return card_from(m.group(1), m.group(2))
    return None

def parse_from_text(html: str) -> Optional[Dict[str,Any]]:
    # 1) Compact symbol form: "8♦", "Q♠"
    m = PAT_SYM.search(html)
    if m:
        c = card_from(m.group(1), m.group(2))
        if c: return c
    # 2) "8 of diamonds"
    m = PAT_OFWORD.search(html)
    if m:
        rank_raw = m.group(1).upper()
        rank = rank_raw if rank_raw in RANK_MAP else str(int(rank_raw))  # normalize
        suit_word = m.group(2).upper().rstrip('S')  # "DIAMONDS" → "DIAMOND"
        c = card_from(rank, suit_word)
        if c: return c
    # 3) JSON state like {"rank":8,"suit":"DIAMOND"}
    m = PAT_JSON_1.search(html)
    if m:
        rank_num = int(m.group(1))
        suit_word = m.group(2).upper()
        if rank_num in range(1,14):
            return {"rank": rank_num, "suit_key": SUIT_WORD.get(suit_word, None) or suit_word[0]}
    # 4) JSON like {"card":"8D"}
    m = PAT_JSON_2.search(html)
    if m:
        return card_from(m.group(1), m.group(2))
    return None

def result_of(rank: int) -> str:
    if rank < 7: return "below7"
    if rank == 7: return "seven"
    return "above7"

# ------------ JS-powered image discovery ------------
JS_COLLECT_IMAGES = r"""
const out = new Set();
const add = (u) => {
  if (!u) return;
  const s = String(u).trim().split(/\s+/)[0];
  if (!s) return;
  const L = s.toLowerCase();
  if (L.includes('closed') || L.includes('card-back') || L.includes('backside') || L.includes('1_card_20_20')) return;
  out.add(s);
};

// scroll likely areas (for lazy loaders)
for (const sel of ['div.casino-video-cards','.flip-card-container','.video','.web-view','.casino-iframe-ctn']) {
  const el = document.querySelector(sel);
  if (el) el.scrollIntoView({block:'center', inline:'center'});
}

// <img>
document.querySelectorAll('img').forEach(img => {
  add(img.getAttribute('src'));
  add(img.getAttribute('data-src'));
  add(img.getAttribute('data-original'));
  add(img.getAttribute('data-lazy'));
  const sets = [img.getAttribute('srcset'), img.getAttribute('data-srcset')];
  for (const sv of sets) if (sv) add(sv.split(',')[0]);
});

// <source>
document.querySelectorAll('source').forEach(src => {
  const sv = src.getAttribute('srcset');
  if (sv) add(sv.split(',')[0]);
});

// computed background-image
document.querySelectorAll('*').forEach(el => {
  const st = getComputedStyle(el);
  const bg = st && st.backgroundImage || '';
  const m = bg.match(/url\((["']?)(.*?)\1\)/i);
  if (m && m[2]) add(m[2]);
});

return Array.from(out);
"""

def extract_candidates_js(driver) -> list[str]:
    try: return driver.execute_script(JS_COLLECT_IMAGES) or []
    except Exception: return []

# ------------ Fallback HTML scrape ------------
def extract_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    def add(u):
        if not u: return
        u = u.strip().split()[0]
        if not u: return
        if any(h in u.lower() for h in CLOSED_HINTS): return
        if u not in urls: urls.append(u)
    for img in soup.find_all("img"):
        add(img.get("src")); add(img.get("data-src")); add(img.get("data-original")); add(img.get("data-lazy"))
        for attr in ("srcset","data-srcset"):
            sv = img.get(attr)
            if sv: add(sv.split(",")[0].strip())
    for src in soup.find_all("source"):
        sv = src.get("srcset")
        if sv: add(sv.split(",")[0].strip())
    for img in soup.select("div.casino-video-cards img, div.flip-card-container img, img.open-card-image"):
        add(img.get("src"))
    return urls

# ------------ Selenium helpers / navigation ------------
def make_driver():
    opts = Options()
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
        ActionChains(driver).move_to_element(el).pause(0.05).click().perform()
    except Exception:
        driver.execute_script("arguments[0].click();", el)

def login_same_site(driver):
    driver.get(URL)
    time.sleep(2.5)
    for link in driver.find_elements(By.CSS_SELECTOR, "a.auth-link.m-r-5"):
        if link.text.strip().lower() == "login":
            safe_click(driver, link); break
    time.sleep(1.0)
    try:
        user_input = driver.find_element(By.XPATH, "//input[@name='User Name']")
        pass_input = driver.find_element(By.XPATH, "//input[@name='Password']")
        user_input.clear(); user_input.send_keys(USERNAME)
        pass_input.clear(); pass_input.send_keys(PASSWORD)
        pass_input.submit()
        print("✅ Logged in", flush=True)
    except NoSuchElementException:
        print("⚠️ Login inputs not found; maybe already logged in", flush=True)
    time.sleep(2.0)
    dump_debug(driver, "after_login")

def click_nav_casino(driver, timeout=45):
    def try_click(el):
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(0.2)
        try: el.click()
        except Exception: driver.execute_script("arguments[0].click();", el)
    end = time.time() + timeout
    while time.time() < end:
        togglers = driver.find_elements(By.XPATH,
            "//button[contains(@class,'navbar-toggler') or contains(@class,'hamburger') or contains(@class,'menu') or @aria-label='Toggle navigation']")
        for tog in togglers[:2]:
            if tog.is_displayed():
                try: try_click(tog); time.sleep(0.6)
                except Exception: pass
        xps = [
            "//a[contains(translate(., 'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CASINO')]",
            "//button[contains(translate(., 'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CASINO')]",
            "//div[contains(translate(., 'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CASINO')]",
            "//a[contains(@href, '/casino') or contains(@href,'casino')]",
        ]
        for xp in xps:
            els = [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]
            if els:
                try_click(els[0]); time.sleep(1.5); return
        try:
            clicked = bool(driver.execute_script("""
                const U=s=> (s||'').toUpperCase();
                const els=[...document.querySelectorAll('a,button,div,span')];
                const el = els.find(e => U(e.innerText||e.textContent).includes('CASINO'));
                if (el) { el.scrollIntoView({block:'center'}); el.click(); return true; }
                return false;
            """))
        except Exception: clicked = False
        if clicked: time.sleep(1.5); return
        time.sleep(0.5)
    raise TimeoutException("Casino link not found")

def click_lucky7_subtab(driver, timeout=40):
    def try_click(el):
        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", el)
        time.sleep(0.2)
        try: el.click()
        except Exception: driver.execute_script("arguments[0].click();", el)
    end = time.time() + timeout
    while time.time() < end:
        xp = ("//*[self::a or self::button or self::div or self::span or self::li]"
              "[contains(translate(normalize-space(.),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'LUCKY 7') "
              " or contains(translate(normalize-space(.),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'LUCKY7')]")
        els = [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]
        if els: try_click(els[0]); time.sleep(1.2); return True
        for cont in driver.find_elements(By.XPATH, "//*[contains(@class,'tabs') or contains(@class,'nav') or contains(@class,'tab')]")[:3]:
            try: driver.execute_script("if(arguments[0].scrollWidth>arguments[0].clientWidth){arguments[0].scrollLeft += 200;}", cont)
            except Exception: pass
        try:
            clicked = bool(driver.execute_script("""
                const U=s=>(s||'').toUpperCase();
                const els=[...document.querySelectorAll('a,button,div,span,li')];
                const el = els.find(e => { const t=U(e.innerText||e.textContent); return t.includes('LUCKY 7')||t.includes('LUCKY7');});
                if (el) { el.scrollIntoView({block:'center'}); el.click(); return true; }
                return false;
            """))
        except Exception: clicked = False
        if clicked: time.sleep(1.2); return True
        time.sleep(0.5)
    return False

def click_first_game_in_active_pane(driver):
    try:
        pane = W(driver, EC.visibility_of_element_located((By.XPATH, "//*[contains(@class,'tab-pane') and contains(@class,'active')]")), 15)
    except TimeoutException:
        pane = driver
    tiles = pane.find_elements(By.XPATH, ".//*[contains(@class,'casino-name')]")
    target = tiles[0].find_element(By.XPATH, "..") if tiles else None
    if not target:
        cands = pane.find_elements(By.XPATH, ".//*[contains(@class,'casinoicon') or contains(@class,'casinoicons') or contains(@class,'casino-') or self::a]")
        target = cands[0] if cands else None
    if not target:
        raise RuntimeError("No game tiles found in Lucky 7 pane")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
    time.sleep(0.2); safe_click(driver, target)
    time.sleep(0.8)
    if len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[-1])
    time.sleep(2.0)
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    if iframes: driver.switch_to.frame(iframes[0])
    dump_debug(driver, "after_enter_game")

# ------------ Search strategies ------------
def find_round_id_text(driver) -> Optional[str]:
    for sel in [".round-id", ".casino-round-id", "span.roundId", "div.round-id"]:
        try:
            t = driver.find_element(By.CSS_SELECTOR, sel).text.strip()
            if t: return t
        except NoSuchElementException: pass
    return None

def try_parse_here(driver) -> Tuple[Optional[Dict[str,Any]], int, str]:
    # 1) JS/DOM image discovery
    urls = extract_candidates_js(driver)
    for u in urls:
        p = parse_from_url(u)
        if p: return p, len(urls), f"via=url:{u[-30:]}"
    # 2) Fallback: HTML image discovery
    if not urls:
        urls = extract_from_html(driver.page_source)
        for u in urls:
            p = parse_from_url(u)
            if p: return p, len(urls), f"via=html:{u[-30:]}"
    # 3) Last resort: text/JSON in DOM
    txt_card = parse_from_text(driver.page_source)
    if txt_card:
        return txt_card, len(urls), "via=text"
    return None, len(urls), ""

def dfs_frames_for_card(driver, max_depth=5, depth=0) -> Tuple[Optional[Dict[str,Any]], int, int, str]:
    parsed, cnt, how = try_parse_here(driver)
    if parsed: return parsed, cnt, 1, how
    if depth >= max_depth: return None, cnt, 0, ""
    total_cnt, hits = cnt, 0
    note = ""
    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for fr in frames:
        try:
            driver.switch_to.frame(fr)
            p, c, h, how2 = dfs_frames_for_card(driver, max_depth, depth+1)
            total_cnt += c; hits += h
            driver.switch_to.parent_frame()
            if p:
                note = how2
                return p, total_cnt, hits, note
        except Exception:
            try: driver.switch_to.parent_frame()
            except Exception: pass
    return None, total_cnt, hits, note

# ------------ Main ------------
def main():
    print(f"📊 CSV → {CSV_PATH}", flush=True)
    driver = make_driver()
    ensure_csv(CSV_PATH)
    try:
        login_same_site(driver)
        W(driver, EC.presence_of_element_located((By.TAG_NAME, "body")), 30)
        click_nav_casino(driver)
        click_lucky7_subtab(driver)
        click_first_game_in_active_pane(driver)
        print("✅ Entered Lucky 7 game", flush=True)

        start_ts = time.time()
        last_sig = None
        saved = 0
        round_num = 1
        last_heartbeat = 0

        while True:
            elapsed = time.time() - start_ts
            if RUN_SECONDS and elapsed >= RUN_SECONDS:
                print(f"⏱️ Time cap reached ({int(elapsed)}s). Saved {saved} rounds.", flush=True)
                break

            t0 = time.time()
            parsed = None
            total_imgs = 0
            how = ""

            while not parsed:
                # Encourage lazy loaders:
                try:
                    driver.execute_script("""
                      for (const sel of ['div.casino-video-cards','.flip-card-container','.video','.web-view']) {
                        const el = document.querySelector(sel); if (el) el.scrollIntoView({block:'center', inline:'center'});
                      }
                    """)
                except Exception: pass

                p, c, h, how = dfs_frames_for_card(driver, max_depth=5)
                parsed, total_imgs = p, c

                now = time.time()
                if now - last_heartbeat >= 5:
                    last_heartbeat = now
                    try: frames_top = len(driver.find_elements(By.TAG_NAME, "iframe"))
                    except Exception: frames_top = -1
                    print(f"⏳ Waiting… imgs={total_imgs} frames(top)={frames_top} elapsed={int(elapsed)}s", flush=True)
                    dump_debug(driver, f"wait_{int(elapsed)}s")

                if parsed: break

                if time.time() - t0 > ROUND_TIMEOUT:
                    print("🔄 Round timeout: refreshing game view", flush=True)
                    driver.refresh(); time.sleep(5)
                    ifr = driver.find_elements(By.TAG_NAME, "iframe")
                    if ifr: driver.switch_to.frame(ifr[0])
                    t0 = time.time()
                time.sleep(0.35 + random.uniform(0.05, 0.25))

            rid = find_round_id_text(driver)
            rank, suit = parsed["rank"], parsed["suit_key"]
            color = "red" if suit in ("H","D") else "black"
            res = "seven" if rank==7 else ("below7" if rank<7 else "above7")
            row = {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "round_id": rid, "rank": rank, "suit_key": suit,
                "color": color, "result": res,
            }

            sig = f"{rid}|{rank}|{suit}"
            if sig == last_sig:
                time.sleep(POLL_SEC); continue
            last_sig = sig

            append_row(CSV_PATH, row)
            print(f"✅ Round {round_num}: {rank}{suit} → {res} ({how})", flush=True)
            saved += 1

            if (MAX_ROUNDS and saved >= MAX_ROUNDS) or (RUN_SECONDS and (time.time() - start_ts >= RUN_SECONDS)):
                print(f"🏁 Done — captured {saved} rounds in {int(time.time()-start_ts)}s.", flush=True)
                break

            round_num += 1
            time.sleep(POLL_SEC)

    except KeyboardInterrupt:
        print("🛑 Stopped by user", flush=True)
    finally:
        if saved == 0:
            dump_debug(driver, "no_data_end")
            with open("debug/NO_DATA.txt","w",encoding="utf-8") as f:
                f.write("Scraper saved 0 rounds this run.")
        try: driver.quit()
        except Exception: pass

if __name__ == "__main__":
    main()
