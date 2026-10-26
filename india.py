#!/usr/bin/env python3
"""
Gatekeeper INDIA module - Nifty 50 and Sensex alerts (separate alerts, never combined).
Semi-manual: price candles are automatic (free Yahoo feed). Things the free feed can't give
(option chain, Option Geek, Gift Nifty, news) are asked for in Telegram, and you send them to Claude.
No extra libraries needed. Does not touch the BTC/Gold engine.
"""
import json, os, sys, traceback, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = "india_state.json"
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT", "")
EVENT = os.environ.get("EVENT", "schedule")

# ---------------- SETTINGS (change numbers here only) ----------------
INDICES = {
    "NIFTY":  {"name": "Nifty 50", "symbol": "^NSEI",  "sl": 10, "expiry_weekday": 1},  # Tuesday
    "SENSEX": {"name": "Sensex",   "symbol": "^BSESN", "sl": 30, "expiry_weekday": 3},  # Thursday (SL 30 = placeholder)
}
RANGE_MINUTES = 15        # opening range used for the break (15m). Set 5 to use the 5m range.
SUSTAIN_MINUTES = 5       # break must hold this many 1-min closes
RR1, RR2 = 3, 5           # target 1 = 1:3 , target 2 = 1:5
MAX_TRADES_PER_DAY = 3    # per index
MAX_LAG_MIN = 10          # skip fresh entries if data is older than this (late GitHub run)
REVERSAL_WINDOW_MIN = 30  # rejection after PDH/PDL sweep must happen within this time
ASK_EVERY_MIN = 58        # hourly request for data the bot cannot fetch
ASK_FROM, ASK_UNTIL = 8 * 60 + 45, 15 * 60          # 08:45 to 15:00 IST
RUN_FROM, RUN_UNTIL = 8 * 60 + 45, 15 * 60 + 40     # engine active window
OPEN_MIN, EOD_WARN_MIN, EOD_CLOSE_MIN = 9 * 60 + 15, 15 * 60 + 15, 15 * 60 + 35
# ----------------------------------------------------------------------


def px(x):
    return "{:,.1f}".format(x)


def send_telegram(text):
    if not TOKEN or not CHAT:
        print("ERROR: TELEGRAM_TOKEN / TELEGRAM_CHAT missing")
        return False
    url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            ok = json.load(r).get("ok", False)
        print("telegram sent" if ok else "ERROR telegram refused the message")
        return ok
    except Exception as e:
        print("ERROR telegram:", e)
        return False


def fetch_yahoo(symbol, interval, rng):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=%s&range=%s" % (
        urllib.parse.quote(symbol), interval, rng)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append({"t": datetime.fromtimestamp(t, IST), "o": o, "h": h, "l": l, "c": c})
    return out


# ---------------- state ----------------
def new_idx():
    return {"trades": 0, "active": None, "armed": {}, "flags": {}, "early_done": False, "day_type": None}


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"date": "", "last_ask": None, "last_feed_err": None, "idx": {}, "history": []}


def save_state(state):
    state["history"] = state.get("history", [])[-200:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def mins_ago(iso, now):
    if not iso:
        return 10 ** 6
    return (now - datetime.fromisoformat(iso)).total_seconds() / 60


# ---------------- messages ----------------
ASK_LINES = [
    "- Gift Nifty / pre-market sentiment",
    "- Option chain (PCR, OI build-up / unwinding) for Nifty and Sensex",
    "- Option Geek: greeks + whether option premium is rising with the index",
    "- Any big news (Trump, war, crude, USDINR, inflation)",
]


def ask_text(now, vix, reason):
    lines = ["DATA CHECK - %s IST" % now.strftime("%H:%M"), reason, "",
             "Automatic: Nifty and Sensex price candles" + (", India VIX %s" % px(vix) if vix else "")]
    lines += ["", "Not available on free feeds - send ONLY what you want checked, to Claude (in the Claude app):"]
    lines += ASK_LINES
    if now.hour >= 13:
        lines.append("- After 1 PM: if the index moved but premium did not, send the Option Geek greeks screenshot")
    lines += ["", "No reply within 15 minutes = skipped for this hour; alerts continue on price action only."]
    if not vix:
        lines.insert(3, "India VIX feed failed - please send VIX too.")
    return "\n".join(lines)


def heads_up_text(cfg, direction, why, level, now):
    side = "CE (call)" if direction == "CE" else "PE (put)"
    return "\n".join([
        "%s - SETUP BUILDING [Part 1 of 3]" % cfg["name"].upper(),
        why,
        "If it holds %d min -> BUY %s (ATM or just OTM, never ITM)." % (SUSTAIN_MINUTES, side),
        "Level: %s | Time: %s IST" % (px(level), now.strftime("%H:%M")),
        "",
        "Send Claude now: option chain + Option Geek screenshot for %s." % cfg["name"],
    ])


def entry_text(cfg, a, pd, st, c1, now):
    side = "CE (call)" if a["dir"] == "CE" else "PE (put)"
    d = 1 if a["dir"] == "CE" else -1
    obstacles = [(n, v) for n, v in (("PDH", pd["h"]), ("PDL", pd["l"]), ("PDC", pd["c"]))
                 if (v - a["entry"]) * d > 0 and (a["t1"] - v) * d > 0]
    lines = ["%s - BUY %s [Part 2 of 3: ENTRY]" % (cfg["name"].upper(), side),
             "Setup: " + a["setup"],
             "Index entry: %s" % px(a["entry"]),
             "SL (index): %s  (%d pts)" % (px(a["sl"]), cfg["sl"]),
             "Target 1: %s  (1:%d)" % (px(a["t1"]), RR1),
             "Target 2: %s  (1:%d)" % (px(a["t2"]), RR2),
             "Levels: PDH %s | PDL %s | PDC %s" % (px(pd["h"]), px(pd["l"]), px(pd["c"]))]
    if obstacles:
        lines.append("Levels in the way before T1: " + ", ".join("%s %s" % (n, px(v)) for n, v in obstacles))
    if now.weekday() == cfg["expiry_weekday"]:
        lines.append("EXPIRY DAY for %s: zero-hero possible, premium decays fast." % cfg["name"])
    if now.hour >= 14:
        lines.append("After 2 PM: BTST candidate - your call whether to carry overnight.")
    lines += ["Data used: price action only (no option chain / greeks).",
              "Before buying: send Claude the option chain + premium screenshot to confirm.",
              "Already in a %s trade? Tell Claude and ask what to do." % cfg["name"]]
    return "\n".join(lines)


def result_text(cfg, a, outcome, points, now):
    names = {"T1": "TARGET 1 HIT - book 50%, move SL to your entry (cost)",
             "T2": "TARGET 2 HIT - trade complete",
             "SL": "STOP LOSS HIT",
             "COST": "Stopped at cost after Target 1 (profit already booked on first half)",
             "OPEN": "Market closing - trade still open, not resolved"}
    return "\n".join(["%s %s [Part 3 of 3: RESULT]" % (cfg["name"].upper(), a["dir"]),
                      names[outcome],
                      "Entry %s | Now %s pts" % (px(a["entry"]), ("%+.1f" % points)),
                      "Setup was: " + a["setup"], "Time: %s IST" % now.strftime("%H:%M")])


# ---------------- trade tracking ----------------
def close_trade(key, cfg, state, st, outcome, points, now, send):
    a = st["active"]
    send(result_text(cfg, a, outcome, points, now))
    state["history"].append({"date": state["date"], "index": key, "dir": a["dir"], "setup": a["setup"],
                             "entry": a["entry"], "sl": a["sl"], "t1": a["t1"], "t2": a["t2"],
                             "alert_time": a["alert_time"], "exit_time": now.isoformat(),
                             "outcome": outcome, "points": round(points, 1)})
    st["active"] = None


def track(key, cfg, state, st, c1, now, send):
    a = st["active"]
    if not a:
        return
    last_t = datetime.fromisoformat(a["last_t"])
    d = 1 if a["dir"] == "CE" else -1
    for x in c1:
        if x["t"] <= last_t:
            continue
        a["last_t"] = x["t"].isoformat()
        stop = a["entry"] if a["t1_hit"] else a["sl"]
        if (d == 1 and x["l"] <= stop) or (d == -1 and x["h"] >= stop):   # stop checked first (cautious)
            close_trade(key, cfg, state, st, "COST" if a["t1_hit"] else "SL",
                        0.0 if a["t1_hit"] else -cfg["sl"], now, send)
            return
        if not a["t1_hit"] and ((d == 1 and x["h"] >= a["t1"]) or (d == -1 and x["l"] <= a["t1"])):
            a["t1_hit"] = True
            send(result_text(cfg, a, "T1", RR1 * cfg["sl"], now))
        if a["t1_hit"] and ((d == 1 and x["h"] >= a["t2"]) or (d == -1 and x["l"] <= a["t2"])):
            close_trade(key, cfg, state, st, "T2", RR2 * cfg["sl"], now, send)
            return


# ---------------- signals ----------------
def open_trade(cfg, st, direction, setup, entry_candle, entry, now):
    d = 1 if direction == "CE" else -1
    st["trades"] += 1
    st["active"] = {"dir": direction, "setup": setup, "entry": entry,
                    "sl": entry - d * cfg["sl"], "t1": entry + d * RR1 * cfg["sl"],
                    "t2": entry + d * RR2 * cfg["sl"], "t1_hit": False,
                    "last_t": entry_candle["t"].isoformat(), "alert_time": now.isoformat()}
    return st["active"]


def rearm(st, sid, ok):
    if ok:
        st["armed"][sid] = True


def check_signals(key, cfg, state, st, c1, pd, now, send):
    """Returns True if a heads-up asked for screenshots (so the hourly ask can wait)."""
    asked = False
    PDH, PDL = pd["h"], pd["l"]
    start = c1[0]["t"].replace(hour=9, minute=15, second=0, microsecond=0)
    open_px, last = c1[0]["o"], c1[-1]
    day = "gap_up" if open_px > PDH else "gap_down" if open_px < PDL else "inside"
    st["day_type"] = day
    lag = (now - (last["t"] + timedelta(minutes=1))).total_seconds() / 60
    can_trade = st["active"] is None and st["trades"] < MAX_TRADES_PER_DAY and lag <= MAX_LAG_MIN
    gap_txt = {"gap_up": "gap-up day (opened above PDH)", "gap_down": "gap-down day (opened below PDL)",
               "inside": "open inside previous day range"}[day]
    sig = None  # (id, direction, setup text, level)

    # early entry: 6th 1-min candle breaks the first 5-min candle (gap days, fully aligned)
    if day != "inside" and not st["early_done"] and len(c1) >= 6 and c1[5]["t"] == start + timedelta(minutes=5):
        st["early_done"] = True
        f5, s6 = c1[:5], c1[5]
        hi5, lo5 = max(x["h"] for x in f5), min(x["l"] for x in f5)
        fresh6 = (now - (s6["t"] + timedelta(minutes=1))).total_seconds() / 60 <= MAX_LAG_MIN
        if can_trade and fresh6:
            if day == "gap_up" and f5[-1]["c"] > f5[0]["o"] and s6["c"] > hi5:
                sig = ("EARLY_CE", "CE", "EARLY ENTRY - 6th 1-min candle broke the first 5-min high, %s" % gap_txt, hi5, s6)
            elif day == "gap_down" and f5[-1]["c"] < f5[0]["o"] and s6["c"] < lo5:
                sig = ("EARLY_PE", "PE", "EARLY ENTRY - 6th 1-min candle broke the first 5-min low, %s" % gap_txt, lo5, s6)

    if day != "inside":
        rng_end = start + timedelta(minutes=RANGE_MINUTES)
        if last["t"] >= rng_end:
            rng = [x for x in c1 if x["t"] < rng_end]
            hi, lo = max(x["h"] for x in rng), min(x["l"] for x in rng)
            after = [x for x in c1 if x["t"] >= rng_end]
            rearm(st, "CE_RANGE", last["c"] <= hi)
            rearm(st, "PE_RANGE", last["c"] >= lo)
            sus = after[-SUSTAIN_MINUTES:]
            held_up = len(sus) == SUSTAIN_MINUTES and all(x["c"] > hi for x in sus)
            held_dn = len(sus) == SUSTAIN_MINUTES and all(x["c"] < lo for x in sus)
            if sig is None and can_trade and held_up and st["armed"].get("CE_RANGE", True):
                sig = ("CE_RANGE", "CE", "%dm range high %s broken and held %d min, %s" % (RANGE_MINUTES, px(hi), SUSTAIN_MINUTES, gap_txt), hi, last)
            elif sig is None and can_trade and held_dn and st["armed"].get("PE_RANGE", True):
                sig = ("PE_RANGE", "PE", "%dm range low %s broken and held %d min, %s" % (RANGE_MINUTES, px(lo), SUSTAIN_MINUTES, gap_txt), lo, last)
            for dr, lvl, beyond, held in (("CE", hi, last["c"] > hi, held_up), ("PE", lo, last["c"] < lo, held_dn)):
                fl = "heads_" + dr
                if beyond and not held and not st["flags"].get(fl) and st["active"] is None and sig is None:
                    st["flags"][fl] = True
                    send(heads_up_text(cfg, dr, "%dm range %s %s closed beyond (%s)" % (
                        RANGE_MINUTES, "high" if dr == "CE" else "low", px(lvl), gap_txt), lvl, now))
                    asked = True
    else:
        sus = c1[-SUSTAIN_MINUTES:]
        full = len(sus) == SUSTAIN_MINUTES
        hi_sw = [x for x in c1 if x["h"] > PDH]
        lo_sw = [x for x in c1 if x["l"] < PDL]
        rearm(st, "CE_PDH_cont", last["c"] <= PDH)
        rearm(st, "PE_PDH_rev", last["c"] >= PDH)
        rearm(st, "PE_PDL_cont", last["c"] >= PDL)
        rearm(st, "CE_PDL_rev", last["c"] <= PDL)
        win = REVERSAL_WINDOW_MIN + SUSTAIN_MINUTES
        cand = []
        if hi_sw and full and all(x["c"] > PDH for x in sus):
            cand.append(("CE_PDH_cont", "CE", "PDH %s swept and SUSTAINED above (continuation), %s" % (px(PDH), gap_txt), PDH))
        if hi_sw and full and all(x["c"] < PDH for x in sus) and (now - hi_sw[-1]["t"]).total_seconds() / 60 <= win:
            cand.append(("PE_PDH_rev", "PE", "PDH %s swept then REJECTED back below (reversal), %s" % (px(PDH), gap_txt), PDH))
        if lo_sw and full and all(x["c"] < PDL for x in sus):
            cand.append(("PE_PDL_cont", "PE", "PDL %s swept and SUSTAINED below (continuation), %s" % (px(PDL), gap_txt), PDL))
        if lo_sw and full and all(x["c"] > PDL for x in sus) and (now - lo_sw[-1]["t"]).total_seconds() / 60 <= win:
            cand.append(("CE_PDL_rev", "CE", "PDL %s swept then REJECTED back above (reversal), %s" % (px(PDL), gap_txt), PDL))
        for c in cand:
            if sig is None and can_trade and st["armed"].get(c[0], True):
                sig = (c[0], c[1], c[2], c[3], last)
        for tag, sw, lvl, dr in (("PDH", hi_sw, PDH, "PE"), ("PDL", lo_sw, PDL, "CE")):
            fl = "heads_" + tag
            if sw and not st["flags"].get(fl) and st["active"] is None and sig is None:
                st["flags"][fl] = True
                send("\n".join(["%s - SETUP BUILDING [Part 1 of 3]" % cfg["name"].upper(),
                                "%s %s swept (%s). Watching: holds %d min beyond = continuation, closes back inside and holds = reversal." % (tag, px(lvl), gap_txt, SUSTAIN_MINUTES),
                                "Time: %s IST" % now.strftime("%H:%M"), "",
                                "Send Claude now: option chain + Option Geek screenshot for %s." % cfg["name"]]))
                asked = True

    if sig:
        sid, direction, setup, level, ecandle = sig
        st["armed"][sid] = False
        a = open_trade(cfg, st, direction, setup, ecandle, ecandle["c"], now)
        send(entry_text(cfg, a, pd, st, c1, now))
    return asked


# ---------------- main loop ----------------
def process(key, cfg, state, now, fetch, send):
    st = state["idx"].setdefault(key, new_idx())
    today = now.date()
    c1 = [x for x in fetch(cfg["symbol"], "1m", "1d")
          if x["t"].date() == today and x["t"].hour * 60 + x["t"].minute >= OPEN_MIN
          and x["t"] + timedelta(minutes=1) <= now]
    if not c1:
        print(key, "no completed candles today yet (holiday or before open)")
        return False
    prev = [d for d in fetch(cfg["symbol"], "1d", "15d") if d["t"].date() < today]
    if not prev:
        raise RuntimeError("no previous day data")
    pd = prev[-1]
    track(key, cfg, state, st, c1, now, send)
    asked = check_signals(key, cfg, state, st, c1, pd, now, send)
    mins = now.hour * 60 + now.minute
    if st["active"] and mins >= EOD_WARN_MIN and not st["flags"].get("eod"):
        st["flags"]["eod"] = True
        send("%s: market closes soon and your %s trade is still open. Square off or carry as BTST - your call." % (cfg["name"], st["active"]["dir"]))
    if st["active"] and mins >= EOD_CLOSE_MIN:
        a = st["active"]
        d = 1 if a["dir"] == "CE" else -1
        close_trade(key, cfg, state, st, "OPEN", (c1[-1]["c"] - a["entry"]) * d, now, send)
    print(key, "ok | candles", len(c1), "| day", st["day_type"], "| trades", st["trades"],
          "| active", bool(st["active"]), "| last", px(c1[-1]["c"]))
    return asked


def run(now=None, fetch=fetch_yahoo, send=send_telegram):
    now = now or datetime.now(IST)
    state = load_state()
    today = now.strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["date"], state["idx"] = today, {}
    mins = now.hour * 60 + now.minute

    if EVENT == "workflow_dispatch":
        rep = []
        for key, cfg in INDICES.items():
            try:
                c = fetch(cfg["symbol"], "1m", "1d")
                rep.append("%s feed OK (last %s, %s)" % (cfg["name"], px(c[-1]["c"]), c[-1]["t"].strftime("%d %b %H:%M")))
            except Exception as e:
                rep.append("%s feed FAILED: %s" % (cfg["name"], e))
        send("INDIA MODULE ONLINE (manual test)\n" + "\n".join(rep))

    if now.weekday() >= 5 or not (RUN_FROM <= mins <= RUN_UNTIL):
        save_state(state)
        print("outside Indian market hours - nothing to do")
        return

    asked = False
    feed_failed = False
    for key, cfg in INDICES.items():
        try:
            asked = process(key, cfg, state, now, fetch, send) or asked
        except Exception as e:
            feed_failed = True
            print("ERROR", key, e)
            traceback.print_exc()
    if feed_failed and mins_ago(state.get("last_feed_err"), now) >= 60:
        state["last_feed_err"] = now.isoformat()
        send("India data feed problem - could not read Nifty/Sensex candles. Will retry automatically. Check the run log if it repeats.")

    if asked:
        state["last_ask"] = now.isoformat()
    elif ASK_FROM <= mins <= ASK_UNTIL and mins_ago(state.get("last_ask"), now) >= ASK_EVERY_MIN:
        vix = None
        try:
            v = fetch("^INDIAVIX", "1m", "1d")
            vix = v[-1]["c"] if v else None
        except Exception:
            pass
        send(ask_text(now, vix, "Hourly check - I can't fetch these on the free plan."))
        state["last_ask"] = now.isoformat()
    save_state(state)


if __name__ == "__main__":
    run()
