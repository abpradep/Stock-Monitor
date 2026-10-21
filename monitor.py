#!/usr/bin/env python3
"""
Stock monitor -> Telegram alerts.

  python monitor.py scan     check news, NSE/BSE filings and price/volume, alert on new
                             material events
  python monitor.py report   scan, then send the 3-hourly report (7 AM to 7 PM IST). Claude
                             is only called when something crucial happened since the last report
  python monitor.py summary  send the plain daily summary (no Claude needed)
  python monitor.py check    check price and filing sources, save status, send nothing
  python monitor.py test     send a test message to confirm Telegram works

Configuration comes from environment variables (GitHub Secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   required
  ANTHROPIC_API_KEY                      optional, enables Bullish/Bearish analysis and
                                         the detailed report (without it, report sends the
                                         plain summary instead)
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
from zoneinfo import ZoneInfo

import anthropic
import feedparser
import requests
import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")          # per-headline alerts
REPORT_MODEL = os.environ.get("REPORT_MODEL", "claude-haiku-5-5")        # 3-hourly report

PRICE_MOVE_PCT = float(os.environ.get("PRICE_MOVE_PCT", "5"))   # alert on moves >= 5%
VOLUME_SPIKE_X = float(os.environ.get("VOLUME_SPIKE_X", "2.5")) # volume >= 2.5x 20-day avg
MIN_IMPORTANCE = int(os.environ.get("MIN_IMPORTANCE", "6"))     # only alert news rated >= 6/10 (Claude mode)
MIN_IMPORTANCE_RULES = int(os.environ.get("MIN_IMPORTANCE_RULES", "5"))  # same, free rule-based mode

# NSE symbol -> (name used for the news search, words that must appear in the headline).
# A headline is only considered if it mentions the company by one of these names.
WATCHLIST = {
    "LAURUSLABS": ("Laurus Labs", ["Laurus"]),
    "SOLARA": ("Solara Active Pharma", ["Solara"]),
    "NORTHARC": ("Northern Arc Capital", ["Northern Arc"]),
    "SHIVALIK": ("Shivalik Rasayan", ["Shivalik Rasayan"]),
    "SYNGENE": ("Syngene International", ["Syngene"]),
    "ALLETEC": ("All E Technologies", ["All E Technologies", "All e Tech"]),
    "INFOBEAN": ("InfoBeans Technologies", ["InfoBeans"]),
    "JAYBEE": ("Jay Bee Laminations", ["Jay Bee"]),
    "PRAJIND": ("Praj Industries", ["Praj"]),
    "BAJAJHCARE": ("Bajaj Healthcare", ["Bajaj Healthcare"]),
    "ALIVUS": ("Alivus Life Sciences", ["Alivus"]),
    "PDSL": ("PDS Limited", ["PDS Ltd", "PDS Limited", "PDS share", "PDS Q"]),
    "GREAVESCOT": ("Greaves Cotton", ["Greaves"]),
    "RAYMOND": ("Raymond Ltd", ["Raymond"]),
    "RBA": ("Restaurant Brands Asia", ["Restaurant Brands Asia", "Burger King India"]),
    "ARMANFIN": ("Arman Financial", ["Arman Financial"]),
    "PENIND": ("Pennar Industries", ["Pennar"]),
    "JMFINANCIL": ("JM Financial", ["JM Financial"]),
    "BIRLANU": ("BirlaNu", ["BirlaNu", "Birla Nu"]),
    "FDC": ("FDC Limited", ["FDC Ltd", "FDC Limited", "FDC share", "FDC Q"]),
    "JUBLPHARMA": ("Jubilant Pharmova", ["Jubilant Pharmova"]),
    "VOEPL": ("Virtuoso Optoelectronics", ["Virtuoso Optoelectronics", "Virtuoso Opto", "VOEPL"]),
    "PPLPHARMA": ("Piramal Pharma", ["Piramal Pharma"]),
    "UNIPARTS": ("Uniparts India", ["Uniparts"]),
    "CYIENT": ("Cyient", ["Cyient"]),
    "DENTA": ("Denta Water and Infra Solutions", ["Denta Water"]),
    "ENTERO": ("Entero Healthcare Solutions", ["Entero Healthcare", "Entero"]),
    "CARBORUNIV": ("Carborundum Universal", ["Carborundum", "CUMI"]),
    "NATCOPHARM": ("Natco Pharma", ["Natco"]),
    "HINDWAREAP": ("Hindware Home Innovation", ["Hindware Home"]),
    "KARBON": ("Karbonsteel Engineering", ["Karbonsteel"]),
    "AARTIPHARM": ("Aarti Pharmalabs", ["Aarti Pharmalabs"]),
    "KPITTECH": ("KPIT Technologies", ["KPIT"]),
    "CONCORDBIO": ("Concord Biotech", ["Concord Biotech"]),
    "SONACOMS": ("Sona BLW Precision Forgings", ["Sona BLW", "Sona Comstar"]),
    "EUREKAFORB": ("Eureka Forbes", ["Eureka Forbes"]),
}

# Stocks not on NSE: Yahoo Finance tickers to try, in order, instead of SYMBOL.NS.
YAHOO_TICKERS = {
    "KARBON": ["KARBON.BO", "544511.BO"],  # Karbonsteel Engineering, BSE SME only
}


def download_prices(period):
    """symbol -> daily price DataFrame from Yahoo Finance. Stocks with several
    candidate tickers fall back to the next one when the first has no data."""
    first = {s: YAHOO_TICKERS.get(s, [s + ".NS"])[0] for s in WATCHLIST}
    data = yf.download(list(first.values()), period=period, interval="1d", group_by="ticker",
                       auto_adjust=False, progress=False, threads=True)
    frames = {}
    for sym, ticker in first.items():
        try:
            df = data[ticker].dropna(subset=["Close"])
        except KeyError:
            df = None
        for alt in YAHOO_TICKERS.get(sym, [])[1:]:
            if df is not None and len(df):
                break
            try:
                df = yf.download([alt], period=period, interval="1d", group_by="ticker",
                                 auto_adjust=False, progress=False)[alt].dropna(subset=["Close"])
            except Exception:
                df = None
        if df is not None and len(df):
            frames[sym] = df
    return frames


# Other listed companies whose names contain a watched company's name. These
# phrases are ignored when checking whether a headline mentions the company.
NOT_THIS_COMPANY = {
    "CYIENT": re.compile(r"Cyient DLM", re.I),
}

# BSE scrip codes, used to fetch BSE filings. None = not listed on BSE (NSE SME
# stocks). A stock missing here is looked up on BSE's site automatically.
BSE_CODES = {
    "LAURUSLABS": "540222", "SOLARA": "541540", "NORTHARC": "544260", "SHIVALIK": "539148",
    "SYNGENE": "539268", "ALLETEC": None, "INFOBEAN": "543644", "JAYBEE": None,
    "PRAJIND": "522205", "BAJAJHCARE": "539872", "ALIVUS": "543322", "PDSL": "538730",
    "GREAVESCOT": "501455", "RAYMOND": "500330", "RBA": "543248", "ARMANFIN": "531179",
    "PENIND": "513228", "JMFINANCIL": "523405", "BIRLANU": "509675", "FDC": "531599",
    "JUBLPHARMA": "530019", "VOEPL": "543597", "PPLPHARMA": "543635", "UNIPARTS": "543689",
    "CYIENT": "532175", "DENTA": "544345", "ENTERO": "544122", "CARBORUNIV": "513375",
    "NATCOPHARM": "524816", "HINDWAREAP": "542905", "KARBON": "544511", "AARTIPHARM": "543748",
    "KPITTECH": "542651", "CONCORDBIO": "543960", "SONACOMS": "543300", "EUREKAFORB": "543482",
}

# Headlines matching these are skipped even if they mention a watched company.
JUNK = re.compile(
    r"stocks? to (watch|buy|sell)|shares? to buy|top (gainers|losers)|live updates|market live|"
    r"iphone|samsung|smartphone|mobile phone|laptop|\\btv\\b|flipkart|amazon|myntra|meesho|"
    r"big billion|great indian|shopping|\\bsale\\b|\\boffers?\\b|\\bdeals?\\b|discount|coupon|cashback|"
    r"festive|diwali|dhanteras|gift|best buy|price list|horoscope|recipe|"
    r"credit card|fd rates|fixed deposit|savings account|loan offer|"
    r"cricket|ipl|bollywood|movie|box office|weather",
    re.I,
)

# Watched companies that are also brokerages: headlines that only quote them as the
# analyst on some other stock are dropped. symbol -> (quoted as broker, about itself).
# A headline is dropped when the first pattern matches and the second doesn't.
BROKER_ONLY = {
    "JMFINANCIL": (
        re.compile(
            r"JM Financial (institutional|research|analysts?|securities|services)\b|"
            r"(according to|says|said|per|brokerage|broker|analysts? at|by|from|via) JM Financial|"
            r"JM Financial (says|sees|expects|maintains|retains|upgrades?|downgrades?|initiates|"
            r"recommends|gives|assigns|is (bullish|bearish|positive|negative)|bullish|bearish|"
            r"likes|prefers|top picks?|picks?|(raises|cuts|hikes|lowers) (its )?(target|rating|estimates?))|"
            r"JM Financial'?s? (top picks?|target|view|outlook|report|note|estimates?|rating)|"
            r"[:|]\s*JM Financial\s*$",
            re.I),
        re.compile(
            r"JM Financial( Ltd| Limited)?('s)? (shares?|stock|q[1-4]|quarter|results?|net profit|"
            r"profit|revenue|board|dividend|ceo|md|chairman|arm|subsidiar|home loans|"
            r"asset reconstruction|credit solutions|products|nbfc|unit|raises \S+ (crore|cr)|ncd)|"
            r"(shares? of|stake in|rbi|sebi|curbs on|ban on)\b.{0,40}JM Financial",
            re.I),
    ),
}

# Routine exchange filings that are never worth an alert.
ROUTINE_FILING = re.compile(
    r"trading window|loss of (share )?certificate|duplicate (share )?certificate|reg(ulation)?\.? ?74|"
    r"74 ?\(5\)|newspaper|publication|investor (meet|conference)|analysts?(/| and )?(institutional )?investor|"
    r"\bmeet\b|\bkyc\b|transfer of shares|transmission of shares|compliance certificate|certificate under|"
    r"reg(ulation)?\.? ?(7|40|39)\b|esop|esos|allotment of (equity )?shares under",
    re.I,
)

# Headlines must match one of these before they are analysed (keeps noise and API cost down).
KEYWORDS = re.compile(
    r"result|profit|loss|revenue|earning|ebitda|margin|dividend|bonus|split|buyback|"
    r"ceo|cfo|\bmd\b|chairman|resign|appoint|steps down|upgrade|downgrade|target|rating|"
    r"order|contract|acqui|merger|demerger|stake|usfda|fda|warning letter|inspection|"
    r"block deal|bulk deal|pledge|fund ?rais|qip|rights issue|penalty|sebi|rbi|"
    r"guidance|concall|nim|npa|asset quality|surge|jump|soar|plunge|crash|slump|tumble|rall|52-week|upper circuit|lower circuit|fall|drop|declin|slide|rise|rose|gain|climb|probe|raid|fraud|warning letter|import alert",
    re.I,
)


# ---------- state ----------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state, today):
    cutoff = (today - dt.timedelta(days=7)).isoformat()
    for bucket in ("seen", "reported_news"):
        state[bucket] = {k: d for k, d in state.get(bucket, {}).items() if d >= cutoff}
    for bucket in ("events", "price_alerted"):
        state[bucket] = {d: v for d, v in state.get(bucket, {}).items() if d >= cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def record_event(state, today, symbol, a):
    state.setdefault("events", {}).setdefault(today.isoformat(), []).append({
        "symbol": symbol,
        "event": a.get("event", ""),
        "impact": a.get("impact", ""),
        "importance": a.get("importance", "-"),
        "time": dt.datetime.now(IST).strftime("%H:%M"),
    })


# ---------- telegram ----------

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured. Message would be:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Telegram caps messages at 4096 chars. Split between blank-line-separated
    # blocks (then lines, if one block is too long) so an HTML tag or table is
    # never cut in half, which makes Telegram reject the message.
    parts = []  # (separator before it, text)
    for block in text.split("\n\n"):
        lines = [block] if len(block) <= 4000 else block.split("\n")
        parts += [("\n\n", lines[0])] + [("\n", ln) for ln in lines[1:]]
    chunks, cur = [], ""
    for sep, part in parts:
        if cur and len(cur) + len(sep) + len(part) > 4000:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}{sep}{part}" if cur else part
    chunks.append(cur)
    for chunk in chunks:
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": "true",
            "parse_mode": "HTML",
        }, timeout=20)
        if not r.ok:
            print("Telegram error:", r.status_code, r.text)


def format_alert(symbol, a, link=""):
    e = lambda v: html.escape(str(v))
    now = dt.datetime.now(IST)
    impact = str(a.get("impact", ""))
    icon = "🟢" if impact.startswith("Bullish") else "🔴" if impact.startswith("Bearish") else "⚪"
    try:
        imp = max(0, min(10, int(a.get("importance"))))
        meter = f"{'█' * imp}{'░' * (10 - imp)} {imp}/10"
    except (TypeError, ValueError):
        meter = "-"

    # Blank lines separate blocks, the same layout as the 3-hourly report.
    blocks = [
        f"🔔 <b>ALERT · {e(symbol)}</b>\n"
        f"🏢 {e(WATCHLIST[symbol][0] if symbol in WATCHLIST else symbol)}\n"
        f"🕒 {now:%I:%M %p} · {now:%a, %d %b}\n"
        "━━━━━━━━━━━━━━━━━━━━",
        f"📌 <b>EVENT</b>\n{e(a.get('event', ''))}",
        f"{icon} <b>{e(impact or 'Neutral')}</b>\n"
        f"<code>Importance  {e(meter)}</code>\n"
        f"<code>Confidence  {e(a.get('confidence', '-'))}</code>",
        f"💡 <b>WHY IT MATTERS</b>\n{e(a.get('reason', ''))}",
        f"👀 <b>WHAT TO WATCH</b>\n<i>{e(a.get('action', ''))}</i>",
    ]
    if link:
        label = "Open the filing" if "nseindia.com" in link or "bseindia.com" in link else "Read the news"
        blocks.append(f'━━━━━━━━━━━━━━━━━━━━\n🔗 <a href="{e(link)}">{label}</a>')
    return "\n\n".join(blocks)


# ---------- news ----------

def fetch_news(name):
    q = urllib.parse.quote(f'"{name}" when:1d')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries[:20]:
        title = entry.get("title", "")
        # Google News titles end with " - Source name"; drop it so the source
        # name (e.g. "NDTV Profit") doesn't trigger keyword matches.
        if " - " in title:
            title = title.rsplit(" - ", 1)[0]
        items.append({"title": title.strip(), "link": entry.get("link", "")})
    return items


# ---------- exchange filings (NSE + BSE corporate announcements) ----------

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
}


def filing_item(source, subject, text, link):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > 300:
        text = text[:300] + "…"
    title = f"[{source} filing] {subject}" + (f": {text}" if text and text != subject else "")
    return {"title": title, "link": link, "filing": True, "source": source, "subject": subject}


def fetch_nse_filings(today):
    """All NSE announcements from yesterday and today, main board and SME,
    for the watchlist. NSE needs a browser-like session cookie first."""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    s.headers["Referer"] = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
    s.get("https://www.nseindia.com/companies-listing/corporate-filings-announcements", timeout=20)
    params = {"from_date": f"{today - dt.timedelta(days=1):%d-%m-%Y}", "to_date": f"{today:%d-%m-%Y}"}
    out = {}
    for index in ("equities", "sme"):
        r = s.get("https://www.nseindia.com/api/corporate-announcements",
                  params={"index": index, **params}, timeout=30)
        r.raise_for_status()
        for a in r.json():
            sym = a.get("symbol", "")
            if sym in WATCHLIST:
                out.setdefault(sym, []).append(filing_item(
                    "NSE", a.get("desc", ""), a.get("attchmntText", ""), a.get("attchmntFile", "")))
    return out


def bse_code(session, state, symbol):
    """BSE scrip code for an NSE symbol: from BSE_CODES, otherwise looked up once
    and remembered in state."""
    if symbol in BSE_CODES:
        return BSE_CODES[symbol]
    codes = state.setdefault("bse_codes", {})
    if symbol not in codes:
        r = session.get("https://api.bseindia.com/BseIndiaAPI/api/PeerSmartSearch/w",
                        params={"Type": "SS", "text": symbol}, timeout=20)
        r.raise_for_status()
        # The response is HTML like "<strong>LAURUSLABS</strong>  INE947Q01028  540222"
        m = re.search(rf"<\w+>{re.escape(symbol)}</\w+>\s+\w+\s+(\d{{6}})",
                      r.text.replace("&nbsp;", " "), re.I)
        if not m:
            return None  # not listed on BSE (e.g. NSE SME stocks); tried again next run
        codes[symbol] = m.group(1)
    return codes[symbol]


def fetch_bse_filings(state, today):
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    s.headers.update({"Origin": "https://www.bseindia.com", "Referer": "https://www.bseindia.com/",
                      "Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"})
    try:
        # Pick up BSE's session cookies the way a browser would before calling the API.
        s.get("https://www.bseindia.com/corporates/ann.html", timeout=20)
    except Exception as ex:
        print("BSE homepage fetch failed:", ex)
    out = {}
    for sym in WATCHLIST:
        try:
            code = bse_code(s, state, sym)
            if not code:
                continue
            r = s.get("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w", params={
                "pageno": 1, "strCat": -1, "subcategory": -1, "strSearch": "P", "strType": "C",
                "strPrevDate": f"{today - dt.timedelta(days=1):%Y%m%d}", "strToDate": f"{today:%Y%m%d}",
                "strScrip": code,
            }, timeout=20)
            if r.status_code in (401, 403):
                # BSE is refusing this server; the other stocks would fail the same way.
                raise PermissionError(f"BSE refused the request (HTTP {r.status_code})")
            r.raise_for_status()
        except PermissionError:
            raise
        except Exception as ex:
            print(f"BSE filings failed for {sym}: {ex}")
            continue
        for a in r.json().get("Table", []):
            att = (a.get("ATTACHMENTNAME") or "").strip()
            link = (f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{att}" if att
                    else a.get("NSURL", ""))
            subject = (a.get("NEWSSUB") or "").split(" - ")[-1].strip() or a.get("SUBCATNAME") or ""
            out.setdefault(sym, []).append(filing_item("BSE", subject, a.get("HEADLINE", ""), link))
    return out


def fetch_filings(state, today):
    """symbol -> list of filing items from NSE and BSE. A failure on one exchange
    (they sometimes block cloud servers) doesn't stop the other."""
    filings = {}
    status = {"checked": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M")}
    for name, fetch in (("NSE", lambda: fetch_nse_filings(today)),
                        ("BSE", lambda: fetch_bse_filings(state, today))):
        try:
            found = fetch()
            for sym, items in found.items():
                filings.setdefault(sym, []).extend(items)
            status[name] = f"ok: {sum(len(v) for v in found.values())} filings for {len(found)} stocks"
        except Exception as ex:
            print(f"{name} filings fetch failed: {ex}")
            status[name] = f"failed: {ex}"[:200]
    state["filing_sources"] = status  # saved in state.json so a failing source is easy to spot
    return filings


def is_relevant(item, mentions, symbol=None):
    """Filings are always about the company; news headlines must name it, and not
    only as the brokerage quoted on another stock."""
    t = item["title"]
    if item.get("filing"):
        return not ROUTINE_FILING.search(t)
    if symbol in BROKER_ONLY:
        as_broker, about_itself = BROKER_ONLY[symbol]
        if as_broker.search(t) and not about_itself.search(t):
            return False
    if symbol in NOT_THIS_COMPANY:
        t = NOT_THIS_COMPANY[symbol].sub("", t)
    return bool(mentions.search(t)) and not JUNK.search(t)


def is_material(item):
    return bool(item.get("filing") or KEYWORDS.search(item["title"]))


CLAUDE_PROMPT = """You are a stock monitoring analyst for Indian equities.
Below are new news headlines and stock exchange filings about {name} (NSE: {symbol}).

Decide whether any of them is a material event for this stock: earnings results, corporate
announcements, dividends/bonus/buyback, management changes, analyst upgrades/downgrades,
large orders, regulatory action, M&A, or other significant company-specific news.
Ignore generic market wrap-ups, "stocks to watch" lists, and articles about a different company
with a similar name.

Headlines:
{items}

Reply with ONLY a JSON object, no other text:
{{"significant": true or false,
 "headline_index": <number of the most important headline>,
 "event": "<short description of the event>",
 "impact": "Bullish" or "Bearish" or "Neutral",
 "reason": "<1-2 sentences on the likely impact>",
 "action": "<what to monitor next>",
 "confidence": "High" or "Medium" or "Low",
 "importance": <integer 1-10>}}
Judge only from the headlines. If they are too vague to judge, lower the confidence."""


def claude_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=180)


def analyze_with_claude(symbol, name, headlines):
    items = "\n".join(f"{i + 1}. {h['title']}" for i, h in enumerate(headlines))
    resp = claude_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": CLAUDE_PROMPT.format(
            name=name, symbol=symbol, items=items)}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    match = re.search(r"\{.*\}", text, re.S)
    return json.loads(match.group(0)) if match else None


# ---------- free rule-based analysis (used when there is no API key) ----------

# (pattern, event type, importance, what to monitor). First match wins, so the
# most serious event types come first.
EVENT_RULES = [
    (r"usfda|warning letter|import alert|form 483|sebi|\brbi\b|penalty|raid|probe|show.cause|fraud|default",
     "Regulatory / legal", 8, "Company clarification and the financial impact."),
    (r"resign|steps down|appoint|new ceo|\bceo\b|\bcfo\b|\bmd\b|chairman|management change",
     "Management change", 7, "Reason for the change and who succeeds."),
    (r"acqui|merger|demerger|takeover|stake sale|buys stake|sells stake|stake",
     "M&A / stake change", 7, "Deal value, valuation and funding."),
    (r"result|earning|\bq[1-4]\b|quarter|net profit|profit|revenue|ebitda|margin|net loss|guidance|\bnim\b|\bnpa\b|asset quality",
     "Earnings / results", 7, "Margins, guidance and concall commentary."),
    (r"upgrade|downgrade|target price|\btarget\b|rating|initiates coverage|brokerage",
     "Analyst action", 6, "Other brokers' views and the new target."),
    (r"fund ?rais|\bqip\b|rights issue|preferential|\bncd\b",
     "Fund raising", 6, "Issue price, dilution and use of funds."),
    (r"block deal|bulk deal|pledge",
     "Large deal / pledge", 6, "Who bought or sold, and promoter pledge levels."),
    (r"surge|jump|soar|plunge|crash|slump|tumble|rall|upper circuit|lower circuit|52-week",
     "Sharp price move", 6, "Whether news explains the move and if it holds."),
    (r"order|contract|bags|wins",
     "Order win", 5, "Order size versus annual revenue, and execution timeline."),
    (r"dividend|bonus|split|buyback|record date",
     "Corporate action", 5, "Record date and payout details."),
    (r"\\bfall|\\bfalls|\\bfell|drop|declin|slide|\\brise|\\brises|\\brose|gain|climb",
     "Price move", 5, "Whether the move continues and what's driving it."),
]
EVENT_RULES = [(re.compile(p, re.I), t, imp, act) for p, t, imp, act in EVENT_RULES]

BULLISH_WORDS = re.compile(
    r"\brise|\brises|\brose|jump|surge|soar|gain|beat|record|strong|upgrade|wins|bags|"
    r"approv|improv|raises|higher|growth|grows|expands|turnaround|narrow|rall|upper circuit|"
    r"52-week high|outperform|\bbuy\b|dividend|bonus|buyback|up \d", re.I)
BEARISH_WORDS = re.compile(
    r"\bfall|\bfalls|\bfell|drop|declin|plunge|crash|slump|tumble|miss|weak|downgrade|"
    r"\bloss\b|worsen|deteriorat|resign|penalty|warning letter|import alert|probe|raid|\bcut|lower|"
    r"\bsell\b|underperform|lower circuit|52-week low|pledge|default|delay|down \d", re.I)


def rule_analyze(headlines):
    """Pick the most important headline and label it with simple keyword rules."""
    best = None
    for idx, h in enumerate(headlines):
        t = h["title"]
        for pattern, event_type, importance, action in EVENT_RULES:
            if pattern.search(t):
                if best is None or importance > best[2]:
                    best = (idx, event_type, importance, action)
                break
    if best is None:
        return None, 0
    idx, event_type, importance, action = best
    title = headlines[idx]["title"]
    scored = re.sub(r"loss(es)? narrow", "narrow", title, flags=re.I)  # "loss narrows" is good news
    bull, bear = len(BULLISH_WORDS.findall(scored)), len(BEARISH_WORDS.findall(scored))
    impact = "Bullish" if bull > bear else "Bearish" if bear > bull else "Neutral"
    if len(headlines) >= 3:
        importance = min(10, importance + 1)  # several new stories at once = bigger news
    extra = f" (+{len(headlines) - 1} more stories)" if len(headlines) > 1 else ""
    a = {
        "event": f"{event_type}: {title}{extra}",
        "impact": impact,
        "reason": f"Headline points to {event_type.lower()} with {impact.lower()} wording. "
                  "Keyword-based read, so open the story before acting.",
        "action": action,
        "confidence": "Low" if impact == "Neutral" else "Medium",
        "importance": importance,
    }
    return a, idx


def scan_news(state, today, collected=None):
    """Alert on new material news. If `collected` is a dict, it is filled with
    symbol -> all of today's relevant headlines (new or already seen) for the report."""
    priming = "seen" not in state  # first run: remember existing news without alerting
    # Filings were added later: the first run with them only remembers what's already there.
    filings_priming = not state.get("filings_primed")
    seen = state.setdefault("seen", {})
    # Stocks added to the watchlist since the last run: remember their current news
    # without alerting, so adding a stock doesn't trigger a burst of old alerts.
    known = state.get("known_symbols")
    new_symbols = set() if known is None else set(WATCHLIST) - set(known)
    filings = fetch_filings(state, today)
    for symbol, (name, aliases) in WATCHLIST.items():
        mentions = re.compile(r"\b(" + "|".join(re.escape(a) for a in aliases) + r")\b", re.I)
        try:
            news = fetch_news(name)
        except Exception as ex:
            print(f"News fetch failed for {symbol}: {ex}")
            news = []
        items = filings.get(symbol, []) + news

        if collected is not None:
            collected[symbol] = [it for it in items if is_relevant(it, mentions, symbol)][:8]

        new = []
        for it in items:
            key = hashlib.sha1(it["title"].lower().encode()).hexdigest()[:16]
            if key in seen:
                continue
            seen[key] = today.isoformat()
            if filings_priming and it.get("filing"):
                continue
            if is_relevant(it, mentions, symbol) and is_material(it):
                new.append(it)
        if priming or symbol in new_symbols or not new:
            continue

        a, link = None, new[0]["link"]
        if ANTHROPIC_API_KEY:
            try:
                a = analyze_with_claude(symbol, name, new)
            except Exception as ex:
                print(f"Claude analysis failed for {symbol}: {ex}")
            if a is not None:
                try:
                    importance = int(a.get("importance", 0))
                except (TypeError, ValueError):
                    importance = 0
                if not a.get("significant") or importance < MIN_IMPORTANCE:
                    continue
                idx = int(a.get("headline_index", 1) or 1) - 1
                link = new[max(0, min(idx, len(new) - 1))]["link"]

        if a is None:  # no API key, or analysis failed: use the free keyword rules
            a, idx = rule_analyze(new)
            if a is None or a["importance"] < MIN_IMPORTANCE_RULES:
                continue
            link = new[idx]["link"]

        send_telegram(format_alert(symbol, a, link))
        record_event(state, today, symbol, a)
    state["filings_primed"] = True
    state["known_symbols"] = sorted(WATCHLIST)
    if priming:
        print("First run: existing news recorded, no alerts sent.")


# ---------- price & volume ----------

def get_price_moves(today):
    moves = []
    for sym, df in download_prices("40d").items():
        if len(df) < 5 or df.index[-1].date() != today:
            continue  # no data, or no candle for today (holiday / not listed on Yahoo)
        last, prev = df.iloc[-1], df.iloc[-2]
        pct = (last["Close"] / prev["Close"] - 1) * 100
        avg_vol = df["Volume"].iloc[-21:-1].mean()
        vol_x = (last["Volume"] / avg_vol) if avg_vol else 0
        moves.append({"symbol": sym, "close": float(last["Close"]), "pct": float(pct), "vol_x": float(vol_x)})
    return moves


def scan_prices(state, today):
    if today.weekday() >= 5:
        return
    try:
        moves = get_price_moves(today)
    except Exception as ex:
        print("Price fetch failed:", ex)
        return
    done = state.setdefault("price_alerted", {}).setdefault(today.isoformat(), [])
    for m in moves:
        sym, pct, vol_x = m["symbol"], m["pct"], m["vol_x"]
        if abs(pct) >= PRICE_MOVE_PCT and f"{sym}:price" not in done:
            done.append(f"{sym}:price")
            a = {
                "event": f"Price {pct:+.1f}% today at ₹{m['close']:,.2f} (volume {vol_x:.1f}x 20-day avg)",
                "impact": "Bullish (price action)" if pct > 0 else "Bearish (price action)",
                "reason": "Large one-day move. Check whether news or a filing explains it; "
                          "moves without news can reverse.",
                "action": "Exchange filings, news, and whether the move holds at close.",
                "confidence": "Medium",
                "importance": 7 if abs(pct) >= 8 else 6,
            }
            send_telegram(format_alert(sym, a))
            record_event(state, today, sym, a)
        elif vol_x >= VOLUME_SPIKE_X and f"{sym}:volume" not in done:
            done.append(f"{sym}:volume")
            a = {
                "event": f"Unusual volume: {vol_x:.1f}x 20-day avg (price {pct:+.1f}%)",
                "impact": "Neutral",
                "reason": "Volume spike without a large price move can signal block deals "
                          "or positioning ahead of news.",
                "action": "Block/bulk deal data and upcoming announcements.",
                "confidence": "Low",
                "importance": 5,
            }
            send_telegram(format_alert(sym, a))
            record_event(state, today, sym, a)


# ---------- daily summary ----------

def daily_summary(state, today):
    lines = [f"📊 Daily summary — {today:%d %b %Y}", ""]

    events = state.get("events", {}).get(today.isoformat(), [])

    def imp(e):
        try:
            return int(e.get("importance"))
        except (TypeError, ValueError):
            return 0

    if events:
        lines.append("Material developments today:")
        for e in sorted(events, key=imp, reverse=True):
            lines.append(f"• {e['symbol']} ({e['time']}): {e['event']} — {e['impact']}, {e['importance']}/10")
    else:
        lines.append("No material developments flagged today.")

    try:
        moves = get_price_moves(today) if today.weekday() < 5 else []
    except Exception as ex:
        print("Price fetch failed:", ex)
        moves = []

    if moves:
        moves.sort(key=lambda m: m["pct"], reverse=True)
        lines += ["", "Top gainers:"]
        lines += [f"• {m['symbol']} {m['pct']:+.1f}% (₹{m['close']:,.2f})" for m in moves[:5] if m["pct"] > 0]
        lines += ["", "Top losers:"]
        lines += [f"• {m['symbol']} {m['pct']:+.1f}% (₹{m['close']:,.2f})" for m in moves[::-1][:5] if m["pct"] < 0]
        spikes = [m for m in moves if m["vol_x"] >= VOLUME_SPIKE_X]
        if spikes:
            lines += ["", "Volume spikes:"]
            lines += [f"• {m['symbol']} {m['vol_x']:.1f}x avg" for m in spikes]
    else:
        lines += ["", "No price data for today (market holiday or weekend)."]

    send_telegram("\n".join(lines))


# ---------- 3-hourly report ----------

def get_snapshot():
    """Latest price picture for every stock: day / week / month change, volume and
    52-week range. Uses the most recent candle, so it also works before the open."""
    snap = {}
    for sym, df in download_prices("1y").items():
        if len(df) < 22:
            continue
        close = df["Close"]
        last = float(close.iloc[-1])
        chg = lambda n: (last / float(close.iloc[-1 - n]) - 1) * 100
        avg_vol = df["Volume"].iloc[-21:-1].mean()
        hi, lo = float(df["High"].max()), float(df["Low"].min())
        snap[sym] = {
            "date": df.index[-1].date().isoformat(),
            "close": round(last, 2),
            "day_pct": round(chg(1), 2),
            "week_pct": round(chg(5), 2),
            "month_pct": round(chg(21), 2),
            "vol_x": round(float(df["Volume"].iloc[-1] / avg_vol), 2) if avg_vol else 0,
            "from_52w_high_pct": round((last / hi - 1) * 100, 1),
            "from_52w_low_pct": round((last / lo - 1) * 100, 1),
        }
    return snap


def report_slot(now):
    """Name for this report, based on where we are in the Indian trading day."""
    t = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return "Weekend review"
    if t < 9 * 60 + 15:
        return "Pre-market"
    if t < 15 * 60 + 30:
        return "Intraday"
    if t < 17 * 60:
        return "Market close"
    return "Evening wrap"


REPORT_SYSTEM = """You are an equity research analyst covering Indian small and mid caps for
one investor. You are shown only the stocks where something crucial happened since the last
report (a sharp move, a volume spike, an alert, new company news or a stock exchange filing).

Rules:
- Be specific. Cite the numbers you were given (price change, volume multiple, distance from
  the 52-week high/low) and name the actual news. No generic filler such as "monitor closely".
- Use only the data provided. Don't invent results, targets or events.
- "view" is the near-term (days to weeks) read from the price action and news, not a buy/sell call.
- "summary" explains what happened and why it matters, in 1-2 sentences.
- "watch" is the one concrete thing to check next (a level, a filing, an event, a date)."""

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string",
                     "description": "One sentence on what matters most right now."},
        "stocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "view": {"type": "string", "enum": ["Bullish", "Bearish", "Neutral"]},
                    "summary": {"type": "string"},
                    "watch": {"type": "string"},
                },
                "required": ["symbol", "view", "summary", "watch"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "stocks"],
    "additionalProperties": False,
}


def news_key(title):
    return hashlib.sha1(title.lower().encode()).hexdigest()[:16]


def find_crucial(state, today, news):
    """Stocks with something crucial since the last report: an alert that was sent (news,
    price move or volume spike) or a material headline not covered by an earlier report.
    Returns symbol -> {"reasons": [...], "fresh": [headline keys], "filings": [new filing items]}."""
    last = state.get("last_report", "")
    reported = state.get("reported_news", {})
    events = state.get("events", {}).get(today.isoformat(), [])
    crucial = {}
    for sym in WATCHLIST:
        reasons = [f"alert at {e['time']}: {e['event']}" for e in events
                   if e["symbol"] == sym and f"{today.isoformat()}T{e['time']}" > last]
        fresh_items = [h for h in news.get(sym, [])
                       if is_material(h) and news_key(h["title"]) not in reported]
        fresh = [news_key(h["title"]) for h in fresh_items]
        if fresh:
            reasons.append(f"{len(fresh)} new filing(s) or material headline(s)")
        if reasons:
            crucial[sym] = {"reasons": reasons, "fresh": fresh,
                            "filings": [h for h in fresh_items if h.get("filing") and h.get("link")]}
    return crucial


def claude_report(now, slot, snap, news, events, crucial):
    stocks = []
    for sym, c in crucial.items():
        stocks.append({
            "symbol": sym,
            "name": WATCHLIST[sym][0],
            "why_flagged": c["reasons"],
            "price": snap.get(sym, "no price data"),
            "news_and_filings_last_24h": [h["title"] for h in news.get(sym, [])],
            "alerts_sent_today": [f"{e['time']} {e['event']} ({e['impact']})"
                                  for e in events if e["symbol"] == sym],
        })
    user = (f"Report time: {now:%A %d %b %Y, %I:%M %p} IST ({slot}).\n"
            "NSE trades 9:15 AM to 3:30 PM IST on weekdays; the price data's \"date\" is the "
            "latest daily candle, which is still forming during market hours.\n\n"
            "Flagged stocks:\n" + json.dumps(stocks, ensure_ascii=False, indent=1) +
            "\n\nWrite the report. Include every flagged stock, in the order given.")
    resp = claude_client().messages.create(
        model=REPORT_MODEL,
        max_tokens=8000,
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
        system=REPORT_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason != "end_turn":
        raise RuntimeError(f"report stopped early: {resp.stop_reason}")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def format_report(now, slot, snap, crucial, r):
    """r is Claude's analysis of the crucial stocks, or None when Claude wasn't used."""
    e = lambda v: html.escape(str(v))
    icon = {"Bullish": "🟢", "Bearish": "🔴", "Neutral": "⚪"}
    rule = "━━━━━━━━━━━━━━━━━━━━"

    def filing_links(sym):
        """📄 link to each new exchange filing for this stock (up to 3)."""
        items = crucial.get(sym, {}).get("filings", [])
        lines = [f'📄 <a href="{e(f["link"])}">{e(f["source"])}: {e(f["subject"] or "Filing")}</a>'
                 for f in items[:3]]
        if len(items) > 3:
            lines.append(f"📄 <i>+{len(items) - 3} more filings</i>")
        return lines

    def price_line(sym):
        """Monospace line with price, day, week and (if unusual) volume, e.g.
        ₹612.30 │ +6.1% day │ +8.0% wk │ vol 3.1x"""
        p = snap.get(sym)
        if not p:
            return ""
        parts = [f"₹{p['close']:,.2f}", f"{p['day_pct']:+.1f}% day", f"{p['week_pct']:+.1f}% wk"]
        if p["vol_x"] >= VOLUME_SPIKE_X:
            parts.append(f"vol {p['vol_x']:.1f}x")
        return f"<code>{e(' │ '.join(parts))}</code>"

    # Blank lines separate blocks; send_telegram never splits a message inside one.
    blocks = [f"📈 <b>STOCK REPORT</b>\n🕒 {now:%I:%M %p} · {e(slot)}\n📅 {now:%a, %d %b %Y}\n{rule}"]

    if r:
        blocks.append(f"🧠 <b>WHAT MATTERS</b>\n<i>{e(r['headline'])}</i>")
        for s in r["stocks"]:
            sym = s["symbol"]
            block = [f"{icon.get(s['view'], '⚪')} <b>{e(sym)}</b>  ·  {e(s['view'])}"]
            if price_line(sym):
                block.append(price_line(sym))
            block += [f"📝 {e(s['summary'])}", f"👀 <i>{e(s['watch'])}</i>"] + filing_links(sym)
            blocks.append("\n".join(block))
    elif crucial:
        blocks.append("⚠️ <b>FLAGGED SINCE LAST REPORT</b>")
        for sym, c in crucial.items():
            block = [f"🔸 <b>{e(sym)}</b>"]
            if price_line(sym):
                block.append(price_line(sym))
            block += [f"• {e(reason)}" for reason in c["reasons"]] + filing_links(sym)
            blocks.append("\n".join(block))
    else:
        blocks.append("✅ <b>Nothing crucial since the last report.</b>")

    if snap:
        # Fixed-width table so the columns line up on the phone.
        rows = [f"{'STOCK':<10}  {'PRICE':>9}  {'DAY':>6}", "─" * 29]
        for sym in sorted(snap, key=lambda x: snap[x]["day_pct"], reverse=True):
            p = snap[sym]
            mark = " ⚡" if p["vol_x"] >= VOLUME_SPIKE_X else ""
            rows.append(f"{sym[:10]:<10}  {p['close']:>9,.2f}  {p['day_pct']:>+5.1f}%{mark}")
        as_of = dt.date.fromisoformat(max(p["date"] for p in snap.values()))
        blocks.append(f"{rule}\n📊 <b>WATCHLIST</b>\n<pre>{e(chr(10).join(rows))}</pre>\n"
                      f"<i>Prices in ₹ as of {as_of:%d %b}. ⚡ = volume {VOLUME_SPIKE_X:g}x+ average.</i>")
    else:
        blocks.append(f"{rule}\n📊 <b>WATCHLIST</b>\nNo price data available.")
    return "\n\n".join(blocks)


def send_report(state, today, news):
    now = dt.datetime.now(IST)
    slot = report_slot(now)
    try:
        snap = get_snapshot()
    except Exception as ex:
        print("Price fetch failed:", ex)
        snap = {}
    crucial = find_crucial(state, today, news)
    r = None
    if crucial and ANTHROPIC_API_KEY:  # Claude is only called when something crucial happened
        events = state.get("events", {}).get(today.isoformat(), [])
        try:
            r = claude_report(now, slot, snap, news, events, crucial)
        except Exception as ex:
            print("Claude report failed, sending without analysis:", ex)
    send_telegram(format_report(now, slot, snap, crucial, r))
    for c in crucial.values():
        for k in c["fresh"]:
            state.setdefault("reported_news", {})[k] = today.isoformat()
    state["last_report"] = now.strftime("%Y-%m-%dT%H:%M")


# ---------- main ----------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    today = dt.datetime.now(IST).date()
    state = load_state()

    if mode == "test":
        send_telegram("✅ Stock monitor is connected. You'll get alerts here.")
        return
    if mode == "check":
        # Diagnostics only, nothing is sent: which stocks have prices, and filing sources.
        snap = get_snapshot()
        fetch_filings(state, today)
        state["price_status"] = {"checked": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
                                 "found": len(snap), "missing": sorted(set(WATCHLIST) - set(snap)),
                                 **{s: snap[s]["close"] for s in YAHOO_TICKERS if s in snap}}
        print(state["price_status"], state["filing_sources"])
    elif mode == "summary":
        daily_summary(state, today)
    elif mode == "report":
        news = {}
        scan_news(state, today, news)
        scan_prices(state, today)
        send_report(state, today, news)
    else:
        scan_news(state, today)
        scan_prices(state, today)
    save_state(state, today)


if __name__ == "__main__":
    main()
