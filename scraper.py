#!/usr/bin/env python3
# Lucky 7 / Hi-Low Scraper — CI-ready (iframe join/next nudges + video-center click)
# - Kills lobby modals
# - Prefers LUCKY 7 (falls back to HI LOW)
# - Clicks join/next *inside provider iframes* (text + explicit selectors)
# - Also clicks center of video/canvas inside iframe to resume
# - Rotates tables aggressively if stuck
# - Writes clean CSV + debug HTML/PNG

import os, re, csv, time, random, json, hashlib
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
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

# ---------- ENV ----------
URL           = os.getenv("LUCKY7_URL", "https://www.nohmy99.vip/m/home")
USERNAME      = os.getenv("NOH_USER")
PASSWORD      = os.getenv("NOH_PASS")
if not USERNAME or not PASSWORD:
    raise SystemExit("Missing NOH_USER / NOH_PASS environment variables.")

CSV_PATH      = os.getenv("CSV_PATH", "lucky7_data.csv")
RUN_SECONDS   = int(os.getenv("RUN_SECONDS", "3000"))
MAX_ROUNDS    = int(os.getenv("MAX_ROUNDS", "0"))        # 0 = unlimited
ROUND_TIMEOUT = int(os.getenv("ROUND_TIMEOUT", "180")) # Increased timeout
POLL_SEC      = float(os.getenv("POLL_SEC", "2.0"))    # Increased poll time
DEBUG_DUMP    = int(os.getenv("DEBUG_DUMP", "1"))
MAX_TABLES    = int(os.getenv("MAX_TABLES", "10"))
GAME_PREF     = (os.getenv("GAME_PREF") or "LUCKY 7").strip().upper()

GAME_TAB_NAMES = [GAME_PREF, "LUCKY 7", "LUCKY7", "HI LOW", "HIGH LOW", "HI-LOW", "HIGHLOW"]
HEADERS = ["ts_utc","round_id","rank","suit_key","color","result"]

# ---------- CSV / DEBUG ----------
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

def dump_text(name: str, lines: List[str]):
    if not DEBUG_DUMP: return
    ensure_debug()
    try:
        with open(f"debug/{name}", "w", encoding="utf-8") as f:
            for l in lines: f.write(str(l)+"\n")
    except Exception: pass

def dump_debug_html(driver, tag="snapshot"):
    if not DEBUG_DUMP: return
    ensure_debug()
    try:
        with open(f"debug/{tag}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception: pass
    try:
        driver.save_screenshot(f"debug/{tag}.png")
    except Exception: pass

# ---------- CARD / RULES ----------
RANK_MAP   = {"A":1,"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"10":10,"J":11,"Q":12,"K":13}
SUIT_WORD  = {"SPADE":"S","HEART":"H","DIAMOND":"D","CLUB":"C"}
SUIT_SYMBOL= {"♠":"S","♥":"H","♦":"D","♣":"C"}
CLOSED_HINTS = ("closed","back","backside","card-back","1_card_20_20")

def color_of(suit_key: str) -> str: return "red" if suit_key in ("H","D") else "black"
def result_of(rank: int) -> str: return "below7" if rank<7 else ("seven" if rank==7 else "above7")

PAT_SIMPLE = re.compile(r"/(A|K|Q|J|10|[2-9])([SHDC])\.(?:png|jpe?g|webp|webm)\b", re.I)
PAT_DOUBLE = re.compile(r"/(A|K|Q|J|10|[2-9])(SS|HH|DD|CC)\.(?:png|jpe?g|webp|webm)\b", re.I)
PAT_WORDY  = re.compile(r"(ace|king|queen|jack|10|[2-9]).*?(spade|heart|diamond|club)s?", re.I)
PAT_CLASS  = re.compile(r"rank[-_ ]?(A|K|Q|J|10|[2-9]).*?suit[-_ ]?([shdc])", re.I)
PAT_SYM    = re.compile(r"\b(A|K|Q|J|10|[2-9])\s*([♠♥♦♣])\b", re.I)
PAT_OFWORD = re.compile(r"\b(A|K|Q|J|10|[2-9])\s*(?:OF\s+)?(SPADES?|HEARTS?|DIAMONDS?|CLUBS?)\b", re.I)
PAT_JSON_1 = re.compile(r'"rank"\s*:\s*(\d+)\s*,\s*"suit"\s*:\s*"(SPADE|HEART|DIAMOND|CLUB)"', re.I)
PAT_JSON_2 = re.compile(r'"card"\s*:\s*"(A|K|Q|J|10|[2-9])\s*([SHDC])"', re.I)
PAT_ROUNDID= re.compile(r'"roundId"\s*:\s*"?(.*?)"?(,|\})', re.I)

def card_from(rank_txt: str, suit_txt: str) -> Optional[Dict[str,Any]]:
    r = (rank_txt or "").upper()
    s = (suit_txt or "").upper()
    if r not in RANK_MAP: return None
    if s in ("S","H","D","C"): suit = s
    elif s in SUIT_WORD: suit = SUIT_WORD[s]
    elif suit_txt in SUIT_SYMBOL: suit = SUIT_SYMBOL[suit_txt]
    else: return None
    return {"rank": RANK_MAP[r], "suit_key": suit}

def parse_from_any(s: str) -> Optional[Dict[str, Any]]:
    if not s: return None
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
        rank_num = int(m.group(1)); suit_word = m.group(2).upper()
        if 1 <= rank_num <= 13:
            return {"rank": rank_num, "suit_key": SUIT_WORD.get(suit_word, suit_word[0])}
    m = PAT_JSON_2.search(html)
    if m: return card_from(m.group(1), m.group(2))
    return None

# ---------- HTML/JS collectors ----------
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
        for img in soup.select(sel): add(img.get("src"))
    for el in soup.find_all(True):
        style = (el.get("style") or "")
        m = re.search(r"background-image\s*:\s*url\(([^)]+)\)", style, re.I)
        if m: add(m.group(1).strip('\'" '))
        dr, ds = el.get("data-rank"), el.get("data-suit")
        if dr and ds: add(f"/{str(dr).upper()}{str(ds).upper()}.png")
        cls = " ".join(el.get("class") or [])
        m2 = re.search(r"rank[-_ ]?(A|10|[2-9]|J|Q|K).*?suit[-_ ]?([shdc])", cls, re.I)
        if m2: add(f"/{m2.group(1).upper()}{m2.group(2).upper()}.png")
    return urls

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

JS_SHADOW_COLLECT = r"""
const out = new Set();
const toks = [];
const seen = new WeakSet();
const addUrl = (u) => {
  if (!u) return; const s = String(u).trim().split(/\s+/)[0]; if (!s) return;
  const L = s.toLowerCase();
  if (L.includes('closed') || L.includes('card-back') || L.includes('backside') || L.includes('1_card_20_20')) return;
  out.add(s);
};
const addTok = (t) => { if (!t) return; const s=String(t).trim(); if (!s) return; toks.push(s); };

const pushFromEl = (root) => {
  root.querySelectorAll('img').forEach(img => {
    addUrl(img.getAttribute('src'));
    addUrl(img.getAttribute('data-src'));
    addUrl(img.getAttribute('data-original'));
    addUrl(img.getAttribute('data-lazy'));
    const sets=[img.getAttribute('srcset'),img.getAttribute('data-srcset')];
    for (const sv of sets) if (sv) addUrl(sv.split(',')[0]);
    addTok(img.getAttribute('alt'));
    addTok(img.getAttribute('aria-label'));
  });
  root.querySelectorAll('*').forEach(el => {
    const cs = getComputedStyle(el);
    const bg = cs && cs.backgroundImage || '';
    const m = bg.match(/url\((["']?)(.*?)\1\)/i);
    if (m && m[2]) addUrl(m[2]);
    for (const a of ['data-rank','data-suit','data-card','data-value','title','aria-label']) {
      const v = el.getAttribute(a); if (v) addTok(v);
    }
    const cls = (el.className||'')+''; if (cls) addTok(cls);
    const txt = (el.innerText||el.textContent||'').trim();
    if (txt && txt.length <= 80) addTok(txt);
  });
};

const stack=[document];
while (stack.length){
  const node=stack.pop();
  if (!node || seen.has(node)) continue;
  seen.add(node);
  try { pushFromEl(node); } catch(e){}
  if (node.querySelectorAll){
    node.querySelectorAll('*').forEach(el => { if (el.shadowRoot) stack.push(el.shadowRoot); });
  }
}
return { urls: Array.from(out).slice(0,800), toks: toks.slice(0,800) };
"""

def tokens_from_js_hints(hints):
    toks = []
    for k, v in hints:
        vv = (v or "").strip()
        if not vv: continue
        if k.startswith(("img.","css.bg")):
            toks.append(vv.split()[0])
        else:
            m = re.search(r"\b(A|K|Q|J|10|[2-9])\b.*?\b([SHDC]|SPADE|HEART|DIAMOND|CLUB)\b", vv, re.I)
            if m: toks.append("/%s%s.png" % (m.group(1).upper(), m.group(2).upper()[0]))
            m2 = re.search(r"\b(A|K|Q|J|10|[2-9])\s*([♠♥♦♣])\b", vv, re.I)
            if m2: toks.append("/%s%s.png" % (m2.group(1).upper(), m2.group(2)))
    seen=set(); out=[]
    for t in toks:
        if any(h in t.lower() for h in CLOSED_HINTS): continue
        if t not in seen:
            seen.add(t); out.append(t)
    return out[:800]

# ---------- Driver / Network ----------
IMAGE_EXT_RE = re.compile(r"\.(?:png|jpe?g|webp|gif|webm|svg)(?:\?|#|$)", re.I)

def make_driver():
    opts = Options()
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1600,900")
    opts.add_argument("--log-level=3")
    opts.set_capability("goog:loggingPrefs", {"performance":"ALL"})
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    try:
        driver.execute_cdp_cmd("Network.enable", {"maxTotalBufferSize": 10_000_000, "maxResourceBufferSize": 5_000_000})
    except Exception:
        pass
    return driver

def W(driver, cond, timeout=15): return WebDriverWait(driver, timeout).until(cond)

def safe_click(driver, el):
    try:
        ActionChains(driver).move_to_element(el).pause(0.05).click().perform()
    except Exception:
        driver.execute_script("arguments[0].click();", el)

def collect_network_images(driver, seen_req_ids: set, store: List[str]) -> List[str]:
    out_new = []; lines_dump=[]
    try: logs = driver.get_log("performance")
    except Exception: logs = []
    for entry in logs:
        try:
            msg = json.loads(entry.get("message", "{}")).get("message", {})
            method = msg.get("method", ""); params = msg.get("params", {})
            req_id = params.get("requestId") or params.get("loaderId")
            if not req_id or req_id in seen_req_ids: continue
            if method in ("Network.requestWillBeSent","Network.responseReceived","Network.loadingFinished"):
                url = ""
                if "request" in params and params["request"]:
                    url = params["request"].get("url","")
                if not url and "response" in params and params["response"]:
                    url = params["response"].get("url","")
                if url and IMAGE_EXT_RE.search(url):
                    lower = url.lower()
                    if not any(h in lower for h in CLOSED_HINTS):
                        out_new.append(url); store.append(url); seen_req_ids.add(req_id); lines_dump.append(url)
        except Exception:
            continue
    if lines_dump: dump_text("network_images.txt", lines_dump[-200:])
    return out_new

def collect_network_json_card(driver, seen_json_ids: set) -> Tuple[Optional[Dict[str,Any]], Optional[str]]:
    try: logs = driver.get_log("performance")
    except Exception: logs = []
    card, round_id = None, None
    for entry in logs:
        try:
            msg = json.loads(entry.get("message","{}")).get("message", {})
            if msg.get("method") != "Network.responseReceived": continue
            params = msg.get("params", {}); resp = params.get("response", {}) or {}
            mime = (resp.get("mimeType") or "").lower(); req_id = params.get("requestId")
            if not req_id or req_id in seen_json_ids: continue
            if ("json" not in mime and "javascript" not in mime and "text/plain" not in mime): continue
            try:
                body_obj = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": req_id})
                body = body_obj.get("body","")
                if not body: seen_json_ids.add(req_id); continue
                m = PAT_JSON_1.search(body)
                if m:
                    rank_num = int(m.group(1)); suit = SUIT_WORD.get(m.group(2).upper(), m.group(2)[0].upper())
                    if 1 <= rank_num <= 13: card = {"rank": rank_num, "suit_key": suit}
                if not card:
                    m2 = PAT_JSON_2.search(body)
                    if m2: card = card_from(m2.group(1), m2.group(2))
                mR = PAT_ROUNDID.search(body)
                if mR: round_id = (mR.group(1) or "").strip()
                if not card:
                    m3 = re.search(r'"(?:open|result|winning|win)Card"\s*:\s*"([AKQJ]|10|[2-9])\s*([SHDC])"', body, re.I)
                    if m3: card = card_from(m3.group(1), m3.group(2))
                seen_json_ids.add(req_id)
                if card: return card, round_id
            except WebDriverException:
                seen_json_ids.add(req_id); continue
        except Exception:
            continue
    return None, None

# ---------- Navigation / Modals ----------
def login_same_site(driver):
    driver.get(URL); time.sleep(5)
    # open login if present
    try:
        login_link = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.auth-link.m-r-5")))
        if login_link.text.strip().lower() == "login":
            safe_click(driver, login_link);
    except TimeoutException:
        print("Login link not found, assuming already logged in.", flush=True)

    time.sleep(5)
    try:
        user_input = driver.find_element(By.XPATH, "//input[@name='User Name' or @name='username' or contains(@placeholder,'User')]")
        pass_input = driver.find_element(By.XPATH, "//input[@name='Password' or @name='password' or @type='password']")
        user_input.clear(); user_input.send_keys(USERNAME)
        pass_input.clear(); pass_input.send_keys(PASSWORD)
        pass_input.submit()
        print("✅ Logged in", flush=True)
    except NoSuchElementException:
        print("⚠️ Login inputs not found; maybe already logged in", flush=True)
    time.sleep(10)
    dump_debug_html(driver, "after_login")

CLOSE_WORDS = [
    "CLOSE","OK","GOT IT","DISMISS","ACCEPT","I AGREE","START","ENTER","WATCH NOW","CONTINUE",
    "X","×","SKIP","LATER","NOT NOW","CANCEL","NO THANKS","REMIND ME LATER"
]
def close_top_modal(driver, attempts=5) -> bool:
    did_any = False
    for _ in range(attempts):
        try: driver.switch_to.default_content()
        except Exception: pass
        try:
            clicked = driver.execute_script("""
                const WORDS = arguments[0].map(s=>String(s||'').toUpperCase());
                const U = s => (s||'').toUpperCase();
                const hideNode = (el) => {
                  if (!el) return false;
                  el.style.setProperty('display','none','important');
                  el.style.setProperty('visibility','hidden','important');
                  el.style.setProperty('pointer-events','none','important');
                  el.setAttribute('hidden','true');
                  if (el.parentElement) { try { el.parentElement.removeChild(el); } catch(e) {} }
                  return true;
                };
                for (const node of [document.documentElement, document.body]) {
                  if (!node) continue;
                  node.classList.remove('modal-open','overflow-hidden','no-scroll');
                  node.style.removeProperty('overflow');
                }
                const sels = [
                  '.modal.show', '.modal', '.modal-backdrop',
                  '.swal2-container', '.swal2-popup',
                  '.overlay', '.popup', '.modal-market',
                  '.force-change-password-popup', '.bookModal', '.app_version'
                ];
                let acted = false;
                const poster = document.querySelector('img[src*="poster-login-popup"]');
                if (poster) {
                  let p = poster; for (let i=0;i<4 && p;i++) p = p.parentElement;
                  if (p) { hideNode(p); acted = true; }
                }
                const containers = [];
                for (const sel of sels) document.querySelectorAll(sel).forEach(el => { if (el && !containers.includes(el)) containers.push(el); });
                for (const modal of containers) {
                  if (!modal || !modal.offsetParent) continue;
                  const btns = modal.querySelectorAll('button,a,div[role="button"],span,[aria-label]');
                  for (const b of btns) {
                    if (!b || !b.offsetParent) continue;
                    const t=U(b.innerText||b.textContent||b.getAttribute('aria-label')||'');
                    if (t && WORDS.some(w => t.includes(w))) { try { b.click(); acted = true; } catch(e){} }
                  }
                }
                if (!acted) for (const modal of containers) { try { if (hideNode(modal)) acted = true; } catch(e){} }
                document.querySelectorAll('.modal-backdrop, .cdk-overlay-backdrop').forEach(bd => hideNode(bd));
                return !!acted;
            """, CLOSE_WORDS)
            if clicked:
                did_any = True
                time.sleep(2)
        except Exception:
            pass
        try:
            has_modal = driver.execute_script("""
                const sels = ['.modal.show','.modal','.modal-backdrop','.swal2-container','.overlay','.popup','.modal-market','.force-change-password-popup','.bookModal','.app_version'];
                return sels.some(s => document.querySelector(s));
            """)
            if not has_modal: break
        except Exception:
            break
    if did_any: print("🟢 Closed/removed top-page modal", flush=True)
    return did_any

def click_nav_casino(driver, timeout=45):
    def click_it(el):
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(2)
        try: el.click()
        except Exception: driver.execute_script("arguments[0].click();", el)
    end = time.time() + timeout
    while time.time() < end:
        for tg in driver.find_elements(By.XPATH,
            "//button[contains(@class,'navbar-toggler') or contains(@class,'hamburger') or contains(@class,'menu') or @aria-label='Toggle navigation']")[:2]:
            if tg.is_displayed():
                try: click_it(tg); time.sleep(2)
                except Exception: pass
        xps = [
            "//a[contains(translate(., 'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CASINO')]",
            "//button[contains(translate(., 'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CASINO')]",
            "//div[contains(translate(., 'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'CASINO')]",
            "//a[contains(@href, '/casino') or contains(@href,'casino')]",
        ]
        for xp in xps:
            els = [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]
            if els: click_it(els[0]); time.sleep(5); return
        # fallback via JS
        try:
            clicked = bool(driver.execute_script("""
                const U=s=> (s||'').toUpperCase();
                const els=[...document.querySelectorAll('a,button,div,span')];
                const el = els.find(e => U(e.innerText||e.textContent).includes('CASINO'));
                if (el) { el.scrollIntoView({block:'center'}); el.click(); return true; }
                return false;
            """))
        except Exception: clicked = False
        if clicked: time.sleep(5); return
        time.sleep(2)
    raise TimeoutException("Casino link not found")

def click_game_subtab(driver, timeout=45) -> bool:
    targets = [t for t in GAME_TAB_NAMES if t]
    def click_it(el):
        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", el)
        time.sleep(2)
        try: el.click()
        except Exception: driver.execute_script("arguments[0].click();", el)
    end = time.time() + timeout
    while time.time() < end:
        for token in targets:
            xp = ("//*[self::a or self::button or self::div or self::span or self::li]"
                  "[contains(translate(normalize-space(.),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'%s')]" % token)
            els = [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]
            if els:
                click_it(els[0]); time.sleep(5)
                print(f"🎯 Game tab: {token}", flush=True)
                return True
        for cont in driver.find_elements(By.XPATH, "//*[contains(@class,'tabs') or contains(@class,'nav') or contains(@class,'tab')]")[:3]:
            try: driver.execute_script("if(arguments[0].scrollWidth>arguments[0].clientWidth){arguments[0].scrollLeft += 240;}", cont)
            except Exception: pass
        time.sleep(2)
    return False

def reattach_game_iframe(driver):
    try: driver.switch_to.default_content()
    except Exception: pass
    time.sleep(2)
    frames = driver.find_elements(By.TAG_NAME, "iframe")
    if frames:
        for fr in frames[:5]:
            try:
                if fr.is_displayed():
                    driver.switch_to.frame(fr); return
            except Exception: pass
        driver.switch_to.frame(frames[0])

def click_first_game_in_active_pane(driver, idx: int = 0):
    try:
        pane = WebDriverWait(driver, 15).until(EC.visibility_of_element_located((By.XPATH, "//*[contains(@class,'tab-pane') and contains(@class,'active')]")))
    except TimeoutException:
        pane = driver
    tiles = pane.find_elements(By.XPATH, ".//*[contains(@class,'casino-name') or contains(@class,'casinoicon') or contains(@class,'casino-') or self::a]")
    if not tiles: raise RuntimeError("No game tiles found in selected pane")
    idx = max(0, min(idx, len(tiles)-1))
    target = tiles[idx]
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
    time.sleep(2); safe_click(driver, target)
    time.sleep(5)
    if len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[-1])
        print("↪️ Switched to game window")
    time.sleep(5); reattach_game_iframe(driver); dump_debug_html(driver, f"after_enter_game_{idx}")

# ---------- Round parsing ----------
def try_parse_here(driver):
    html = driver.page_source
    urls = extract_card_sources_from_html(html)
    for u in urls:
        p = parse_from_any(u)
        if p: return p, "via=dom-url"
    hints = js_collect_hints(driver)
    toks = tokens_from_js_hints(hints)
    for t in toks:
        p = parse_from_any(t)
        if p: return p, "via=dom-js"
    txt = parse_from_text(html)
    if txt: return txt, "via=dom-text"
    return None, ""

def try_parse_shadow(driver):
    urls, toks = js_collect_shadow(driver)
    for u in urls:
        p = parse_from_any(u)
        if p: return p, "via=shadow-url"
    hints = []
    for t in toks:
        hints.append(t)
        m = re.search(r"\b(A|K|Q|J|10|[2-9])\b.*?\b([SHDC]|SPADE|HEART|DIAMOND|CLUB)\b", t, re.I)
        if m: hints.append("/%s%s.png" % (m.group(1).upper(), m.group(2).upper()[0]))
        m2 = re.search(r"\b(A|K|Q|J|10|[2-9])\s*([♠♥♦♣])\b", t, re.I)
        if m2: hints.append("/%s%s.png" % (m2.group(1).upper(), m2.group(2)))
    for h in hints:
        p = parse_from_any(h)
        if p: return p, "via=shadow-text"
    return None, ""

def dfs_frames_for_card(driver, max_depth=5, depth=0):
    p, how = try_parse_here(driver)
    if p: return p, how
    p2, how2 = try_parse_shadow(driver)
    if p2: return p2, how2
    if depth >= max_depth: return None, ""
    for fr in driver.find_elements(By.TAG_NAME, "iframe"):
        try:
            driver.switch_to.frame(fr)
            p3, how3 = dfs_frames_for_card(driver, max_depth, depth+1)
            driver.switch_to.parent_frame()
            if p3: return p3, how3
        except Exception:
            try: driver.switch_to.parent_frame()
            except Exception: pass
    return None, ""

# ---------- Extra iframe nudges ----------
EXCLUDE_TOKENS = ("RULE","RULES","HELP","FAQ","TERMS","DISCLAIMER","PRIVACY")
JOIN_WORDS = ["JOIN","PLAY","ENTER","WATCH","START","LIVE","OPEN","SEAT","TAKE SEAT","SIT","CONTINUE",
              "PLAY NOW","WATCH LIVE","ENTER TABLE","JOIN TABLE",
              "जॉइन","खेलें","प्रवेश","देखें","लाइव","खोलें",
              "加入","进入","开始","观看","直播","进入游戏","加入游戏",
              "参加","開始","入場","観戦","ライブ",
              "입장","시작","참여","관전","라이브"]
NEXT_WORDS = ["NEXT","CONTINUE","OK","CLOSE","DISMISS","SKIP","RESULT","REVEAL","SHOW","START","GO",
              "»","→","▶","⏭","❯","➔","►","आगे","ठीक","बंद","जारी","LIVE"]

# common button selectors used by many providers
NEXT_SELECTORS = [
    "button.next","button.deal","button.start","button.ok","button.continue","button.play",
    ".next-btn",".btn-next",".btn--next",".btn.ok",".btn.start",".btn.continue",".btn.play",
    "[data-action='next']","[data-action='continue']","[data-action='start']",
    ".reveal",".result",".show",".start-btn",".continue-btn",".play-btn",
]
VIDEO_SELECTORS = [
    "video",".video-container",".casino-video",".casino-video-cards",".game-video","canvas",".table-video",".main-video"
]

def click_selectors_in_frame(driver, selectors) -> bool:
    try:
        return bool(driver.execute_script("""
            const sels = arguments[0];
            const pick = [];
            for (const s of sels) {
              const nodes = document.querySelectorAll(s);
              for (const el of nodes) {
                if (!el || !el.offsetParent) continue;
                pick.push(el);
              }
            }
            if (pick.length) {
              const el = pick[0];
              el.scrollIntoView({block:'center'});
              el.click();
              return true;
            }
            return false;
        """, selectors))
    except Exception:
        return False

def click_center_of_video(driver) -> bool:
    # click the visual surface; many providers require tapping the video/canvas to resume
    try:
        return bool(driver.execute_script("""
            const sels = arguments[0];
            for (const s of sels) {
              const el = document.querySelector(s);
              if (el && el.offsetParent) {
                const r = el.getBoundingClientRect();
                const x = r.left + r.width/2, y = r.top + r.height/2;
                const evt = new MouseEvent('click', {view: window, bubbles: true, cancelable: true, clientX: x, clientY: y});
                el.dispatchEvent(evt);
                return true;
              }
            }
            return false;
        """, VIDEO_SELECTORS))
    except Exception:
        return False

def click_tokens_in_this_frame(driver, words) -> bool:
    try:
        clicked = driver.execute_script("""
            const WORDS = arguments[0].map(s=>String(s||'').toUpperCase());
            const EXC = arguments[1];
            const U=s=>(s||'').toUpperCase();
            const els=[...document.querySelectorAll('button,a,div,span,[role="button"],[aria-label]')];
            const el = els.find(e => {
              if (!e || !e.offsetParent) return false;
              const nav = e.closest('nav, header, footer, .nav, .navbar, .menu, .header, .footer, .rules, .help');
              if (nav) return false;
              const t=U(e.innerText||e.textContent||e.getAttribute('aria-label')||'');
              if (EXC.some(x=>t.includes(x))) return false;
              return WORDS.some(k=>t.includes(k));
            });
            if (el) { el.scrollIntoView({block:'center'}); el.click(); return true; }
            return false;
        """, [w.upper() for w in words], list(EXCLUDE_TOKENS))
        return bool(clicked)
    except Exception:
        return False

def deep_join_nudge(driver) -> bool:
    did = False
    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for fr in frames[:5]:
        try:
            driver.switch_to.frame(fr)
            if click_tokens_in_this_frame(driver, JOIN_WORDS) or click_selectors_in_frame(driver, NEXT_SELECTORS) or click_center_of_video(driver):
                print("🟢 Clicked (iframe) join/play/watch/center", flush=True)
                did = True
            driver.switch_to.parent_frame()
            if did: break
        except Exception:
            try: driver.switch_to.parent_frame()
            except Exception: pass
    return did

def poke_next_like(driver) -> bool:
    did = False
    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for fr in frames[:5]:
        try:
            driver.switch_to.frame(fr)
            if (click_tokens_in_this_frame(driver, NEXT_WORDS)
                or click_selectors_in_frame(driver, NEXT_SELECTORS)
                or click_center_of_video(driver)):
                print("🟢 Clicked (iframe) next/ok/center", flush=True)
                did = True
            driver.switch_to.parent_frame()
            if did: break
        except Exception:
            try: driver.switch_to.parent_frame()
            except Exception: pass
    return did

def reopen_table(driver, next_index: int = 0):
    print("🔃 Re-opening table…", flush=True)
    try: driver.switch_to.default_content()
    except Exception: pass
    try:
        if len(driver.window_handles) > 1:
            base = driver.window_handles[0]
            for h in driver.window_handles[1:]:
                try: driver.switch_to.window(h); driver.close()
                except Exception: pass
            driver.switch_to.window(base)
    except Exception: pass

    try:
        driver.get(URL); time.sleep(10)
        close_top_modal(driver, attempts=2)
        click_nav_casino(driver)
        close_top_modal(driver, attempts=2)
        if not click_game_subtab(driver): raise RuntimeError("Game subtab not found")
        close_top_modal(driver, attempts=1)
        tried = 0
        for i in range(next_index, next_index + MAX_TABLES):
            idx = i % max(MAX_TABLES, 1)
            try:
                click_first_game_in_active_pane(driver, idx=idx)
                print(f"🟢 Opened table #{idx+1}", flush=True)
                break
            except Exception:
                tried += 1
                continue
        if tried and tried >= MAX_TABLES:
            print("⚠️ Could not open any table tile.", flush=True)
    except Exception as e:
        print(f"⚠️ Re-open navigation error: {e}", flush=True)
    try:
        reattach_game_iframe(driver)
        # immediate iframe nudges
        deep_join_nudge(driver)
        poke_next_like(driver)
        time.sleep(10)
    except Exception:
        pass

def shadow_signature_hex(driver) -> str:
    try:
        urls, toks = js_collect_shadow(driver)
        toks = [t for t in toks if len(t) <= 80]
        compact = (urls[-120:] + toks[-360:])
        key = "|".join(compact)
        return hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()
    except Exception:
        return "0"*40

# ---------- Main ----------
def main():
    print(f"📊 CSV → {CSV_PATH}", flush=True)
    ensure_csv(CSV_PATH)

    driver = make_driver()
    seen_img_ids: set = set()
    seen_json_ids: set = set()
    network_seen: List[str] = []

    try:
        driver.set_page_load_timeout(120)

        # LOGIN + kill lobby popups
        login_same_site(driver)
        close_top_modal(driver, attempts=5)

        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        click_nav_casino(driver)
        close_top_modal(driver, attempts=3)

        if not click_game_subtab(driver):
            raise TimeoutException("Could not find required game subtab")
        close_top_modal(driver, attempts=2)

        table_idx = 0
        click_first_game_in_active_pane(driver, idx=table_idx)

        # immediate iframe nudges before first parse
        reattach_game_iframe(driver)
        deep_join_nudge(driver)
        poke_next_like(driver)
        time.sleep(10)

        print("✅ Entered Lucky 7 / Hi-Low game", flush=True)

        start_ts = time.time()
        last_sig = None
        saved = 0
        round_num = 1
        stuck_cycles = 0

        while True:
            if RUN_SECONDS and (time.time() - start_ts) >= RUN_SECONDS:
                print(f"⏱️ Time cap reached. Saved {saved} rounds.", flush=True); break

            # ---- parse current round ----
            t0 = time.time(); parsed=None; how=""; rid=None
            while not parsed:
                p, how_dom = dfs_frames_for_card(driver, max_depth=5)
                if p: parsed, how = p, how_dom
                if not parsed:
                    pj, pr = collect_network_json_card(driver, seen_json_ids)
                    if pj: parsed, how = pj, "via=json"
                    if pr: rid = pr
                collect_network_images(driver, seen_img_ids, network_seen)
                if not parsed and (time.time() - t0) > ROUND_TIMEOUT:
                    print("🔄 Round timeout: refresh + join + next", flush=True)
                    driver.refresh(); time.sleep(10)
                    reattach_game_iframe(driver)
                    deep_join_nudge(driver)
                    poke_next_like(driver)
                    t0 = time.time()
                time.sleep(5)

            # ---- save (skip duplicate) ----
            rank, suit = parsed["rank"], parsed["suit_key"]
            if not rid:
                try:
                    rtxt = driver.find_element(By.CSS_SELECTOR, ".round-id, .casino-round-id, span.roundId, div.round-id").text.strip()
                    rid = rtxt or None
                except Exception: rid = None
            sig = f"{rid}|{rank}|{suit}"
            if sig != last_sig:
                row = {
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                    "round_id": rid, "rank": rank, "suit_key": suit,
                    "color": color_of(suit), "result": result_of(rank),
                }
                append_row(CSV_PATH, row)
                print(f"✅ Round {round_num}: {rank}{suit} → {row['result']} ({how})", flush=True)
                dump_debug_html(driver, f"after_save_{round_num}")
                last_sig = sig; round_num += 1; saved += 1; stuck_cycles = 0
            else:
                print("ℹ️ Same card snapshot; not appending duplicate.", flush=True)

            # ---- next-round gate ----
            prev_net = len(network_seen); prev_shadow = shadow_signature_hex(driver)
            gate_start = time.time(); last_log = 0; joined=False
            while True:
                pj, pr = collect_network_json_card(driver, seen_json_ids)
                if pj: parsed = pj; rid = pr or rid; break
                new_imgs = collect_network_images(driver, seen_img_ids, network_seen)
                sig_now = shadow_signature_hex(driver)
                if len(network_seen) > prev_net or new_imgs or sig_now != prev_shadow:
                    break

                waited = int(time.time() - gate_start)

                # periodic nudges inside iframe
                if waited in (4, 8, 12, 16):
                    poke_next_like(driver)
                    reattach_game_iframe(driver)

                if waited >= 8 and not joined:
                    try: driver.switch_to.default_content()
                    except Exception: pass
                    if deep_join_nudge(driver):
                        joined = True
                    reattach_game_iframe(driver)

                if waited >= 20:
                    print("🔁 No change — refreshing table", flush=True)
                    driver.refresh(); time.sleep(10)
                    reattach_game_iframe(driver)
                    deep_join_nudge(driver)
                    poke_next_like(driver)
                    prev_net = len(network_seen); prev_shadow = shadow_signature_hex(driver)
                    gate_start = time.time(); joined=False; stuck_cycles += 1
                    # rotate earlier (after first failed refresh cycle)
                    if stuck_cycles >= 1:
                        table_idx = (table_idx + 1) % max(MAX_TABLES, 1)
                        reopen_table(driver, next_index=table_idx)
                        stuck_cycles = 0
                        prev_net = len(network_seen); prev_shadow = shadow_signature_hex(driver)
                        gate_start = time.time(); joined=False

                if time.time() - last_log >= 5:
                    last_log = time.time()
                    print(f"🟡 Next-round gate… net={len(network_seen)} (prev {prev_net}) shadow={'same' if sig_now==prev_shadow else 'changed'} waited={waited}s", flush=True)

                if RUN_SECONDS and (time.time()-start_ts) >= RUN_SECONDS: break
                time.sleep(5)

            if (MAX_ROUNDS and saved >= MAX_ROUNDS) or (RUN_SECONDS and (time.time()-start_ts) >= RUN_SECONDS):
                print(f"🏁 Done — captured {saved} rounds.", flush=True); break
            time.sleep(5)

    except KeyboardInterrupt:
        print("🛑 Stopped by user", flush=True)
    finally:
        try: driver.quit()
        except Exception: pass

if __name__ == "__main__":
    main()
