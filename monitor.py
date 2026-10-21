#!/usr/bin/env python3
"""
Stock monitor -> Telegram alerts.

  python monitor.py scan     check news + price/volume, alert on new material events
  python monitor.py summary  send the 7 PM daily summary
  python monitor.py test     send a test message to confirm Telegram works

Configuration comes from environment variables (GitHub Secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   required
  ANTHROPIC_API_KEY                      optional, enables Bullish/Bearish analysis
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.parse
from zoneinfo import ZoneInfo

import feedparser
import requests
import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")

PRICE_MOVE_PCT = float(os.environ.get("PRICE_MOVE_PCT", "5"))   # alert on moves >= 5%
VOLUME_SPIKE_X = float(os.environ.get("VOLUME_SPIKE_X", "2.5")) # volume >= 2.5x 20-day avg
MIN_IMPORTANCE = int(os.environ.get("MIN_IMPORTANCE", "6"))     # only alert news rated >= 6/10

# NSE symbol -> name used for the news search. Edit this to change your watchlist.
WATCHLIST = {
    "RELIANCE": "Reliance Industries",
    "TCS": "Tata Consultancy Services",
    "INFY": "Infosys",
    "HDFCBANK": "HDFC Bank",
    "ICICIBANK": "ICICI Bank",
    "LAURUSLABS": "Laurus Labs",
    "SOLARA": "Solara Active Pharma",
    "NORTHARC": "Northern Arc Capital",
    "SHIVALIK": "Shivalik Rasayan",
    "SYNGENE": "Syngene International",
    "ALLETEC": "All E Technologies",
    "INFOBEAN": "InfoBeans Technologies",
    "JAYBEE": "Jay Bee Laminations",
    "PRAJIND": "Praj Industries",
    "BAJAJHCARE": "Bajaj Healthcare",
    "ALIVUS": "Alivus Life Sciences",
    "PDSL": "PDS Limited",
    "GREAVESCOT": "Greaves Cotton",
    "RAYMOND": "Raymond Ltd",
    "RBA": "Restaurant Brands Asia",
    "ARMANFIN": "Arman Financial",
    "PENIND": "Pennar Industries",
    "JMFINANCIL": "JM Financial",
    "BIRLANU": "BirlaNu",
    "FDC": "FDC Limited",
}

# Headlines must match one of these before they are analysed (keeps noise and API cost down).
KEYWORDS = re.compile(
    r"result|profit|loss|revenue|earning|ebitda|margin|dividend|bonus|split|buyback|"
    r"ceo|cfo|\bmd\b|chairman|resign|appoint|steps down|upgrade|downgrade|target|rating|"
    r"order|contract|acqui|merger|demerger|stake|usfda|fda|warning letter|inspection|"
    r"block deal|bulk deal|pledge|fund ?rais|qip|rights issue|penalty|sebi|rbi|"
    r"guidance|concall|surge|jump|soar|plunge|crash|slump|tumble|rall|52-week|upper circuit|lower circuit",
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
    state["seen"] = {k: d for k, d in state.get("seen", {}).items() if d >= cutoff}
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
    for i in range(0, len(text), 4000):
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[i:i + 4000],
            "disable_web_page_preview": "true",
        }, timeout=20)
        if not r.ok:
            print("Telegram error:", r.status_code, r.text)


def format_alert(symbol, a, link=""):
    lines = [
        f"🔔 ALERT — {symbol}",
        f"Stock: {WATCHLIST.get(symbol, symbol)}",
        f"Event: {a.get('event', '')}",
        f"Impact: {a.get('impact', '')}",
        f"Reason: {a.get('reason', '')}",
        f"Action to Monitor: {a.get('action', '')}",
        f"Confidence: {a.get('confidence', '')}",
        f"Importance: {a.get('importance', '-')}/10",
    ]
    if link:
        lines.append(f"Source: {link}")
    return "\n".join(lines)


# ---------- news ----------

def fetch_news(name):
    q = urllib.parse.quote(f'"{name}" when:1d')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
    feed = feedparser.parse(url)
    return [{"title": e.get("title", ""), "link": e.get("link", "")} for e in feed.entries[:20]]


CLAUDE_PROMPT = """You are a stock monitoring analyst for Indian equities.
Below are new news headlines about {name} (NSE: {symbol}).

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


def analyze_with_claude(symbol, name, headlines):
    items = "\n".join(f"{i + 1}. {h['title']}" for i, h in enumerate(headlines))
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 600,
            "messages": [{"role": "user", "content": CLAUDE_PROMPT.format(
                name=name, symbol=symbol, items=items)}],
        },
        timeout=60,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    match = re.search(r"\{.*\}", text, re.S)
    return json.loads(match.group(0)) if match else None


def scan_news(state, today):
    priming = "seen" not in state  # first run: remember existing news without alerting
    seen = state.setdefault("seen", {})
    for symbol, name in WATCHLIST.items():
        try:
            items = fetch_news(name)
        except Exception as ex:
            print(f"News fetch failed for {symbol}: {ex}")
            continue

        new = []
        for it in items:
            key = hashlib.sha1(it["title"].lower().encode()).hexdigest()[:16]
            if key in seen:
                continue
            seen[key] = today.isoformat()
            if KEYWORDS.search(it["title"]):
                new.append(it)
        if priming or not new:
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

        if a is None:  # no API key, or analysis failed: send the raw headline
            extra = f" (+{len(new) - 1} more)" if len(new) > 1 else ""
            a = {
                "event": new[0]["title"] + extra,
                "impact": "Not analysed",
                "reason": "Add ANTHROPIC_API_KEY to get Bullish/Bearish analysis.",
                "action": "Read the story and the exchange filing.",
                "confidence": "-",
                "importance": "-",
            }

        send_telegram(format_alert(symbol, a, link))
        record_event(state, today, symbol, a)
    if priming:
        print("First run: existing news recorded, no alerts sent.")


# ---------- price & volume ----------

def get_price_moves(today):
    tickers = [s + ".NS" for s in WATCHLIST]
    data = yf.download(tickers, period="40d", interval="1d", group_by="ticker",
                       auto_adjust=False, progress=False, threads=True)
    moves = []
    for sym in WATCHLIST:
        try:
            df = data[sym + ".NS"].dropna(subset=["Close"])
        except KeyError:
            continue
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


# ---------- main ----------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    today = dt.datetime.now(IST).date()
    state = load_state()

    if mode == "test":
        send_telegram("✅ Stock monitor is connected. You'll get alerts here.")
        return
    if mode == "summary":
        daily_summary(state, today)
    else:
        scan_news(state, today)
        scan_prices(state, today)
    save_state(state, today)


if __name__ == "__main__":
    main()
