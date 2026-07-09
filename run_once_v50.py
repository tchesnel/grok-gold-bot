from datetime import datetime, timezone
from pathlib import Path
import csv
import traceback

from run_once_v41 import SYMBOL, send_telegram, cfg
from run_once_v44 import load_state, save_state, now_utc
import run_once_v49


STATUS_REPORT_MIN = int(cfg("STATUS_REPORT_MIN", "30"))
HISTORY_PATH = Path(".bot_state/signal_history.csv")


def parse_dt(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def read_last_history_rows(limit=40):
    if not HISTORY_PATH.exists():
        return []

    try:
        with HISTORY_PATH.open("r", newline="") as f:
            rows = list(csv.DictReader(f))
            return rows[-limit:]
    except Exception:
        return []


def boolish(v):
    return str(v).lower() in ("true", "1", "yes")


def explain_last_row(row):
    if not row:
        return "Historique pas encore assez rempli. Le bot vient de démarrer ou n’a pas encore écrit de cycle complet."

    kind = row.get("kind", "NONE")
    side = row.get("side", "NONE")
    skip = row.get("skip_reason", "")
    long_score = row.get("long_score", "")
    short_score = row.get("short_score", "")
    long_gate = row.get("long_gate", "")
    short_gate = row.get("short_gate", "")
    tier = row.get("tier", "")
    brain = row.get("brain_score", "")
    news = row.get("news_guard", "")
    session = row.get("session", "")
    dxy = row.get("dxy", "")
    us10y = row.get("us10y", "")

    reasons = []

    if kind == "NONE":
        reasons.append("Aucun trade validé : les conditions complètes ne sont pas réunies.")
        reasons.append(f"Score LONG : {long_score} / Score SHORT : {short_score}.")
        reasons.append(f"Gate LONG : {long_gate} / Gate SHORT : {short_gate}.")
        reasons.append("Le bot attend une vraie confirmation structure + macro + timing.")

    if "anti-spam" in skip.lower():
        reasons.append("Le bot a détecté un scénario, mais l’anti-spam bloque pour éviter d’envoyer le même signal plusieurs fois.")

    if "filtré" in skip.lower() or "tier" in skip.lower():
        reasons.append("Le Confluence Brain a filtré le signal : setup jugé trop faible ou pas assez propre.")

    if boolish(news):
        reasons.append("News Guard actif : le bot évite les entrées agressives autour des grosses news USD.")

    if kind in ("FORMATION", "IMPULSE") and not skip:
        reasons.append(f"Le bot a vu une opportunité {kind} {side}, mais ce n’est pas encore un trade confirmé.")

    if kind == "TRADE" and not skip:
        reasons.append(f"Un trade {side} a été détecté/envoyé récemment.")

    if not reasons:
        reasons.append("Le bot analyse normalement, mais aucun setup prioritaire n’est validé pour l’instant.")

    context = [
        f"Session : {session or 'inconnue'}",
        f"DXY : {dxy or 'N/A'}",
        f"US10Y : {us10y or 'N/A'}",
    ]

    if tier or brain:
        context.append(f"Tier : {tier or 'N/A'}")
        context.append(f"Brain score : {brain or 'N/A'}")

    return "\n".join([f"• {r}" for r in reasons]) + "\n\n" + "\n".join([f"• {c}" for c in context])


def build_status_message(rows):
    last = rows[-1] if rows else {}

    total = len(rows)
    sent_count = sum(1 for r in rows if boolish(r.get("sent", "")))
    none_count = sum(1 for r in rows if r.get("kind") == "NONE")
    formation_count = sum(1 for r in rows if r.get("kind") == "FORMATION")
    impulse_count = sum(1 for r in rows if r.get("kind") == "IMPULSE")
    trade_count = sum(1 for r in rows if r.get("kind") == "TRADE")
    filtered_count = sum(1 for r in rows if "filtré" in str(r.get("skip_reason", "")).lower() or "tier" in str(r.get("skip_reason", "")).lower())
    spam_count = sum(1 for r in rows if "anti-spam" in str(r.get("skip_reason", "")).lower())

    price = last.get("price", "N/A")
    last_time = last.get("time", "N/A")
    kind = last.get("kind", "NONE")
    side = last.get("side", "NONE")
    long_score = last.get("long_score", "N/A")
    short_score = last.get("short_score", "N/A")
    session = last.get("session", "N/A")
    news = last.get("news_guard", "N/A")

    explanation = explain_last_row(last)

    return f"""<b>🟢 GROK GOLD V5.0 — BOT ACTIF</b>

<b>Actif :</b> {SYMBOL}
<b>Prix dernière analyse :</b> {price}
<b>Dernier cycle :</b> {last_time}

<b>Dernier état :</b>
Signal : {kind} {side}
Score LONG : {long_score}
Score SHORT : {short_score}
Session : {session}
News Guard : {news}

<b>Résumé des derniers cycles :</b>
Cycles analysés : {total}
Alertes envoyées : {sent_count}
HOLD / rien de propre : {none_count}
Formations : {formation_count}
Impulsions : {impulse_count}
Trades validés : {trade_count}
Signaux filtrés : {filtered_count}
Anti-spam : {spam_count}

<b>Pourquoi pas de trade maintenant ?</b>
{explanation}

<i>Le bot travaille. Il n’envoie pas un trade tant que le setup n’est pas assez propre.</i>"""


def status_report_due(state):
    last = state.get("last_status_report_time")
    last_dt = parse_dt(last)

    if last_dt is None:
        return True

    age_min = (now_utc() - last_dt).total_seconds() / 60
    return age_min >= STATUS_REPORT_MIN


def maybe_send_status_report():
    state = load_state()

    if not status_report_due(state):
        print(f"[V5.0] Rapport régulier non dû. Intervalle: {STATUS_REPORT_MIN} min.")
        return

    rows = read_last_history_rows()
    msg = build_status_message(rows)

    print("[V5.0] Envoi rapport régulier Telegram.")
    ok = send_telegram(msg)

    if ok:
        state["last_status_report_time"] = now_utc().isoformat()
        save_state(state)
        print("[V5.0] Rapport régulier envoyé.")
    else:
        print("[V5.0] Échec envoi rapport régulier.")


def send_error_report(error):
    state = load_state()
    last = parse_dt(state.get("last_error_report_time"))
    due = last is None or (now_utc() - last).total_seconds() / 60 >= 30

    if not due:
        return

    msg = f"""<b>⚠️ GROK GOLD V5.0 — ERREUR BOT</b>

Le bot a rencontré une erreur pendant l’analyse.

<b>Erreur :</b>
{str(error)}

<i>Le service VPS va continuer grâce au redémarrage automatique. À vérifier si l’erreur se répète.</i>"""

    ok = send_telegram(msg)

    if ok:
        state["last_error_report_time"] = now_utc().isoformat()
        save_state(state)


def main():
    print(f"GROK GOLD BOT V5.0 HEARTBEAT + NO TRADE EXPLANATION - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        run_once_v49.main()
    except Exception as e:
        print("[V5.0] Erreur pendant run_once_v49:")
        print(traceback.format_exc())
        send_error_report(e)
        raise

    maybe_send_status_report()


if __name__ == "__main__":
    main()
