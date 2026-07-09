from datetime import datetime, timedelta
from pathlib import Path
import csv
import traceback

import run_once_v55

from run_once_v41 import SYMBOL, send_telegram, cfg
from run_once_v44 import load_state, save_state, now_utc, parse_time
from run_once_v50 import send_error_report


HISTORY_PATH = Path(".bot_state/signal_history.csv")

ANTICIPATION_ENABLED = str(cfg("ANTICIPATION_ENABLED", "True")).lower() == "true"
SCENARIO_REPORT_MIN = int(cfg("SCENARIO_REPORT_MIN", "20"))
SCENARIO_ALERT_MIN = float(cfg("SCENARIO_ALERT_MIN", "62"))
SCENARIO_COOLDOWN_MIN = int(cfg("SCENARIO_COOLDOWN_MIN", "10"))


def fnum(x, default=0.0):
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def r2(x):
    return round(fnum(x), 2)


def boolish(v):
    return str(v).lower() in ("true", "1", "yes")


def read_rows(limit=30):
    if not HISTORY_PATH.exists():
        return []

    try:
        with HISTORY_PATH.open("r", newline="") as f:
            rows = list(csv.DictReader(f))
            return rows[-limit:]
    except Exception:
        return []


def latest(rows):
    return rows[-1] if rows else {}


def recent_prices(rows):
    prices = []

    for r in rows:
        if r.get("price"):
            prices.append(fnum(r.get("price")))

    return prices


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, round(fnum(x), 1)))


def detect_regime(rows):
    if not rows:
        return "UNKNOWN", "Pas assez d’historique."

    last = latest(rows)
    prices = recent_prices(rows[-8:])

    if boolish(last.get("news_guard")):
        return "NEWS VOLATILITY", "News Guard actif : priorité à la prudence."

    if len(prices) < 4:
        return "STARTING", "Le bot collecte encore les derniers cycles."

    rng = max(prices) - min(prices)
    move = prices[-1] - prices[0]

    if rng <= 2.2:
        return "COMPRESSION", "Le prix se contracte : possible préparation de cassure."

    if abs(move) >= max(4.0, rng * 0.55):
        if move > 0:
            return "TREND / ACCÉLÉRATION LONG", "Le prix accélère vers le haut."
        return "TREND / ACCÉLÉRATION SHORT", "Le prix accélère vers le bas."

    if rng <= 5.0:
        return "RANGE CALME", "Le marché travaille en range, peu directionnel."

    return "CHOP / MIXTE", "Marché irrégulier : faux signaux possibles."


def active_pending_bias(state):
    active = state.get("pending_setups", [])
    buy = 0
    sell = 0
    details = []

    for p in active[-10:]:
        if p.get("status") not in ("WAITING", "BREAKOUT", "TRIGGERED"):
            continue

        action = p.get("action")

        if action == "BUY":
            buy += 12
        elif action == "SELL":
            sell += 12

        details.append(
            f"{action} {p.get('status')} | trigger {p.get('trigger')} | SL {p.get('sl')}"
        )

    return buy, sell, details[-3:]


def score_scenarios(rows, state):
    last = latest(rows)

    long_score = fnum(last.get("long_score"))
    short_score = fnum(last.get("short_score"))
    brain = fnum(last.get("brain_score"))
    confidence = fnum(last.get("confidence"))

    buy = long_score * 1.35
    sell = short_score * 1.35
    wait = 35

    dxy = str(last.get("dxy", "")).upper()
    us10y = str(last.get("us10y", "")).upper()
    session = str(last.get("session", ""))
    regime, regime_txt = detect_regime(rows)

    if dxy == "DOWN":
        buy += 10
        sell -= 6
    elif dxy == "UP":
        sell += 10
        buy -= 6

    if us10y == "DOWN":
        buy += 6
    elif us10y == "UP":
        sell += 6

    if boolish(last.get("m15_bos_up")):
        buy += 14
    if boolish(last.get("m15_bos_down")):
        sell += 14

    if boolish(last.get("m15_bull_sweep")):
        buy += 10
    if boolish(last.get("m15_bear_sweep")):
        sell += 10

    if boolish(last.get("m15_top_reject")):
        sell += 8
    if boolish(last.get("m15_bottom_reject")):
        buy += 8

    if brain:
        if last.get("side") == "LONG":
            buy += brain * 0.25
        elif last.get("side") == "SHORT":
            sell += brain * 0.25

    if confidence:
        if last.get("side") == "LONG":
            buy += confidence * 0.18
        elif last.get("side") == "SHORT":
            sell += confidence * 0.18

    if session in ("LONDON", "NEW YORK OPEN", "NEW YORK"):
        buy += 5
        sell += 5
    elif session == "ASIA":
        wait += 8
        buy -= 3
        sell -= 3
    elif session == "OFF HOURS":
        wait += 15
        buy -= 7
        sell -= 7

    if "COMPRESSION" in regime:
        buy += 5
        sell += 5
        wait += 8

    if "ACCÉLÉRATION LONG" in regime:
        buy += 12
        sell -= 5
    elif "ACCÉLÉRATION SHORT" in regime:
        sell += 12
        buy -= 5

    if "RANGE" in regime or "CHOP" in regime:
        wait += 12
        buy -= 4
        sell -= 4

    if boolish(last.get("news_guard")):
        wait += 30
        buy -= 18
        sell -= 18

    if last.get("long_gate") == "False" and last.get("short_gate") == "False":
        wait += 10

    pb, ps, pending_details = active_pending_bias(state)
    buy += pb
    sell += ps

    buy = clamp(buy)
    sell = clamp(sell)
    wait = clamp(wait)

    return {
        "buy": buy,
        "sell": sell,
        "wait": wait,
        "regime": regime,
        "regime_txt": regime_txt,
        "pending_details": pending_details,
    }


def build_levels(rows, scores):
    last = latest(rows)
    prices = recent_prices(rows[-10:])

    price = fnum(last.get("price"))

    if len(prices) >= 3:
        rng = max(prices) - min(prices)
    else:
        rng = 4.0

    buffer = max(2.0, rng * 0.45)
    risk = max(6.0, rng * 1.4)

    buy_trigger = price + buffer
    sell_trigger = price - buffer

    return {
        "price": r2(price),

        "buy_trigger": r2(buy_trigger),
        "buy_entry": r2(buy_trigger),
        "buy_sl": r2(buy_trigger - risk),
        "buy_tp1": r2(buy_trigger + risk),
        "buy_tp2": r2(buy_trigger + risk * 1.8),
        "buy_tp3": r2(buy_trigger + risk * 2.8),

        "sell_trigger": r2(sell_trigger),
        "sell_entry": r2(sell_trigger),
        "sell_sl": r2(sell_trigger + risk),
        "sell_tp1": r2(sell_trigger - risk),
        "sell_tp2": r2(sell_trigger - risk * 1.8),
        "sell_tp3": r2(sell_trigger - risk * 2.8),

        "risk": r2(risk),
        "buffer": r2(buffer),
    }


def decide_priority(scores):
    buy = scores["buy"]
    sell = scores["sell"]
    wait = scores["wait"]

    if wait >= buy and wait >= sell:
        return "WAIT", "Le scénario dominant reste l’attente."

    if buy >= sell and buy >= SCENARIO_ALERT_MIN:
        return "BUY", "Le scénario BUY devient prioritaire si la cassure confirme."

    if sell > buy and sell >= SCENARIO_ALERT_MIN:
        return "SELL", "Le scénario SELL devient prioritaire si la cassure confirme."

    return "WAIT", "Aucun scénario directionnel n’a assez de force."


def format_anticipation_message(rows, state):
    last = latest(rows)
    scores = score_scenarios(rows, state)
    levels = build_levels(rows, scores)
    priority, priority_txt = decide_priority(scores)

    dxy = last.get("dxy", "N/A")
    us10y = last.get("us10y", "N/A")
    session = last.get("session", "N/A")
    news = last.get("news_guard", "N/A")

    pending = scores.get("pending_details") or []
    pending_txt = "\n".join([f"• {x}" for x in pending]) if pending else "• Aucun pending setup prioritaire actif."

    return f"""<b>🧠 GROK GOLD V6.0 — MARKET ANTICIPATION BRAIN</b>

<b>Actif :</b> {SYMBOL}
<b>Prix :</b> {levels['price']}
<b>Session :</b> {session}
<b>Régime :</b> {scores['regime']}
<b>News Guard :</b> {news}

<b>Lecture du marché :</b>
{scores['regime_txt']}

<b>Scénario BUY :</b> {scores['buy']}/100
Condition : cassure au-dessus {levels['buy_trigger']} + maintien/retest M5.
Entrée potentielle : {levels['buy_entry']}
SL : {levels['buy_sl']}
TP1 : {levels['buy_tp1']}
TP2 : {levels['buy_tp2']}
TP3 : {levels['buy_tp3']}

<b>Scénario SELL :</b> {scores['sell']}/100
Condition : cassure sous {levels['sell_trigger']} + maintien/retest M5.
Entrée potentielle : {levels['sell_entry']}
SL : {levels['sell_sl']}
TP1 : {levels['sell_tp1']}
TP2 : {levels['sell_tp2']}
TP3 : {levels['sell_tp3']}

<b>Scénario NO TRADE :</b> {scores['wait']}/100
Raison : marché pas assez clair, gate false, range/chop ou horaire moins liquide.

<b>Priorité actuelle :</b>
{priority} — {priority_txt}

<b>Macro :</b>
DXY : {dxy}
US10Y : {us10y}

<b>Pending setups actifs :</b>
{pending_txt}

<b>Ce qui ferait agir le bot :</b>
• Cassure propre du niveau.
• Maintien/retest confirmé.
• Pas de news USD dangereuse.
• Confiance suffisante pour transformer le scénario en plan BUY/SELL.

<i>Ce message anticipe les scénarios. Ce n’est pas encore une entrée tant que le bot n’envoie pas un plan BUY/SELL validé.</i>"""


def scenario_key(rows, state):
    scores = score_scenarios(rows, state)
    levels = build_levels(rows, scores)
    priority, _ = decide_priority(scores)

    bucket = round(fnum(levels["price"]) / 3) * 3

    return f"{priority}:{scores['regime']}:{bucket}:{round(scores['buy'])}:{round(scores['sell'])}"


def should_send_anticipation(state, rows):
    if not ANTICIPATION_ENABLED:
        return False, "anticipation disabled"

    if not rows:
        return False, "no rows"

    scores = score_scenarios(rows, state)
    key = scenario_key(rows, state)

    last_report = parse_time(state.get("last_anticipation_report_time", ""))
    last_alert = parse_time(state.get("last_anticipation_alert_time", ""))
    last_key = state.get("last_anticipation_key")

    now = now_utc()

    report_due = True

    if last_report:
        age = (now - last_report).total_seconds() / 60
        report_due = age >= SCENARIO_REPORT_MIN

    strong = max(scores["buy"], scores["sell"]) >= SCENARIO_ALERT_MIN

    alert_due = True

    if last_alert:
        age_alert = (now - last_alert).total_seconds() / 60
        alert_due = age_alert >= SCENARIO_COOLDOWN_MIN

    if strong and key != last_key and alert_due:
        return True, "nouveau scénario fort"

    if report_due:
        return True, "rapport anticipation régulier"

    return False, "pas dû"


def maybe_send_anticipation():
    rows = read_rows()

    if not rows:
        print("[V6.0] Pas d’historique pour anticipation.")
        return

    state = load_state()
    ok, reason = should_send_anticipation(state, rows)

    if not ok:
        print(f"[V6.0] Anticipation non envoyée: {reason}")
        return

    msg = format_anticipation_message(rows, state)

    print(f"[V6.0] Envoi anticipation Telegram: {reason}")
    sent = send_telegram(msg)

    if sent:
        state["last_anticipation_report_time"] = now_utc().isoformat()

        scores = score_scenarios(rows, state)

        if max(scores["buy"], scores["sell"]) >= SCENARIO_ALERT_MIN:
            state["last_anticipation_alert_time"] = now_utc().isoformat()

        state["last_anticipation_key"] = scenario_key(rows, state)
        save_state(state)


def main():
    print(f"GROK GOLD BOT V6.0 MARKET ANTICIPATION BRAIN - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        run_once_v55.main()
        maybe_send_anticipation()
    except Exception as e:
        print("[V6.0] Erreur:")
        print(traceback.format_exc())
        send_error_report(e)
        raise


if __name__ == "__main__":
    main()
