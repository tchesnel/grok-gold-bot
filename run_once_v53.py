from datetime import datetime, timedelta
from pathlib import Path
import csv
import json
import traceback

import run_once_v51
import run_once_v52

from run_once_v41 import SYMBOL, send_telegram, cfg
from run_once_v44 import load_state, save_state, now_utc, parse_time
from run_once_v50 import send_error_report


TRADE_PLAN_EXPIRY_MIN = int(cfg("TRADE_PLAN_EXPIRY_MIN", "45"))
PLAN_COOLDOWN_MIN = int(cfg("PLAN_COOLDOWN_MIN", "25"))
HISTORY_PATH = Path(".bot_state/signal_history.csv")

_original_build_trade_plan = run_once_v51.build_trade_plan
_original_register_plan = run_once_v51.register_plan


def fnum(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def r2(x):
    return round(fnum(x), 2)


def read_last_price():
    if not HISTORY_PATH.exists():
        return None

    try:
        with HISTORY_PATH.open("r", newline="") as f:
            rows = list(csv.DictReader(f))

        for r in reversed(rows):
            if r.get("price"):
                return float(r["price"])
    except Exception:
        return None

    return None


def enhance_plan_precision(plan):
    if not plan:
        return None

    action = plan.get("action", "")
    source = plan.get("source_kind", "")
    entry = fnum(plan.get("entry"))
    price = fnum(plan.get("price"))
    risk = max(fnum(plan.get("risk"), 6.0), 4.0)

    if action == "BUY":
        pullback_low = entry - risk * 0.35
        pullback_high = entry + risk * 0.15
        late = price > entry + risk * 0.45
        trigger = "Clôture M5 au-dessus de l’entrée + retest/rejet acheteur."
        cancel = f"Annulation si retour sous {plan.get('sl')} ou si cassure sans retest propre."
    else:
        pullback_low = entry - risk * 0.15
        pullback_high = entry + risk * 0.35
        late = price < entry - risk * 0.45
        trigger = "Clôture M5 sous l’entrée + retest/rejet vendeur."
        cancel = f"Annulation si retour au-dessus de {plan.get('sl')} ou si cassure sans retest propre."

    if source in ("BIG_MOVE", "IMPULSE"):
        entry_mode = "PULLBACK / RETEST"
        trigger = "Le mouvement est déjà parti : attendre pullback dans la zone, puis rejet clair."
    elif source == "TRADE":
        entry_mode = "ENTRÉE VALIDÉE"
        trigger = "Entrée seulement si le prix reste proche du niveau et confirme la direction."
    else:
        entry_mode = "CASSURE + RETEST"

    plan["entry_type"] = entry_mode
    plan["pullback_low"] = r2(pullback_low)
    plan["pullback_high"] = r2(pullback_high)
    plan["trigger_rule"] = trigger
    plan["cancel_rule"] = cancel
    plan["valid_minutes"] = TRADE_PLAN_EXPIRY_MIN
    plan["expires_at"] = (now_utc() + timedelta(minutes=TRADE_PLAN_EXPIRY_MIN)).isoformat()
    plan["late_warning"] = late

    if late:
        plan["entry_type"] = "ATTENDRE PULLBACK"
        plan["trigger_rule"] = "Prix déjà éloigné : ne pas courir après le mouvement. Attendre retour dans la zone de pullback."

    return plan


def build_trade_plan_v53(event, sig, imp, sm, m15a, h1a, h4a, macro, session, guard, current_price):
    plan = _original_build_trade_plan(event, sig, imp, sm, m15a, h1a, h4a, macro, session, guard, current_price)
    return enhance_plan_precision(plan)


def format_trade_plan_v53(plan):
    emoji = "🟢" if plan.get("action") == "BUY" else "🔴"

    reasons = plan.get("reasons", [])
    short_reasons = []

    for r in reasons:
        clean = str(r).replace("✅ ", "").replace("⚠️ ", "")
        short_reasons.append(clean)

    why = " + ".join(short_reasons[:4]) if short_reasons else "Confluence technique suffisante."

    late = ""
    if plan.get("late_warning"):
        late = "\n⚠️ Prix déjà parti : attendre pullback, ne pas entrer en retard.\n"

    news_txt = "OK" if not plan.get("news_guard") else "PRUDENCE"

    return f"""<b>{emoji} {plan['action']} {SYMBOL} — {plan['confidence']}/100</b>

<b>Entrée :</b> {plan['entry']}
<b>SL :</b> {plan['sl']}
<b>TP1 :</b> {plan['tp1']}
<b>TP2 :</b> {plan['tp2']}
<b>TP3 :</b> {plan['tp3']}

<b>Type :</b> {plan.get('entry_type')}
<b>Zone pullback :</b> {plan.get('pullback_low')} / {plan.get('pullback_high')}
<b>Validité :</b> {plan.get('valid_minutes')} min

<b>Déclencheur :</b>
{plan.get('trigger_rule')}

<b>Pourquoi :</b>
{why}

<b>Session :</b> {plan.get('session')}
<b>News :</b> {news_txt}

<b>Annulation :</b>
{plan.get('cancel_rule')}
{late}
<i>Plan automatique. Pas une garantie. Ne pas entrer si la condition n’est pas respectée.</i>"""


def register_plan_v53(state, plan):
    _original_register_plan(state, plan)

    active = state.setdefault("active_trade_plans", [])

    active.append({
        "id": f"{plan.get('action')}:{plan.get('entry')}:{now_utc().isoformat()}",
        "created_at": now_utc().isoformat(),
        "expires_at": plan.get("expires_at"),
        "action": plan.get("action"),
        "side": plan.get("side"),
        "entry": plan.get("entry"),
        "sl": plan.get("sl"),
        "tp1": plan.get("tp1"),
        "tp2": plan.get("tp2"),
        "tp3": plan.get("tp3"),
        "confidence": plan.get("confidence"),
        "status": "ACTIVE",
        "notified_close": False,
    })

    state["active_trade_plans"] = active[-30:]


def format_close_message(plan, reason, price):
    emoji = "🟢" if plan.get("action") == "BUY" else "🔴"

    return f"""<b>⚠️ GROK GOLD — PLAN MIS À JOUR</b>

<b>Plan :</b> {emoji} {plan.get('action')} {SYMBOL}
<b>Entrée :</b> {plan.get('entry')}
<b>Prix actuel :</b> {r2(price)}
<b>SL / Invalidation :</b> {plan.get('sl')}

<b>Statut :</b>
{reason}

<i>Ne pas utiliser ce plan s’il est expiré ou invalidé.</i>"""


def monitor_active_plans():
    state = load_state()
    active = state.get("active_trade_plans", [])

    if not active:
        print("[V5.3] Aucun plan actif à surveiller.")
        return

    price = read_last_price()

    if price is None:
        print("[V5.3] Prix indisponible pour surveillance plans.")
        return

    changed = False
    now = now_utc()

    for p in active:
        if p.get("status") != "ACTIVE":
            continue

        action = p.get("action")
        sl = fnum(p.get("sl"))
        expires_at = parse_time(p.get("expires_at", ""))

        close_reason = None

        if expires_at and now > expires_at:
            close_reason = "⏱️ Plan expiré : la fenêtre d’entrée est terminée."

        if action == "BUY" and price <= sl:
            close_reason = "❌ Plan invalidé : prix passé sous l’invalidation."

        if action == "SELL" and price >= sl:
            close_reason = "❌ Plan invalidé : prix passé au-dessus de l’invalidation."

        if close_reason:
            p["status"] = "CLOSED"
            p["close_reason"] = close_reason
            p["closed_at"] = now.isoformat()
            changed = True

            if not p.get("notified_close"):
                send_telegram(format_close_message(p, close_reason, price))
                p["notified_close"] = True

    if changed:
        save_state(state)
        print("[V5.3] Plans actifs mis à jour.")
    else:
        print("[V5.3] Plans actifs OK.")


def main():
    print(f"GROK GOLD BOT V5.3 PRECISION ENTRY ENGINE - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    run_once_v51.build_trade_plan = build_trade_plan_v53
    run_once_v51.format_trade_plan = format_trade_plan_v53
    run_once_v51.register_plan = register_plan_v53
    run_once_v51.maybe_send_status_report = run_once_v52.maybe_send_status_report_v52

    try:
        run_once_v51.main()
        monitor_active_plans()
    except Exception as e:
        print("[V5.3] Erreur:")
        print(traceback.format_exc())
        send_error_report(e)
        raise


if __name__ == "__main__":
    main()
