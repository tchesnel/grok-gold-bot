from datetime import datetime
from pathlib import Path
import csv
import json
import re
import traceback
import requests

import run_once_v61
import run_once_v55

from run_once_v41 import SYMBOL, cfg, send_telegram as base_send_telegram
from run_once_v44 import load_state, save_state, now_utc, parse_time
from run_once_v50 import send_error_report


HISTORY_PATH = Path(".bot_state/signal_history.csv")

TG_TOKEN = cfg("TELEGRAM_BOT_TOKEN", cfg("TELEGRAM_TOKEN", cfg("TG_BOT_TOKEN", "")))
TG_CHAT_ID = cfg("TELEGRAM_CHAT_ID", cfg("TG_CHAT_ID", ""))

GUARDIAN_UPDATE_MIN = int(cfg("GUARDIAN_UPDATE_MIN", "10"))


def fnum(x, default=0.0):
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def r2(x):
    return round(fnum(x), 2)


def telegram_send(text, reply_markup=None):
    if not TG_TOKEN or not TG_CHAT_ID:
        return base_send_telegram(text)

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        r = requests.post(url, data=payload, timeout=10)
        print(f"[TELEGRAM] Status: {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"[TELEGRAM] Erreur send: {e}")
        return False


def parse_plan_from_text(text):
    action = None

    if "BUY" in text:
        action = "BUY"
    elif "SELL" in text:
        action = "SELL"

    def grab(label):
        m = re.search(label + r"\s*:\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
        return fnum(m.group(1), None) if m else None

    return {
        "action": action,
        "entry": grab("Entrée"),
        "sl": grab("SL"),
        "tp1": grab("TP1"),
        "tp2": grab("TP2"),
        "tp3": grab("TP3"),
    }


def make_trade_buttons(text):
    plan = parse_plan_from_text(text)

    if not plan["action"] or plan["entry"] is None:
        return None

    data_base = f"{plan['action']}|{r2(plan['entry'])}"

    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ J’ai pris le trade",
                    "callback_data": f"TAKE|{data_base}",
                }
            ],
            [
                {
                    "text": "❌ Pas pris",
                    "callback_data": f"SKIP|{data_base}",
                }
            ],
        ]
    }


def guardian_send_telegram(text):
    # Boutons seulement sur les vrais plans exploitables.
    if (
        "ORDRE À PRÉPARER MAINTENANT" in text
        or "PLAN ACTIVÉ" in text
        or "BUY XAUUSD" in text
        or "SELL XAUUSD" in text
    ):
        buttons = make_trade_buttons(text)

        if buttons:
            return telegram_send(text, reply_markup=buttons)

    return telegram_send(text)


def answer_callback(callback_id, text):
    if not TG_TOKEN:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
            data={
                "callback_query_id": callback_id,
                "text": text,
                "show_alert": False,
            },
            timeout=8,
        )
    except Exception as e:
        print(f"[V6.2] answerCallback error: {e}")


def get_updates(offset=None):
    if not TG_TOKEN:
        return []

    params = {
        "timeout": 1,
        "allowed_updates": json.dumps(["callback_query"]),
    }

    if offset:
        params["offset"] = offset

    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params=params,
            timeout=8,
        )
        js = r.json()
        return js.get("result", [])
    except Exception as e:
        print(f"[V6.2] getUpdates error: {e}")
        return []


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


def find_plan_in_state(state, action, entry, fallback=None):
    entry = fnum(entry)

    for p in reversed(state.get("pending_setups", [])):
        if p.get("action") == action and abs(fnum(p.get("entry")) - entry) <= 0.3:
            return {
                "action": p.get("action"),
                "entry": fnum(p.get("entry")),
                "sl": fnum(p.get("sl")),
                "tp1": fnum(p.get("tp1")),
                "tp2": fnum(p.get("tp2")),
                "tp3": fnum(p.get("tp3")),
                "confidence": fnum(p.get("confidence")),
                "source": "pending_setup",
            }

    for p in reversed(state.get("active_trade_plans", [])):
        if p.get("action") == action and abs(fnum(p.get("entry")) - entry) <= 0.3:
            return {
                "action": p.get("action"),
                "entry": fnum(p.get("entry")),
                "sl": fnum(p.get("sl")),
                "tp1": fnum(p.get("tp1")),
                "tp2": fnum(p.get("tp2")),
                "tp3": fnum(p.get("tp3")),
                "confidence": fnum(p.get("confidence")),
                "source": "trade_plan",
            }

    if fallback:
        return {
            "action": fallback.get("action"),
            "entry": fnum(fallback.get("entry")),
            "sl": fnum(fallback.get("sl")),
            "tp1": fnum(fallback.get("tp1")),
            "tp2": fnum(fallback.get("tp2")),
            "tp3": fnum(fallback.get("tp3")),
            "confidence": 0,
            "source": "telegram_text",
        }

    return None


def activate_user_trade(state, plan):
    trade_id = f"{plan['action']}:{r2(plan['entry'])}"

    active = state.setdefault("user_active_trades", [])

    for t in active:
        if t.get("id") == trade_id and t.get("status") in ("WATCHING_ENTRY", "ACTIVE"):
            return False, "Trade déjà suivi."

    active.append({
        "id": trade_id,
        "created_at": now_utc().isoformat(),
        "action": plan["action"],
        "entry": r2(plan["entry"]),
        "sl": r2(plan["sl"]),
        "tp1": r2(plan["tp1"]),
        "tp2": r2(plan["tp2"]),
        "tp3": r2(plan["tp3"]),
        "confidence": r2(plan.get("confidence", 0)),
        "source": plan.get("source", ""),
        "status": "WATCHING_ENTRY",
        "entry_triggered": False,
        "notified_entry": False,
        "notified_tp1": False,
        "notified_tp2": False,
        "notified_tp3": False,
        "notified_sl": False,
        "last_guardian_update_at": "",
    })

    state["user_active_trades"] = active[-20:]

    return True, "Suivi activé."


def format_trade_taken(trade):
    emoji = "🟢" if trade["action"] == "BUY" else "🔴"

    return f"""<b>🛡️ GROK GOLD V6.2 — TRADE GUARDIAN ACTIVÉ</b>

<b>Trade suivi :</b> {emoji} {trade['action']} {SYMBOL}

<b>Entrée :</b> {trade['entry']}
<b>SL :</b> {trade['sl']}
<b>TP1 :</b> {trade['tp1']}
<b>TP2 :</b> {trade['tp2']}
<b>TP3 :</b> {trade['tp3']}

Le bot va maintenant suivre ce trade et t’envoyer :
• entrée déclenchée
• trade propre / attention
• TP1 / TP2 / TP3
• SL ou invalidation

<i>Le bot suit le plan que tu as validé. Il ne sait pas si tu modifies ton trade manuellement.</i>"""


def format_not_taken(action, entry):
    return f"""<b>👌 GROK GOLD — TRADE NON SUIVI</b>

Tu as indiqué ne pas prendre le plan :

<b>{action} {SYMBOL}</b>
Entrée : {entry}

Le bot ne suivra pas ce trade et pourra proposer un autre plan plus tard."""


def process_trade_buttons():
    state = load_state()
    offset = state.get("telegram_update_offset")

    updates = get_updates(offset)

    if not updates:
        print("[V6.2] Aucun clic Telegram à traiter.")
        return

    max_update_id = offset or 0

    for u in updates:
        max_update_id = max(max_update_id, int(u.get("update_id", 0)) + 1)

        cb = u.get("callback_query")

        if not cb:
            continue

        data = cb.get("data", "")
        callback_id = cb.get("id")
        message_text = cb.get("message", {}).get("text", "")

        parts = data.split("|")

        if len(parts) != 3:
            answer_callback(callback_id, "Commande inconnue.")
            continue

        action_type, action, entry = parts
        entry = fnum(entry)

        fallback = parse_plan_from_text(message_text)
        plan = find_plan_in_state(state, action, entry, fallback=fallback)

        if action_type == "SKIP":
            answer_callback(callback_id, "Ok, pas de suivi.")
            telegram_send(format_not_taken(action, entry))
            continue

        if action_type == "TAKE":
            if not plan:
                answer_callback(callback_id, "Plan introuvable.")
                telegram_send("⚠️ Impossible de retrouver ce plan dans la mémoire du bot.")
                continue

            ok, msg = activate_user_trade(state, plan)
            answer_callback(callback_id, msg)

            if ok:
                trade = state["user_active_trades"][-1]
                telegram_send(format_trade_taken(trade))

    state["telegram_update_offset"] = max_update_id
    save_state(state)
    print("[V6.2] Clics Telegram traités.")


def is_favorable(action, entry, price):
    if action == "BUY":
        return price - entry
    return entry - price


def format_guardian_update(trade, price, status_text):
    action = trade["action"]
    emoji = "🟢" if action == "BUY" else "🔴"

    entry = fnum(trade["entry"])
    sl = fnum(trade["sl"])
    tp1 = fnum(trade["tp1"])

    move = is_favorable(action, entry, price)

    if action == "BUY":
        dist_tp1 = tp1 - price
        dist_sl = price - sl
    else:
        dist_tp1 = price - tp1
        dist_sl = sl - price

    return f"""<b>📡 GROK GOLD — SUIVI TRADE</b>

<b>Trade :</b> {emoji} {action} {SYMBOL}
<b>Entrée :</b> {trade['entry']}
<b>Prix actuel :</b> {r2(price)}

<b>SL :</b> {trade['sl']}
<b>TP1 :</b> {trade['tp1']}
<b>TP2 :</b> {trade['tp2']}
<b>TP3 :</b> {trade['tp3']}

<b>État :</b>
{status_text}

<b>Mouvement depuis entrée :</b> {r2(move)} $
<b>Distance TP1 :</b> {r2(dist_tp1)} $
<b>Distance SL :</b> {r2(dist_sl)} $

<i>Suivi automatique du trade que tu as validé.</i>"""


def monitor_user_trades():
    state = load_state()
    trades = state.get("user_active_trades", [])

    if not trades:
        print("[V6.2] Aucun trade utilisateur à suivre.")
        return

    price = read_last_price()

    if price is None:
        print("[V6.2] Prix indisponible pour suivi trade.")
        return

    changed = False
    now = now_utc()

    for t in trades:
        if t.get("status") in ("CLOSED", "TP3_DONE", "SL_HIT"):
            continue

        action = t.get("action")
        entry = fnum(t.get("entry"))
        sl = fnum(t.get("sl"))
        tp1 = fnum(t.get("tp1"))
        tp2 = fnum(t.get("tp2"))
        tp3 = fnum(t.get("tp3"))

        triggered = t.get("entry_triggered", False)

        if not triggered:
            if action == "BUY" and price >= entry:
                triggered = True
            if action == "SELL" and price <= entry:
                triggered = True

            if triggered:
                t["entry_triggered"] = True
                t["status"] = "ACTIVE"
                t["triggered_at"] = now.isoformat()
                changed = True

                if not t.get("notified_entry"):
                    telegram_send(format_guardian_update(t, price, "✅ Entrée déclenchée. Le trade est maintenant suivi en actif."))
                    t["notified_entry"] = True

        if t.get("entry_triggered"):
            if action == "BUY":
                sl_hit = price <= sl
                tp1_hit = price >= tp1
                tp2_hit = price >= tp2
                tp3_hit = price >= tp3
            else:
                sl_hit = price >= sl
                tp1_hit = price <= tp1
                tp2_hit = price <= tp2
                tp3_hit = price <= tp3

            if sl_hit and not t.get("notified_sl"):
                t["status"] = "SL_HIT"
                t["closed_at"] = now.isoformat()
                t["notified_sl"] = True
                changed = True
                telegram_send(format_guardian_update(t, price, "❌ SL / invalidation touchée. Le plan est terminé."))

            elif tp3_hit and not t.get("notified_tp3"):
                t["status"] = "TP3_DONE"
                t["notified_tp3"] = True
                changed = True
                telegram_send(format_guardian_update(t, price, "🚀 TP3 touché. Très gros mouvement favorable."))

            elif tp2_hit and not t.get("notified_tp2"):
                t["notified_tp2"] = True
                changed = True
                telegram_send(format_guardian_update(t, price, "✅ TP2 touché. Le trade se déroule très bien."))

            elif tp1_hit and not t.get("notified_tp1"):
                t["notified_tp1"] = True
                changed = True
                telegram_send(format_guardian_update(t, price, "✅ TP1 touché. Premier objectif atteint."))

            else:
                last_update = parse_time(t.get("last_guardian_update_at", ""))

                due = False
                if not last_update:
                    due = True
                else:
                    age = (now - last_update).total_seconds() / 60
                    due = age >= GUARDIAN_UPDATE_MIN

                if due:
                    move = is_favorable(action, entry, price)

                    if move > 0:
                        text = "🟢 Le trade va dans le bon sens. Scénario encore propre."
                    elif move < 0:
                        text = "⚠️ Le prix revient contre l’entrée, mais le SL n’est pas touché."
                    else:
                        text = "⏸️ Le trade stagne autour de l’entrée."

                    telegram_send(format_guardian_update(t, price, text))
                    t["last_guardian_update_at"] = now.isoformat()
                    changed = True

    if changed:
        save_state(state)

    print("[V6.2] Suivi trades utilisateur terminé.")


def main():
    print(f"GROK GOLD BOT V6.2 TELEGRAM TRADE GUARDIAN - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    run_once_v55.send_telegram = guardian_send_telegram

    try:
        process_trade_buttons()
        run_once_v61.main()
        process_trade_buttons()
        monitor_user_trades()
    except Exception as e:
        print("[V6.2] Erreur:")
        print(traceback.format_exc())
        send_error_report(e)
        raise


if __name__ == "__main__":
    main()
