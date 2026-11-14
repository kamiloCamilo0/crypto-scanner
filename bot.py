import os
import json
import time
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
API = "https://data-api.binance.vision/api/v3"
TG = f"https://api.telegram.org/bot{TOKEN}"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

MIN_VOLUME = 10_000_000                              # أقل حجم تداول يومي بالدولار
TOP_N = 70                                           # عدد العملات المفحوصة
MAX_ALERTS = 6                                       # أقصى إشارات في التشغيلة
MIN_SCORE = int(os.environ.get("MIN_SCORE", "4"))    # أقل درجة للإرسال
FRESH_MS = 25 * 60 * 1000                            # الشمعة تُعتبر جديدة 25 دقيقة بعد إغلاقها
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "BUSD", "USDE", "XUSD", "AEUR"}

# الفريمات: الاسم، مدة الشمعة، الفريم الأكبر للتأكيد، مدة متابعة الصفقة بالساعات
TFS = {
    "1h": {"name": "ساعة", "ms": 3_600_000, "higher": "4h", "hname": "4 ساعات", "horizon": 48},
    "4h": {"name": "4 ساعات", "ms": 14_400_000, "higher": "1d", "hname": "اليومي", "horizon": 120},
    "1d": {"name": "يومي", "ms": 86_400_000, "higher": None, "hname": "اليومي (EMA50)", "horizon": 336},
}

HELP = (
    "🤖 <b>رادار الكريبتو</b>\n\n"
    "/top — أقوى 10 عملات الآن\n"
    "/scan — أفضل الفرص الحالية (1س، 4س، يومي)\n"
    "/coin BTC — تحليل أي عملة على 3 فريمات\n"
    "/btc — حالة البيتكوين والسوق\n"
    "/open — الصفقات المفتوحة الآن\n"
    "/stats — سجل أداء الإشارات\n"
    "/help — هذه القائمة\n\n"
    "⏱ الردود تصل خلال 15 دقيقة كحد أقصى.\n"
    "⚠️ إشارات فنية وليست نصيحة مالية."
)


# ---------- أدوات مساعدة ----------
def get(path, **params):
    r = requests.get(f"{API}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def tg(method, **data):
    r = requests.post(f"{TG}/{method}", data=data, timeout=25)
    r.raise_for_status()
    return r.json()


def send(text, chat=None):
    tg("sendMessage", chat_id=chat or CHAT_ID, text=text,
       parse_mode="HTML", disable_web_page_preview="true")


def fmt(x):
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.8f}".rstrip("0")


# ---------- الحالة (سجل الصفقات) ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("sent", {})
    st.setdefault("trades", [])
    st.setdefault("stats", {})
    return st


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def prune_state(st, now_ms):
    st["sent"] = {k: v for k, v in st["sent"].items() if now_ms - v < 3 * 86_400_000}
    st["trades"] = [
        t for t in st["trades"]
        if t["status"] == "open" or now_ms - t["t"] < 30 * 86_400_000
    ]


# ---------- المؤشرات ----------
def ema(values, n):
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    g /= n
    l /= n
    out[n] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def atr(h, l, c, n=14):
    trs = [
        max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        for i in range(1, len(c))
    ]
    return sum(trs[-n:]) / n


def macd_hist(c):
    e12, e26 = ema(c, 12), ema(c, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema(line, 9)
    return [a - b for a, b in zip(line, sig)]


def candles(sym, interval, limit):
    k = get("/klines", symbol=sym, interval=interval, limit=limit)[:-1]  # بدون الشمعة غير المكتملة
    return {
        "t": [int(x[0]) for x in k],
        "o": [float(x[1]) for x in k],
        "h": [float(x[2]) for x in k],
        "l": [float(x[3]) for x in k],
        "c": [float(x[4]) for x in k],
        "v": [float(x[5]) for x in k],
    }


# ---------- حالة البيتكوين ----------
def btc_info():
    t = get("/ticker/24hr", symbol="BTCUSDT")
    k = candles("BTCUSDT", "1h", 80)
    e50 = ema(k["c"], 50)[-1]
    return {
        "price": float(t["lastPrice"]),
        "ch24": float(t["priceChangePercent"]),
        "rsi": rsi(k["c"])[-1],
        "up": k["c"][-1] > e50,
        "ch1h": (k["c"][-1] / k["c"][-2] - 1) * 100,
    }


def mood(b):
    if b["ch1h"] <= -2.5:
        return "🔴 هبوط حاد، الحذر واجب"
    if b["up"] and b["rsi"] < 70:
        return "🟢 اتجاه صاعد صحي"
    if b["up"]:
        return "🟡 صاعد لكن متضخم"
    return "🔴 اتجاه هابط"


# ---------- تحليل العملة على فريم واحد ----------
def analyze(sym, tf):
    cfg = TFS[tf]
    m = candles(sym, tf, 120)
    o, h, lo, c, v = m["o"], m["h"], m["l"], m["c"], m["v"]
    r = rsi(c)
    e9, e21 = ema(c, 9), ema(c, 21)
    e50 = ema(c, 50)
    hist = macd_hist(c)
    avg_v = sum(v[-21:-1]) / 20
    vr = v[-1] / avg_v if avg_v else 0
    a = atr(h, lo, c)

    if r[-1] is None or r[-1] > 75:
        return None                       # متضخمة
    if (c[-1] - o[-1]) > 2.5 * a:
        return None                       # الحركة بدأت قبل قليل، متأخرة

    reasons, score = [], 0
    if vr >= 3 and c[-1] > o[-1]:
        reasons.append(f"🔊 انفجار حجم {vr:.1f}x")
        score += 3 if vr >= 5 else 2
    if c[-1] > max(h[-21:-1]):
        reasons.append("🚀 كسر أعلى سعر لـ 20 شمعة")
        score += 2
    if r[-2] is not None and r[-2] < 30 <= r[-1]:
        reasons.append("📈 ارتداد من تشبع البيع")
        score += 2
    if e9[-2] <= e21[-2] and e9[-1] > e21[-1]:
        reasons.append("✂️ تقاطع EMA9 فوق EMA21")
        score += 2
    if hist[-2] <= 0 < hist[-1]:
        reasons.append("⚡ تقاطع MACD صاعد")
        score += 1
    if c[-2] < e50[-2] and c[-1] > e50[-1]:
        reasons.append("🔓 استعادة EMA50")
        score += 1

    if score < 2:
        return None

    # تأكيد الاتجاه من الفريم الأكبر
    hc = candles(sym, cfg["higher"], 80)["c"] if cfg["higher"] else c
    if hc[-1] > ema(hc, 50)[-1]:
        reasons.append(f"🧭 اتجاه {cfg['hname']} صاعد")
        score += 1
    else:
        reasons.append(f"⚠️ ضد اتجاه {cfg['hname']}")
        score -= 1

    entry = c[-1]
    risk = 1.5 * a
    return {
        "key": f"{sym}|{tf}|{m['t'][-1]}",
        "symbol": sym,
        "tf": tf,
        "score": max(min(score, 10), 0),
        "reasons": reasons,
        "rsi": r[-1],
        "entry": entry,
        "stop": entry - risk,
        "t1": entry + 1.5 * risk,
        "t2": entry + 3 * risk,
        "risk_pct": risk / entry * 100,
    }


def liquid(tickers):
    out = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in STABLES or base.endswith(("UP", "DOWN")):
            continue
        if float(t["quoteVolume"]) >= MIN_VOLUME:
            out.append(t)
    return out


def scan(tickers, tfs, now_ms, st=None):
    """st مُمرَّر = وضع الإشارات الحية (يتجاهل ما أُرسل سابقاً)."""
    pairs = sorted(liquid(tickers), key=lambda t: float(t["quoteVolume"]), reverse=True)[:TOP_N]
    out = []
    for t in pairs:
        sym = t["symbol"]
        for tf in tfs:
            if st is not None:
                ms = TFS[tf]["ms"]
                last_open = now_ms - now_ms % ms - ms
                if f"{sym}|{tf}|{last_open}" in st["sent"]:
                    continue
            try:
                res = analyze(sym, tf)
                if res:
                    out.append(res)
            except Exception as e:
                print("skip", sym, tf, e)
            time.sleep(0.1)
    out.sort(key=lambda x: (x["score"], TFS[x["tf"]]["ms"]), reverse=True)
    return out, len(pairs)


# ---------- متابعة الصفقات ----------
def update_trades(st, now_ms):
    msgs = []
    for tr in st["trades"]:
        if tr["status"] != "open":
            continue
        base, tfn = tr["sym"][:-4], TFS[tr["tf"]]["name"]
        try:
            k = get("/klines", symbol=tr["sym"], interval="15m",
                    startTime=max(tr["t0"], tr.get("checked", 0) + 1), limit=1000)
        except Exception as e:
            print("track error", tr["sym"], e)
            continue

        res, last_closed = None, tr.get("checked", 0)
        for x in k:
            ot, hi, lo, ct = int(x[0]), float(x[2]), float(x[3]), int(x[6])
            stop = tr["entry"] if tr["t1hit"] else tr["stop"]
            if lo <= stop:
                res = "win1" if tr["t1hit"] else "loss"
                break
            if hi >= tr["t2"]:
                res = "win2"
                break
            if hi >= tr["t1"] and not tr["t1hit"]:
                tr["t1hit"] = True
                p = (tr["t1"] / tr["entry"] - 1) * 100
                msgs.append(
                    f"🎯 <b>{base}</b> ({tfn}): تحقق الهدف 1 (+{p:.1f}%)\n"
                    f"💡 يمكنك نقل الوقف إلى نقطة الدخول <code>{fmt(tr['entry'])}</code>"
                )
            if ct < now_ms:
                last_closed = ot
        tr["checked"] = last_closed

        if res is None and now_ms - tr["t"] > TFS[tr["tf"]]["horizon"] * 3_600_000:
            res = "expired"

        if res:
            tr["status"], tr["result"] = "closed", res
            st["stats"].setdefault(tr["tf"], {"win2": 0, "win1": 0, "loss": 0, "expired": 0})
            st["stats"][tr["tf"]][res] += 1
            p1 = (tr["t1"] / tr["entry"] - 1) * 100
            p2 = (tr["t2"] / tr["entry"] - 1) * 100
            rp = (1 - tr["stop"] / tr["entry"]) * 100
            if res == "win2":
                msgs.append(f"🏆 <b>{base}</b> ({tfn}): تحقق الهدف 2 (+{p2:.1f}%) ✅")
            elif res == "win1":
                msgs.append(f"✅ <b>{base}</b> ({tfn}): بلغ الهدف 1 (+{p1:.1f}%) ثم عاد لنقطة الدخول، أُغلقت الصفقة")
            elif res == "loss":
                msgs.append(f"🛑 <b>{base}</b> ({tfn}): ضُرب الوقف (-{rp:.1f}%)")
            else:
                msgs.append(f"⌛ <b>{base}</b> ({tfn}): انتهت مدة المتابعة دون حسم، أُغلقت")
    return msgs


def totals(st):
    w2 = w1 = ls = ex = 0
    for s in st["stats"].values():
        w2 += s["win2"]
        w1 += s["win1"]
        ls += s["loss"]
        ex += s["expired"]
    return w2, w1, ls, ex


def msg_stats(st):
    w2, w1, ls, ex = totals(st)
    decided = w2 + w1 + ls
    open_n = sum(1 for t in st["trades"] if t["status"] == "open")
    if decided == 0:
        return f"📊 <b>سجل الأداء</b>\n\nلا توجد صفقات مغلقة بعد.\nالصفقات المفتوحة الآن: {open_n}"
    rate = (w2 + w1) / decided * 100
    lines = [
        "📊 <b>سجل أداء الإشارات</b>\n",
        f"🏆 بلغت الهدف 2: {w2}",
        f"✅ بلغت الهدف 1: {w1}",
        f"🛑 ضُرب الوقف: {ls}",
        f"⌛ منتهية بلا حسم: {ex}",
        f"\n🎯 نسبة النجاح (بلوغ الهدف 1 على الأقل): <b>{rate:.0f}%</b> من {decided} صفقة محسومة",
        f"📡 مفتوحة الآن: {open_n}",
    ]
    for tf, s in st["stats"].items():
        d = s["win2"] + s["win1"] + s["loss"]
        if d:
            lines.append(f"• فريم {TFS[tf]['name']}: {(s['win2'] + s['win1']) / d * 100:.0f}% من {d}")
    lines.append("\n⚠️ الأداء السابق لا يضمن المستقبل.")
    return "\n".join(lines)


def msg_open(st, prices):
    op = [t for t in st["trades"] if t["status"] == "open"]
    if not op:
        return "📡 لا توجد صفقات مفتوحة حالياً."
    lines = ["📡 <b>الصفقات المفتوحة</b>\n"]
    for t in op[-15:]:
        now = prices.get(t["sym"])
        pnl = f"{(now / t['entry'] - 1) * 100:+.1f}%" if now else "؟"
        tag = " 🎯" if t["t1hit"] else ""
        lines.append(f"• <b>{t['sym'][:-4]}</b> ({TFS[t['tf']]['name']})  دخول {fmt(t['entry'])}  الآن {pnl}{tag}")
    return "\n".join(lines)


# ---------- الرسائل ----------
def msg_signal(x, b):
    s = x["score"]
    tier = "🔥 قوية جداً" if s >= 7 else "✅ قوية" if s >= 5 else "👀 مراقبة"
    base = x["symbol"][:-4]
    p1 = (x["t1"] / x["entry"] - 1) * 100
    p2 = (x["t2"] / x["entry"] - 1) * 100
    warn = "\n⚠️ البيتكوين يهبط الآن، خفّف المخاطرة" if b and b["ch1h"] <= -2.5 else ""
    return (
        f"{tier}  <b>{base}/USDT</b>\n"
        f"⏱ تحليل فريم: <b>{TFS[x['tf']]['name']}</b>   |   الدرجة: {s}/10\n\n"
        + "\n".join(x["reasons"])
        + f"\n\n💵 الدخول: <code>{fmt(x['entry'])}</code>"
        f"\n🛑 الوقف: <code>{fmt(x['stop'])}</code>  (-{x['risk_pct']:.1f}%)"
        f"\n🎯 الهدف 1: <code>{fmt(x['t1'])}</code>  (+{p1:.1f}%)"
        f"\n🎯 الهدف 2: <code>{fmt(x['t2'])}</code>  (+{p2:.1f}%)"
        f"\n📊 RSI: {x['rsi']:.0f}"
        f"{warn}\n\n📡 سأتابع الصفقة وأخبرك بالنتيجة.\n⚠️ إشارة فنية وليست نصيحة مالية."
    )


def msg_btc(b):
    return (
        "₿ <b>حالة البيتكوين</b>\n\n"
        f"السعر: <code>{fmt(b['price'])}</code>\n"
        f"24 ساعة: {b['ch24']:+.2f}%\n"
        f"آخر ساعة: {b['ch1h']:+.2f}%\n"
        f"RSI (ساعة): {b['rsi']:.0f}\n"
        f"الحالة: {mood(b)}"
    )


def rank_lines(tickers, reverse, n):
    L = sorted(liquid(tickers), key=lambda t: float(t["priceChangePercent"]), reverse=reverse)[:n]
    lines = []
    for i, t in enumerate(L, 1):
        lines.append(
            f"{i}. <b>{t['symbol'][:-4]}</b>  {float(t['priceChangePercent']):+.1f}%"
            f"  |  {fmt(float(t['lastPrice']))}  |  حجم {float(t['quoteVolume']) / 1e6:.0f}M"
        )
    return "\n".join(lines)


def msg_top(tickers):
    return "🏆 <b>أقوى 10 عملات (24 ساعة)</b>\n\n" + rank_lines(tickers, True, 10)


def msg_daily(tickers, b, st):
    extra = ""
    w2, w1, ls, _ = totals(st)
    if w2 + w1 + ls >= 5:
        extra = f"\n\n📊 نسبة نجاح الإشارات: {(w2 + w1) / (w2 + w1 + ls) * 100:.0f}%  (/stats)"
    return (
        "🌅 <b>ملخص السوق اليومي</b>\n\n"
        f"₿ BTC: <code>{fmt(b['price'])}</code>  ({b['ch24']:+.1f}%)  {mood(b)}\n\n"
        "📈 <b>الأكثر صعوداً</b>\n" + rank_lines(tickers, True, 5)
        + "\n\n📉 <b>الأكثر هبوطاً</b>\n" + rank_lines(tickers, False, 5)
        + extra
    )


def coin_report(arg):
    sym = arg.upper().replace("/", "")
    if not sym.endswith("USDT"):
        sym += "USDT"
    base = sym[:-4]
    try:
        price = float(get("/ticker/price", symbol=sym)["price"])
    except Exception:
        return f"❌ لم أجد العملة {base}. جرّب مثلاً: /coin SOL"

    lines = [f"🔎 <b>تحليل {base}/USDT</b>  |  <code>{fmt(price)}</code>\n"]
    ups = 0
    found = []
    for tf, cfg in TFS.items():
        m = candles(sym, tf, 120)
        c, v = m["c"], m["v"]
        up = c[-1] > ema(c, 50)[-1]
        ups += up
        r = rsi(c)[-1]
        mh = macd_hist(c)[-1]
        vr = v[-1] / (sum(v[-21:-1]) / 20) if sum(v[-21:-1]) else 0
        lines.append(
            f"⏱ <b>{cfg['name']}</b>: {'صاعد 🟢' if up else 'هابط 🔴'}  |  RSI {r:.0f}"
            f"  |  MACD {'+' if mh > 0 else '−'}  |  حجم {vr:.1f}x"
        )
        try:
            res = analyze(sym, tf)
            if res and res["score"] >= 3:
                found.append(f"{cfg['name']} ({res['score']}/10)")
        except Exception:
            pass
    verdict = {3: "🟢 توافق صاعد على كل الفريمات", 0: "🔴 توافق هابط على كل الفريمات"}.get(ups, "🟡 الفريمات متضاربة")
    lines.append(f"\n{verdict}")
    if found:
        lines.append("🎯 إشارة دخول على: " + "، ".join(found))
    lines.append("\n⚠️ تحليل فني وليس نصيحة مالية.")
    return "\n".join(lines)


# ---------- الأوامر ----------
def read_commands():
    updates = tg("getUpdates", timeout=0).get("result", [])
    if not updates:
        return []
    tg("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)  # تأكيد الاستلام
    seen, cmds = set(), []
    for u in updates:
        m = u.get("message") or {}
        parts = (m.get("text") or "").strip().split()
        chat = (m.get("chat") or {}).get("id")
        if chat and parts and parts[0].startswith("/"):
            cmd = parts[0].lower().split("@")[0]
            arg = parts[1] if len(parts) > 1 else ""
            if (chat, cmd, arg) not in seen:
                seen.add((chat, cmd, arg))
                cmds.append((chat, cmd, arg))
    return cmds[:15]


def handle_commands(st, tickers, prices, b, now_ms):
    for chat, cmd, arg in read_commands():
        try:
            if cmd in ("/start", "/help"):
                send(HELP, chat)
            elif cmd == "/top":
                send(msg_top(tickers), chat)
            elif cmd == "/btc" and b:
                send(msg_btc(b), chat)
            elif cmd == "/stats":
                send(msg_stats(st), chat)
            elif cmd == "/open":
                send(msg_open(st, prices), chat)
            elif cmd == "/coin":
                send(coin_report(arg) if arg else "اكتب اسم العملة، مثال: /coin SOL", chat)
            elif cmd == "/scan":
                res, _ = scan(tickers, list(TFS), now_ms)
                if res:
                    for x in res[:3]:
                        send(msg_signal(x, b), chat)
                else:
                    send("🔍 لا توجد فرص واضحة الآن. السوق هادئ.", chat)
        except Exception as e:
            print("cmd error", cmd, e)


# ---------- التشغيل ----------
def main():
    now_ms = int(time.time() * 1000)
    event = os.environ.get("GITHUB_EVENT_NAME")
    daily = os.environ.get("SCHEDULE") == "7 7 * * *"
    manual = event == "workflow_dispatch"

    st = load_state()
    prune_state(st, now_ms)
    try:
        tickers = get("/ticker/24hr")
        prices = {t["symbol"]: float(t["lastPrice"]) for t in tickers}
        try:
            b = btc_info()
        except Exception as e:
            print("btc error", e)
            b = None

        handle_commands(st, tickers, prices, b, now_ms)

        for m in update_trades(st, now_ms):
            send(m)

        fresh = [tf for tf in TFS if now_ms % TFS[tf]["ms"] <= FRESH_MS]
        alerts = []
        if fresh:
            results, _ = scan(tickers, fresh, now_ms, st)
            alerts = [x for x in results if x["score"] >= MIN_SCORE]

        if daily and b:
            send(msg_daily(tickers, b, st))

        t0 = now_ms - now_ms % 900_000
        for x in alerts[:MAX_ALERTS]:
            send(msg_signal(x, b))
            st["sent"][x["key"]] = now_ms
            st["trades"].append({
                "id": x["key"], "sym": x["symbol"], "tf": x["tf"],
                "entry": x["entry"], "stop": x["stop"], "t1": x["t1"], "t2": x["t2"],
                "t": now_ms, "t0": t0, "checked": 0, "t1hit": False, "status": "open",
            })
            time.sleep(1)

        if manual and not alerts:
            names = "، ".join(TFS[tf]["name"] for tf in fresh) or "لا فريم جديد الآن"
            open_n = sum(1 for t in st["trades"] if t["status"] == "open")
            extra = f"\n{mood(b)}" if b else ""
            send(f"✅ البوت يعمل.\nالفريمات المفحوصة: {names}\nصفقات مفتوحة: {open_n}{extra}\n"
                 f"💡 جرّب /scan لأفضل الفرص الحالية.")
    finally:
        save_state(st)


if __name__ == "__main__":
    main()
