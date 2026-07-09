from datetime import datetime, timedelta
from pathlib import Path
import csv
import traceback

import run_once_v54

from run_once_v41 import SYMBOL, send_telegram, cfg
from run_once_v44 import load_state, save_state, now_utc, parse_time
from run_once_v50 import send_error_report


HISTORY_PATH = Path(".bot_state/signal_history.csv")

PENDING_ENABLED = str(cfg("PENDING_ENABLED", "True")).lower() == "true"
PENDING_MIN_SCORE = float(cfg("PENDING_MIN_SCORE", "24"))
PENDING_COOLDOWN_MIN = int(cfg("PENDING_COOLDOWN_MIN", "12"))
PENDING_EXPIRY_MIN = int(cfg("PENDING_EXPIRY_MIN", "45"))
PENDING_BREAK_BUFFER = float(cfg("PENDING_BREAK_BUFFER", "2.0"))
PENDING_MIN_RISK = float(cfg("PENDING_MIN_RISK", "6.0"))


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


def read_rows(limit=20):
    if not HISTORY_PATH.exists():
        return []

    try:
        with HISTORY_PATH.open("r", newline="") as f:
            rows = list(csv.DictReader(f))
            return rows[-limit:]
    except Exception:
        return []


def current_context(rows):
    if not rows:
        return None

    last = rows[-1]

    price = fnum(last.get("price"))
    long_score = fnum(last.get("long_score"))
    short_score = fnum(last.get("short_score"))

    side = last.get("side", "NONE")

    if side not in ("LONG", "SHORT"):
        if long_score >= short_score + 3 and long_score >= PENDING_MIN_SCORE:
            side = "LONG"
        elif short_score >= long_score + 3 and short_score >= PENDING_MIN_SCORE:
            side = "SHORT"
        else:
            side = "NONE"

    score = max(long_score, short_score, fnum(last.get("brain_score")), fnum(last.get("confidence")), fnum(last.get("score")))

    return {
        "row": last,
        "price": price,
        "side": side,
        "score": score,
        "long_score": long_score,
        "short_score": short_score,
        "kind": last.get("kind", "NONE"),
        "source_kind": last.get("source_kind", ""),
        "session": last.get("session", "N/A"),
        "dxy": last.get("dxy", "N/A"),
        "us10y": last.get("us10y", "N/A"),
        "news_guard": last.get("news_guard", "N/A"),
        "skip_reason": last.get("skip_reason", ""),
    }


def price_range(rows):
    prices = [fnum(r.get("price"), None) for r in rows if r.get("price")]
    prices = [p for p in prices if p is not None]

    if len(prices) < 3:
        return 4.0

    return max(prices) - min(prices)


def pending_key(side, price):
    bucket = round(price / 5) * 5
    return f"{side}:{bucket}"


def has_recent_pending(state, key):
    last_map = state.get("last_pending_setup_alerts", {})
    last = last_map.get(key)

    if not last:
        return False

    t = parse_time(last)

    if not t:
        return False

    age = (now_utc() - t).total_seconds() / 60
    return age < PENDING_COOLDOWN_MIN


def register_pending_alert(state, key):
    state.setdefault("last_pending_setup_alerts", {})
    state["last_pending_setup_alerts"][key] = now_utc().isoformat()


def active_same_pending(state, side, price):
    active = state.get("pending_setups", [])

    for p in active:
        if p.get("status") in ("WAITING", "BREAKOUT"):
            if p.get("side") == side and abs(fnum(p.get("base_price")) - price) <= 5:
                return True

    return False


def build_pending_setup(ctx, rows):
    side = ctx["side"]
    price = ctx["price"]

    rng = price_range(rows)
    buffer = max(PENDING_BREAK_BUFFER, rng * 0.35)
    risk = max(PENDING_MIN_RISK, rng * 1.2)

    if side == "LONG":
        action = "BUY"
        trigger = price + buffer
        entry = trigger
        sl = trigger - risk
        tp1 = entry + risk
        tp2 = entry + risk * 1.8
        tp3 = entry + risk * 2.8
        invalidation = sl
        condition = f"cassure au-dessus de {r2(trigger)} + maintien/retest acheteur"
    else:
        action = "SELL"
        trigger = price - buffer
        entry = trigger
        sl = trigger + risk
        tp1 = entry - risk
        tp2 = entry - risk * 1.8
        tp3 = entry - risk * 2.8
        invalidation = sl
        condition = f"cassure sous {r2(trigger)} + maintien/retest vendeur"

    confidence = min(85, max(45, ctx["score"] * 2.1))

    return {
        "id": f"{side}:{r2(price)}:{now_utc().isoformat()}",
        "created_at": now_utc().isoformat(),
        "expires_at": (now_utc() + timedelta(minutes=PENDING_EXPIRY_MIN)).isoformat(),
        "status": "WAITING",
        "side": side,
        "action": action,
        "base_price": r2(price),
        "trigger": r2(trigger),
        "entry": r2(entry),
        "sl": r2(sl),
        "tp1": r2(tp1),
        "tp2": r2(tp2),
        "tp3": r2(tp3),
        "risk": r2(risk),
        "invalidation": r2(invalidation),
        "confidence": r2(confidence),
        "score": r2(ctx["score"]),
        "long_score": r2(ctx["long_score"]),
        "short_score": r2(ctx["short_score"]),
        "session": ctx["session"],
        "dxy": ctx["dxy"],
        "us10y": ctx["us10y"],
        "news_guard": ctx["news_guard"],
        "condition": condition,
        "notified_breakout": False,
        "notified_trade": False,
        "notified_close": False,
        "notified_tp1": False,
    }


def format_pending_created(p):
    emoji = "🟢" if p["action"] == "BUY" else "🔴"

    return f"""<b>🟡 GROK GOLD V5.5 — SETUP EN SURVEILLANCE</b>

<b>Radar :</b> {emoji} {p['action']} {SYMBOL}
<b>Prix actuel :</b> {p['base_price']}
<b>Déclencheur :</b> {p['trigger']}
<b>Condition :</b> {p['condition']}

<b>Plan potentiel :</b>
Entrée : {p['entry']}
SL : {p['sl']}
TP1 : {p['tp1']}
TP2 : {p['tp2']}
TP3 : {p['tp3']}

<b>Confiance radar :</b> {p['confidence']}/100
<b>Score LONG :</b> {p['long_score']}
<b>Score SHORT :</b> {p['short_score']}

<b>Contexte :</b>
Session : {p['session']}
DXY : {p['dxy']}
US10Y : {p['us10y']}
News Guard : {p['news_guard']}

<i>Pas encore un trade. Le bot attend la cassure/retest pour activer BUY ou SELL.</i>"""


def format_breakout(p, price):
    emoji = "🟢" if p["action"] == "BUY" else "🔴"

    return f"""<b>🦅 GROK GOLD V5.5 — CASSURE DÉTECTÉE</b>

<b>Setup :</b> {emoji} {p['action']} {SYMBOL}
<b>Prix actuel :</b> {r2(price)}
<b>Niveau cassé :</b> {p['trigger']}

<b>Lecture :</b>
Le niveau surveillé vient d’être cassé.
Le bot attend maintenant maintien/retest propre avant activation complète.

<b>Entrée prévue :</b> {p['entry']}
<b>SL :</b> {p['sl']}
<b>TP1 :</b> {p['tp1']}

<i>Ne pas courir après le prix si le mouvement est déjà trop loin.</i>"""


def format_trade_activated(p, price):
    emoji = "🟢" if p["action"] == "BUY" else "🔴"

    return f"""<b>{emoji} {p['action']} {SYMBOL} — {p['confidence']}/100</b>

<b>Entrée :</b> {p['entry']}
<b>SL :</b> {p['sl']}
<b>TP1 :</b> {p['tp1']}
<b>TP2 :</b> {p['tp2']}
<b>TP3 :</b> {p['tp3']}

<b>Prix actuel :</b> {r2(price)}
<b>Type :</b> Pending setup confirmé
<b>Validité :</b> immédiate, seulement si le prix respecte le niveau.

<b>Pourquoi :</b>
Cassure du niveau surveillé + maintien du scénario.
DXY : {p['dxy']}
US10Y : {p['us10y']}
Session : {p['session']}

<b>Annulation :</b>
Plan invalide si prix touche {p['sl']}.

<i>Plan automatique. Pas une garantie. Respecter la condition et l’invalidation.</i>"""


def format_pending_closed(p, price, reason):
    emoji = "🟢" if p["action"] == "BUY" else "🔴"

    return f"""<b>❌ GROK GOLD V5.5 — SETUP ANNULÉ</b>

<b>Setup :</b> {emoji} {p['action']} {SYMBOL}
<b>Prix actuel :</b> {r2(price)}
<b>Trigger :</b> {p['trigger']}
<b>Invalidation :</b> {p['invalidation']}

<b>Raison :</b>
{reason}

<i>Ne pas utiliser ce setup.</i>"""


def format_tp1_hit(p, price):
    emoji = "🟢" if p["action"] == "BUY" else "🔴"

    return f"""<b>✅ GROK GOLD V5.5 — TP1 TOUCHÉ</b>

<b>Plan :</b> {emoji} {p['action']} {SYMBOL}
<b>Prix actuel :</b> {r2(price)}
<b>TP1 :</b> {p['tp1']}

<i>Le scénario a donné son premier objectif. Gestion prudente recommandée.</i>"""


def create_new_pending_if_needed(state, ctx, rows):
    if not PENDING_ENABLED:
        return

    if not ctx or ctx["side"] not in ("LONG", "SHORT"):
        print("[V5.5] Aucun côté assez clair pour pending setup.")
        return

    if ctx["kind"] == "TRADE_PLAN" and boolish(ctx["row"].get("sent")):
        print("[V5.5] Plan déjà envoyé, pas de pending.")
        return

    if ctx["score"] < PENDING_MIN_SCORE:
        print(f"[V5.5] Score pending insuffisant: {ctx['score']} < {PENDING_MIN_SCORE}")
        return

    if active_same_pending(state, ctx["side"], ctx["price"]):
        print("[V5.5] Pending similaire déjà actif.")
        return

    key = pending_key(ctx["side"], ctx["price"])

    if has_recent_pending(state, key):
        print("[V5.5] Pending setup bloqué par cooldown.")
        return

    p = build_pending_setup(ctx, rows)

    state.setdefault("pending_setups", [])
    state["pending_setups"].append(p)
    state["pending_setups"] = state["pending_setups"][-40:]

    sent = send_telegram(format_pending_created(p))

    if sent:
        register_pending_alert(state, key)
        print(f"[V5.5] Pending setup envoyé: {p['action']} trigger={p['trigger']}")


def monitor_pending_setups(state, price):
    setups = state.get("pending_setups", [])

    if not setups:
        print("[V5.5] Aucun pending setup à surveiller.")
        return

    now = now_utc()

    for p in setups:
        status = p.get("status")

        if status in ("CLOSED", "EXPIRED"):
            continue

        action = p.get("action")
        trigger = fnum(p.get("trigger"))
        sl = fnum(p.get("sl"))
        tp1 = fnum(p.get("tp1"))
        expires = parse_time(p.get("expires_at", ""))

        close_reason = None

        if expires and now > expires and status in ("WAITING", "BREAKOUT"):
            close_reason = "Temps expiré : le setup n’a pas confirmé dans la fenêtre prévue."

        if action == "BUY" and price <= sl:
            close_reason = "Invalidation touchée : le prix est passé sous le SL."

        if action == "SELL" and price >= sl:
            close_reason = "Invalidation touchée : le prix est passé au-dessus du SL."

        if close_reason:
            p["status"] = "CLOSED"
            p["closed_at"] = now.isoformat()

            if not p.get("notified_close"):
                send_telegram(format_pending_closed(p, price, close_reason))
                p["notified_close"] = True

            continue

        if status == "WAITING":
            breakout = False

            if action == "BUY" and price >= trigger:
                breakout = True

            if action == "SELL" and price <= trigger:
                breakout = True

            if breakout:
                p["status"] = "BREAKOUT"
                p["breakout_at"] = now.isoformat()

                if not p.get("notified_breakout"):
                    send_telegram(format_breakout(p, price))
                    p["notified_breakout"] = True

        elif status == "BREAKOUT":
            breakout_at = parse_time(p.get("breakout_at", ""))

            if breakout_at:
                age_sec = (now - breakout_at).total_seconds()
            else:
                age_sec = 999

            confirmed = False

            if age_sec >= 60:
                if action == "BUY" and price >= trigger:
                    confirmed = True
                if action == "SELL" and price <= trigger:
                    confirmed = True

            if confirmed:
                p["status"] = "TRIGGERED"
                p["triggered_at"] = now.isoformat()

                if not p.get("notified_trade"):
                    send_telegram(format_trade_activated(p, price))
                    p["notified_trade"] = True

        elif status == "TRIGGERED":
            hit_tp1 = False

            if action == "BUY" and price >= tp1:
                hit_tp1 = True

            if action == "SELL" and price <= tp1:
                hit_tp1 = True

            if hit_tp1 and not p.get("notified_tp1"):
                send_telegram(format_tp1_hit(p, price))
                p["notified_tp1"] = True


def main():
    print(f"GROK GOLD BOT V5.5 PENDING SETUP WATCHER - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        run_once_v54.main()

        rows = read_rows()
        ctx = current_context(rows)

        if not ctx:
            print("[V5.5] Historique indisponible.")
            return

        state = load_state()
        price = ctx["price"]

        monitor_pending_setups(state, price)
        create_new_pending_if_needed(state, ctx, rows)

        save_state(state)
        print("[V5.5] Pending watcher terminé.")

    except Exception as e:
        print("[V5.5] Erreur:")
        print(traceback.format_exc())
        send_error_report(e)
        raise


if __name__ == "__main__":
    main()
