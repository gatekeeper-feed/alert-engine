#!/usr/bin/env python3
"""
GATEKEEPER ALERT ENGINE v3 - free-data edition
Runs every ~5 minutes on GitHub Actions. All market data comes from Yahoo Finance
(free). Alerts go to Telegram. State is kept in state.json (committed by the workflow).

Covers: Gold + BTC (main strategy + scalping module), Nifty + Sensex (index levels only),
news override, 2-hourly status reports, briefings, trade tracking and reviews.
NOT included (needs paid data): option chain, PCR/OI, option premiums/greeks, Gift Nifty.
"""
import os, sys, json, time, re, html, hashlib, traceback
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
import requests
import numpy as np
import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT", "")
DRY = os.environ.get("DRY_RUN") == "1" or not TOKEN
API = f"https://api.telegram.org/bot{TOKEN}/"
STATE_FILE = "state.json"

# ----------------------------------------------------------------------------
# SETTINGS (change numbers here, nowhere else)
# ----------------------------------------------------------------------------
CFG = dict(
    main_min_rr=15,        # swing / intraday minimum reward:risk
    scalp_min_rr=3,        # scalping minimum reward:risk (below = silent paper trade)
    add_lot_cap=2,         # max "add lot / re-entry" alerts per trade
    approach_atr_main=1.5, # "price approaching zone" distance, in 1H ATRs
    approach_atr_scalp=0.75,
    volatile_ratio=1.8,    # 5m ATR vs its normal -> use 15m for sweep/rejection
    fast_minutes=90, slow_minutes=120,
    nifty_sl=10, sensex_sl=33,     # index-point stop losses (Sensex 33 is a suggestion)
    big_gap=0.4, flat_gap=0.2,     # gap % thresholds (suggested values)
    btst_min_rr=3,                 # suggested value
    india_last_entry=(14, 45), india_exit=(15, 15),
)

ASSETS = {
    "GOLD": dict(name="Gold (XAU/USD)", syms=["XAUUSD=X", "GC=F"], corr=["XAGUSD=X", "SI=F"],
                 corr_name="Silver", dec=2, query="gold price OR XAUUSD"),
    "BTC": dict(name="Bitcoin (BTC/USD)", syms=["BTC-USD"], corr=["ETH-USD"],
                corr_name="ETH", dec=1, query="bitcoin price OR BTC"),
}
INDIA = {
    "NIFTY": dict(name="Nifty 50", sym="^NSEI", step=50, sl="nifty_sl", expiry_wd=1),
    "SENSEX": dict(name="Sensex", sym="^BSESN", step=100, sl="sensex_sl", expiry_wd=3),
}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# ----------------------------------------------------------------------------
# BASIC HELPERS
# ----------------------------------------------------------------------------
def log(*a):
    print(*a, flush=True)

def utcnow():
    return pd.Timestamp.now(tz="UTC")

def esc(s):
    return html.escape(str(s))

def fmt(x, d=2):
    return f"{x:,.{d}f}"

def hhmm(ts):
    return pd.Timestamp(ts).tz_convert(IST).strftime("%d %b %H:%M")

def epoch(ts):
    return int(pd.Timestamp(ts).timestamp())

def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    for k, v in dict(sent={}, watch={}, zones={}, levels={}, trades=[], hist=[], hold={},
                     post={}, news_seen=[], india={}, oi={}, review={}, upd=0, errors={}).items():
        st.setdefault(k, v)
    return st

def save_state(st):
    cut = time.time() - 4 * 86400
    st["sent"] = {k: v for k, v in st["sent"].items() if v > cut}
    st["hist"] = st["hist"][-600:]
    st["news_seen"] = st["news_seen"][-300:]
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)

def once(st, key):
    if key in st["sent"]:
        return False
    st["sent"][key] = time.time()
    return True

def due(st, key, now, hh, mm, window=14, days=None):
    if days is not None and now.weekday() not in days:
        return False
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if not (0 <= (now - start).total_seconds() < window * 60):
        return False
    return once(st, f"{key}|{now.date()}")

# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------
def tg(text, kb=None):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for i, c in enumerate(chunks):
        if DRY:
            log("\n=== TELEGRAM ===\n" + c)
            continue
        payload = dict(chat_id=CHAT, text=c, parse_mode="HTML", disable_web_page_preview=True)
        if kb and i == len(chunks) - 1:
            payload["reply_markup"] = kb
        for _ in range(3):
            try:
                r = requests.post(API + "sendMessage", json=payload, timeout=20)
                if r.status_code == 200:
                    break
                log("telegram error", r.status_code, r.text[:200])
                time.sleep(2)
            except Exception as e:
                log("telegram exc", e)
                time.sleep(2)

def hold_kb(asset):
    row = [("None", "N"), ("Scalp", "S"), ("Swing", "W"), ("Intraday", "I")]
    return {"inline_keyboard": [[{"text": t, "callback_data": f"h|{asset}|{k}"} for t, k in row]]}

def poll_callbacks(st, now):
    if DRY:
        return
    try:
        r = requests.get(API + "getUpdates", timeout=20, params={
            "offset": st.get("upd", 0), "timeout": 0,
            "allowed_updates": json.dumps(["callback_query"])}).json()
    except Exception:
        return
    names = {"N": "None", "S": "Scalp", "W": "Swing", "I": "Intraday"}
    for u in r.get("result", []):
        st["upd"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq:
            continue
        p = cq.get("data", "").split("|")
        txt = "Saved"
        try:
            if p[0] == "h":
                st["hold"][p[1]] = dict(type=p[2], ts=time.time())
                txt = f"{ASSETS[p[1]]['name']}: holding {names.get(p[2], '?')}"
            elif p[0] == "p":                      # post-target: still holding?
                if p[2] == "C":
                    st["post"].pop(p[1], None)
                    txt = "Okay, marked closed"
                else:
                    st["post"].setdefault(p[1], {})["last_ask"] = time.time()
                    txt = "Okay, still holding"
            elif p[0] == "x":                      # did you execute the alert?
                for t in st["trades"]:
                    if t["id"] == p[1]:
                        t["taken"] = p[2]
                txt = "Noted"
        except Exception:
            pass
        try:
            requests.post(API + "answerCallbackQuery", json={"callback_query_id": cq["id"], "text": txt}, timeout=10)
        except Exception:
            pass

# ----------------------------------------------------------------------------
# DATA (Yahoo Finance, free)
# ----------------------------------------------------------------------------
_cache = {}

def yf(sym, interval, rng):
    key = (sym, interval, rng)
    if key in _cache:
        return _cache[key]
    df = pd.DataFrame()
    for k in range(4):
        host = "query1" if k % 2 == 0 else "query2"
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}",
                             params={"interval": interval, "range": rng}, headers=UA, timeout=20)
            if r.status_code != 200:
                time.sleep(1.5)
                continue
            j = r.json()["chart"]["result"][0]
            ts = j.get("timestamp") or []
            q = j["indicators"]["quote"][0]
            df = pd.DataFrame({"open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"]},
                              index=pd.to_datetime(ts, unit="s", utc=True))
            df = df.dropna()
            if len(df):
                break
        except Exception as e:
            log("yahoo fail", sym, interval, e)
            time.sleep(1.5)
    _cache[key] = df
    return df

def get(syms, interval, rng):
    for s in syms:
        df = yf(s, interval, rng)
        if len(df) > 20:
            return df
    return pd.DataFrame()

def closed(df, minutes):
    if df.empty:
        return df
    if df.index[-1] + pd.Timedelta(minutes=minutes) > utcnow():
        return df.iloc[:-1]
    return df

def rs(df, rule):
    if df.empty:
        return df
    return df.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()

def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def swings(df, n=2):
    h, l = df["high"].values, df["low"].values
    H, L = [], []
    for i in range(n, len(df) - n):
        if h[i] > max(h[i - n:i]) and h[i] >= max(h[i + 1:i + n + 1]):
            H.append((i, float(h[i])))
        if l[i] < min(l[i - n:i]) and l[i] <= min(l[i + 1:i + n + 1]):
            L.append((i, float(l[i])))
    return H, L

def chg_pct(sym):
    df = yf(sym, "1d", "5d")
    if len(df) < 2:
        return None
    return float((df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100)

def structure(df):
    """+1 higher highs & higher lows, -1 lower highs & lower lows, else 0"""
    if len(df) < 30:
        return 0
    H, L = swings(df, 3)
    if len(H) < 2 or len(L) < 2:
        return 0
    up = H[-1][1] > H[-2][1] and L[-1][1] > L[-2][1]
    dn = H[-1][1] < H[-2][1] and L[-1][1] < L[-2][1]
    return 1 if up else -1 if dn else 0

# ----------------------------------------------------------------------------
# NEWS (Google News RSS, free)
# ----------------------------------------------------------------------------
NEWS_RULES = [
    (r"\b(war|missile|airstrike|air strike|invasion|escalat)", dict(GOLD=1, BTC=-1, INDIA=-1), "Geopolitical escalation"),
    (r"\b(ceasefire|cease-fire|peace deal|truce|de-escalat)", dict(GOLD=-1, BTC=1, INDIA=1), "De-escalation"),
    (r"\b(tariff|trade war|sanction)", dict(GOLD=1, BTC=-1, INDIA=-1), "Tariffs / sanctions"),
    (r"\b(rate cut|cuts rates|dovish)", dict(GOLD=1, BTC=1, INDIA=1), "Rate-cut signal"),
    (r"\b(rate hike|hikes rates|hawkish|hot inflation|inflation (rises|jumps|surges|spikes))", dict(GOLD=-1, BTC=-1, INDIA=-1), "Hawkish / inflation shock"),
    (r"\b(crude|oil)\b.{0,15}\b(surge|jump|spike|soar|rally)", dict(GOLD=1, BTC=0, INDIA=-1), "Oil spike"),
    (r"\b(crude|oil)\b.{0,15}\b(fall|drop|slump|plunge|tumble)", dict(GOLD=0, BTC=0, INDIA=1), "Oil drop"),
    (r"\brupee\b.{0,20}\b(falls|slumps|plunges|record low|weakens)", dict(GOLD=0, BTC=0, INDIA=-1), "Rupee weakness"),
    (r"\b(crash|selloff|sell-off|meltdown|plunge)", dict(GOLD=1, BTC=-1, INDIA=-1), "Market sell-off"),
]

def headlines(query, hours=3, limit=8):
    try:
        r = requests.get("https://news.google.com/rss/search", headers=UA, timeout=20,
                         params={"q": f"{query} when:{hours}h", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
        root = ET.fromstring(r.content)
    except Exception:
        return []
    out = []
    for it in root.iter("item"):
        t = (it.findtext("title") or "").strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            pub = None
        if t:
            out.append((t, pub))
        if len(out) >= limit:
            break
    return out

def classify(title):
    for pat, imp, label in NEWS_RULES:
        if re.search(pat, title, re.I):
            return imp, label
    return None, None

def news_score(query, key, hours=6):
    s = 0
    for t, _ in headlines(query, hours, 10):
        imp, _ = classify(t)
        if imp:
            s += imp.get(key, 0)
    return max(-1, min(1, s))

def run_news(st, now):
    q = "war OR missile OR tariff OR sanctions OR Fed OR OPEC OR crude OR rupee OR inflation OR ceasefire OR Trump"
    sent = 0
    for t, pub in headlines(q, 1, 15):
        h = hashlib.md5(t.encode()).hexdigest()[:12]
        if h in st["news_seen"]:
            continue
        imp, label = classify(t)
        if not imp:
            continue
        st["news_seen"].append(h)
        if not once(st, f"news|{label}|{now:%Y%m%d}|{now.hour // 2}") or sent >= 2:
            continue
        arrow = lambda v: "▲" if v > 0 else "▼" if v < 0 else "•"
        tg(f"🚨 <b>NEWS OVERRIDE: {esc(label)}</b>\n{esc(t)}\n\n"
           f"Likely impact (headline-based, unverified):\n"
           f"Gold {arrow(imp['GOLD'])}   BTC {arrow(imp['BTC'])}   Nifty/Sensex {arrow(imp['INDIA'])}\n\n"
           f"Trade alerts still need the normal sweep, CHoCH and retest. If this reverses a running trade, "
           f"watch for a reversal-risk alert.")
        sent += 1

# ----------------------------------------------------------------------------
# ZONES (order blocks + liquidity) - higher timeframes
# ----------------------------------------------------------------------------
def find_zones(df, tf):
    if len(df) < 30:
        return []
    a = atr(df).values
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    last, zs = len(df) - 1, []
    for i in range(15, len(df)):
        if not (a[i - 1] > 0):
            continue
        if abs(c[i] - o[i]) < 1.5 * a[i - 1]:
            continue
        bull = c[i] > o[i]
        for j in range(i - 1, max(i - 6, 0), -1):
            if (c[j] < o[j]) if bull else (c[j] > o[j]):
                lo_, hi_ = float(l[j]), float(h[j])
                later = slice(i + 1, None)
                touched = (l[later] <= hi_).any() if bull else (h[later] >= lo_).any()
                if i == last:
                    touched = False
                if not touched:
                    zs.append(dict(tf=tf, kind="OB", side="demand" if bull else "supply", lo=lo_, hi=hi_,
                                   t=str(df.index[j])))
                break
    H, L = swings(df, 3)
    for i, p in H:
        if not (h[i + 1:] > p).any():
            zs.append(dict(tf=tf, kind="LIQ", side="buyside", lo=p, hi=p, t=str(df.index[i])))
    for i, p in L:
        if not (l[i + 1:] < p).any():
            zs.append(dict(tf=tf, kind="LIQ", side="sellside", lo=p, hi=p, t=str(df.index[i])))
    seen, out = set(), []
    for z in zs:                                  # one displacement can point at the same candle twice
        k = (z["kind"], z["side"], round(z["lo"], 4), round(z["hi"], 4))
        if k not in seen:
            seen.add(k)
            out.append(z)
    return out

def zdir(z):
    return "buy" if z["side"] in ("demand", "sellside") else "sell"

def zid(asset, z):
    return f"{asset}|{z['tf']}|{z['kind']}|{z['side']}|{round(z['lo'], 2)}|{round(z['hi'], 2)}"

def zdesc(z, d):
    if z["kind"] == "OB":
        return f"{z['tf']} {'demand' if z['side'] == 'demand' else 'supply'} order block {fmt(z['lo'], d)} - {fmt(z['hi'], d)}"
    return f"{z['tf']} {'sell-side' if z['side'] == 'sellside' else 'buy-side'} liquidity at {fmt(z['lo'], d)}"

def nearest(zs, price, k=3):
    below = sorted([z for z in zs if zdir(z) == "buy" and z["hi"] <= price], key=lambda z: price - z["hi"])[:k]
    above = sorted([z for z in zs if zdir(z) == "sell" and z["lo"] >= price], key=lambda z: z["lo"] - price)[:k]
    return below + above

def refresh_htf(st, asset, now, force=False):
    c = st["zones"].get(asset)
    if c and not force and time.time() - c["ts"] < 55 * 60:
        return True
    A = ASSETS[asset]
    h1 = get(A["syms"], "60m", "60d")
    d1 = get(A["syms"], "1d", "2y")
    w1 = get(A["syms"], "1wk", "5y")
    m1 = get(A["syms"], "1mo", "10y")
    if len(h1) < 60 or len(d1) < 40:
        return bool(c)
    frames = {"1H": h1, "4H": rs(h1, "4h"), "1D": d1, "1W": w1, "1M": m1, "3M": rs(m1, "QS")}
    price = float(h1["close"].iloc[-1])
    zones, levels = {}, {}
    for tf, df in frames.items():
        zones[tf] = nearest(find_zones(df, tf), price)
        if tf in ("1H", "4H", "1D", "1W") and len(df) > 12:
            H, L = swings(df, 3 if tf in ("1H", "4H") else 2)
            levels[tf] = dict(highs=[p for _, p in H][-40:], lows=[p for _, p in L][-40:])
    st["zones"][asset] = dict(ts=time.time(), atr1h=float(atr(h1).iloc[-1]), z=zones)
    st["levels"][asset] = levels
    return True

# ----------------------------------------------------------------------------
# SETUP DETECTION: approach -> sweep + rejection -> CHoCH -> retest entry
# ----------------------------------------------------------------------------
def find_rejection(sig, z, dirn, since_ts):
    sub = sig[sig.index >= pd.Timestamp(since_ts, unit="s", tz="UTC")]
    for ts, r in sub.iterrows():
        rng = r["high"] - r["low"]
        if rng <= 0:
            continue
        if dirn == "buy":
            touch = r["low"] <= z["hi"] if z["kind"] == "OB" else r["low"] < z["lo"]
            hold = (z["lo"] + z["hi"]) / 2 if z["kind"] == "OB" else z["lo"]
            wick = (min(r["open"], r["close"]) - r["low"]) / rng
            if touch and r["close"] > hold and wick >= 0.35:
                return dict(ts=ts, o=float(r["open"]), h=float(r["high"]), l=float(r["low"]), c=float(r["close"]))
        else:
            touch = r["high"] >= z["lo"] if z["kind"] == "OB" else r["high"] > z["hi"]
            hold = (z["lo"] + z["hi"]) / 2 if z["kind"] == "OB" else z["hi"]
            wick = (r["high"] - max(r["open"], r["close"])) / rng
            if touch and r["close"] < hold and wick >= 0.35:
                return dict(ts=ts, o=float(r["open"]), h=float(r["high"]), l=float(r["low"]), c=float(r["close"]))
    return None

def plan_entry(df5, rej, sig_min, dirn):
    """returns None (waiting), 'invalid', or dict(plan)"""
    pre = df5[df5.index < rej["ts"]].tail(16)
    if len(pre) < 6:
        return "invalid"
    H, L = swings(pre, 2)
    lvl = (H[-1][1] if H else float(pre["high"].tail(8).max())) if dirn == "buy" else \
          (L[-1][1] if L else float(pre["low"].tail(8).min()))
    post = df5[df5.index >= rej["ts"] + pd.Timedelta(minutes=sig_min)]
    b = None
    for ts, r in post.iterrows():
        if dirn == "buy" and r["close"] < rej["l"]:
            return "invalid"
        if dirn == "sell" and r["close"] > rej["h"]:
            return "invalid"
        if (dirn == "buy" and r["close"] > lvl) or (dirn == "sell" and r["close"] < lvl):
            b = ts
            break
    if b is None:
        return None
    seg = df5[(df5.index >= rej["ts"]) & (df5.index <= b)]
    hi, lo = seg["high"].values, seg["low"].values
    fvg, entry = None, lvl
    for k in range(2, len(seg)):
        if dirn == "buy" and lo[k] > hi[k - 2]:
            fvg = (float(hi[k - 2]), float(lo[k]))
        if dirn == "sell" and hi[k] < lo[k - 2]:
            fvg = (float(hi[k]), float(lo[k - 2]))
    last_close = float(seg["close"].iloc[-1])
    if fvg:
        e = fvg[1] if dirn == "buy" else fvg[0]
        if (dirn == "buy" and rej["l"] < e < last_close) or (dirn == "sell" and last_close < e < rej["h"]):
            entry = e
        else:
            fvg = None
    sl = rej["l"] if dirn == "buy" else rej["h"]
    if (dirn == "buy" and entry <= sl) or (dirn == "sell" and entry >= sl):
        return "invalid"
    return dict(choch_ts=b, level=lvl, entry=float(entry), sl=float(sl), fvg=bool(fvg))

def pick_target(st, asset, dirn, entry, sl, scope):
    lv = st["levels"].get(asset, {})
    tfs = ["1H", "4H"] if scope == "scalp" else ["1H", "4H", "1D", "1W"]
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    cands = []
    for tf in tfs:
        for p in lv.get(tf, {}).get("highs" if dirn == "buy" else "lows", []):
            if (dirn == "buy" and p > entry) or (dirn == "sell" and p < entry):
                cands.append((abs(p - entry), p, tf))
    cands.sort()
    need = CFG["main_min_rr"] if scope == "main" else CFG["scalp_min_rr"]
    for dist, p, tf in cands:
        if dist / risk >= need:
            return dict(tp=p, tf=tf, rr=dist / risk, paper=False)
    if scope == "scalp" and cands:
        dist, p, tf = cands[0]
        return dict(tp=p, tf=tf, rr=dist / risk, paper=True)
    return None

def corr_check(asset, dirn):
    A = ASSETS[asset]
    df = closed(get(A["corr"], "5m", "5d"), 5)
    if len(df) < 40:
        return None, f"{A['corr_name']} data unavailable"
    ema = df["close"].ewm(span=20).mean().iloc[-1]
    px, back = df["close"].iloc[-1], df["close"].iloc[-13]
    ok = (px > ema and px > back) if dirn == "buy" else (px < ema and px < back)
    return ok, f"{A['corr_name']} {'confirms' if ok else 'NOT confirming'}"

def kind_of(tf):
    return "Scalp" if tf == "1H" else "Intraday" if tf == "4H" else "Swing"

def open_real(st, asset):
    return [t for t in st["trades"] if t["asset"] == asset and t["status"] == "open" and not t["paper"]]

def news_ctx(asset, n=2):
    hs = headlines(ASSETS[asset]["query"], 6, n)
    return "\n".join("• " + esc(t) for t, _ in hs)

def process_asset(st, asset, now):
    A = ASSETS[asset]
    d = A["dec"]
    df5 = closed(get(A["syms"], "5m", "5d"), 5)
    if len(df5) < 80:
        return
    if (utcnow() - df5.index[-1]).total_seconds() > 25 * 60:
        return                                   # market closed / stale feed
    if not refresh_htf(st, asset, now):
        return
    zc = st["zones"][asset]
    price = float(df5["close"].iloc[-1])
    a5 = atr(df5)
    volatile = a5.iloc[-1] > CFG["volatile_ratio"] * a5.tail(150).median()
    sig, sig_min = (rs(df5, "15min"), 15) if volatile else (df5, 5)
    sig = closed(sig, sig_min)

    # 1) approach -> start watching
    for scope, tfs in (("main", ["4H", "1D", "1W", "1M", "3M"]), ("scalp", ["1H"])):
        for tf in tfs:
            for z in zc["z"].get(tf, []):
                zi = zid(asset, z)
                if zi in st["watch"] or f"zdone|{zi}" in st["sent"]:
                    continue
                dirn = zdir(z)
                dist = (price - z["hi"]) if dirn == "buy" else (z["lo"] - price)
                if 0 <= dist <= CFG[f"approach_atr_{scope}"] * zc["atr1h"]:
                    st["watch"][zi] = dict(asset=asset, scope=scope, dir=dirn, z=z, stage="approach",
                                           since=time.time() - 3600, start=time.time())
                    if scope == "main":
                        ok, ftxt = corr_check(asset, dirn)
                        tg(f"👀 <b>{esc(A['name'])}: price approaching a zone</b>\n"
                           f"{esc(zdesc(z, d))}\nPrice now {fmt(price, d)} | looking for a "
                           f"<b>{dirn.upper()}</b> setup ({kind_of(tf)})\n"
                           f"Filter: {esc(ftxt)}\nNext: sweep + rejection candle, then CHoCH.")

    # 2) advance every watch for this asset
    for wid, w in list(st["watch"].items()):
        if w["asset"] != asset:
            continue
        max_h = 72 if w["scope"] == "main" else 12
        if time.time() - w["start"] > max_h * 3600:
            st["watch"].pop(wid, None)
            st["sent"][f"zdone|{wid}"] = time.time()
            continue
        advance(st, wid, w, df5, sig, sig_min, now)

def labels_for(st, asset, w, dirn):
    kind = kind_of(w["z"]["tf"])
    same = [t for t in open_real(st, asset) if t["dir"] == dirn and t["kind"] == kind]
    opp = [t for t in open_real(st, asset) if t["dir"] != dirn]
    held = st["hold"].get(asset, {}).get("type", "N")
    add = bool(same) or {"S": "Scalp", "W": "Swing", "I": "Intraday"}.get(held) == kind
    if same and same[0].get("adds", 0) >= CFG["add_lot_cap"]:
        return None, opp, "cap"
    return ("ADD LOT / RE-ENTRY" if add else ""), opp, same[0] if same else None

def advance(st, wid, w, df5, sig, sig_min, now):
    asset, z, dirn = w["asset"], w["z"], w["dir"]
    A = ASSETS[asset]
    d = A["dec"]
    main = w["scope"] == "main"
    kind = kind_of(z["tf"])

    if w["stage"] == "approach":
        rej = find_rejection(sig, z, dirn, w["since"])
        if not rej:
            return
        w["rej"] = dict(rej, ts=str(rej["ts"]))
        w["rej_tf"] = sig_min
        w["stage"] = "rej"
        if main:
            ok, ftxt = corr_check(asset, dirn)
            tg(f"🎯 <b>{esc(A['name'])}: sweep + rejection ({dirn.upper()})</b>\n{esc(zdesc(z, d))}\n"
               f"{sig_min}m rejection candle at {hhmm(rej['ts'])} | SL would sit at "
               f"{fmt(rej['l'] if dirn == 'buy' else rej['h'], d)}\nFilter: {esc(ftxt)}\n"
               f"Next: CHoCH on 5m, then entry on the retest.")
        return

    rej = dict(w["rej"], ts=pd.Timestamp(w["rej"]["ts"]))
    if w["stage"] == "rej":
        plan = plan_entry(df5, rej, w["rej_tf"], dirn)
        if plan is None:
            return
        if plan == "invalid":
            return drop(st, wid)
        tgt = pick_target(st, asset, dirn, plan["entry"], plan["sl"], w["scope"])
        if not tgt:
            if main:
                tg(f"⏭ <b>{esc(A['name'])}</b>: CHoCH formed but no higher-timeframe target gives "
                   f"1:{CFG['main_min_rr']}+ RR, so this setup is skipped.")
            return drop(st, wid)
        label, opp, parent = labels_for(st, asset, w, dirn)
        if label is None:
            return drop(st, wid)
        w.update(plan=dict(plan, choch_ts=str(plan["choch_ts"])), tgt=tgt, stage="choch")
        if opp and not tgt["paper"] and once(st, f"revrisk|{wid}"):
            tg(f"⚠️ <b>REVERSAL RISK - {esc(A['name'])}</b>\nA {dirn.upper()} setup (CHoCH) formed at "
               f"{esc(zdesc(z, d))} while you may hold a {opp[0]['dir'].upper()}.\n"
               f"Consider squaring off / protecting. A separate opposite-signal alert comes only if the "
               f"reversal is confirmed with RR 1:5+.\n{news_ctx(asset)}")
        if main:
            ok, ftxt = corr_check(asset, dirn)
            tg(f"🔔 <b>{esc(A['name'])}: CHoCH confirmed ({dirn.upper()} - {kind})</b>\n"
               f"Planned entry {fmt(plan['entry'], d)} ({'FVG' if plan['fvg'] else 'broken CHoCH level'}) "
               f"| SL {fmt(plan['sl'], d)} | target {fmt(tgt['tp'], d)} ({tgt['tf']}) | RR 1:{tgt['rr']:.1f}\n"
               f"Waiting for the 5m retest. Filter: {esc(ftxt)}")
        return

    if w["stage"] == "choch":
        p, tgt = w["plan"], w["tgt"]
        post = df5[df5.index > pd.Timestamp(p["choch_ts"])]
        if len(post) > 60:
            return drop(st, wid)
        for ts, r in post.iterrows():
            if (dirn == "buy" and r["low"] <= p["sl"]) or (dirn == "sell" and r["high"] >= p["sl"]):
                return drop(st, wid)
            hit = r["low"] <= p["entry"] if dirn == "buy" else r["high"] >= p["entry"]
            if hit:
                return open_trade(st, wid, w, ts, df5, now)

def drop(st, wid):
    st["watch"].pop(wid, None)
    st["sent"][f"zdone|{wid}"] = time.time()

def open_trade(st, wid, w, ts, df5, now):
    asset, z, dirn = w["asset"], w["z"], w["dir"]
    A = ASSETS[asset]
    d = A["dec"]
    p, tgt = w["plan"], w["tgt"]
    kind = kind_of(z["tf"])
    label, opp, parent = labels_for(st, asset, w, dirn)
    if label is None:
        return drop(st, wid)
    if opp and tgt["rr"] < 5:
        return drop(st, wid)                     # opposite signals need RR 1:5+
    ok, ftxt = corr_check(asset, dirn)
    t = dict(id=f"{asset}-{epoch(ts)}", asset=asset, dir=dirn, kind=kind, scope=w["scope"], zone=wid,
             zone_desc=zdesc(z, d), entry=p["entry"], sl=p["sl"], tp=tgt["tp"], tp_tf=tgt["tf"],
             rr=tgt["rr"], entry_ts=epoch(ts), last_ts=epoch(ts), paper=tgt["paper"], status="open",
             peak=0.0, t50=None, clean=True, runmin=None, adds=0, filter_ok=ok, filter_txt=ftxt,
             fvg=p["fvg"], rej_tf=w["rej_tf"], rej_ts=w["rej"]["ts"], choch_ts=p["choch_ts"],
             rev_sent=False, ask1=False, taken=None, label=label, opp=bool(opp))
    t["logic"] = (f"{zdesc(z, d)}; {w['rej_tf']}m sweep+rejection {hhmm(w['rej']['ts'])}; 5m CHoCH "
                  f"{hhmm(p['choch_ts'])}; entry on {'FVG' if p['fvg'] else 'CHoCH-level'} retest; {ftxt}")
    st["trades"].append(t)
    if parent and isinstance(parent, dict):
        parent["adds"] = parent.get("adds", 0) + 1
    drop(st, wid)
    if t["paper"]:
        return                                   # silent paper trade (RR below scalp minimum)
    if ok is False:
        t["paper"] = True                        # Silver/ETH disagrees: track silently only
        return
    head = {"Scalp": "SCALPING ONLY", "Swing": "SWING", "Intraday": "INTRADAY"}[kind]
    icon = "🟢" if dirn == "buy" else "🔴"
    mgmt = ("" if kind == "Scalp" else
            "\nManage: 50% base entry, scale on pullbacks, SL to breakeven, swings: book 50% and carry 50%.")
    extra = ""
    if opp:
        extra = f"\n<b>OPPOSITE SIGNAL (reversal confirmed, RR 1:5+)</b>\n{news_ctx(asset)}"
    tg(f"{icon} <b>{esc(A['name'])} {dirn.upper()} - {head}</b>{(' | <b>' + label + '</b>') if label else ''}\n"
       f"Zone: {esc(t['zone_desc'])}\n"
       f"Entry: <b>{fmt(t['entry'], d)}</b> (5m retest, {'FVG' if p['fvg'] else 'broken CHoCH level'})\n"
       f"SL: <b>{fmt(t['sl'], d)}</b> ({'rejection candle low' if dirn == 'buy' else 'rejection candle high'})\n"
       f"Target: <b>{fmt(t['tp'], d)}</b> ({tgt['tf']} {'high' if dirn == 'buy' else 'low'}) | RR 1:{t['rr']:.1f}\n"
       f"Filter: {esc(ftxt)}{mgmt}{extra}\n\n"
       f"Do you already hold a {esc(A['name'])} trade? Tap your current type:", hold_kb(asset))

# ----------------------------------------------------------------------------
# TRADE TRACKING (crypto / gold)
# ----------------------------------------------------------------------------
def update_trades(st, asset, now):
    A = ASSETS[asset]
    df5 = closed(get(A["syms"], "5m", "5d"), 5)
    if df5.empty:
        return
    d = A["dec"]
    for t in [x for x in st["trades"] if x["asset"] == asset and x["status"] == "open"]:
        buy = t["dir"] == "buy"
        R = abs(t["entry"] - t["sl"])
        span = abs(t["tp"] - t["entry"])
        sub = df5[df5.index > pd.Timestamp(t["last_ts"], unit="s", tz="UTC")]
        for ts, r in sub.iterrows():
            t["last_ts"] = epoch(ts)
            lo, hi = float(r["low"]), float(r["high"])
            if t["runmin"] is None:
                t["runmin"] = lo if buy else hi
            else:
                t["runmin"] = min(t["runmin"], lo) if buy else max(t["runmin"], hi)
            sl_hit = lo <= t["sl"] if buy else hi >= t["sl"]
            tp_hit = hi >= t["tp"] if buy else lo <= t["tp"]
            if not sl_hit:
                fav = (hi - t["entry"]) if buy else (t["entry"] - lo)
                prog = max(0.0, fav / span) if span else 0
                if prog > t["peak"]:
                    t["peak"] = prog
                    tol = 0.25 * R
                    t["clean"] = (t["runmin"] >= t["entry"] - tol) if buy else (t["runmin"] <= t["entry"] + tol)
                    if prog >= 0.5 and t["t50"] is None:
                        t["t50"] = (epoch(ts) + 300 - t["entry_ts"]) / 60
            if sl_hit:
                return_close(st, t, "loss", ts, now)
                break
            if tp_hit:
                t["peak"] = 1.0
                return_close(st, t, "win", ts, now)
                break
            # reversal sign after 50% progress on RR 5+ trades
            if t["peak"] >= 0.5 and t["rr"] >= 5 and not t["rev_sent"] and not t["paper"]:
                prev = df5[df5.index < ts].tail(4)
                if len(prev) == 4 and ((buy and r["close"] < prev["low"].min()) or
                                       (not buy and r["close"] > prev["high"].max())):
                    t["rev_sent"] = True
                    tg(f"⚠️ <b>{esc(A['name'])} {t['kind']}: reversal sign</b>\n"
                       f"Trade reached {t['peak'] * 100:.0f}% of target, then price broke the last 4 candles "
                       f"against you. Consider exiting or protecting profit (entry {fmt(t['entry'], d)}).")
        # ask once if the alert was executed (1 hour after entry)
        if (t["status"] == "open" and not t["paper"] and not t["ask1"] and
                time.time() - t["entry_ts"] > 3600):
            t["ask1"] = True
            tg(f"❓ {esc(A['name'])} {t['kind']} alert from {hhmm(pd.Timestamp(t['entry_ts'], unit='s', tz='UTC'))} "
               f"is still open (no SL or target yet). Did you take it?",
               {"inline_keyboard": [[{"text": "Yes, in trade", "callback_data": f"x|{t['id']}|Y"},
                                     {"text": "Not taken", "callback_data": f"x|{t['id']}|N"}]]})

def fail_reason(t, dur):
    if t["peak"] >= 0.5:
        return (f"reached {t['peak'] * 100:.0f}% of the target, then reversed to SL - "
                f"consider booking part profit / exiting on a reversal sign")
    if dur <= 15:
        return "stopped out within 15 minutes - retest wick was deeper than the SL (or a news spike)"
    if t.get("filter_ok") is False:
        return f"{t['filter_txt']} at entry - correlation filter was against the trade"
    if t["peak"] < 0.1:
        return "price never moved in favour after the retest - zone likely did not hold"
    return "structure failed after a small move in favour - momentum faded before the target"

def return_close(st, t, res, ts, now):
    dur = (epoch(ts) + 300 - t["entry_ts"]) / 60
    t["status"], t["result"], t["dur"] = "closed", res, dur
    scalp = t["kind"] == "Scalp"                 # speed rules apply to scalps only
    t["fast"] = scalp and dur <= CFG["fast_minutes"]
    t["usable"] = (not scalp) or dur < CFG["slow_minutes"] or \
                  (t["t50"] is not None and (res == "loss" or t["t50"] <= 60))
    t["special"] = (res == "loss" and t["rr"] >= 5 and t["peak"] >= 0.5 and t["clean"])
    t["reason"] = fail_reason(t, dur) if res == "loss" else ""
    st["trades"].remove(t)
    st["hist"].append(t)
    if res == "win" and not t["paper"]:
        st["post"][t["asset"]] = dict(dir=t["dir"], tp=t["tp"], R=abs(t["entry"] - t["sl"]),
                                      last_ask=time.time(), ts=time.time())
    if t["paper"]:
        return
    A = ASSETS[t["asset"]]
    d = A["dec"]
    flag = "⚡ FAST (under 90 min)" if t["fast"] else ("⛔ 2h+ scalp, excluded from future logic" if not t["usable"] else "")
    if res == "win":
        tg(f"✅ <b>{esc(A['name'])} {t['kind']} {t['dir'].upper()} - TARGET HIT</b> (+{t['rr']:.1f}R)\n"
           f"Duration {dur:.0f} min {flag}\nWhy it worked: {esc(t['logic'])}")
    else:
        sp = "\n🔎 SPECIAL WATCH: RR 1:5+ trade that reached 50%+ before reversing without dipping back - " \
             "priority review." if t["special"] else ""
        tg(f"❌ <b>{esc(A['name'])} {t['kind']} {t['dir'].upper()} - STOPPED OUT</b> (-1R)\n"
           f"Duration {dur:.0f} min {flag}\nFailure reason: {esc(t['reason'])}\nSetup: {esc(t['logic'])}{sp}")
    maybe_review(st, t["asset"], t["kind"])

def post_target_checks(st, asset):
    p = st["post"].get(asset)
    if not p:
        return
    A = ASSETS[asset]
    df5 = closed(get(A["syms"], "5m", "5d"), 5)
    if df5.empty:
        return
    px = float(df5["close"].iloc[-1])
    beyond = (px - p["tp"]) if p["dir"] == "buy" else (p["tp"] - px)
    if beyond < 0 or time.time() - p["ts"] > 14 * 86400:
        st["post"].pop(asset, None)
        return
    if beyond >= 0.5 * p["R"] and time.time() - p["last_ask"] > 2 * 3600:
        p["last_ask"] = time.time()
        tg(f"❓ {esc(A['name'])}: price keeps running {p['dir'].upper()} past the target "
           f"({fmt(px, A['dec'])}). Are you still holding?",
           {"inline_keyboard": [[{"text": "Still holding", "callback_data": f"p|{asset}|Y"},
                                 {"text": "Closed", "callback_data": f"p|{asset}|C"}]]})

# ----------------------------------------------------------------------------
# REVIEWS
# ----------------------------------------------------------------------------
def stats(trs):
    n = len(trs)
    if not n:
        return None
    w = [t for t in trs if t["result"] == "win"]
    return dict(n=n, wins=len(w), wr=100 * len(w) / n,
                avgR=(sum(t["rr"] for t in w) - (n - len(w))) / n,
                fast=sum(1 for t in trs if t.get("fast")),
                slow=sum(1 for t in trs if not t.get("usable", True)),
                special=sum(1 for t in trs if t.get("special")))

def review_text(title, trs):
    out = [f"📊 <b>{title}</b>"]
    for scope, label in (("main", "Swing / Intraday"), ("scalp", "Scalping")):
        for paper in (False, True):
            sub = [t for t in trs if t["scope"] == scope and bool(t["paper"]) == paper]
            s = stats(sub)
            if s:
                out.append(f"{label}{' (paper)' if paper else ''}: {s['n']} trades, {s['wins']} wins "
                           f"({s['wr']:.0f}%), net {s['avgR']:+.2f}R per trade, fast {s['fast']}, "
                           f"2h+ excluded {s['slow']}, special-watch {s['special']}")
    real = [t for t in trs if not t["paper"]]
    if real:
        hrs = {}
        for t in real:
            h = pd.Timestamp(t["entry_ts"], unit="s", tz="UTC").tz_convert(IST).hour
            hrs.setdefault(h, []).append(t["result"] == "win")
        best = sorted(hrs.items(), key=lambda kv: (-sum(kv[1]) / len(kv[1]), -len(kv[1])))[:2]
        out.append("Best entry hours (IST): " + ", ".join(f"{h:02d}h ({100 * sum(v) / len(v):.0f}%)" for h, v in best))
        s = stats(real)
        if s["n"] >= 5 and s["wr"] < 40:
            out.append("Suggestion: win rate is low - consider requiring Silver/ETH confirmation at every stage.")
    if len(out) == 1:
        out.append("No closed trades in this period.")
    return "\n".join(out)

def maybe_review(st, asset, kind):
    if kind != "Scalp":
        return
    sc = [t for t in st["hist"] if t["asset"] == asset and t["scope"] == "scalp"]
    last = st["review"].get(asset, 0)
    if len(sc) - last >= 30:
        st["review"][asset] = len(sc)
        tg(review_text(f"{ASSETS[asset]['name']} scalping review (last 30+ scalps)", sc[last:]))

def run_reviews(st, now):
    cut_w = time.time() - 7 * 86400
    if due(st, "weekly", now, 20, 0, days=[6]):
        tg(review_text("Weekly summary", [t for t in st["hist"] if t["entry_ts"] > cut_w]))
    if now.day == 1 and due(st, "monthly", now, 10, 0):
        cut_m = time.time() - 31 * 86400
        tg(review_text("Monthly review", [t for t in st["hist"] if t["entry_ts"] > cut_m]))

# ----------------------------------------------------------------------------
# STATUS REPORTS / BRIEFINGS (Gold + BTC)
# ----------------------------------------------------------------------------
def arrow_pct(p):
    return "n/a" if p is None else f"{'▲' if p >= 0 else '▼'} {abs(p):.2f}%"

def okx_oi():
    try:
        r = requests.get("https://www.okx.com/api/v5/public/open-interest",
                         params={"instType": "SWAP", "instId": "BTC-USDT-SWAP"}, timeout=15, headers=UA).json()
        return float(r["data"][0]["oiUsd"])
    except Exception:
        return None

def status_report(st, asset, now, title):
    A = ASSETS[asset]
    d = A["dec"]
    df5 = get(A["syms"], "5m", "5d")
    if df5.empty:
        return
    stale = (utcnow() - df5.index[-1]).total_seconds() > 40 * 60
    price = float(df5["close"].iloc[-1])
    refresh_htf(st, asset, now)
    zc = st["zones"].get(asset)
    h1 = get(A["syms"], "60m", "60d")
    st1, st4 = structure(h1), structure(rs(h1, "4h"))
    score = 2 * st4 + st1
    corr = get(A["corr"], "60m", "10d")
    cch = None
    if len(corr) > 30:
        cch = float((corr["close"].iloc[-1] / corr["close"].iloc[-25] - 1) * 100)
        score += 1 if cch > 0.2 else -1 if cch < -0.2 else 0
    dxy, tnx, oil = chg_pct("DX-Y.NYB"), chg_pct("^TNX"), chg_pct("CL=F")
    if dxy is not None:
        score += -1 if dxy > 0.3 else 1 if dxy < -0.3 else 0
    oi_txt = ""
    if asset == "GOLD":
        if tnx is not None:
            score += -1 if tnx > 2 else 1 if tnx < -2 else 0
    else:
        oi = okx_oi()
        if oi:
            prev = st["oi"].get("BTC")
            st["oi"]["BTC"] = oi
            pchg = float((df5["close"].iloc[-1] / df5["close"].iloc[-25] - 1) * 100) if len(df5) > 25 else 0
            if prev:
                ch = (oi / prev - 1) * 100
                oi_txt = f"\nOpen interest: {ch:+.2f}% vs last report"
                if ch > 0.5:
                    score += 1 if pchg > 0 else -1
    nsc = news_score(A["query"], asset)
    score += nsc
    label = ("Strong bullish" if score >= 4 else "Bullish" if score >= 2 else "Neutral / mixed" if score > -2
             else "Bearish" if score > -4 else "Strong bearish")
    lines = [f"📡 <b>{esc(A['name'])} - {title}</b>",
             f"Price {fmt(price, d)}{' (market closed / data old)' if stale else ''}",
             f"Structure: 4H {'bullish' if st4 > 0 else 'bearish' if st4 < 0 else 'range'}, "
             f"1H {'bullish' if st1 > 0 else 'bearish' if st1 < 0 else 'range'}"]
    if zc:
        lines.append("<b>Nearest zones</b>")
        for tf in ("1H", "4H", "1D", "1W", "1M"):
            zs = zc["z"].get(tf, [])
            up = [z for z in zs if zdir(z) == "sell"]
            dn = [z for z in zs if zdir(z) == "buy"]
            seg = []
            if up:
                seg.append(f"above {fmt(up[0]['lo'], d)}")
            if dn:
                seg.append(f"below {fmt(dn[0]['hi'], d)}")
            if seg:
                lines.append(f"{tf}: " + ", ".join(seg))
    lines.append(f"<b>Drivers</b>\n{A['corr_name']} 24h {arrow_pct(cch)} | Dollar {arrow_pct(dxy)} | "
                 f"US 10y {arrow_pct(tnx)} | Oil {arrow_pct(oil)}{oi_txt}")
    hs = headlines(A["query"], 6, 3)
    if hs:
        lines.append("<b>News</b>\n" + "\n".join("• " + esc(t) for t, _ in hs))
    lines.append(f"<b>Overall bias: {label}</b> (score {score:+d}). No setup is active unless an alert says so.")
    tg("\n".join(lines))

def run_crypto_gold(st, now):
    for asset in ASSETS:
        try:
            update_trades(st, asset, now)
            process_asset(st, asset, now)
            update_trades(st, asset, now)
            post_target_checks(st, asset)
        except Exception:
            err(st, f"asset {asset}")
    h = now.hour
    for hh in (5, 17):
        if due(st, f"brief{hh}", now, hh, 0, window=14):
            for asset in ASSETS:
                try:
                    status_report(st, asset, now, "global briefing")
                except Exception:
                    err(st, "briefing")
    if h % 2 == 0 and due(st, f"status{h}", now, h, 0, window=14):
        for asset in ASSETS:
            try:
                status_report(st, asset, now, "2-hour status")
            except Exception:
                err(st, "status")

# ----------------------------------------------------------------------------
# INDIAN INDICES (Nifty / Sensex) - index levels only, free Yahoo data
# ----------------------------------------------------------------------------
def india_cues():
    cues = {}
    for name, sym in [("S&P fut", "ES=F"), ("Nasdaq fut", "NQ=F"), ("Nikkei", "^N225"), ("Hang Seng", "^HSI"),
                      ("Dollar idx", "DX-Y.NYB"), ("Crude", "CL=F"), ("Gold", "GC=F"), ("USD/INR", "INR=X")]:
        cues[name] = chg_pct(sym)
    return cues

def india_bias(cues, ns):
    g = lambda k: cues.get(k) or 0
    s = 0
    for k in ("S&P fut", "Nasdaq fut"):
        s += 1 if g(k) > 0.3 else -1 if g(k) < -0.3 else 0
    for k in ("Nikkei", "Hang Seng"):
        s += 1 if g(k) > 0.5 else -1 if g(k) < -0.5 else 0
    s += -1 if g("Crude") > 1.5 else 1 if g("Crude") < -1.5 else 0
    s += -1 if g("USD/INR") > 0.3 else 1 if g("USD/INR") < -0.3 else 0
    s += -1 if g("Dollar idx") > 0.4 else 1 if g("Dollar idx") < -0.4 else 0
    return s + ns

def bias_label(s):
    return "bullish" if s >= 2 else "bearish" if s <= -2 else "neutral"

def prev_day(sym, today):
    dd = yf(sym, "1d", "10d")
    if dd.empty:
        return None
    dd = dd.copy()
    dd.index = dd.index.tz_convert(IST).date
    dd = dd[dd.index < today]
    if dd.empty:
        return None
    r = dd.iloc[-1]
    return dict(o=float(r["open"]), h=float(r["high"]), l=float(r["low"]), c=float(r["close"]))

def next_expiry(now, wd):
    return now.date() + timedelta(days=(wd - now.weekday()) % 7)

def india_premarket(st, now):
    cues = india_cues()
    ns = news_score("Sensex OR Nifty OR India markets OR Trump OR crude OR rupee", "INDIA", 8)
    bias = india_bias(cues, ns)
    st["india"]["bias"] = dict(score=bias, date=str(now.date()))
    cue_txt = " | ".join(f"{k} {arrow_pct(v)}" for k, v in cues.items() if v is not None)
    hs = headlines("Sensex OR Nifty OR India markets OR Trump OR crude OR rupee", 8, 3)
    for key, I in INDIA.items():
        pdv = prev_day(I["sym"], now.date())
        if not pdv:
            continue
        exp = next_expiry(now, I["expiry_wd"])
        extra = "\n⚡ <b>Expiry day today</b> - moves can be sharp." if exp == now.date() else ""
        tg(f"🌅 <b>{I['name']} pre-market ({now:%d %b})</b>\n"
           f"Previous day: O {fmt(pdv['o'])} | H {fmt(pdv['h'])} | L {fmt(pdv['l'])} | C {fmt(pdv['c'])}\n"
           f"Key levels: PDH {fmt(pdv['h'])}, PDL {fmt(pdv['l'])}\n"
           f"Global cues: {cue_txt}\n"
           + ("<b>News</b>\n" + "\n".join("• " + esc(t) for t, _ in hs) + "\n" if hs else "")
           + f"<b>Bias: {bias_label(bias)}</b> (score {bias:+d})\n"
           f"Plan: wait for the open, first 15-minute candle (9:15-9:30), then 1-minute entries.{extra}")

def strike_text(key, price, side):
    I = INDIA[key]
    atm = round(price / I["step"]) * I["step"]
    otm = atm + I["step"] if side == "CE" else atm - I["step"]
    return f"{I['name']} {atm:.0f} {side} (ATM) or {otm:.0f} {side} (one OTM)"

def india_signal(df, d, I, key, last_sig=0):
    """returns dict(side, price, ts, why) or None. df: today's 1-min candles (IST index)"""
    pdv, H15, L15 = d["prev"], d.get("h15"), d.get("l15")
    if H15 is None:
        return None
    after = df[df.index.time >= datetime(2000, 1, 1, 9, 30).time()]
    if last_sig:
        after = after[after.index > pd.Timestamp(last_sig, unit="s", tz="UTC")]
    cl = after["close"].values
    n = len(after)
    # Rule A: day opened outside yesterday's range -> 15m high/low break that holds 5 minutes
    if d["open_type"] in ("above", "below"):
        for i in range(4, n):
            w = cl[i - 4:i + 1]
            if (w < L15).all():
                return dict(side="PE", price=float(cl[i]), ts=after.index[i], why="15m low broken and held 5 minutes")
            if (w > H15).all():
                return dict(side="CE", price=float(cl[i]), ts=after.index[i], why="15m high broken and held 5 minutes")
        return None
    # Rule B: opened inside yesterday's range -> wait for a PDH/PDL sweep, then continuation or rejection
    hi, lo, op = after["high"].values, after["low"].values, after["open"].values
    for i in range(n):
        if hi[i] > pdv["h"]:
            win = cl[i:i + 5]
            if len(win) >= 1 and (cl[i:i + 5] < pdv["h"]).any():
                j = i + int(np.argmax(cl[i:i + 5] < pdv["h"]))
                return dict(side="PE", price=float(cl[j]), ts=after.index[j], why="PDH swept then rejected (closed back below)")
            if len(win) == 5 and (win > pdv["h"]).all():
                return dict(side="CE", price=float(cl[i + 4]), ts=after.index[i + 4], why="PDH swept and held 5 minutes (continuation)")
        if lo[i] < pdv["l"]:
            win = cl[i:i + 5]
            if len(win) >= 1 and (cl[i:i + 5] > pdv["l"]).any():
                j = i + int(np.argmax(cl[i:i + 5] > pdv["l"]))
                return dict(side="CE", price=float(cl[j]), ts=after.index[j], why="PDL swept then rejected (closed back above)")
            if len(win) == 5 and (win < pdv["l"]).all():
                return dict(side="PE", price=float(cl[i + 4]), ts=after.index[i + 4], why="PDL swept and held 5 minutes (continuation)")
    return None

def india_fast(df, d, bias):
    if len(df) < 6:
        return None
    first5 = df.iloc[:5]
    c6 = df.iloc[5]
    if (utcnow() - c6.name.tz_convert("UTC")).total_seconds() > 20 * 60:
        return None
    gap = d.get("gap", 0)
    if bias >= 2 and gap >= 0 and c6["high"] > first5["high"].max():
        return dict(side="CE", price=float(first5["high"].max()), ts=c6.name, why="fast entry: 6th 1m candle broke the 5m high, all factors bullish")
    if bias <= -2 and gap <= 0 and c6["low"] < first5["low"].min():
        return dict(side="PE", price=float(first5["low"].min()), ts=c6.name, why="fast entry: 6th 1m candle broke the 5m low, all factors bearish")
    return None

def india_open_trade(st, key, d, sig, df, now, tag=""):
    I = INDIA[key]
    sl_pts = CFG[I["sl"]]
    side = sig["side"]
    entry = sig["price"]
    last = float(df["close"].iloc[-1])
    sgn = 1 if side == "CE" else -1
    sl = entry - sgn * sl_pts
    t1, t2 = entry + sgn * 3 * sl_pts, entry + sgn * 5 * sl_pts
    if (sgn > 0 and (last <= sl or last >= t1)) or (sgn < 0 and (last >= sl or last <= t1)):
        return                                   # already stopped / already past target - too late
    tr = dict(key=key, side=side, entry=entry, sl=sl, t1=t1, t2=t2, sgn=sgn, ts=epoch(sig["ts"]),
              last_ts=epoch(sig["ts"]), be=False, status="open", why=sig["why"])
    d["trades"].append(tr)
    d["n"] += 1
    fresh = (utcnow() - sig["ts"].tz_convert("UTC")).total_seconds() / 60
    tg(f"{'🟢' if side == 'CE' else '🔴'} <b>{I['name']}: BUY {side} entry{tag}</b> (part 3 of 3)\n"
       f"Why: {esc(sig['why'])}\n"
       f"Index entry level: <b>{fmt(entry)}</b> (signal candle {sig['ts']:%H:%M}, price now {fmt(last)})\n"
       f"Strike: {strike_text(key, entry, side)} | weekly expiry {next_expiry(now, I['expiry_wd']):%a %d %b}\n"
       f"SL (index): <b>{fmt(sl)}</b> ({sl_pts} points)\n"
       f"Target 1: <b>{fmt(t1)}</b> (1:3) | Target 2: <b>{fmt(t2)}</b> (1:5) | then trail, no cap\n"
       f"Move SL to entry after Target 1.\n"
       f"⚠️ Free data can lag a few minutes ({fresh:.0f} min since the signal candle). Check the live chart and "
       f"option premium before entering. Levels are on the index, not the option price.")

def india_update(st, key, d, df, now):
    I = INDIA[key]
    for tr in [t for t in d["trades"] if t["status"] == "open"]:
        sub = df[df.index > pd.Timestamp(tr["last_ts"], unit="s", tz="UTC")]
        for ts, r in sub.iterrows():
            tr["last_ts"] = epoch(ts)
            sgn = tr["sgn"]
            sl = tr["entry"] if tr["be"] else tr["sl"]
            stop = r["low"] <= sl if sgn > 0 else r["high"] >= sl
            hit1 = r["high"] >= tr["t1"] if sgn > 0 else r["low"] <= tr["t1"]
            hit2 = r["high"] >= tr["t2"] if sgn > 0 else r["low"] <= tr["t2"]
            if stop:
                tr["status"] = "closed"
                tr["res"] = "be" if tr["be"] else "loss"
                tg(f"{'⚪' if tr['be'] else '❌'} <b>{I['name']} {tr['side']}: "
                   f"{'stopped at entry (after Target 1)' if tr['be'] else 'SL hit'}</b> at {ts:%H:%M}")
                break
            if hit2:
                tr["status"], tr["res"] = "closed", "t2"
                tg(f"🏆 <b>{I['name']} {tr['side']}: Target 2 (1:5) hit</b> at {ts:%H:%M}. Trail the rest.")
                break
            if hit1 and not tr["be"]:
                tr["be"] = True
                tg(f"✅ <b>{I['name']} {tr['side']}: Target 1 (1:3) hit</b> at {ts:%H:%M}. SL moves to entry.")
        ex = CFG["india_exit"]
        if tr["status"] == "open" and (now.hour, now.minute) >= ex:
            tr["status"], tr["res"] = "closed", "time"
            last = float(df["close"].iloc[-1])
            pts = (last - tr["entry"]) * tr["sgn"]
            tg(f"⏰ <b>{I['name']} {tr['side']}: time exit</b> - close any intraday trade now. "
               f"Index {pts:+.0f} points from entry.")

def india_session(st, key, now):
    I = INDIA[key]
    ind = st["india"]
    d = ind.setdefault(key, {})
    if d.get("date") != str(now.date()):
        d.clear()
        d.update(date=str(now.date()), sent={}, trades=[], n=0)
    raw = closed(yf(I["sym"], "1m", "1d"), 1)
    if raw.empty:
        return
    df = raw.copy()
    df.index = df.index.tz_convert(IST)
    df = df[(df.index.date == now.date()) & (df.index.time >= datetime(2000, 1, 1, 9, 15).time())
            & (df.index.time <= datetime(2000, 1, 1, 15, 30).time())]
    if df.empty or (utcnow() - df.index[-1].tz_convert("UTC")).total_seconds() > 20 * 60:
        return
    if "prev" not in d:
        pdv = prev_day(I["sym"], now.date())
        if not pdv:
            return
        d["prev"] = pdv
    pdv = d["prev"]
    bias = ind.get("bias", {}).get("score", 0) if ind.get("bias", {}).get("date") == str(now.date()) else 0
    india_update(st, key, d, df, now)

    # part 1: setup forming (day classification)
    if "cls" not in d:
        op = float(df["open"].iloc[0])
        gap = (op / pdv["c"] - 1) * 100
        typ = "above" if op > pdv["h"] else "below" if op < pdv["l"] else "inside"
        size = ("big gap" if abs(gap) >= CFG["big_gap"] else "flat open" if abs(gap) < CFG["flat_gap"] else "moderate gap")
        d.update(cls=size, gap=gap, open_type=typ, open=op)
        rule = ("opened outside yesterday's range: trade the break of the first 15m high/low if it holds 5 minutes"
                if typ != "inside" else
                "opened inside yesterday's range: wait for a sweep of PDH/PDL, then trade continuation or rejection")
        tg(f"🧭 <b>{I['name']}: setup forming</b> (part 1 of 3)\n"
           f"Open {fmt(op)} vs prev close {fmt(pdv['c'])}: {gap:+.2f}% ({size})\n"
           f"PDH {fmt(pdv['h'])} | PDL {fmt(pdv['l'])} | morning bias {bias_label(bias)}\n{rule}.")
    # part 2: first 15m candle confirmation
    if "h15" not in d and len(df) >= 15:
        f = df.iloc[:15]
        d["h15"], d["l15"] = float(f["high"].max()), float(f["low"].min())
        o15, c15 = float(f["open"].iloc[0]), float(f["close"].iloc[-1])
        col = "green" if c15 > o15 else "red"
        tg(f"✔️ <b>{I['name']}: first 15m candle done</b> (part 2 of 3)\n"
           f"High {fmt(d['h15'])} | Low {fmt(d['l15'])} | {col} candle, closed {fmt(c15)}\n"
           f"Now watching the 1-minute chart. Only the stop/target distances are shown (index points).")
    # part 3: entries
    h, m = now.hour, now.minute
    allow = (h, m) < CFG["india_last_entry"]
    active = any(t["status"] == "open" for t in d["trades"])
    if allow and not active and d["n"] < 2:
        sig = india_fast(df, d, bias) if d["n"] == 0 else None
        tag = " - FAST ENTRY" if sig else ""
        if not sig and "h15" in d:
            sig = india_signal(df, d, I, key, d.get("last_sig", 0))
        if sig and once(st, f"indiasig|{key}|{now.date()}|{sig['ts']:%H%M}|{sig['side']}"):
            d["last_sig"] = epoch(sig["ts"])
            india_open_trade(st, key, d, sig, df, now, tag)
    # BTST idea after 2:30 pm
    if (h, m) >= (14, 30) and "h15" in d and due(st, f"btst{key}", now, 14, 30, window=30, days=[0, 1, 2, 3, 4]):
        rng = float(df["high"].max() - df["low"].min())
        px = float(df["close"].iloc[-1])
        pos = (px - float(df["low"].min())) / rng if rng else 0.5
        sl_pts = 2 * CFG[I["sl"]]
        side = "CE" if (pos >= 0.75 and px > d["h15"] and bias >= 0) else "PE" if (pos <= 0.25 and px < d["l15"] and bias <= 0) else None
        if side:
            sg = 1 if side == "CE" else -1
            tg(f"🌙 <b>{I['name']}: BTST idea ({side})</b> - provisional\n"
               f"Index {fmt(px)} near the day's {'high' if sg > 0 else 'low'}, beyond the 15m {'high' if sg > 0 else 'low'}, "
               f"bias {bias_label(bias)}.\nStrike: {strike_text(key, px, side)}\n"
               f"SL (index): {fmt(px - sg * sl_pts)} | Target: {fmt(px + sg * CFG['btst_min_rr'] * sl_pts)} "
               f"(1:{CFG['btst_min_rr']})\nOvernight gap risk applies - size small.")

def india_postmarket(st, now):
    for key, I in INDIA.items():
        d = st["india"].get(key, {})
        if d.get("date") != str(now.date()) or "prev" not in d:
            continue
        raw = yf(I["sym"], "1m", "1d")
        if raw.empty:
            continue
        df = raw.copy()
        df.index = df.index.tz_convert(IST)
        df = df[df.index.date == now.date()]
        if df.empty:
            continue
        o, h, l, c = float(df["open"].iloc[0]), float(df["high"].max()), float(df["low"].min()), float(df["close"].iloc[-1])
        pc = d["prev"]["c"]
        pos = (c - l) / (h - l) if h > l else 0.5
        nxt = "closed near the high - bulls in control" if pos > 0.7 else \
              "closed near the low - bears in control" if pos < 0.3 else "closed mid-range - no clear edge"
        tr = d.get("trades", [])
        trtxt = "; ".join(f"{t['side']} {t.get('res', 'open')}" for t in tr) or "no alerts triggered"
        tg(f"🌇 <b>{I['name']} post-market ({now:%d %b})</b>\n"
           f"O {fmt(o)} | H {fmt(h)} | L {fmt(l)} | C {fmt(c)} ({(c / pc - 1) * 100:+.2f}%)\n"
           f"Day type: {d.get('cls', 'n/a')} | 15m range {fmt(d.get('h15', 0))} - {fmt(d.get('l15', 0))}\n"
           f"Trades today: {trtxt}\nTomorrow's PDH/PDL: {fmt(h)} / {fmt(l)} - {nxt}.")

def run_india(st, now):
    if now.weekday() >= 5:
        return
    t = (now.hour, now.minute)
    try:
        if due(st, "india_pre", now, 8, 30, window=40):
            india_premarket(st, now)
    except Exception:
        err(st, "india premarket")
    if (9, 15) <= t <= (15, 40):
        for key in INDIA:
            try:
                india_session(st, key, now)
            except Exception:
                err(st, f"india {key}")
    try:
        if due(st, "india_post", now, 15, 35, window=60):
            india_postmarket(st, now)
    except Exception:
        err(st, "india post")

# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def err(st, where):
    log("ERROR in", where)
    traceback.print_exc()
    k = f"{where}|{datetime.now(IST):%Y%m%d%H}"
    st["errors"][k] = st["errors"].get(k, 0) + 1
    if len(st["errors"]) > 50:
        st["errors"] = dict(list(st["errors"].items())[-20:])

def main():
    st = load_state()
    now = datetime.now(IST)
    for fn in (poll_callbacks, run_news, run_crypto_gold, run_india, run_reviews):
        try:
            fn(st, now)
        except Exception:
            err(st, fn.__name__)
    save_state(st)
    log("done", now.isoformat())

if __name__ == "__main__":
    main()
