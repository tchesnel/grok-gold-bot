from datetime import datetime
from pathlib import Path
import csv
import traceback

import run_once_v51

from run_once_v41 import SYMBOL, send_telegram, cfg
from run_once_v44 import load_state, save_state, now_utc
from run_once_v50 import send_error_report


STATUS_REPORT_MIN = int(cfg("STATUS_REPORT_MIN", "30"))
TRADE_PLAN_MIN_CONF = float(cfg("TRADE_PLAN_MIN_CONF", "68"))
HISTORY_PATH = Path(".bot_state/signal_history.csv")


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def boolish(v):
    return str(v).lower() in ("true", "1", "yes")


def read_rows(limit=60):
    if not HISTORY_PATH.exists():
        return []

    try:
        with HISTORY_PATH.open("r", newline="") as f:
            rows = list(csv.DictReader(f))
            return rows[-limit:]
    except Exception:
        return []


def latest_value(rows, key, default="N/A"):
    for r in reversed(rows):
        v = r.get(key)
        if v not in (None, ""):
            return v
    return default


def explain_no_plan(last):
    if not last:
        return "• Historique encore vide. Le bot vient de redémarrer."

    kind = last.get("kind", "NONE")
    source = last.get("source_kind", "")
    skip = str(last.get("skip_reason", ""))
    long_score = last.get("long_score", "N/A")
    short_score = last.get("short_score", "N/A")
    long_gate = last.get("long_gate", "N/A")
    short_gate = last.get("short_gate", "N/A")
    confidence = last.get("confidence", "")
    tier = last.get("tier", "")
    brain = last.get("brain_score", "")

    reasons = []

    if kind == "TRADE_PLAN" and boolish(last.get("sent")):
        return "• Un plan BUY/SELL a bien été envoyé récemment."

    if "aucun setup exploitable" in skip.lower():
        reasons.append("Aucun setup exploitable : pas assez de cassure, impulsion ou confirmation propre.")

    if "confiance insuffisante" in skip.lower():
        reasons.append(f"Le bot a vu quelque chose, mais la confiance est sous le minimum demandé ({TRADE_PLAN_MIN_CONF}/100).")

    if "filtré" in skip.lower() or "tier" in skip.lower():
        reasons.append("Le Confluence Brain a filtré le signal : trop peu de confluences ou setup trop faible.")

    if "cooldown" in skip.lower() or "similaire" in skip.lower():
        reasons.append("Un plan similaire a déjà été détecté récemment : le bot évite de spammer.")

    if long_gate == "False" and short_gate == "False":
        reasons.append("Gate LONG et Gate SHORT sont à False : le moteur principal n’a pas de confirmation complète.")

    if kind == "FORMATION":
        reasons.append(f"Il voit une formation {last.get('side', '')}, mais elle n’est pas assez forte pour devenir un plan BUY/SELL.")

    if source == "BIG_MOVE":
        reasons.append("Un gros mouvement a été détecté, mais il faut une entrée exploitable ou un pullback propre.")

    if not reasons:
        reasons.append("Les conditions complètes ne sont pas réunies : structure + macro + timing + confiance.")

    reasons.append(f"Score LONG : {long_score} / Score SHORT : {short_score}.")
    reasons.append(f"Gate LONG : {long_gate} / Gate SHORT : {short_gate}.")

    if tier or brain:
        reasons.append(f"Tier : {tier or 'N/A'} / Brain score : {brain or 'N/A'}.")

    if confidence:
        reasons.append(f"Confiance plan : {confidence}/100.")

    return "\n".join([f"• {r}" for r in reasons])


def build_status_message(rows):
    last = rows[-1] if rows else {}

    total = len(rows)

    plans = [r for r in rows if r.get("kind") == "TRADE_PLAN"]
    plans_sent = [r for r in plans if boolish(r.get("sent"))]

    formations = [r for r in rows if r.get("kind") == "FORMATION" or r.get("source_kind") == "FORMATION"]
    impulses = [r for r in rows if r.get("kind") == "IMPULSE" or r.get("source_kind") == "IMPULSE"]
    big_moves = [r for r in rows if r.get("source_kind") == "BIG_MOVE"]
    no_setup = [r for r in rows if r.get("kind") == "NONE"]

    filtered = [
        r for r in rows
        if "filtré" in str(r.get("skip_reason", "")).lower()
        or "tier" in str(r.get("skip_reason", "")).lower()
    ]

    cooldown = [
        r for r in rows
        if "anti-spam" in str(r.get("skip_reason", "")).lower()
        or "cooldown" in str(r.get("skip_reason", "")).lower()
        or "similaire" in str(r.get("skip_reason", "")).lower()
    ]

    price = latest_value(rows, "price")
    last_time = latest_value(rows, "time")
    session = latest_value(rows, "session")
    news = latest_value(rows, "news_guard")
    dxy = latest_value(rows, "dxy")
    us10y = latest_value(rows, "us10y")

    last_plan = None
    for r in reversed(rows):
        if r.get("kind") == "TRADE_PLAN":
            last_plan = r
            break

    if last_plan:
        plan_txt = f"""Action : {last_plan.get('action', 'N/A')} {SYMBOL}
Entrée : {last_plan.get('entry', 'N/A')}
SL : {last_plan.get('sl', 'N/A')}
TP1 : {last_plan.get('tp1', 'N/A')}
TP2 : {last_plan.get('tp2', 'N/A')}
TP3 : {last_plan.get('tp3', 'N/A')}
Confiance : {last_plan.get('confidence', 'N/A')}/100
Envoyé Telegram : {last_plan.get('sent', 'N/A')}"""
    else:
        plan_txt = "Aucun plan BUY/SELL généré sur les derniers cycles."

    explanation = explain_no_plan(last)

    return f"""<b>🟢 GROK GOLD V5.2 — BOT ACTIF</b>

<b>Actif :</b> {SYMBOL}
<b>Prix dernière analyse :</b> {price}
<b>Dernier cycle :</b> {last_time}

<b>Contexte actuel :</b>
Session : {session}
News Guard : {news}
DXY : {dxy}
US10Y : {us10y}

<b>Résumé des derniers cycles :</b>
Cycles analysés : {total}
Plans BUY/SELL détectés : {len(plans)}
Plans BUY/SELL envoyés : {len(plans_sent)}
Formations : {len(formations)}
Impulsions : {len(impulses)}
Big Moves : {len(big_moves)}
Aucun setup : {len(no_setup)}
Signaux filtrés : {len(filtered)}
Cooldown / anti-spam : {len(cooldown)}

<b>Dernier plan :</b>
{plan_txt}

<b>Pourquoi pas de BUY/SELL maintenant ?</b>
{explanation}

<b>Ce que le bot attend :</b>
• Cassure ou retest propre.
• Confluence suffisante.
• Confiance minimum : {TRADE_PLAN_MIN_CONF}/100.
• Pas de news USD dangereuse.
• Confirmation structure + macro + timing.

<i>Le bot ne force pas un BUY/SELL si la qualité est trop faible.</i>"""


def status_due(state):
    last = parse_dt(state.get("last_status_report_time_v52"))

    if last is None:
        return True

    age_min = (now_utc() - last).total_seconds() / 60
    return age_min >= STATUS_REPORT_MIN


def maybe_send_status_report_v52():
    state = load_state()

    if not status_due(state):
        print(f"[V5.2] Rapport non dû. Intervalle: {STATUS_REPORT_MIN} min.")
        return

    rows = read_rows()
    msg = build_status_message(rows)

    print("[V5.2] Envoi rapport Telegram clair.")
    ok = send_telegram(msg)

    if ok:
        state["last_status_report_time_v52"] = now_utc().isoformat()
        save_state(state)
        print("[V5.2] Rapport envoyé.")


def main():
    print(f"GROK GOLD BOT V5.2 CLEAR TRADE PLAN STATUS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    run_once_v51.maybe_send_status_report = maybe_send_status_report_v52

    try:
        run_once_v51.main()
    except Exception as e:
        print("[V5.2] Erreur pendant V5.1:")
        print(traceback.format_exc())
        send_error_report(e)
        raise


if __name__ == "__main__":
    main()
