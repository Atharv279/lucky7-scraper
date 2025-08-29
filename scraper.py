#!/usr/bin/env python3
# Lucky 7 — Scraper vA.8
# Adds Shadow DOM traversal to discover card assets/text inside shadow roots.
# Keeps: login → casino → Lucky7 → first-game; deep iframes; DOM + JS hints; CDP network; text/JSON fallbacks.
# Writes clean CSV; dumps rich debug artifacts.

import os, re, csv, time, random, json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, List

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
ROUND_TIMEOUT = int(os.getenv("ROUND_TIMEOUT", "120"))
RUN_SECONDS   = int(os.getenv("RUN_SECONDS", "3000"))   # ~50 minutes per run
MAX_ROUNDS    = int(os.getenv("MAX_ROUNDS", "0"))       # 0 = unlimited
DEBUG_DUMP    = int(os.getenv("DEBUG_DUMP", "1"))

HEADERS = ["ts_utc","round_id","rank","suit_key","color","result"]

# ------------ CSV / Debug ------------
def ensure_csv(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=HEADERS).writeheader()

def append_row(path: str, row: Dict[str, Any]):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=HEADERS).writerow({k: row.get(k) for k in HEADERS})

def ensure_debug():
    try: os.makedirs("debug", exist_ok=True)
    except Exception: pass

def dump_debug(driver, tag="snapshot"):
    if not DEBUG_DUMP: return
    ensure_debug()
    try:
        with open(f"debug/{tag}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        try: driver.save_screenshot(f"debug/{tag}.png")
        except Exception: pass
    except Exception: pass

def dump_text(name: str, lines: List[str]):
    if not DEBUG_DUMP: return
    ensure_debug()
    try:
        with open(f"debug/{name}", "w", encoding="utf-8") as f:
            for l in lines: f.write(str(l) + "\n")
    except Exception: pass

# ------------ Parsing ------------
RANK_MAP  = {"A":1,"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"10":10,"J":11,"Q":12,"K":13}
SUIT_KEYN = {"S":"S","H":"H","D":"D","C":"C"}
SUIT_WORD = {"SPADE":"S","HEART":"H","DIAMOND":"D","CLUB":"C"}
SUIT_SYMBOL = {"♠":"S","♥":"H","♦":"D","♣":"C"}
CLOSED_HINTS = ("closed","back","backside","card-back","1_card_20_20")

PAT_SIMPLE = re.compile(r"/(A|K|Q|J|10|[2-9])([SHDC])\.(?:png|jpg|jpeg|webp|webm)\b", re.I)
PAT_DOUBLE = re.compile(r"/(A|K|Q|J|10|[2-9])(SS|HH|DD|CC)\.(?:png|jpg|jpeg|webp|webm)\b", re.I)
PAT_WORDY  = re.compile(r"(ace|king|queen|jack|10|[2-9]).*?(spade|heart|diamond|club)s?", re.I)
PAT_CLASS  = re.compile(r"rank[-_ ]?(A|K|Q|J|10|[2-9]).*?suit[-_ ]?([shdc])", re.I)

PAT_SYM     = re.compile(r"\b(A|K|Q|J|10|[2-9])\s*([♠♥♦♣])", re.I)
PAT_OFWORD  = re.compile(r"\b(A|K|Q|J|10|[2-9])\s*(?:OF\s+)?(SPADES?|HEARTS?|DIAMONDS?|CLUBS?)\b", re.I)
PAT_JSON_1  = re.compile(r'"rank"\s*:\s*(\d+)\s*,\s*"suit"\s*:\s*"(SPADE|HEART|DIAMOND|CLUB)"', re.I)
PAT_JSON_2  = re.compile(r'"card"\s*:\s*"(A|K|Q|J|10|[2-9])\s*([SHDC])"', re.I)

def card_from(rank_txt: str, suit_txt: str) -> Optional[Dict[str,Any]]:
    rtxt = rank_txt.upper()
    if rtxt not in RANK_MAP: return None
    stxt = suit_txt.upper()
    if stxt in SUIT_KEYN: s = SUIT_KEYN[stxt]
    elif stxt in SUIT_WORD: s = SUIT_WORD[stxt]
    elif suit_txt in SUIT_SYMBOL: s = SUIT_SYMBOL[suit_txt]
    else: return None
    return {"rank": RANK_MAP[rtxt], "suit_key": s}

def parse_from_any(s: str) -> Optional[Dict[str, Any]]:
    low = s.lower()
    if any(h in low for h in CLOSED_HINTS): return None
    m = PAT_SIMPLE.search(s)
    if m: return card_from(m.group(1), m.group(2))
    m = PAT_DOUBLE.search(s)
    if m: return card_from(m.group(1), m.group(2)[0])
    m = PAT_WORDY.search(s)
    if m: return card_from(m.group(1), m.group(2))
    m = PAT_CLASS.search(s)
    if m: return card_from(m.group(1), m.group(2))
    return None

def parse_from_text(html: str) -> Optional[Dict[str,Any]]:
    m = PAT_SYM.search(html)
    if m:
        c = card_from(m.group(1), m.group(2))
        if c: return c
    m = PAT_OFWORD.search(html)
    if m:
        rank = m.group(1).upper()
        suit = m.group(2).upper().rstrip('S')
        c = card_from(rank, suit)
        if c: return c
    m = PAT_JSON_1.search(html)
    if m:
        rank_num = int(m.group(1))
        suit_word = m.group(2).upper()
        if 1 <= rank_num <= 13:
            return {"rank": rank_num, "suit_key": SUIT_WORD.get(suit_word, suit_word[0])}
    m = PAT_JSON_2.search(html)
    if m:
        return card_from(m.group(1), m.group(2))
    return None

def color_of(suit_key: str) -> str:
    return "red" if suit_key in ("H","D") else "black"

def result_of(rank: int) -> str:
    if rank < 7: return "below7"
    if rank == 7: return "seven"
    return "above7"

# ------------ HTML (BeautifulSoup) discovery ------------
def extract_card_sources_from_html(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: List[str] = []
    def add(u: str):
        if not u: return
        u = u.strip().split()[0]
        if not u: return
        if any(h in u.lower() for h in CLOSED_HINTS): return
        if u not in urls: urls.append(u)

    priority = [
        "div.casino-video-cards div.flip-card-back img",
        "div.flip-card-inner div.flip-card-back img",
        "div.lucky7-open img",
        "img.open-card-image",
        "div.card1-ctn div.l-cards img",
        "div.l-cards img",
    ]
    for q in priority:
        for img in soup.select(q):
            add(img.get("src"))
            for a in ("data-src","data-original","data-lazy","srcset","data-srcset"):
                v = img.get(a)
                if v: add(v.split(",")[0])

    for sel in ["div.casino-video-cards img","div.flip-card-container img","div.card img","img"]:
        for img in soup.select(sel):
            add(img.get("src"))
    # background-image + class/data-* hints
    for el in soup.find_all(True):
        style = (el.get("style") or "")
        m = re.search(r"background-image\s*:\s*url\(([^)]+)\)", style, re.I)
        if m:
            u = m.group(1).strip('\'" ')
            add(u)
        dr, ds = el.get("data-rank"), el.get("data-suit")
        if dr and ds: add(f"/{str(dr).upper()}{str(ds).upper()}.png")
        cls = " ".join(el.get("class") or [])
        m2 = re.search(r"rank[-_ ]?(A|10|[2-9]|J|Q|K).*?suit[-_ ]?([shdc])", cls, re.I)
        if m2: add(f"/{m2.group(1).upper()}{m2.group(2).upper()}.png")
    return urls

# ------------ JS (regular DOM) hints ------------
JS_HINTS = r"""
const out = [];
const add = (k,v) => { if (!v) return; const s=String(v).trim(); if (!s) return; out.push([k,s]); };
for (const img of document.querySelectorAll('img')) {
  add('img.src', img.getAttribute('src'));
  add('img.data-src', img.getAttribute('data-src'));
  add('img.data-original', img.getAttribute('data-original'));
  add('img.data-lazy', img.getAttribute('data-lazy'));
  const sets=[img.getAttribute('srcset'),img.getAttribute('data-srcset')];
  for (const sv of sets) if (sv) add('img.srcset', sv.split(',')[0]);
  add('img.alt', img.getAttribute('alt'));
}
for (const el of document.querySelectorAll('*')) {
  const cs = getComputedStyle(el);
  const bg = cs && cs.backgroundImage || '';
  const m = bg.match(/url\((["']?)(.*?)\1\)/i);
  if (m && m[2]) add('css.bg', m[2]);
  for (const a of ['data-rank','data-suit','data-card','data-value','title','aria-label']) {
    const v = el.getAttribute(a);
    if (v) add('attr.'+a, v);
  }
  const cls = (el.className||'')+'';
  if (cls && /rank[-_ ]?(A|10|[2-9]|J|Q|K)/i.test(cls) && /suit[-_ ]?[shdc]/i.test(cls)) add('class', cls);
}
return out.slice(0, 400);
"""

# ------------ NEW: JS Shadow DOM collector ------------
JS_SHADOW_COLLECT = r"""
const out = new Set();
const tokens = [];
const seen = new WeakSet();

const addUrl = (u) => {
  if (!u) return;
  const s = String(u).trim().split(/\s+/)[0];
  if (!s) return;
  const L = s.toLowerCase();
  if (L.includes('closed') || L.includes('card-back') || L.includes('backside') || L.includes('1_card_20_20')) return;
  out.add(s);
};
const addToken = (t) => {
  if (!t) return;
  const s = String(t).trim();
  if (!s) return;
  tokens.push(s);
};

const pushFromEl = (root) => {
  // images
  root.querySelectorAll('img').forEach(img => {
    addUrl(img.getAttribute('src'));
    addUrl(img.getAttribute('data-src'));
    addUrl(img.getAttribute('data-original'));
    addUrl(img.getAttribute('data-lazy'));
    const sets = [img.getAttribute('srcset'), img.getAttribute('data-srcset')];
    for (const sv of sets) if (sv) addUrl(sv.split(',')[0]);
    addToken(img.getAttribute('alt'));
    addToken(img.getAttribute('aria-label'));
  });
  // all elements: bg-image + attrs + classes + visible text
  root.querySelectorAll('*').forEach(el => {
    const cs = getComputedStyle(el);
    const bg = cs && cs.backgroundImage || '';
    const m = bg.match(/url\((["']?)(.*?)\1\)/i);
    if (m && m[2]) addUrl(m[2]);

    for (const a of ['data-rank','data-suit','data-card','data-value','title','aria-label']) {
      const v = el.getAttribute(a); if (v) addToken(v);
    }
    const cls = (el.className||'')+'';
    if (cls) addToken(cls);
    const txt = (el.innerText||el.textContent||'').trim();
    if (txt && txt.length <= 50) addToken(txt);
  });
};

const stack = [];
stack.push(document);

while (stack.length) {
  const node = stack.pop();
  if (!node || seen.has(node)) continue;
  seen.add(node);

  // node can be a Document, ShadowRoot or Element
  const root = node; // treat generically

  try { pushFromEl(root); } catch(e) {}

  // descend into shadow roots
  if (root.querySelectorAll) {
    root.querySelectorAll('*').forEach(el => {
      if (el.shadowRoot) stack.push(el.shadowRoot);
    });
  }
}

const urls = Array.from(out).slice(0, 600);
const toks = tokens.slice(0, 600);
return { urls, toks };
"""

def js_collect_hints(driver) -> List[Tuple[str,str]]:
    try:
        data = driver.execute_script(JS_HINTS) or []
        return data
    except Exception:
        return []

def js_collect_shadow(driver) -> Tuple[List[str], List[str]]:
    try:
        data = driver.execute_script(JS_SHADOW_COLLECT) or {}
        urls = data.get("urls", []) or []
        toks = data.get("toks", []) or []
        return urls, toks
    except Exception:
        return [], []

def tokens_from_js_hints(hints: List[Tuple[str,str]]) -> List[str]:
    toks = []
    for k, v in hints:
        vv = (v or "").strip()
        if not vv: continue
        if k.startswith(("img.","css.bg")):
            toks.append(vv.split()[0])
        else:
            m = re.search(r"\b(A|K|Q|J|10|[2-9])\b.*?\b([SHDC]|SPADE|HEART|DIAMOND|CLUB)\b", vv, re.I)
            if m: toks.append(f"/{m.group(1).upper()}{m.group(2).upper()[0]}.png")
            # direct symbol like "8♦"
            m2 = re.search(r"\b(A|K|Q|J|10|[2-9])\s*([♠♥♦♣])\b", vv, re.I)
            if m2: toks.append(f"/{m2.group(1).upper()}{m2.group(2)}.png")
    # uniq & filter backs
    seen=set(); out=[]
    for t in toks:
        if any(h in t.lower() for h in CLOSED_HINTS): continue
        if t not in seen:
            seen.add(t); out.append(t)
    return out[:800]

def tokens_to_card(tokens: List[str]) -> Optional[Dict[str,Any]]:
    for t in tokens:
        p = parse_from_any(t)
        if p: return p
    return None

# ------------ CDP network (optional; may be empty if video/canvas only) ------------
IMAGE_EXT_RE = re.compile(r"\.(?:png|jpg|jpeg|webp|gif|webm|svg)(?:\?|#|$)", re.I)

def make_driver():
    opts = Options()
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1600,900")
    opts.add_argument("--log-level=3")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    try: driver.execute_cdp_cmd("Network.enable", {})
    except Exception: pass
    return driver

def W(driver, cond, timeout=15):
    return WebDriverWait(driver, timeout).until(cond)

def safe_click(driver, el):
    try: ActionChains(driver).move_to_element(el).pause(0.05).click().perform()
    except Exception: driver.execute_script("arguments[0].click();", el)

def collect_network_images(driver, seen_ids: set, store: List[str]) -> List[str]:
    out_new = []
    try:
        logs = driver.get_log("performance")
    except Exception:
        logs = []
    lines_dump = []
    for entry in logs:
        try:
            msg = json.loads(entry.get("message", "{}")).get("message", {})
            method = msg.get("method", "")
            params = msg.get("params", {})
            req_id = params.get("requestId") or params.get("loaderId")
            if not req_id or req_id in seen_ids:
                continue
            if method in ("Network.requestWillBeSent","Network.responseReceived","Network.loadingFinished"):
                url = ""
                if "request" in params and params["request"]:
                    url = params["request"].get("url","")
                if not url and "response" in params and params["response"]:
                    url = params["response"].get("url","")
                if url and IMAGE_EXT_RE.search(url):
                    lower = url.lower()
                    if not any(h in lower for h in CLOSED_HINTS):
                        out_new.append(url)
                        store.append(url)
                        seen_ids.add(req_id)
                        lines_dump.append(url)
        except Exception:
            continue
    if lines_dump:
        dump_text("network_images.txt", lines_dump[-200:])
    return out_new

# ------------ Navigation ------------
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
        print("↪️ Switched to game window")
    time.sleep(2.0)
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    if iframes: driver.switch_to.frame(iframes[0])
    dump_debug(driver, "after_enter_game")

# ------------ Round search (DOM → Shadow → Network → Text) ------------
def find_round_id_text(driver) -> Optional[str]:
    for sel in [".round-id", ".casino-round-id", "span.roundId", "div.round-id"]:
        try:
            t = driver.find_element(By.CSS_SELECTOR, sel).text.strip()
            if t: return t
        except NoSuchElementException: pass
    return None

def try_parse_here(driver) -> Tuple[Optional[Dict[str,Any]], int, str, List[str]]:
    html = driver.page_source
    urls = extract_card_sources_from_html(html)
    seen = urls[:]
    for u in urls:
        p = parse_from_any(u)
        if p: return p, len(urls), f"via=dom-url:{os.path.basename(u)[:24]}", seen

    hints = js_collect_hints(driver)
    toks = tokens_from_js_hints(hints)
    seen.extend([f"DOMHINT {k}:{v}" for k,v in hints[:40]])
    for t in toks:
        p = parse_from_any(t)
        if p: return p, len(urls)+len(toks), "via=dom-js", seen

    txt = parse_from_text(html)
    if txt: return txt, len(urls), "via=dom-text", seen
    return None, len(urls), "", seen

def try_parse_shadow(driver) -> Tuple[Optional[Dict[str,Any]], int, str, List[str]]:
    urls, toks = js_collect_shadow(driver)
    seen = []
    # 1) parse URLs first
    for u in urls:
        seen.append(f"SHURL {u}")
        p = parse_from_any(u)
        if p: return p, len(urls), "via=shadow-url", seen
    # 2) then tokens (text/classes/attrs)
    #   convert tokens into URL-like hints for parser
    hints = []
    for t in toks:
        hints.append(t)
        m = re.search(r"\b(A|K|Q|J|10|[2-9])\b.*?\b([SHDC]|SPADE|HEART|DIAMOND|CLUB)\b", t, re.I)
        if m: hints.append(f"/{m.group(1).upper()}{m.group(2).upper()[0]}.png")
        m2 = re.search(r"\b(A|K|Q|J|10|[2-9])\s*([♠♥♦♣])\b", t, re.I)
        if m2: hints.append(f"/{m2.group(1).upper()}{m2.group(2)}.png")
    for h in hints:
        p = parse_from_any(h)
        if p: return p, len(urls)+len(toks), "via=shadow-text", seen[:120]
    return None, len(urls)+len(toks), "", seen[:120]

def dfs_frames_for_card(driver, max_depth=5, depth=0) -> Tuple[Optional[Dict[str,Any]], int, int, str, List[str]]:
    parsed, cnt, how, seen = try_parse_here(driver)
    if parsed: return parsed, cnt, 1, how, seen
    # Shadow DOM pass at each level
    p2, c2, h2, seen2 = try_parse_shadow(driver)
    if p2: return p2, cnt+c2, 1, h2, seen + seen2
    if depth >= max_depth: return None, cnt+c2, 0, "", seen + seen2
    total_cnt, hits, note = cnt+c2, 0, ""
    all_seen = seen + seen2
    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for fr in frames:
        try:
            driver.switch_to.frame(fr)
            p, c, h, how2, seen3 = dfs_frames_for_card(driver, max_depth, depth+1)
            total_cnt += c; hits += h
            all_seen += seen3
            driver.switch_to.parent_frame()
            if p:
                note = how2
                return p, total_cnt, hits, note, all_seen
        except Exception:
            try: driver.switch_to.parent_frame()
            except Exception: pass
    return None, total_cnt, hits, note, all_seen

# ------------ Main ------------
def main():
    print(f"📊 CSV → {CSV_PATH}", flush=True)
    driver = make_driver()
    try: driver.execute_cdp_cmd("Network.enable", {})
    except Exception: pass

    ensure_csv(CSV_PATH)
    seen_req_ids: set = set()
    network_seen: List[str] = []

    try:
        # flow
        driver.set_page_load_timeout(120)
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
            if RUN_SECONDS and (time.time() - start_ts) >= RUN_SECONDS:
                print(f"⏱️ Time cap reached ({int(time.time()-start_ts)}s). Saved {saved} rounds.", flush=True)
                break

            t0 = time.time()
            parsed = None
            how = ""
            seen_tokens: List[str] = []
            imgs_count = 0
            net_count = 0

            while not parsed:
                # 1) DOM + Shadow + Iframes
                p, c, h, how_dom, seen = dfs_frames_for_card(driver, max_depth=5)
                parsed = p
                how = how_dom or how
                imgs_count = c
                seen_tokens = seen

                # 2) Network images (canvas/video cases)
                new_urls = collect_network_images(driver, seen_req_ids, network_seen)
                net_count = len(network_seen)
                if not parsed and new_urls:
                    for u in reversed(new_urls):
                        pnet = parse_from_any(u)
                        if pnet:
                            parsed = pnet; how = "via=network"; break

                # heartbeat every ~5s
                now = time.time()
                if now - last_heartbeat >= 5:
                    last_heartbeat = now
                    frames_top = 0
                    try: frames_top = len(driver.find_elements(By.TAG_NAME, "iframe"))
                    except Exception: pass
                    print(f"⏳ Waiting… imgs={imgs_count} frames(top)={frames_top} net={net_count} elapsed={int(time.time()-start_ts)}s", flush=True)
                    dump_debug(driver, f"wait_{int(time.time()-start_ts)}s")
                    dump_text(f"candidates_{int(time.time()-start_ts)}s.txt", [*map(str, seen_tokens[:200])])
                    dump_text("network_seen_tail.txt", network_seen[-200:])

                if parsed: break

                if time.time() - t0 > ROUND_TIMEOUT:
                    print("🔄 Round timeout: refreshing game view", flush=True)
                    driver.refresh(); time.sleep(5)
                    ifr = driver.find_elements(By.TAG_NAME, "iframe")
                    if ifr: driver.switch_to.frame(ifr[0])
                    t0 = time.time()

                time.sleep(0.35 + random.uniform(0.05, 0.25))

            # Save row
            rid = find_round_id_text(driver)
            rank, suit = parsed["rank"], parsed["suit_key"]
            row = {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "round_id": rid,
                "rank": rank,
                "suit_key": suit,
                "color": "red" if suit in ("H","D") else "black",
                "result": result_of(rank),
            }
            sig = f"{rid}|{rank}|{suit}"
            if sig == last_sig:
                time.sleep(POLL_SEC); continue
            last_sig = sig

            append_row(CSV_PATH, row)
            print(f"✅ Round {round_num}: {rank}{suit} → {row['result']} ({how})", flush=True)
            saved += 1

            if (MAX_ROUNDS and saved >= MAX_ROUNDS) or (RUN_SECONDS and (time.time() - start_ts) >= RUN_SECONDS):
                print(f"🏁 Done — captured {saved} rounds in {int(time.time()-start_ts)}s.", flush=True)
                break

            round_num += 1
            time.sleep(POLL_SEC)

    except KeyboardInterrupt:
        print("🛑 Stopped by user", flush=True)
    finally:
        if 'saved' in locals() and saved == 0:
            dump_debug(driver, "no_data_end")
            dump_text("NO_DATA.txt", [
                "Saved 0 rounds. Inspect:",
                "- debug/wait_*.html/png (DOM around card area)",
                "- debug/candidates_*.txt (DOM + Shadow candidates)",
                "- debug/network_images.txt / network_seen_tail.txt"
            ])
        try: driver.quit()
        except Exception: pass

if __name__ == "__main__":
    main()
