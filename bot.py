import os
import time
import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
API = "https://data-api.binance.vision/api/v3"

MIN_VOLUME = 10_000_000
TOP_N = 40
MIN_SCORE = 3
MAX_ALERTS = 5
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "BUSD", "USDE", "XUSD", "AEUR"}


def get(path, **params):
    r = requests.get(f"{API}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


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


def fmt(x):
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.8f}".rstrip("0")


def analyze(sym):
    k = get("/klines", symbol=sym, interval="1h", limit=120)
    k = k[:-1]
    o = [float(x[1]) for x in k]
    h = [float(x[2]) for x in k]
    lo = [float(x[3]) for x in k]
    c = [float(x[4]) for x in k]
    v = [float(x[5]) for x in k]

    r = rsi(c)
    e9, e21 = ema(c, 9), ema(c, 21)
    avg_v = sum(v[-21:-1]) / 20
    vr = v[-1] / avg_v if avg_v else 0

    reasons, score = [], 0

    if vr >= 3 and c[-1] > o[-1]:
        reasons.append(f"🔊 انفجار حجم {vr:.1f}x")
        score += 2
    if c[-1] > max(h[-21:-1]):
        reasons.append("🚀 كسر أعلى سعر لـ 20 ساعة")
        score += 2
    if r[-2] is not None and r[-2] < 30 <= r[-1]:
        reasons.append("📈 ارتداد من تشبع البيع (RSI)")
        score += 2
    if e9[-2] <= e21[-2] and e9[-1] > e21[-1]:
        reasons.append("✂️ تقاطع EMA9 فوق EMA21")
        score += 2

    if r[-1] is not None and r[-1] > 75:
        return None

    if score < MIN_SCORE:
        return None

    a = atr(h, lo, c)
    entry = c[-1]
    return {
        "symbol": sym,
        "score": score,
        "reasons": reasons,
        "rsi": r[-1],
        "entry": entry,
        "stop": entry - 1.5 * a,
        "t1": entry + 1.5 * a,
        "t2": entry + 3 * a,
    }


def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    ).raise_for_status()


def main():
    tickers = get("/ticker/24hr")
    pairs = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in STABLES or base.endswith(("UP", "DOWN")):
            continue
        vol = float(t["quoteVolume"])
        if vol >= MIN_VOLUME:
            pairs.append((s, vol))
    pairs.sort(key=lambda x: x[1], reverse=True)

    results = []
    for sym, _ in pairs[:TOP_N]:
        try:
            res = analyze(sym)
            if res:
                results.append(res)
        except Exception as e:
            print("skip", sym, e)
        time.sleep(0.2)

    results.sort(key=lambda x: x["score"], reverse=True)
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    if not results:
        if manual:
            send("✅ البوت يعمل. تم فحص السوق ولا توجد فرص قوية الآن.")
        return

    for x in results[:MAX_ALERTS]:
        base = x["symbol"][:-4]
        text = (
            f"🎯 <b>{base}/USDT</b>  |  قوة الإشارة: {x['score']}/8\n\n"
            + "\n".join(x["reasons"])
            + f"\n\n💵 الدخول: <code>{fmt(x['entry'])}</code>"
            f"\n🛑 وقف الخسارة: <code>{fmt(x['stop'])}</code>"
            f"\n🎯 الهدف 1: <code>{fmt(x['t1'])}</code>"
            f"\n🎯 الهدف 2: <code>{fmt(x['t2'])}</code>"
            f"\n📊 RSI: {x['rsi']:.0f}"
            "\n\n⚠️ إشارة فنية وليست نصيحة مالية."
        )
        send(text)
        time.sleep(1)


if __name__ == "__main__":
    main()
