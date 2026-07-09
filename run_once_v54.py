from datetime import datetime
from pathlib import Path
import csv
import json
import traceback

import run_once_v53

from run_once_v41 import SYMBOL, send_telegram, cfg
from run_once_v44 import load_state, save_state, now_utc, parse_time
from run_once_v50 import send_error_report


HISTORY_PATH = Path(".bot_state/signal_history.csv")

HAWK_EYE_ENABLED = str(cfg("HAWK_EYE_ENABLED", "True")).lower() == "true"
HAWK_EYE_MOVE_USD = float(cfg("HAWK_EYE_MOVE_USD", "6"))
HAWK_EYE_SCORE_MIN = float(cfg("HAWK_EYE_SCORE_MIN", "50"))
HAWK_EYE_COOLDOWN_MIN = int(cfg("HAWK_EYE_COOLDOWN_MIN", "8"))


def fnum(x, default=0.0):
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def read_rows(limit=20):
    if not HISTORY_PATH.exists():
        return []

    try:
        with HISTORY_PATH.open("r", newline="") as f:
            rows = list(csv.DictReader(f))
            return rows[-limit:]
    except Exception:
        return []


def best_side(row):
    side = row.get("side", "NONE")

    if side in ("LONG", "SHORT"):
        return side

    long_score = fnum(row.get("long_score"))
    short_score = fnum(row.get("short_score"))

    if long_score > short_score:
        return "LONG"

    if short_score > long_score:
        return "SHORT"

    return "NONE"


def action_from_side(side):
    if side == "LONG":
        return "BUY"
    if side == "SHORT":
        return "SELL"
    return "WAIT"


def hawk_key(alert_type, side, price):
    bucket = round(fnum(price) / 3) * 3
    return f"{alert_type}:{side}:{bucket}"


def hawk_should_send(state, key):
    sent = state.get("last_hawk_eye_alerts", {})
    last = sent.get(key)

    if last:
        t = parse_time(last)

        if t:
            age = (now_utc() - t).total_seconds() / 60

            if age < HAWK_EYE_COOLDOWN_MIN:
                return False, f"cooldown Hawk Eye encore {int(HAWK_EYE_COOLDOWN_MIN - age)} min"

    return True, "nouvelle pré-alerte Hawk Eye"


def register_hawk(state, key):
    state.setdefault("last_hawk_eye_alerts", {})
    state["last_hawk_eye_alerts"][key] = now_utc().isoformat()


def build_hawk_alert(rows):
    if not HAWK_EYE_ENABLED or len(rows) < 2:
        return None

    last = rows[-1]

    price = fnum(last.get("price"))
    kind = last.get("kind", "NONE")
    source = last.get("source_kind", kind)
    side = best_side(last)
    action = action_from_side(side)

    long_score = fnum(last.get("long_score"))
    short_score = fnum(last.get("short_score"))
    brain = fnum(last.get("brain_score"))
    confidence = fnum(last.get("confidence"))
    score = max(long_score, short_score, brain, confidence, fnum(last.get("score")))

    session = last.get("session", "N/A")
    dxy = last.get("dxy", "N/A")
    us10y = last.get("us10y", "N/A")
    skip = last.get("skip_reason", "")

    move = 0.0

    if len(rows) >= 4:
        old_price = fnum(rows[-4].get("price"), price)
        move = price - old_price

    alert_type = None
    title = None
    reason = None

    # 1) Accélération brutale du prix.
    if abs(move) >= HAWK_EYE_MOVE_USD:
        side = "LONG" if move > 0 else "SHORT"
        action = action_from_side(side)
        alert_type = "ACCELERATION"
        title = "🦅 HAWK EYE — ACCÉLÉRATION DÉTECTÉE"
        reason = f"Le prix a bougé de {round(move, 2)} $ sur les derniers cycles."

    # 2) Big move détecté par le moteur.
    elif kind == "BIG_MOVE" or source == "BIG_MOVE":
        alert_type = "BIG_MOVE"
        title = "🦅 HAWK EYE — BIG MOVE EN COURS"
        reason = "Le moteur détecte un gros mouvement potentiel."

    # 3) Formation ou impulsion intéressante mais pas encore assez validée.
    elif kind in ("FORMATION", "IMPULSE") and score >= HAWK_EYE_SCORE_MIN:
        alert_type = "PRE_SIGNAL"
        title = "🦅 HAWK EYE — PRÉ-SIGNAL À SURVEILLER"
        reason = "Le bot voit une opportunité, mais elle n’est pas encore un plan BUY/SELL validé."

    if not alert_type:
        return None

    key = hawk_key(alert_type, side, price)

    msg = f"""<b>{title}</b>

<b>Actif :</b> {SYMBOL}
<b>Action radar :</b> {action}
<b>Prix :</b> {round(price, 2)}
<b>Signal brut :</b> {kind} {side}

<b>Score LONG :</b> {long_score}
<b>Score SHORT :</b> {short_score}
<b>Score max :</b> {round(score, 1)}

<b>Contexte :</b>
Session : {session}
DXY : {dxy}
US10Y : {us10y}

<b>Pourquoi :</b>
{reason}

<b>Lecture :</b>
Le bot est en surveillance active. Ce n’est pas encore forcément une entrée.
Il attend cassure/retest/confiance suffisante pour envoyer un vrai plan avec Entrée, SL et TP.

<b>Dernier blocage :</b>
{skip or "Aucun blocage majeur affiché."}

<i>Mode œil de lynx : pré-alerte rapide avant signal complet.</i>"""

    return key, msg


def maybe_send_hawk_eye():
    rows = read_rows()

    alert = build_hawk_alert(rows)

    if not alert:
        print("[V5.4] Hawk Eye: rien d’assez fort.")
        return

    key, msg = alert
    state = load_state()

    ok, reason = hawk_should_send(state, key)

    if not ok:
        print(f"[V5.4] Hawk Eye bloqué: {reason}")
        return

    print(f"[V5.4] Hawk Eye envoyé: {key}")
    sent = send_telegram(msg)

    if sent:
        register_hawk(state, key)
        save_state(state)


def main():
    print(f"GROK GOLD BOT V5.4 HAWK EYE MODE - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        run_once_v53.main()
        maybe_send_hawk_eye()
    except Exception as e:
        print("[V5.4] Erreur:")
        print(traceback.format_exc())
        send_error_report(e)
        raise


if __name__ == "__main__":
    main()
