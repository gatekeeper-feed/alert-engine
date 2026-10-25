"""
Gatekeeper Alert Engine v2 - Phase 1 (Gold + BTC)
Runs every ~5 minutes on GitHub Actions and posts alerts to Telegram.

HTF zones (order blocks + liquidity) on 4H, Daily, Weekly, Monthly, 3-Month
are tracked until mitigated.

  Stage 1: price approaches an unmitigated HTF zone
  Stage 2: sweep + rejection (5m, or 15m when volatile)
  Stage 3: CHoCH, then entry on the 5m retest (FVG if one formed,
           otherwise the broken CHoCH level)

Silver (for Gold) and ETH (for BTC) lead the move, so they are used as the
confirmation filter.
"""
import os
import json
import time
import datetime
import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT", "")
STATE_FILE = "state.json"
HEAD = {"User-Agent": "Mozilla/5.0"}

ASSETS = {
    "GOLD": {
        "syms": ["XAUUSD=X", "GC=F"],
        "refs": ["XAGUSD=X", "SI=F"],
        "ref_name": "Silver",
        "icon": "🥇",
        "label": "Gold",
        "pair": "XAU/USD",
        "venue": "Futures (XM)",
    },
    "BTC": {
        "syms": ["BTC-USD"],
        "refs": ["ETH-USD"],
        "ref_name": "ETH",
        "icon": "🪙",
        "label": "BTC",
        "pair": "BTC/USD",
        "venue": "Futures (CoinDCX)",
    },
}

TF_SEC = {"4H": 14400, "1D": 86400, "1W": 7 * 86400, "1M": 31 * 86400, "3M": 93 * 86400}
TF_NAME = {"4H": "4H", "1D": "Daily", "1W": "Weekly", "1M": "Monthly", "3M": "3-Month"}
KIND_NAME = {"OB": "Order Block", "LIQ": "Liquidity"}


# ---------------------------------------------------------------- helpers
def fmt(x):
    return f"${x:,.2f}"


def send(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": text},
            timeout=30,
        )
        if r.status_code != 200:
            print("telegram error", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        print("telegram exception", type(e).__name__)
        return False


def fetch(sym, interval, rng):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    for _ in range(3):
        try:
            r = requests.get(
                url,
                params={"interval": interval, "range": rng},
                headers=HEAD,
                timeout=30,
            )
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
            out = []
            for i, t in enumerate(ts):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if None in (o, h, l, c):
                    continue
                out.append({"t": t, "o": o, "h": h, "l": l, "c": c})
            return out
        except Exception as e:
            print("fetch failed", sym, interval, type(e).__name__)
            time.sleep(3)
    return []


def pick(syms, interval, rng):
    for s in syms:
        c = fetch(s, interval, rng)
        if len(c) > 50:
            return s, c
    return None, []


def closed(c, sec, now):
    """Drop the last candle if it is still forming."""
    if c and c[-1]["t"] + sec > now:
        return c[:-1]
    return c


def atr(c, n=14):
    if len(c) < n + 1:
        return None
    total = 0.0
    for i in range(len(c) - n, len(c)):
        total += max(
            c[i]["h"] - c[i]["l"],
            abs(c[i]["h"] - c[i - 1]["c"]),
            abs(c[i]["l"] - c[i - 1]["c"]),
        )
    return total / n


def resample(c, sec):
    out = []
    cur = None
    for x in c:
        b = (x["t"] // sec) * sec
        if cur is None or cur["t"] != b:
            if cur:
                out.append(cur)
            cur = {"t": b, "o": x["o"], "h": x["h"], "l": x["l"], "c": x["c"]}
        else:
            cur["h"] = max(cur["h"], x["h"])
            cur["l"] = min(cur["l"], x["l"])
            cur["c"] = x["c"]
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- zones
def mk(kind, d, top, bottom, t_end, tf):
    top, bottom = max(top, bottom), min(top, bottom)
    return {
        "id": f"{tf}|{kind}|{d}|{top:.2f}|{bottom:.2f}",
        "tf": tf,
        "kind": kind,
        "dir": d,
        "top": top,
        "bottom": bottom,
        "t_end": t_end,
    }


def scan_htf(c, tf):
    """Order blocks + liquidity (swing highs/lows) on one HTF series."""
    zs = []
    sec = TF_SEC[tf]
    a = atr(c)
    n = len(c)
    if not a or n < 16:
        return zs
    for i in range(1, n - 1):
        x, d = c[i], c[i + 1]
        if abs(d["c"] - d["o"]) < 1.5 * a:
            continue
        if x["c"] < x["o"] and d["c"] > d["o"] and d["c"] > x["h"]:
            zs.append(mk("OB", "bull", x["h"], x["l"], d["t"] + sec, tf))
        if x["c"] > x["o"] and d["c"] < d["o"] and d["c"] < x["l"]:
            zs.append(mk("OB", "bear", x["h"], x["l"], d["t"] + sec, tf))
    k = 3 if tf in ("4H", "1D") else 2
    for i in range(k, n - k):
        window = range(i - k, i + k + 1)
        if all(c[i]["h"] > c[j]["h"] for j in window if j != i):
            zs.append(mk("LIQ", "bear", c[i]["h"], c[i]["h"], c[i + k]["t"] + sec, tf))
        if all(c[i]["l"] < c[j]["l"] for j in window if j != i):
            zs.append(mk("LIQ", "bull", c[i]["l"], c[i]["l"], c[i + k]["t"] + sec, tf))
    return zs


def is_fresh(z, series):
    for s in series:
        for c in s:
            if c["t"] >= z["t_end"] and c["l"] <= z["top"] and c["h"] >= z["bottom"]:
                return False
    return True


# ---------------------------------------------------------------- LTF logic
def ltf_pick(m5c, now):
    m15 = closed(resample(m5c, 900), 900, now)
    a15 = atr(m15)
    vol = bool(a15 and m15 and (m15[-1]["h"] - m15[-1]["l"]) > 1.2 * a15)
    if vol:
        return m15, "15m", 900, True
    return m5c, "5m", 300, False


def moving_together(m5c, r5c):
    def mv(c):
        return c[-1]["c"] - c[-7]["c"] if len(c) >= 7 else 0
    return mv(m5c) * mv(r5c) > 0


def lead_shift(r5c, d):
    """Silver / ETH already broke its recent 5m range in the trade direction."""
    if len(r5c) < 30:
        return False
    win = r5c[-24:-6]
    if d == "bull":
        return r5c[-1]["c"] > max(x["h"] for x in win)
    return r5c[-1]["c"] < min(x["l"] for x in win)


def find_rejection(z, cands, sec, ta):
    bull = z["dir"] == "bull"
    if z["kind"] == "LIQ":
        lvl = z["top"] if bull else z["bottom"]
    else:
        lvl = (z["top"] + z["bottom"]) / 2
    js = None
    for j, c in enumerate(cands):
        if c["t"] + sec <= ta:
            continue
        if js is None:
            js = j
        if bull:
            if c["c"] < z["bottom"]:
                return "invalid"
            if c["l"] <= z["top"] and c["c"] > lvl and c["c"] > c["o"]:
                lh = max(x["h"] for x in cands[max(0, j - 6): j + 1])
                sx = min(x["l"] for x in cands[js: j + 1])
                return j, lh, sx
        else:
            if c["c"] > z["top"]:
                return "invalid"
            if c["h"] >= z["bottom"] and c["c"] < lvl and c["c"] < c["o"]:
                lh = min(x["l"] for x in cands[max(0, j - 6): j + 1])
                sx = max(x["h"] for x in cands[js: j + 1])
                return j, lh, sx
    return None


def find_choch(z, cands):
    bull = z["dir"] == "bull"
    for i, c in enumerate(cands):
        if c["t"] <= z["tr"]:
            continue
        if bull:
            if c["c"] < z["bottom"]:
                return "invalid"
            if c["c"] > z["lh"]:
                return i, c
        else:
            if c["c"] > z["top"]:
                return "invalid"
            if c["c"] < z["lh"]:
                return i, c
    return None


def retest_zone(z, cands, ic, atr_l):
    """FVG left by the CHoCH move if there is one, else the broken level."""
    bull = z["dir"] == "bull"
    start = 2
    for i, c in enumerate(cands):
        if c["t"] >= z["tr"]:
            start = max(2, i)
            break
    found = None
    for i in range(start, ic + 1):
        a, b = cands[i - 2], cands[i]
        if bull and b["l"] > a["h"] and (b["l"] - a["h"]) >= 0.1 * atr_l:
            found = (b["l"], a["h"])
        if (not bull) and b["h"] < a["l"] and (a["l"] - b["h"]) >= 0.1 * atr_l:
            found = (a["l"], b["h"])
    if found:
        return found[0], found[1], "FVG"
    band = 0.1 * atr_l
    return z["lh"] + band, z["lh"] - band, "CHoCH level"


def find_retest(z, cands):
    bull = z["dir"] == "bull"
    for c in cands:
        if c["t"] <= z["tc"]:
            continue
        if bull:
            if c["c"] < z["sx"]:
                return "invalid"
            if c["l"] <= z["rzt"] and c["c"] > z["rzb"] and c["c"] > c["o"]:
                return c
        else:
            if c["c"] > z["sx"]:
                return "invalid"
            if c["h"] >= z["rzb"] and c["c"] < z["rzt"] and c["c"] < c["o"]:
                return c
    return None


def pick_target(z, entry, pool):
    if z["dir"] == "bull":
        cands = [p["bottom"] for p in pool if p["dir"] == "bear" and p["bottom"] > entry]
        return min(cands) if cands else None
    cands = [p["top"] for p in pool if p["dir"] == "bull" and p["top"] < entry]
    return max(cands) if cands else None


# ---------------------------------------------------------------- messages
def tag(z):
    return "🟢 SWING" if z["tf"] != "4H" else "🟡 INTRADAY"


def bias_line(z):
    side = "Bullish" if z["dir"] == "bull" else "Bearish"
    if z["kind"] == "LIQ":
        what = "Sell-side Liquidity" if z["dir"] == "bull" else "Buy-side Liquidity"
        return f"{side} HTF {what} ({TF_NAME[z['tf']]}) at {fmt(z['top'])}"
    return (
        f"{side} HTF Order Block ({TF_NAME[z['tf']]}) "
        f"({fmt(z['bottom'])} - {fmt(z['top'])})"
    )


def build(cfg, title, lines):
    head = f"📢 @GatekeeperFeed — {cfg['label'].upper()} ALERT ({title})\n"
    return head + "\n" + "\n".join("• " + x for x in lines)


def corr1_text(ok, cfg):
    if ok:
        return f"✅ CORRELATION CONFIRMED ({cfg['ref_name']} moving with {cfg['label']})"
    return f"⚠️ CORRELATION UNCONFIRMED / DIVERGENT ({cfg['ref_name']} not moving with {cfg['label']})"


def lead_text(ok, cfg, d):
    way = "up" if d == "bull" else "down"
    if ok:
        return f"✅ FILTER PASSED ({cfg['ref_name']} already turned {way})"
    return f"⚠️ FILTER DIVERGENT - Stage 3 High Risk ({cfg['ref_name']} has not turned {way})"


# ---------------------------------------------------------------- per asset
def run_asset(name, cfg, state, now):
    sym, m5 = pick(cfg["syms"], "5m", "5d")
    if not sym:
        print("no 5m data for", name)
        return None
    _, r5 = pick(cfg["refs"], "5m", "5d")
    h1 = fetch(sym, "1h", "6mo")
    if not h1:
        print("no 1h data for", name)
        return None

    if state["src"].get(name) != sym:
        state["zones"][name] = {}
        state["scan"][name] = 0
        state["src"][name] = sym

    m5c = closed(m5, 300, now)
    r5c = closed(r5, 300, now)
    h1c = closed(h1, 3600, now)
    h4c = closed(resample(h1c, 14400), 14400, now)
    atr4 = atr(h4c)
    if not atr4 or len(m5c) < 30:
        print("not enough data for", name)
        return None
    price = m5c[-1]["c"]

    cutoff = now - 7200
    h1_old = [c for c in h1c if c["t"] + 3600 <= cutoff]
    reg = state["zones"].setdefault(name, {})

    # --- find zones: 4H every run, Daily/Weekly/Monthly/3-Month hourly
    new = scan_htf(h4c, "4H")
    series = [h1_old]
    if now - state["scan"].get(name, 0) > 3600:
        d1 = fetch(sym, "1d", "max")
        w1 = fetch(sym, "1wk", "max")
        mo = fetch(sym, "1mo", "max")
        q3 = fetch(sym, "3mo", "max")
        if d1:
            d1c = closed(d1, TF_SEC["1D"], now)
            state["atr1d"][name] = atr(d1c)
            series = [d1c, h1_old]
            new += scan_htf(d1c, "1D")
            if w1:
                new += scan_htf(closed(w1, TF_SEC["1W"], now), "1W")
            if mo:
                new += scan_htf(closed(mo, TF_SEC["1M"], now), "1M")
            if q3:
                new += scan_htf(closed(q3, TF_SEC["3M"], now), "3M")
            state["scan"][name] = now
    for z in new:
        if z["id"] not in reg and is_fresh(z, series):
            nz = dict(z)
            nz.update(stage=0, added=now)
            reg[z["id"]] = nz

    atr1d = state["atr1d"].get(name) or atr4 * 3
    ltf, ltf_name, ltf_sec, vol = ltf_pick(m5c, now)
    atr_l = atr(ltf) or 0
    together = moving_together(m5c, r5c)
    pool = [z for z in reg.values() if z["stage"] <= 3]

    for zid, z in list(reg.items()):
        st = z["stage"]
        if st >= 4:
            if now - z.get("closed_at", now) > 3 * 86400:
                del reg[zid]
            continue
        d = z["dir"]

        # ---- Stage 1: approaching
        if st == 0:
            if not is_fresh(z, [h1_old]):
                z["stage"] = 9
                z["closed_at"] = now
                continue
            buf = 0.25 * atr4 if z["tf"] == "4H" else 0.5 * atr1d
            hit = None
            for c in m5c:
                if c["t"] < now - 7200:
                    continue
                if c["h"] >= z["bottom"] - buf and c["l"] <= z["top"] + buf:
                    hit = c
                    break
            if not hit:
                continue
            msg = build(cfg, "STAGE 1: APPROACHING HTF ZONE", [
                f"Asset: {cfg['pair']} (Isolated)",
                f"Bias / Zone: {bias_line(z)}",
                f"Priority: {tag(z)}",
                f"Price now: {fmt(price)}",
                f"Trigger Type: Price reached zone - waiting for sweep + rejection on {ltf_name}",
                f"Inter-Asset Filter Check: {corr1_text(together, cfg)}",
                f"Execution Parameters: {cfg['venue']} | Watch only - no entry yet",
            ])
            if not send(msg):
                continue
            z["stage"] = 1
            z["ta"] = hit["t"]
            z["f1"] = together
            st = 1

        # ---- Stage 2: sweep + rejection
        if st == 1:
            res = find_rejection(z, ltf, ltf_sec, z["ta"])
            if res == "invalid" or (res is None and now - z["ta"] > 3 * 86400):
                z["stage"] = 9
                z["closed_at"] = now
                continue
            if res is None:
                continue
            jr, lh, sx = res
            lead = lead_shift(r5c, d)
            vtxt = (
                "Volatile Market (15m Confirmed)"
                if vol
                else "Non-Volatile Market (5m Confirmed)"
            )
            msg = build(cfg, "STAGE 2: SWEEP + REJECTION", [
                f"Asset: {cfg['pair']} (Isolated)",
                f"Bias / Zone: {bias_line(z)}",
                f"Priority: {tag(z)}",
                f"Price now: {fmt(price)}",
                f"Trigger Type: HTF sweep + wick rejection on {ltf_name}",
                f"Volatility Filter (ATR-14): {vtxt}",
                f"Inter-Asset Filter Check: {lead_text(lead, cfg, d)}",
                "Next: wait for CHoCH, then entry on the retest",
            ])
            if not send(msg):
                continue
            z.update(stage=2, tr=ltf[jr]["t"], lh=lh, sx=sx, f2=lead)
            st = 2

        # ---- Stage 3 prep: CHoCH formed -> define the retest area
        if st == 2:
            res = find_choch(z, ltf)
            if res == "invalid" or (res is None and now - z["tr"] > 4 * 3600):
                z["stage"] = 9
                z["closed_at"] = now
                continue
            if res is None:
                continue
            ic, cc = res
            rzt, rzb, rtype = retest_zone(z, ltf, ic, atr_l)
            sl = z["sx"] - 0.1 * atr_l if d == "bull" else z["sx"] + 0.1 * atr_l
            lead = lead_shift(r5c, d)
            msg = build(cfg, "STAGE 3 PREP: CHoCH FORMED", [
                f"Asset: {cfg['pair']} (Isolated)",
                f"Bias / Zone: {bias_line(z)}",
                f"Priority: {tag(z)}",
                f"Trigger Type: {ltf_name} CHoCH confirmed - waiting for retest",
                f"Retest area ({rtype}): {fmt(rzb)} - {fmt(rzt)}",
                f"Invalidation / SL: {fmt(sl)}",
                f"Inter-Asset Filter Check: {lead_text(lead, cfg, d)}",
                "Skip the trade if price runs away without a retest",
            ])
            if not send(msg):
                continue
            z.update(stage=3, tc=cc["t"], rzt=rzt, rzb=rzb, rtype=rtype)
            st = 3

        # ---- Stage 3: retest entry
        if st == 3:
            res = find_retest(z, ltf)
            if res == "invalid" or (res is None and now - z["tc"] > 4 * 3600):
                z["stage"] = 9
                z["closed_at"] = now
                continue
            if res is None:
                continue
            entry = res["c"]
            sl = z["sx"] - 0.1 * atr_l if d == "bull" else z["sx"] + 0.1 * atr_l
            side = "BUY" if d == "bull" else "SELL"
            tgt = pick_target(z, entry, pool)
            risk = abs(entry - sl)
            if tgt and risk > 0:
                rr = abs(tgt - entry) / risk
                tline = f"Target: {fmt(tgt)} (next HTF level) | RR 1:{rr:.1f}"
                if rr >= 15:
                    tline += " 🔴 HIGH-RR"
            else:
                tline = "Target: no clear HTF level found - manage manually"
            lead = lead_shift(r5c, d)
            plan = "50% base entry | scale on pullback | SL to breakeven after first target"
            if z["tf"] != "4H":
                plan += " | Swing: book 50%, carry 50%"
            msg = build(cfg, f"STAGE 3: CONFIRMATION ENTRY - {side}", [
                f"Asset: {cfg['pair']} (Isolated)",
                f"Bias / Zone: {bias_line(z)}",
                f"Priority: {tag(z)}",
                f"Trigger Type: {ltf_name} CHoCH + {z['rtype']} retest",
                f"Entry: ~{fmt(entry)} | Stop Loss: {fmt(sl)}",
                tline,
                f"Filter @ approach: {'✅' if z.get('f1') else '⚠️'} | @ sweep: {'✅' if z.get('f2') else '⚠️'} | now: {'✅' if lead else '⚠️'} ({cfg['ref_name']})",
                f"Execution Parameters: {cfg['venue']} | {plan}",
                "Always check the chart before placing the trade",
            ])
            if send(msg):
                z["stage"] = 4
                z["closed_at"] = now

    return {"name": name, "icon": cfg["icon"], "price": price, "reg": reg}


# ---------------------------------------------------------------- summary
def summary_text(items, ist, title):
    lines = [f"{title} - {ist.strftime('%d %b %Y, %H:%M')} IST", ""]
    for it in items:
        p = it["price"]
        act = [z for z in it["reg"].values() if z["stage"] <= 3]
        above = [z for z in act if z["bottom"] > p]
        below = [z for z in act if z["top"] < p]
        lines.append(f"{it['icon']} {it['name']}: {fmt(p)} | tracking {len(act)} HTF zones")
        if above:
            z = min(above, key=lambda x: x["bottom"])
            lines.append(
                f"  Next above: {TF_NAME[z['tf']]} {z['kind']} {fmt(z['bottom'])}"
            )
        if below:
            z = max(below, key=lambda x: x["top"])
            lines.append(
                f"  Next below: {TF_NAME[z['tf']]} {z['kind']} {fmt(z['top'])}"
            )
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- main
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    for k, v in (("zones", {}), ("src", {}), ("scan", {}), ("atr1d", {})):
        s.setdefault(k, v)
    s.setdefault("hb", "")
    s.setdefault("last_run", 0)
    return s


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)


def main(now=None):
    now = int(now or time.time())
    state = load_state()
    items = []
    try:
        for name, cfg in ASSETS.items():
            try:
                r = run_asset(name, cfg, state, now)
                if r:
                    items.append(r)
            except Exception as e:
                print("asset failed", name, type(e).__name__, e)
        ist = datetime.datetime.fromtimestamp(now + 19800, datetime.timezone.utc)
        today = ist.strftime("%Y-%m-%d")
        gap = now - state["last_run"] if state["last_run"] else 0
        title = None
        if state["last_run"] == 0:
            title = "🚀 Gatekeeper engine started"
        elif gap > 2700:
            title = f"⏱ Back online after {gap // 60} min - live snapshot"
        elif ist.hour >= 5 and state.get("hb") != today:
            title = "✅ Gatekeeper engine running"
        if items and title:
            if send(summary_text(items, ist, title)):
                state["hb"] = today
    finally:
        state["last_run"] = now
        save_state(state)


if __name__ == "__main__":
    main()
