from datetime import datetime
import math

from run_once_v41 import (
    SYMBOL,
    send_telegram,
    fetch_frames,
    get_tf,
    analyze,
    macro_filter,
    score_signal,
    format_trade,
    print_hold,
    cfg,
    wick_info,
)

from run_once_v42 import detect_formation, format_formation
from run_once_v43 import fetch_m5_twelvedata, impulse_engine, format_impulse

try:
    from run_once_v44 import (
        load_state,
        save_state,
        should_send,
        register_sent_alert,
        append_history,
        update_tracking,
        now_utc,
    )
except Exception:
    raise RuntimeError("run_once_v44.py est requis. Installe d’abord la V4.4.")


SMART_MIN_SCORE = float(cfg("V45_SMART_MIN_SCORE", "62"))


def nearest_round_levels(price):
    levels = []

    for step in [10, 25, 50, 100]:
        base = round(price / step) * step
        for i in range(-3, 4):
            levels.append(base + i * step)

    levels = sorted(set([round(x, 2) for x in levels if x > 0]))
    nearest = min(levels, key=lambda x: abs(price - x))
    return nearest, levels


def session_levels(m15):
    df = m15["df"].copy()
    price = m15["close"]

    last_96 = df.tail(96)
    prev_96 = df.tail(192).head(96) if len(df) >= 192 else df.head(0)

    day_high = float(last_96["high"].max())
    day_low = float(last_96["low"].min())

    prev_day_high = float(prev_96["high"].max()) if len(prev_96) else float(df["high"].tail(160).max())
    prev_day_low = float(prev_96["low"].min()) if len(prev_96) else float(df["low"].tail(160).min())

    asian_high = None
    asian_low = None

    if "datetime" in df.columns:
        tmp = df.copy()
        tmp["datetime"] = tmp["datetime"].dt.tz_localize(None)
        recent = tmp.tail(160)
        asian = recent[(recent["datetime"].dt.hour >= 0) & (recent["datetime"].dt.hour < 8)]
        if len(asian) >= 6:
            asian_high = float(asian["high"].tail(40).max())
            asian_low = float(asian["low"].tail(40).min())

    if asian_high is None or asian_low is None:
        # fallback robuste : range récent de compression
        recent_8h = df.tail(32)
        asian_high = float(recent_8h["high"].max())
        asian_low = float(recent_8h["low"].min())

    round_level, round_levels = nearest_round_levels(price)

    return {
        "day_high": day_high,
        "day_low": day_low,
        "prev_day_high": prev_day_high,
        "prev_day_low": prev_day_low,
        "asian_high": asian_high,
        "asian_low": asian_low,
        "round_level": round_level,
        "round_levels": round_levels,
    }


def near(price, level, atr, mult=0.8):
    return abs(price - level) <= atr * mult


def crossed_above(last, level):
    return float(last["high"]) > level and float(last["close"]) < level


def crossed_below(last, level):
    return float(last["low"]) < level and float(last["close"]) > level


def smart_money_engine(m15, h1, h4, macro):
    df = m15["df"]
    last = df.iloc[-1]
    prev = df.iloc[-2]

    price = float(m15["close"])
    atr = max(float(m15["atr"]), 0.01)

    upper, lower, body = wick_info(last)
    levels = session_levels(m15)

    key_highs = [
        ("high veille", levels["prev_day_high"]),
        ("high 24h", levels["day_high"]),
        ("high asiatique/récent", levels["asian_high"]),
        ("high structure H1", h1["prev_30_high"]),
    ]

    key_lows = [
        ("low veille", levels["prev_day_low"]),
        ("low 24h", levels["day_low"]),
        ("low asiatique/récent", levels["asian_low"]),
        ("low structure H1", h1["prev_30_low"]),
    ]

    long_score = 0
    short_score = 0
    long_reasons = []
    short_reasons = []

    nearest_high = min(key_highs, key=lambda x: abs(price - x[1]))
    nearest_low = min(key_lows, key=lambda x: abs(price - x[1]))

    # Piège acheteur : prise de liquidité au-dessus d’un niveau puis réintégration.
    for name, lvl in key_highs:
        if crossed_above(last, lvl):
            short_score += 28
            short_reasons.append(f"piège acheteurs au-dessus {name} {round(lvl, 2)}")
        elif near(price, lvl, atr, 0.65) and upper >= 0.42:
            short_score += 15
            short_reasons.append(f"rejet proche {name} {round(lvl, 2)}")

    # Piège vendeur : prise de liquidité sous un niveau puis réintégration.
    for name, lvl in key_lows:
        if crossed_below(last, lvl):
            long_score += 28
            long_reasons.append(f"piège vendeurs sous {name} {round(lvl, 2)}")
        elif near(price, lvl, atr, 0.65) and lower >= 0.42:
            long_score += 15
            long_reasons.append(f"rejet proche {name} {round(lvl, 2)}")

    # Niveaux ronds
    round_level = levels["round_level"]

    if near(price, round_level, atr, 0.55):
        long_score += 5
        short_score += 5

        if price > round_level and lower >= 0.35:
            long_score += 10
            long_reasons.append(f"réaction au-dessus niveau rond {round_level}")

        if price < round_level and upper >= 0.35:
            short_score += 10
            short_reasons.append(f"réaction sous niveau rond {round_level}")

    # Breakout propre / perte de niveau
    if price > levels["asian_high"] and prev["close"] <= levels["asian_high"]:
        long_score += 18
        long_reasons.append(f"cassure high asiatique/récent {round(levels['asian_high'], 2)}")

    if price < levels["asian_low"] and prev["close"] >= levels["asian_low"]:
        short_score += 18
        short_reasons.append(f"cassure low asiatique/récent {round(levels['asian_low'], 2)}")

    if price > levels["prev_day_high"]:
        long_score += 12
        long_reasons.append(f"prix au-dessus high veille {round(levels['prev_day_high'], 2)}")

    if price < levels["prev_day_low"]:
        short_score += 12
        short_reasons.append(f"prix sous low veille {round(levels['prev_day_low'], 2)}")

    # Retest simple
    if price > levels["asian_high"] and near(price, levels["asian_high"], atr, 0.45) and lower >= 0.30:
        long_score += 18
        long_reasons.append("retest propre high asiatique cassé")

    if price < levels["asian_low"] and near(price, levels["asian_low"], atr, 0.45) and upper >= 0.30:
        short_score += 18
        short_reasons.append("retest propre low asiatique cassé")

    # Biais H1 / H4
    if h1["trend_up"] or h1["soft_up"]:
        long_score += 10
        long_reasons.append("biais H1 favorable hausse")

    if h1["trend_down"] or h1["soft_down"]:
        short_score += 10
        short_reasons.append("biais H1 favorable baisse")

    if h4["trend_up"]:
        long_score += 8
        long_reasons.append("H4 soutient scénario haussier")

    if h4["trend_down"]:
        short_score += 8
        short_reasons.append("H4 soutient scénario baissier")

    # Macro
    if macro["dxy_bias"] == "DOWN":
        long_score += 12
        long_reasons.append("DXY baissier, favorable or")

    if macro["dxy_bias"] == "UP":
        short_score += 12
        short_reasons.append("DXY haussier, pression sur l’or")

    if macro["tnx_bias"] == "DOWN":
        long_score += 10
        long_reasons.append("US10Y baissier, favorable or")

    if macro["tnx_bias"] == "UP":
        short_score += 10
        short_reasons.append("US10Y haussier, pression baissière or")

    # Contradictions fortes
    if macro["dxy_bias"] == "UP" and macro["tnx_bias"] == "UP":
        long_score -= 14

    if macro["dxy_bias"] == "DOWN" and macro["tnx_bias"] == "DOWN":
        short_score -= 14

    # Évite les scénarios sans vrai niveau
    long_has_key = any(x in " / ".join(long_reasons) for x in ["piège", "cassure", "retest", "rejet"])
    short_has_key = any(x in " / ".join(short_reasons) for x in ["piège", "cassure", "retest", "rejet"])

    if not long_has_key:
        long_score -= 10

    if not short_has_key:
        short_score -= 10

    side = "LONG" if long_score >= short_score else "SHORT"
    score = max(long_score, short_score)

    if side == "LONG":
        reasons = long_reasons[:10]
        confirmation = max(levels["asian_high"], float(m15["prev_12_high"]))
        invalidation = min(levels["asian_low"], levels["prev_day_low"], float(m15["prev_30_low"]))
        zone = nearest_low
        scenario = "piège vendeurs / continuation haussière"
        alert = score >= SMART_MIN_SCORE and long_has_key
    else:
        reasons = short_reasons[:10]
        confirmation = min(levels["asian_low"], float(m15["prev_12_low"]))
        invalidation = max(levels["asian_high"], levels["prev_day_high"], float(m15["prev_30_high"]))
        zone = nearest_high
        scenario = "piège acheteurs / continuation baissière"
        alert = score >= SMART_MIN_SCORE and short_has_key

    return {
        "alert": bool(alert),
        "side": side,
        "score": round(score, 1),
        "long_score": round(long_score, 1),
        "short_score": round(short_score, 1),
        "price": round(price, 2),
        "scenario": scenario,
        "confirmation": round(confirmation, 2),
        "invalidation": round(invalidation, 2),
        "zone_name": zone[0],
        "zone_level": round(zone[1], 2),
        "round_level": round_level,
        "levels": levels,
        "macro": macro,
        "reasons": reasons,
    }


def format_smart_map(sm):
    emoji = "🧠"
    side_emoji = "🟢" if sm["side"] == "LONG" else "🔴"

    reasons = "\n".join([f"• {r}" for r in sm["reasons"]]) or "• scénario smart money en construction"

    if sm["side"] == "LONG":
        plan = f"Surveiller cassure/maintien au-dessus de {sm['confirmation']}"
    else:
        plan = f"Surveiller cassure/maintien sous {sm['confirmation']}"

    return f"""<b>{emoji} GROK GOLD V4.5 — SMART MONEY MAP</b>

<b>Scénario :</b> {side_emoji} {sm['side']}
<b>Actif :</b> {SYMBOL}
<b>Prix :</b> {sm['price']}
<b>Score smart :</b> {sm['score']}/100

<b>Lecture :</b>
{sm['scenario']}

<b>Zone clé :</b>
{sm['zone_name']} : {sm['zone_level']}
Niveau rond proche : {sm['round_level']}

<b>Confirmation attendue :</b>
{plan}

<b>Invalidation scénario :</b>
{sm['invalidation']}

<b>Macro :</b>
DXY : {sm['macro']['dxy_bias']}
US10Y : {sm['macro']['tnx_bias']}

<b>Raisons :</b>
{reasons}

<i>Ce n’est pas automatiquement une entrée. C’est une carte de scénario : attendre confirmation/retest pour éviter les pièges.</i>"""


def smart_event(sm):
    return {
        "kind": "FORMATION",
        "side": sm["side"],
        "score": sm["score"],
        "price": sm["price"],
        "level": sm["confirmation"],
        "message": format_smart_map(sm),
    }


def main():
    print(f"GROK GOLD BOT V4.5 SMART MONEY MAP - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Symbol: {SYMBOL}")
    print(f"Smart min score: {SMART_MIN_SCORE}")

    state = load_state()

    frames = fetch_frames()

    m15 = get_tf(frames, "M15")
    h1 = get_tf(frames, "H1")
    h4 = get_tf(frames, "H4")

    if m15 is None or h1 is None or h4 is None:
        raise RuntimeError("Données insuffisantes M15/H1/H4.")

    m15a = analyze(m15, "M15")
    h1a = analyze(h1, "H1")
    h4a = analyze(h4, "H4")
    macro = macro_filter()

    current_price = float(m15a["close"])
    update_tracking(state, current_price)

    sig = score_signal(h4a, h1a, m15a, macro)

    sent = False
    skip_reason = ""
    event_kind = "NONE"
    event_side = "NONE"
    event_score = 0

    if sig["side"] != "HOLD":
        print(
            f"TRADE VALIDÉ | Prix: {sig['price']} | Action: {sig['side']} | "
            f"Score: {sig['score']} | Entry: {sig['entry']} | SL: {sig['sl']} | TP1: {sig['tp1']}"
        )
        event = {
            "kind": "TRADE",
            "side": sig["side"],
            "score": sig["score"],
            "price": sig["price"],
            "entry": sig["entry"],
            "sl": sig["sl"],
            "risk": sig.get("risk", 6.0),
            "message": format_trade(sig),
        }

    else:
        print_hold(sig)

        m5 = fetch_m5_twelvedata()

        if m5 is not None:
            imp = impulse_engine(m5, "M5", m15a, h1a, h4a, macro)
        else:
            print("[M5] Fallback impulse engine sur M15.")
            imp = impulse_engine(m15a["df"], "M15", m15a, h1a, h4a, macro)

        print(
            f"Impulse | Alert={imp.get('alert')} | Side={imp.get('side')} | "
            f"Score={imp.get('score')} | Regime={imp.get('regime')} | "
            f"RangeATR={imp.get('range_ratio')} | Speed3={imp.get('speed_3')} | "
            f"ATRExpansion={imp.get('atr_expansion')}"
        )

        sm = smart_money_engine(m15a, h1a, h4a, macro)

        print(
            f"SmartMap | Alert={sm.get('alert')} | Side={sm.get('side')} | "
            f"Score={sm.get('score')} | Zone={sm.get('zone_name')} {sm.get('zone_level')} | "
            f"Confirm={sm.get('confirmation')} | Invalid={sm.get('invalidation')}"
        )

        formation = detect_formation(sig)

        if imp.get("alert"):
            event = {
                "kind": "IMPULSE",
                "side": imp["side"],
                "score": imp["score"],
                "price": imp["price"],
                "tf": imp.get("tf", ""),
                "level": imp.get("prev_20_high") if imp["side"] == "LONG" else imp.get("prev_20_low"),
                "message": format_impulse(imp),
            }
        elif sm.get("alert"):
            event = smart_event(sm)
        elif formation:
            print(f"Formation détectée: {formation}")
            event = {
                "kind": "FORMATION",
                "side": formation,
                "score": sig["short_score"] if formation == "SHORT" else sig["long_score"],
                "price": sig["price"],
                "level": sig["m15"]["prev_12_low"] if formation == "SHORT" else sig["m15"]["prev_12_high"],
                "message": format_formation(formation, sig),
            }
        else:
            print("Aucune alerte assez propre.")
            event = {
                "kind": "NONE",
                "side": "NONE",
                "score": 0,
                "price": current_price,
                "message": "",
            }

    if event["kind"] != "NONE":
        ok, reason = should_send(state, event)

        if ok:
            print(f"ALERTE ENVOYÉE | Type={event['kind']} | Side={event['side']} | Reason={reason}")
            sent = send_telegram(event["message"])
            if sent:
                register_sent_alert(state, event)
        else:
            skip_reason = reason
            print(f"ALERTE BLOQUÉE ANTI-SPAM | Type={event['kind']} | Side={event['side']} | Reason={reason}")

        event_kind = event["kind"]
        event_side = event["side"]
        event_score = event["score"]

    append_history({
        "time": now_utc().isoformat(),
        "symbol": SYMBOL,
        "kind": event_kind,
        "side": event_side,
        "price": current_price,
        "score": event_score,
        "sent": sent,
        "skip_reason": skip_reason,
        "long_score": sig.get("long_score", ""),
        "short_score": sig.get("short_score", ""),
        "long_gate": sig.get("long_gate", ""),
        "short_gate": sig.get("short_gate", ""),
        "dxy": macro.get("dxy_bias", ""),
        "us10y": macro.get("tnx_bias", ""),
        "m15_bull_sweep": m15a.get("bull_sweep", ""),
        "m15_bear_sweep": m15a.get("bear_sweep", ""),
        "m15_bos_up": m15a.get("bos_up", ""),
        "m15_bos_down": m15a.get("bos_down", ""),
        "m15_top_reject": m15a.get("top_reject", ""),
        "m15_bottom_reject": m15a.get("bottom_reject", ""),
    })

    save_state(state)

    print("Mémoire V4.5 sauvegardée.")


if __name__ == "__main__":
    main()
