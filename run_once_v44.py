import csv
import json
from pathlib import Path
from datetime import datetime, timezone

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
)

from run_once_v42 import detect_formation, format_formation

from run_once_v43 import (
    fetch_m5_twelvedata,
    impulse_engine,
    format_impulse,
)


STATE_DIR = Path(".bot_state")
STATE_FILE = STATE_DIR / "v44_state.json"
HISTORY_FILE = STATE_DIR / "signal_history.csv"

COOLDOWN_SECONDS = {
    "TRADE": 20 * 60,
    "IMPULSE": 30 * 60,
    "FORMATION": 45 * 60,
}

PRIORITY = {
    "NONE": 0,
    "FORMATION": 1,
    "IMPULSE": 2,
    "TRADE": 3,
}


def now_utc():
    return datetime.now(timezone.utc)


def parse_time(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def load_state():
    STATE_DIR.mkdir(exist_ok=True)

    if not STATE_FILE.exists():
        return {
            "last_alerts": {},
            "tracked_alerts": [],
        }

    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {
            "last_alerts": {},
            "tracked_alerts": [],
        }


def save_state(state):
    STATE_DIR.mkdir(exist_ok=True)

    # Limite la taille mémoire
    state["tracked_alerts"] = state.get("tracked_alerts", [])[-200:]

    # Nettoie les vieilles alertes anti-spam
    cleaned = {}
    current = now_utc()

    for k, v in state.get("last_alerts", {}).items():
        t = parse_time(v.get("time", ""))
        if t is None:
            continue
        age_hours = (current - t).total_seconds() / 3600
        if age_hours <= 24:
            cleaned[k] = v

    state["last_alerts"] = cleaned

    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def append_history(row):
    STATE_DIR.mkdir(exist_ok=True)

    fields = [
        "time",
        "symbol",
        "kind",
        "side",
        "price",
        "score",
        "sent",
        "skip_reason",
        "long_score",
        "short_score",
        "long_gate",
        "short_gate",
        "impulse_score",
        "impulse_side",
        "regime",
        "dxy",
        "us10y",
        "m15_bull_sweep",
        "m15_bear_sweep",
        "m15_bos_up",
        "m15_bos_down",
        "m15_top_reject",
        "m15_bottom_reject",
    ]

    exists = HISTORY_FILE.exists()

    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def event_key(event):
    kind = event["kind"]
    side = event["side"]

    if kind == "TRADE":
        return f"TRADE|{side}|entry:{round(event.get('entry', event['price']) / 2) * 2}|sl:{round(event.get('sl', event['price']) / 2) * 2}"

    if kind == "IMPULSE":
        return f"IMPULSE|{side}|tf:{event.get('tf')}|level:{round(event.get('level', event['price']) / 2) * 2}"

    if kind == "FORMATION":
        return f"FORMATION|{side}|confirm:{round(event.get('level', event['price']) / 2) * 2}"

    return "NONE"


def should_send(state, event):
    key = event_key(event)
    last = state.get("last_alerts", {}).get(key)

    if not last:
        return True, "nouvelle alerte"

    last_time = parse_time(last.get("time", ""))
    if last_time is None:
        return True, "ancienne alerte illisible"

    age = (now_utc() - last_time).total_seconds()
    cooldown = COOLDOWN_SECONDS.get(event["kind"], 30 * 60)

    old_score = float(last.get("score", 0))
    new_score = float(event.get("score", 0))

    # Envoie quand même si le score s'améliore très fortement
    if age > 10 * 60 and new_score >= old_score + 14:
        return True, "score fortement amélioré"

    if age < cooldown:
        minutes = int((cooldown - age) / 60)
        return False, f"anti-spam actif encore {minutes} min"

    return True, "cooldown terminé"


def register_sent_alert(state, event):
    key = event_key(event)

    state.setdefault("last_alerts", {})[key] = {
        "time": now_utc().isoformat(),
        "kind": event["kind"],
        "side": event["side"],
        "score": event.get("score", 0),
        "price": event.get("price", 0),
    }

    state.setdefault("tracked_alerts", []).append({
        "time": now_utc().isoformat(),
        "kind": event["kind"],
        "side": event["side"],
        "entry": event.get("entry", event.get("price", 0)),
        "price_start": event.get("price", 0),
        "score": event.get("score", 0),
        "risk": event.get("risk", 6.0),
        "max_favorable": 0.0,
        "max_adverse": 0.0,
        "closed": False,
        "result": "",
    })


def update_tracking(state, current_price):
    current = now_utc()

    for a in state.get("tracked_alerts", []):
        if a.get("closed"):
            continue

        t = parse_time(a.get("time", ""))
        if t is None:
            continue

        age_min = (current - t).total_seconds() / 60
        entry = float(a.get("entry", current_price))
        side = a.get("side", "LONG")

        if side == "LONG":
            move = current_price - entry
        else:
            move = entry - current_price

        a["max_favorable"] = max(float(a.get("max_favorable", 0)), move)
        a["max_adverse"] = min(float(a.get("max_adverse", 0)), move)

        base_risk = abs(float(a.get("risk", 6.0)))
        target = max(base_risk, 5.0)

        # Évaluation après 4h
        if age_min >= 240:
            if a["max_favorable"] >= target:
                a["result"] = "GOOD_MOVE"
            elif a["max_adverse"] <= -target:
                a["result"] = "BAD_MOVE"
            else:
                a["result"] = "NEUTRAL"

            a["closed"] = True


def build_event(sig, imp, formation):
    if sig["side"] != "HOLD":
        return {
            "kind": "TRADE",
            "side": sig["side"],
            "score": sig.get("score", 0),
            "price": sig.get("price", 0),
            "entry": sig.get("entry", sig.get("price", 0)),
            "sl": sig.get("sl", 0),
            "risk": sig.get("risk", 6.0),
            "message": format_trade(sig),
        }

    if imp and imp.get("alert"):
        side = imp.get("side", "LONG")
        level = imp.get("prev_20_high") if side == "LONG" else imp.get("prev_20_low")

        return {
            "kind": "IMPULSE",
            "side": side,
            "score": imp.get("score", 0),
            "price": imp.get("price", 0),
            "tf": imp.get("tf", ""),
            "level": level or imp.get("price", 0),
            "message": format_impulse(imp),
        }

    if formation:
        m15 = sig["m15"]

        if formation == "SHORT":
            level = m15["prev_12_low"]
        else:
            level = m15["prev_12_high"]

        score = sig["short_score"] if formation == "SHORT" else sig["long_score"]

        return {
            "kind": "FORMATION",
            "side": formation,
            "score": score,
            "price": sig.get("price", 0),
            "level": level,
            "message": format_formation(formation, sig),
        }

    return {
        "kind": "NONE",
        "side": "NONE",
        "score": 0,
        "price": sig.get("price", 0),
        "message": "",
    }


def main():
    print(f"GROK GOLD BOT V4.4 GENIUS MEMORY - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Symbol: {SYMBOL}")

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

    if sig["side"] != "HOLD":
        print(
            f"TRADE VALIDÉ | Prix: {sig['price']} | Action: {sig['side']} | "
            f"Score: {sig['score']} | Entry: {sig['entry']} | SL: {sig['sl']} | TP1: {sig['tp1']}"
        )
        imp = None
        formation = None
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

        formation = None

        if not imp.get("alert"):
            formation = detect_formation(sig)
            if formation:
                print(f"Formation détectée: {formation}")
            else:
                print("Aucune formation ou impulsion assez propre pour alerte Telegram.")

    event = build_event(sig, imp, formation)

    sent = False
    skip_reason = ""

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

    append_history({
        "time": now_utc().isoformat(),
        "symbol": SYMBOL,
        "kind": event["kind"],
        "side": event["side"],
        "price": event.get("price", current_price),
        "score": event.get("score", 0),
        "sent": sent,
        "skip_reason": skip_reason,
        "long_score": sig.get("long_score", ""),
        "short_score": sig.get("short_score", ""),
        "long_gate": sig.get("long_gate", ""),
        "short_gate": sig.get("short_gate", ""),
        "impulse_score": imp.get("score", "") if imp else "",
        "impulse_side": imp.get("side", "") if imp else "",
        "regime": imp.get("regime", "") if imp else "",
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

    print("Mémoire V4.4 sauvegardée.")
    print(f"Historique: {HISTORY_FILE}")
    print(f"State: {STATE_FILE}")


if __name__ == "__main__":
    main()
